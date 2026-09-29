"""
Unit tests for Tradego Phase 7: Canonical Order Instruction & Broker Adapter Boundary.

Validates all Phase 7 specifications:
1. Canonical OrderInstruction creation from RISK_APPROVED ExecutionIntent
2. Intent -> Instruction provenance, lineage, and traceability
3. Operational approval requirement enforcement
4. Risk approval requirement enforcement
5. HALTED guard rejection on instruction creation
6. HALTED guard rejection on instruction dispatch
7. Invalid quantity validation
8. Unsupported order type validation
9. Limit price requirement validation
10. Immutable fields enforcement (AttributeError on mutation)
11. Duplicate instruction prevention (Idempotency)
12. BrokerAdapter interface compliance
13. PaperBrokerAdapter standard dispatch and acknowledgement
14. PaperBrokerAdapter simulated broker rejection handling
15. PaperBrokerAdapter simulated broker failure handling
16. Instruction cancellation handling
17. Complete audit trail generation (CREATED, VALIDATED, REQUESTED, DISPATCHED, ACKNOWLEDGED, REJECTED, FAILED, CANCELLED)
18. Correlation ID propagation
19. Unauthorized creation and dispatch rejection (RBAC 403)
20. No live broker connection and strictly is_executed == False
21. Complete end-to-end intent -> approval -> risk approval -> instruction -> dispatch -> ack lifecycle
22. API query endpoints (/order-instructions, /order-instructions/{id}, /execution-intents/{id}/order-instruction)
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient

from gateway.adapters import MarketStateAdapter, PortfolioRiskAdapter
from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.broker_adapter import BrokerAdapter, BrokerDispatchResult, PaperBrokerAdapter
from gateway.command import CommandGateway
from gateway.contracts import Capability
from gateway.intent import ExecutionIntent, ExecutionIntentManager, IntentState
from gateway.order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from gateway.projection import SnapshotGenerator
from gateway.recovery import GuardState, RecoveryManager, TradingGuard
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import InMemorySessionStore, NativeCredentialStore, Tier1AuditLogger
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState


class TestPhase7OrderInstruction(unittest.TestCase):
    """Exhaustive test suite for Phase 7 Canonical Order Instruction & Broker Adapter Boundary."""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.guard = TradingGuard()
        self.seq_mgr = SequenceManager(initial_sequence=0)
        self.broadcaster = EventBroadcaster(sequence_manager=self.seq_mgr)
        self.session_store = InMemorySessionStore(default_ttl_seconds=3600)
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.test_hasher = argon2.PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        self.credential_store = NativeCredentialStore(hasher=self.test_hasher, max_failed_attempts=5)
        self.command_gateway = CommandGateway(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            broadcaster=self.broadcaster,
        )
        self.market_adapter = MarketStateAdapter(state_store=None)
        self.snapshot_gen = SnapshotGenerator(
            trading_guard=self.guard,
            sequence_manager=self.seq_mgr,
            market_adapter=self.market_adapter,
        )
        self.recovery_mgr = RecoveryManager(
            trading_guard=self.guard,
            command_gateway=self.command_gateway,
            audit_logger=self.audit_logger,
            challenge_ttl_seconds=60,
        )
        self.portfolio_risk_adapter = PortfolioRiskAdapter(
            trading_guard=self.guard,
        )
        self.intent_mgr = ExecutionIntentManager(
            audit_logger=self.audit_logger,
            default_ttl_seconds=3600,
        )

        # Authoritative Core structures for risk gate
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.portfolio_state = PortfolioRuntimeState(
            account_id="PAPER_TEST_P7",
            initial_cash=100000.0,
        )
        self.risk_gate = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            max_order_quantity=1000,
            margin_rate=0.20,
        )

        # Phase 7 Order Instruction Boundary
        self.broker_adapter = PaperBrokerAdapter(venue_name="PAPER_TEST_VENUE")
        self.instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            broker_adapter=self.broker_adapter,
        )

        # FastAPI app wiring
        self.app = create_app(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            sequence_manager=self.seq_mgr,
            broadcaster=self.broadcaster,
            credential_store=self.credential_store,
            command_gateway=self.command_gateway,
            snapshot_generator=self.snapshot_gen,
            market_adapter=self.market_adapter,
            recovery_manager=self.recovery_mgr,
            portfolio_risk_adapter=self.portfolio_risk_adapter,
            intent_manager=self.intent_mgr,
            risk_gate=self.risk_gate,
            order_instruction_manager=self.instruction_mgr,
            broker_adapter=self.broker_adapter,
        )
        self.client = TestClient(self.app)

        # Sessions
        self.token_op_a = self.session_store.create_session(
            operator_id="trader_a",
            roles=["TRADER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_SUBMIT},
        )
        self.token_op_b = self.session_store.create_session(
            operator_id="approver_b",
            roles=["APPROVER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_APPROVE},
        )
        self.token_risk_officer = self.session_store.create_session(
            operator_id="risk_officer",
            roles=["RISK_OFFICER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_RISK_EVALUATE},
        )
        self.token_dispatcher = self.session_store.create_session(
            operator_id="dispatcher",
            roles=["DISPATCHER"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_ORDER_INSTRUCT,
                Capability.CAP_ORDER_DISPATCH,
                Capability.CAP_TRADE_CANCEL,
            },
        )
        self.token_admin = self.session_store.create_session(
            operator_id="admin",
            roles=["ADMIN"],
            capabilities={Capability.CAP_ADMIN, Capability.CAP_OBSERVE},
        )
        self.token_unauth = self.session_store.create_session(
            operator_id="viewer",
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    def _create_risk_approved_intent(
        self,
        symbol: str = "NSE:TCS",
        side: str = "BUY",
        quantity: int = 10,
        limit_price: float = 3500.0,
    ) -> str:
        """Helper to advance an intent through operational and risk approval."""
        # Step 1: Create
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "limit_price": limit_price,
                "reason": "Phase 7 setup",
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        intent_id = create_resp.json()["intent_id"]

        # Step 2: Operational approval (Approver B)
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)

        # Step 3: Risk evaluation (Risk Officer)
        risk_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(risk_resp.status_code, 200)
        self.assertEqual(risk_resp.json()["decision"], "ALLOWED")
        self.assertEqual(risk_resp.json()["resulting_state"], "RISK_APPROVED")

        return intent_id

    # 1. Canonical OrderInstruction creation
    def test_01_create_instruction_from_risk_approved_intent(self) -> None:
        intent_id = self._create_risk_approved_intent()

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["instruction_id"].startswith("ins-"))
        self.assertEqual(data["intent_id"], intent_id)
        self.assertEqual(data["symbol"], "NSE:TCS")
        self.assertEqual(data["side"], "BUY")
        self.assertEqual(data["quantity"], 10)
        self.assertEqual(data["order_type"], "LIMIT")
        self.assertEqual(data["limit_price"], 3500.0)
        self.assertEqual(data["state"], "VALIDATED")
        self.assertFalse(data["is_executed"])
        self.assertEqual(data["status_category"], "ORDER_INSTRUCTION")

    # 2. Intent -> Instruction provenance and lineage
    def test_02_provenance_and_lineage(self) -> None:
        intent_id = self._create_risk_approved_intent()

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 201)
        prov = resp.json()["provenance"]
        self.assertEqual(prov["intent_id"], intent_id)
        self.assertEqual(prov["creator_id"], "trader_a")
        self.assertEqual(prov["approver_id"], "approver_b")
        self.assertEqual(prov["risk_evaluator_id"], "risk_officer")
        self.assertEqual(prov["risk_decision"], "ALLOWED")
        self.assertEqual(prov["instruction_creator_id"], "dispatcher")
        self.assertIn("correlation_id", prov)

    # 3. Operational approval required (unapproved intent fails)
    def test_03_operational_approval_required(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:INFY",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 1400.0,
            },
        )
        intent_id = create_resp.json()["intent_id"]

        # Attempt to create instruction directly without approval
        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("EXECUTION_CHAIN_BREACH", str(resp.json()))

    # 4. Risk approval required (operationally approved but not risk approved fails)
    def test_04_risk_approval_required(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:INFY",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 1400.0,
            },
        )
        intent_id = create_resp.json()["intent_id"]

        # Operationally approve
        self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )

        # Attempt to create instruction without risk evaluation
        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("EXECUTION_CHAIN_BREACH", str(resp.json()))

    # 5. HALTED guard rejection on instruction creation
    def test_05_halted_guard_rejects_creation(self) -> None:
        intent_id = self._create_risk_approved_intent()

        # Trip guard to HALTED
        self.guard.trip(reason="SIMULATED_TEST_HALT")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("GUARD_HALTED", str(resp.json()))

    # 6. HALTED guard rejection on instruction dispatch
    def test_06_halted_guard_rejects_dispatch(self) -> None:
        intent_id = self._create_risk_approved_intent()

        create_ins = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_ins.json()["instruction_id"]

        # Trip guard to HALTED
        self.guard.trip(reason="EMERGENCY_HALT")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("GUARD_HALTED", str(resp.json()))

        # Check instruction state was transitioned to FAILED
        get_resp = self.client.get(
            f"/api/v1/order-instructions/{ins_id}",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(get_resp.json()["state"], "FAILED")

    # 7. Invalid quantity validation
    def test_07_invalid_quantity(self) -> None:
        intent = ExecutionIntent(
            intent_id="test-bad-qty",
            correlation_id="corr-bad-qty",
            creator_id="trader_a",
            symbol="NSE:WIPRO",
            side="BUY",
            quantity=0,  # Invalid
            order_type="LIMIT",
            limit_price=400.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            state=IntentState.RISK_APPROVED,
            risk_status="ALLOWED",
            approver_id="approver_b",
        )
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher")
        self.assertIn("INVALID_ORDER_QUANTITY", str(ctx.exception))

    # 8. Unsupported order type validation
    def test_08_unsupported_order_type(self) -> None:
        intent = ExecutionIntent(
            intent_id="test-bad-type",
            correlation_id="corr-bad-type",
            creator_id="trader_a",
            symbol="NSE:WIPRO",
            side="BUY",
            quantity=10,
            order_type="UNSUPPORTED_ICEBERG",
            limit_price=400.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            state=IntentState.RISK_APPROVED,
            risk_status="ALLOWED",
            approver_id="approver_b",
        )
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher")
        self.assertIn("UNSUPPORTED_ORDER_TYPE", str(ctx.exception))

    # 9. Limit price requirement validation
    def test_09_limit_price_validation(self) -> None:
        intent = ExecutionIntent(
            intent_id="test-no-price",
            correlation_id="corr-no-price",
            creator_id="trader_a",
            symbol="NSE:WIPRO",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=None,  # Missing
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            state=IntentState.RISK_APPROVED,
            risk_status="ALLOWED",
            approver_id="approver_b",
        )
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher")
        self.assertIn("INVALID_ORDER_PRICE", str(ctx.exception))

    # 10. Immutable fields enforcement
    def test_10_immutable_fields_enforcement(self) -> None:
        intent_id = self._create_risk_approved_intent()
        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = resp.json()["instruction_id"]
        instruction = self.instruction_mgr.get_instruction(ins_id)
        self.assertIsNotNone(instruction)

        # Attempt to mutate quantity
        with self.assertRaises(AttributeError):
            instruction.quantity = 9999

        # Attempt to mutate symbol
        with self.assertRaises(AttributeError):
            instruction.symbol = "NSE:MUTATED"

        # Attempt to mutate side
        with self.assertRaises(AttributeError):
            instruction.side = "SELL"

        # Attempt to mutate provenance
        with self.assertRaises(AttributeError):
            instruction.provenance = {}

    # 11. Duplicate instruction prevention (Idempotency)
    def test_11_duplicate_instruction_prevention_idempotency(self) -> None:
        intent_id = self._create_risk_approved_intent()

        # First creation succeeds
        resp1 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp1.status_code, 201)

        # Second creation fails with 409 Conflict
        resp2 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(resp2.status_code, 409)
        self.assertIn("DUPLICATE_INSTRUCTION_PREVENTED", str(resp2.json()))

    # 12. BrokerAdapter interface compliance
    def test_12_broker_adapter_interface_compliance(self) -> None:
        self.assertIsInstance(self.broker_adapter, BrokerAdapter)
        self.assertTrue(self.broker_adapter.health_check())

    # 13. PaperBrokerAdapter standard dispatch and acknowledgement
    def test_13_paper_broker_dispatch_and_acknowledgement(self) -> None:
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        dispatch_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(dispatch_resp.status_code, 200)
        data = dispatch_resp.json()
        self.assertEqual(data["state"], "ACKNOWLEDGED")
        self.assertTrue(data["broker_order_id"].startswith("PAPER-ORD-"))
        self.assertIsNotNone(data["acknowledged_at"])
        self.assertFalse(data["is_executed"])  # INVARIANT: Must NOT claim executed

    # 14. PaperBrokerAdapter simulated rejection handling
    def test_14_paper_broker_simulated_rejection(self) -> None:
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        # Configure broker rejection
        self.broker_adapter.simulate_rejection(reason="EXCHANGE_ORDER_REJECTED")

        dispatch_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(dispatch_resp.status_code, 200)
        data = dispatch_resp.json()
        self.assertEqual(data["state"], "REJECTED")
        self.assertEqual(data["rejection_reason"], "EXCHANGE_ORDER_REJECTED")
        self.broker_adapter.reset_simulation()

    # 15. PaperBrokerAdapter simulated failure handling
    def test_15_paper_broker_simulated_failure(self) -> None:
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        # Configure broker failure
        self.broker_adapter.simulate_failure(reason="TRANSPORT_TIMEOUT_504")

        dispatch_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(dispatch_resp.status_code, 200)
        data = dispatch_resp.json()
        self.assertEqual(data["state"], "FAILED")
        self.assertEqual(data["failure_reason"], "TRANSPORT_TIMEOUT_504")
        self.broker_adapter.reset_simulation()

    # 16. Instruction cancellation handling
    def test_16_instruction_cancellation(self) -> None:
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        # Dispatch first
        self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )

        # Cancel
        cancel_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/cancel",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
            json={"reason": "Manual de-risking cancellation"},
        )
        self.assertEqual(cancel_resp.status_code, 200)
        data = cancel_resp.json()
        self.assertEqual(data["state"], "CANCELLED")
        self.assertEqual(data["cancellation_reason"], "Manual de-risking cancellation")

    # 17. Complete audit trail generation
    def test_17_audit_trail_generation(self) -> None:
        mock_audit = MagicMock()
        adapter = PaperBrokerAdapter()
        mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=mock_audit,
            broker_adapter=adapter,
        )
        intent = ExecutionIntent(
            intent_id="audit-intent-p7",
            correlation_id="audit-corr-p7",
            creator_id="trader_a",
            symbol="NSE:LT",
            side="BUY",
            quantity=15,
            order_type="LIMIT",
            limit_price=3000.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            state=IntentState.RISK_APPROVED,
            risk_status="ALLOWED",
            approver_id="approver_b",
        )
        # 1. Create and Validate
        ins = mgr.create_instruction_from_intent(intent, "dispatcher", "audit-corr-p7")
        # 2. Dispatch
        mgr.dispatch_instruction(ins.instruction_id, "dispatcher", "audit-corr-p7")
        # 3. Cancel
        mgr.cancel_instruction(ins.instruction_id, "dispatcher", "Audit test cancel", "audit-corr-p7")

        actions = [call[0][0]["action"] for call in mock_audit.log.call_args_list]
        self.assertIn("ORDER_INSTRUCTION_CREATED", actions)
        self.assertIn("ORDER_INSTRUCTION_VALIDATED", actions)
        self.assertIn("ORDER_DISPATCH_REQUESTED", actions)
        self.assertIn("ORDER_DISPATCHED", actions)
        self.assertIn("ORDER_ACKNOWLEDGED", actions)
        self.assertIn("ORDER_CANCELLED", actions)

    # 18. Correlation ID propagation
    def test_18_correlation_id_propagation(self) -> None:
        intent_id = self._create_risk_approved_intent()
        custom_corr = "X-CORR-P7-TEST-999"

        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={
                "Authorization": f"Bearer {self.token_dispatcher}",
                "X-Correlation-ID": custom_corr,
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        self.assertEqual(create_resp.headers.get("X-Correlation-ID"), custom_corr)
        self.assertEqual(create_resp.json()["correlation_id"], custom_corr)

    # 19. Unauthorized creation and dispatch rejection (RBAC 403)
    def test_19_unauthorized_endpoints(self) -> None:
        intent_id = self._create_risk_approved_intent()

        # Viewer attempts to create instruction
        resp_unauth_create = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_unauth}"},
        )
        self.assertEqual(resp_unauth_create.status_code, 403)
        self.assertIn("INSUFFICIENT_CAPABILITY", str(resp_unauth_create.json()))

        # Create properly with dispatcher
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        # Viewer attempts to dispatch
        resp_unauth_disp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_unauth}"},
        )
        self.assertEqual(resp_unauth_disp.status_code, 403)
        self.assertIn("INSUFFICIENT_CAPABILITY", str(resp_unauth_disp.json()))

    # 20. No live broker connection and strictly is_executed == False
    def test_20_no_live_broker_or_real_order_placement(self) -> None:
        self.assertFalse(hasattr(self.instruction_mgr, "_live_broker"))
        self.assertFalse(hasattr(self.instruction_mgr, "_dhan_client"))
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]
        disp_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        data = disp_resp.json()
        self.assertFalse(data["is_executed"])
        self.assertEqual(data["status_category"], "ORDER_INSTRUCTION")

    # 21. Complete end-to-end lifecycle
    def test_21_complete_end_to_end_lifecycle(self) -> None:
        # Step 1: Create intent (Trader A)
        c_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:HDFCBANK",
                "side": "BUY",
                "quantity": 25,
                "order_type": "LIMIT",
                "limit_price": 1600.0,
                "reason": "Complete lifecycle test",
            },
        )
        self.assertEqual(c_resp.status_code, 201)
        intent_id = c_resp.json()["intent_id"]

        # Step 2: Operational approval (Approver B)
        a_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(a_resp.status_code, 200)
        self.assertEqual(a_resp.json()["state"], "APPROVED")

        # Step 3: Risk Evaluation (Risk Officer)
        r_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(r_resp.status_code, 200)
        self.assertEqual(r_resp.json()["decision"], "ALLOWED")
        self.assertEqual(r_resp.json()["resulting_state"], "RISK_APPROVED")

        # Step 4: Create OrderInstruction (Dispatcher)
        ins_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(ins_resp.status_code, 201)
        ins_id = ins_resp.json()["instruction_id"]
        self.assertEqual(ins_resp.json()["state"], "VALIDATED")

        # Step 5: Dispatch to PaperBrokerAdapter (Dispatcher)
        disp_resp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(disp_resp.status_code, 200)
        disp_data = disp_resp.json()
        self.assertEqual(disp_data["state"], "ACKNOWLEDGED")
        self.assertTrue(disp_data["broker_order_id"].startswith("PAPER-ORD-"))
        self.assertFalse(disp_data["is_executed"])

    # 22. Query endpoints
    def test_22_query_endpoints(self) -> None:
        intent_id = self._create_risk_approved_intent()
        create_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        ins_id = create_resp.json()["instruction_id"]

        # GET /api/v1/order-instructions/{id}
        get_single = self.client.get(
            f"/api/v1/order-instructions/{ins_id}",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(get_single.status_code, 200)
        self.assertEqual(get_single.json()["instruction_id"], ins_id)

        # GET /api/v1/order-instructions
        get_list = self.client.get(
            "/api/v1/order-instructions",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(get_list.status_code, 200)
        self.assertGreaterEqual(get_list.json()["count"], 1)

        # GET /api/v1/execution-intents/{intent_id}/order-instruction
        get_by_intent = self.client.get(
            f"/api/v1/execution-intents/{intent_id}/order-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(get_by_intent.status_code, 200)
        self.assertEqual(get_by_intent.json()["instruction_id"], ins_id)


if __name__ == "__main__":
    unittest.main()
