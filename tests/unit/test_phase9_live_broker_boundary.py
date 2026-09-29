"""
Tradego Phase 9 — Live Broker Integration & Production Execution Readiness Unit Test Suite.

Authoritative verification for:
1. PAPER is default
2. LIVE requires explicit enablement
3. Unauthorized LIVE activation rejected
4. Missing credentials rejected
5. Invalid credentials rejected
6. Broker disconnected blocks dispatch
7. Guard HALTED blocks dispatch
8. Risk unavailable blocks dispatch
9. Risk rejected blocks dispatch
10. Non-risk-approved instruction blocks dispatch
11. Duplicate dispatch prevented
12. Broker acknowledgement mapped correctly
13. Broker rejection mapped correctly
14. Broker fill mapped correctly
15. Duplicate fill ignored
16. Out-of-order event handled
17. Cancellation mapped correctly
18. Disconnect handled safely
19. Reconnect handled safely
20. Reconciliation match
21. Reconciliation mismatch
22. Unknown broker state
23. Secret redaction
24. Audit event generation
25. No direct UI-to-broker path
26. PaperBrokerAdapter regression
27. Full Phase 5->9 lifecycle
28. LIVE mode cannot bypass risk gate
29. LIVE mode cannot bypass operational approval
30. LIVE mode cannot bypass OrderInstruction
"""

from datetime import datetime, timezone
import os
import unittest
import uuid

from gateway.api.app import create_app
from gateway.broker_adapter import (
    BrokerAdapter,
    BrokerDispatchResult,
    LiveBrokerAdapter,
    MockLiveBrokerAdapter,
    PaperBrokerAdapter,
)
from gateway.broker_connectivity import (
    BrokerConnectivityManager,
    BrokerConnectivityState,
    BrokerReconciliationResult,
    ExecutionMode,
)
from gateway.broker_credentials import BrokerCredentialsConfig
from gateway.contracts import Capability
from gateway.execution import (
    ExecutionRecord,
    ExecutionState,
    ExecutionStateManager,
    ReconciliationStatus,
)
from gateway.intent import ExecutionIntent, ExecutionIntentManager, IntentState
from gateway.order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from gateway.recovery import GuardState, TradingGuard
from services.runtime.models import GuardTripReason
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import InMemorySessionStore, NativeCredentialStore, Tier1AuditLogger
from starlette.testclient import TestClient


class TestPhase9LiveBrokerBoundary(unittest.TestCase):
    """Authoritative unit test suite for Phase 9 Live Broker Integration & Execution Readiness."""

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.audit = Tier1AuditLogger(log_file_path=None)

        self.conn_mgr = BrokerConnectivityManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            risk_gate_available=True,
            active_broker="TEST_LIVE_VENUE",
        )
        self.valid_creds = BrokerCredentialsConfig(
            venue_name="TEST_LIVE_VENUE",
            client_id="OP_CLIENT_999",
            access_token="SUPER_SECRET_ACCESS_TOKEN_ABC123",
            account_id="ACC_PRIMARY_01",
        )
        self.live_adapter = MockLiveBrokerAdapter(
            credentials=self.valid_creds,
            connectivity_manager=self.conn_mgr,
            venue_name="TEST_LIVE_VENUE",
        )
        self.exec_mgr = ExecutionStateManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            connectivity_manager=self.conn_mgr,
        )
        self.instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            broker_adapter=self.live_adapter,
            execution_manager=self.exec_mgr,
            connectivity_manager=self.conn_mgr,
        )
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.portfolio_state = PortfolioRuntimeState(
            account_id="LIVE_TEST_P9",
            initial_cash=1000000.0,
        )
        self.risk_evaluator = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            max_order_quantity=1000,
            margin_rate=0.20,
        )
        self.intent_mgr = ExecutionIntentManager(
            audit_logger=self.audit,
        )

    def _create_approved_intent(self) -> ExecutionIntent:
        intent = self.intent_mgr.create_intent(
            creator_id="op_alpha",
            symbol="NSE:RELIANCE",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            correlation_id=str(uuid.uuid4()),
            limit_price=2850.0,
        )
        self.intent_mgr.approve_intent(intent.intent_id, "op_beta", str(uuid.uuid4()))
        self.risk_evaluator.evaluate_intent(intent, "risk_officer_1", str(uuid.uuid4()))
        return intent


    # 1. PAPER is default
    def test_01_paper_is_default(self) -> None:
        self.assertEqual(self.conn_mgr.execution_mode, ExecutionMode.PAPER)
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

    # 2. LIVE requires explicit enablement
    def test_02_live_requires_explicit_enablement(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=False)
        self.assertIn("LIVE_ACTIVATION_REJECTED", str(ctx.exception))
        self.assertEqual(self.conn_mgr.execution_mode, ExecutionMode.PAPER)

    # 3. Unauthorized LIVE activation rejected
    def test_03_unauthorized_live_activation_rejected(self) -> None:
        # Cannot enable live without credentials or connection
        with self.assertRaises(ValueError):
            self.conn_mgr.set_mode(ExecutionMode.LIVE, "unauthorized_user", confirm_live=True)
        self.assertEqual(self.conn_mgr.execution_mode, ExecutionMode.PAPER)

    # 4. Missing credentials rejected
    def test_04_missing_credentials_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.conn_mgr.connect("op_alpha")
        self.assertIn("BROKER_AUTH_FAILED", str(ctx.exception))
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.AUTH_FAILED)

    # 5. Invalid credentials rejected
    def test_05_invalid_credentials_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BrokerCredentialsConfig(venue_name="", client_id="user", access_token="token")
        with self.assertRaises(ValueError):
            BrokerCredentialsConfig(venue_name="venue", client_id="", access_token="token")
        with self.assertRaises(ValueError):
            BrokerCredentialsConfig(venue_name="venue", client_id="user", access_token="")

    # 6. Broker disconnected blocks dispatch
    def test_06_broker_disconnected_blocks_dispatch(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)
        # Disconnect broker
        self.conn_mgr.disconnect("op_alpha")
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")

        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertIn("DISCONNECTED", str(ctx.exception))

    # 7. Guard HALTED blocks dispatch
    def test_07_guard_halted_blocks_dispatch(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")

        self.guard.trip(GuardTripReason.MANUAL, "Emergency halt")
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertIn("GUARD_HALTED", str(ctx.exception))

    # 8. Risk unavailable blocks dispatch
    def test_08_risk_unavailable_blocks_dispatch(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")

        self.conn_mgr.set_risk_gate_available(False)
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertIn("Risk evaluation system is unavailable", str(ctx.exception))

    # 9. Risk rejected blocks dispatch
    def test_09_risk_rejected_blocks_dispatch(self) -> None:
        intent = self.intent_mgr.create_intent(
            creator_id="op_alpha",
            symbol="NSE:RISKY_SYM",
            side="BUY",
            quantity=1000000,
            order_type="LIMIT",
            correlation_id=str(uuid.uuid4()),
            limit_price=500.0,
        )

        self.intent_mgr.approve_intent(intent.intent_id, "op_beta", str(uuid.uuid4()))
        # Evaluate with strict risk rejection
        self.risk_evaluator.evaluate_intent(intent, "risk_officer_1", str(uuid.uuid4()))
        # Instruction creation must be blocked
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.assertIn("EXECUTION_CHAIN_BREACH", str(ctx.exception))

    # 10. Non-risk-approved instruction blocks dispatch
    def test_10_non_risk_approved_instruction_blocks_dispatch(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        # Tamper provenance risk decision
        ins.provenance["risk_decision"] = "REJECTED"

        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertIn("LIVE_DISPATCH_BLOCKED: Instruction has not received risk approval", str(ctx.exception))

    # 11. Duplicate dispatch prevented
    def test_11_duplicate_dispatch_prevented(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")

        # First dispatch succeeds
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertEqual(dispatched.state, InstructionState.ACKNOWLEDGED)

        # Immediate re-dispatch on same instruction is rejected by instruction state check
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.assertIn("INVALID_INSTRUCTION_STATE", str(ctx.exception))

        # Re-dispatching instruction directly against adapter tests adapter idempotency key check
        result = self.live_adapter.dispatch(ins)
        self.assertFalse(result.success)
        self.assertEqual(result.outcome, "FAILED")
        self.assertIn("DUPLICATE_DISPATCH_BLOCKED", result.failure_reason)

    # 12. Broker acknowledgement mapped correctly
    def test_12_broker_acknowledgement_mapped_correctly(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        self.assertEqual(dispatched.state, InstructionState.ACKNOWLEDGED)
        self.assertTrue(dispatched.broker_order_id.startswith("LIVE-ORD-"))
        exec_rec = self.exec_mgr.get_by_instruction(ins.instruction_id)
        self.assertIsNotNone(exec_rec)
        self.assertEqual(exec_rec.current_state, ExecutionState.ACKNOWLEDGED)
        self.assertEqual(exec_rec.broker_order_id, dispatched.broker_order_id)

    # 13. Broker rejection mapped correctly
    def test_13_broker_rejection_mapped_correctly(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)
        self.live_adapter.simulate_rejection("INSUFFICIENT_TRADING_COLLATERAL")

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        self.assertEqual(dispatched.state, InstructionState.REJECTED)
        self.assertEqual(dispatched.rejection_reason, "INSUFFICIENT_TRADING_COLLATERAL")
        exec_rec = self.exec_mgr.get_by_instruction(ins.instruction_id)
        self.assertEqual(exec_rec.current_state, ExecutionState.REJECTED)

    # 14. Broker fill mapped correctly
    def test_14_broker_fill_mapped_correctly(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        exec_rec = self.exec_mgr.get_by_instruction(ins.instruction_id)

        # Simulate broker partial fill (20 @ 2848.50)
        self.live_adapter.create_simulated_fill(dispatched.broker_order_id, 20, 2848.50, "BF-001")
        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="BF-001", quantity=20, price=2848.50)

        self.assertEqual(exec_rec.current_state, ExecutionState.PARTIALLY_FILLED)
        self.assertEqual(exec_rec.filled_quantity, 20)
        self.assertEqual(exec_rec.remaining_quantity, 30)
        self.assertEqual(exec_rec.average_fill_price, 2848.50)

        # Complete fill (30 @ 2850.00)
        self.live_adapter.create_simulated_fill(dispatched.broker_order_id, 30, 2850.00, "BF-002")
        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="BF-002", quantity=30, price=2850.00)

        self.assertEqual(exec_rec.current_state, ExecutionState.FILLED)
        self.assertEqual(exec_rec.filled_quantity, 50)
        self.assertEqual(exec_rec.remaining_quantity, 0)
        expected_avg = (20 * 2848.50 + 30 * 2850.00) / 50
        self.assertAlmostEqual(exec_rec.average_fill_price, expected_avg, places=2)

    # 15. Duplicate fill ignored
    def test_15_duplicate_fill_ignored(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        exec_rec = self.exec_mgr.get_by_instruction(ins.instruction_id)

        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="BF-DUP-01", quantity=10, price=2849.0)
        self.assertEqual(exec_rec.filled_quantity, 10)

        # Duplicate fill call with same fill_id
        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="BF-DUP-01", quantity=10, price=2849.0)
        self.assertEqual(exec_rec.filled_quantity, 10)

    # 16. Out-of-order event handled
    def test_16_out_of_order_event_handled(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        exec_rec = self.exec_mgr.create_execution(ins)

        # Fill arrives before acknowledgement
        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="BF-FAST-01", quantity=15, price=2850.0)
        self.assertEqual(exec_rec.current_state, ExecutionState.PARTIALLY_FILLED)

        # Acknowledgement arrives late
        self.exec_mgr.apply_acknowledgement(exec_rec.execution_id, "LIVE-ORD-LATE", "ACK-LATE")
        # State does not regress to ACKNOWLEDGED
        self.assertEqual(exec_rec.current_state, ExecutionState.PARTIALLY_FILLED)
        self.assertEqual(exec_rec.broker_order_id, "LIVE-ORD-LATE")

    # 17. Cancellation mapped correctly
    def test_17_cancellation_mapped_correctly(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        cancelled = self.instruction_mgr.cancel_instruction(dispatched.instruction_id, "op_alpha", "OPERATOR_CANCEL")
        self.assertEqual(cancelled.state, InstructionState.CANCELLED)

        broker_status = self.live_adapter.get_order_status(dispatched.broker_order_id)
        self.assertEqual(broker_status["status"], "CANCELLED")

    # 18. Disconnect handled safely
    def test_18_disconnect_handled_safely(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        self.conn_mgr.disconnect("op_alpha", reason="NETWORK_PARTITION")
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.DISCONNECTED)
        # Mode remains LIVE (never silently falls back to PAPER)
        self.assertEqual(self.conn_mgr.execution_mode, ExecutionMode.LIVE)

    # 19. Reconnect handled safely
    def test_19_reconnect_handled_safely(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        self.conn_mgr.set_connectivity_state(BrokerConnectivityState.RECONNECTING, "SYSTEM", "Transport drop")
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.RECONNECTING)

        self.conn_mgr.connect("op_alpha")
        self.assertEqual(self.conn_mgr.connectivity_state, BrokerConnectivityState.CONNECTED)

    # 20. Reconciliation match
    def test_20_reconciliation_match(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        report = self.conn_mgr.reconcile("op_alpha", self.exec_mgr, self.instruction_mgr, self.live_adapter)
        self.assertEqual(report.result, BrokerReconciliationResult.MATCHED)
        self.assertEqual(report.mismatch_count, 0)

    # 21. Reconciliation mismatch
    def test_21_reconciliation_mismatch(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        # Configure mock broker book mismatch
        self.live_adapter.simulate_reconciliation_mismatch("QUANTITY")
        report = self.conn_mgr.reconcile("op_alpha", self.exec_mgr, self.instruction_mgr, self.live_adapter)

        self.assertEqual(report.result, BrokerReconciliationResult.MISMATCH)
        self.assertGreater(report.mismatch_count, 0)

    # 22. Unknown broker state
    def test_22_unknown_broker_state(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")

        self.live_adapter.simulate_reconciliation_mismatch("MISSING")
        report = self.conn_mgr.reconcile("op_alpha", self.exec_mgr, self.instruction_mgr, self.live_adapter)

        self.assertEqual(report.result, BrokerReconciliationResult.MISMATCH)
        issues = [d["issue"] for d in report.discrepancies]
        self.assertIn("ORDER_MISSING_FROM_BROKER", issues)

    # 23. Secret redaction
    def test_23_secret_redaction(self) -> None:
        redacted = self.valid_creds.redacted_dict()
        self.assertEqual(redacted["access_token"], "[REDACTED]")
        self.assertNotIn("SUPER_SECRET_ACCESS_TOKEN_ABC123", str(redacted))

        creds_str = str(self.valid_creds)
        self.assertNotIn("SUPER_SECRET_ACCESS_TOKEN_ABC123", creds_str)
        self.assertIn("[REDACTED]", creds_str)

    # 24. Audit event generation
    def test_24_audit_event_generation(self) -> None:
        self.conn_mgr.set_credentials(self.valid_creds, "op_alpha")
        self.conn_mgr.connect("op_alpha")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "op_alpha", confirm_live=True)

        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_alpha")
        self.instruction_mgr.cancel_instruction(ins.instruction_id, "op_alpha")

        self.audit.flush()
        entries = self.audit.in_memory_records
        actions = [e.get("action") for e in entries]

        self.assertIn("LIVE_MODE_REQUESTED", actions)
        self.assertIn("LIVE_MODE_ENABLED", actions)
        self.assertIn("BROKER_CONNECT_REQUESTED", actions)
        self.assertIn("BROKER_CONNECTED", actions)
        self.assertIn("LIVE_ORDER_SUBMISSION_REQUESTED", actions)
        self.assertIn("LIVE_ORDER_SUBMITTED", actions)
        self.assertIn("LIVE_ORDER_ACKNOWLEDGED", actions)
        self.assertIn("LIVE_ORDER_CANCEL_REQUESTED", actions)
        self.assertIn("LIVE_ORDER_CANCELLED", actions)

    # 25. No direct UI-to-broker path
    def test_25_no_direct_ui_to_broker_path(self) -> None:
        app = create_app(
            trading_guard=self.guard,
            broker_adapter=self.live_adapter,
            order_instruction_manager=self.instruction_mgr,
            broker_connectivity_manager=self.conn_mgr,
        )
        client = TestClient(app)
        # Attempt to hit non-existent direct broker execution endpoint
        resp = client.post("/api/v1/broker/order", json={"symbol": "NSE:RELIANCE", "quantity": 10})
        self.assertEqual(resp.status_code, 404)

        resp2 = client.post("/api/v1/broker/execute", json={"symbol": "NSE:RELIANCE", "quantity": 10})
        self.assertEqual(resp2.status_code, 404)

    # 26. PaperBrokerAdapter regression
    def test_26_paper_broker_adapter_regression(self) -> None:
        paper_adapter = PaperBrokerAdapter()
        intent = self._create_approved_intent()
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")

        result = paper_adapter.dispatch(ins)
        self.assertTrue(result.success)
        self.assertEqual(result.outcome, "ACKNOWLEDGED")
        self.assertTrue(result.broker_order_id.startswith("PAPER-ORD-"))

        status_rec = paper_adapter.get_order_status(result.broker_order_id)
        self.assertIsNotNone(status_rec)
        self.assertEqual(status_rec["status"], "ACKNOWLEDGED")

    # 27. Full Phase 5->9 lifecycle
    def test_27_full_phase_5_to_9_lifecycle(self) -> None:
        # 1. Credentials & Connectivity & Live Enablement
        self.conn_mgr.set_credentials(self.valid_creds, "admin")
        self.conn_mgr.connect("admin")
        self.conn_mgr.set_mode(ExecutionMode.LIVE, "admin", confirm_live=True)

        # 2. Phase 5: Intent Creation & Operational Approval
        intent = self.intent_mgr.create_intent(
            creator_id="trader_joe",
            symbol="NSE:TCS",
            side="BUY",
            quantity=100,
            order_type="LIMIT",
            correlation_id=str(uuid.uuid4()),
            limit_price=3950.0,
        )
        intent = self.intent_mgr.approve_intent(intent.intent_id, "risk_mgr_sue", str(uuid.uuid4()))

        # 3. Phase 6: Risk Gate Evaluation
        self.risk_evaluator.evaluate_intent(intent, "gate_officer", str(uuid.uuid4()))
        self.assertEqual(intent.state, IntentState.RISK_APPROVED)

        # 4. Phase 7: Order Instruction
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher_dan")
        self.assertEqual(ins.state, InstructionState.VALIDATED)

        # 5. Phase 9: Live Dispatch & Broker Acknowledgement
        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "dispatcher_dan")
        self.assertEqual(dispatched.state, InstructionState.ACKNOWLEDGED)

        # 6. Phase 8: Execution State Synchronization & Fill
        exec_rec = self.exec_mgr.get_by_instruction(ins.instruction_id)
        self.assertEqual(exec_rec.current_state, ExecutionState.ACKNOWLEDGED)

        self.live_adapter.create_simulated_fill(dispatched.broker_order_id, 100, 3950.0, "FILL-FULL-01")
        self.exec_mgr.apply_fill(exec_rec.execution_id, fill_id="FILL-FULL-01", quantity=100, price=3950.0)
        self.assertEqual(exec_rec.current_state, ExecutionState.FILLED)

        # 7. Phase 9: Four-Way Reconciliation
        report = self.conn_mgr.reconcile("auditor_eve", self.exec_mgr, self.instruction_mgr, self.live_adapter)
        self.assertEqual(report.result, BrokerReconciliationResult.MATCHED)

    # 28. LIVE mode cannot bypass risk gate
    def test_28_live_mode_cannot_bypass_risk_gate(self) -> None:
        intent = self.intent_mgr.create_intent(
            creator_id="op_alpha",
            symbol="NSE:INFY",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            correlation_id=str(uuid.uuid4()),
            limit_price=1600.0,
        )
        self.intent_mgr.approve_intent(intent.intent_id, "op_beta", str(uuid.uuid4()))
        # Attempt to create instruction without risk evaluation
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.assertIn("EXECUTION_CHAIN_BREACH", str(ctx.exception))

    # 29. LIVE mode cannot bypass operational approval
    def test_29_live_mode_cannot_bypass_operational_approval(self) -> None:
        intent = self.intent_mgr.create_intent(
            creator_id="op_alpha",
            symbol="NSE:INFY",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            correlation_id=str(uuid.uuid4()),
            limit_price=1600.0,
        )

        # Attempt to evaluate risk without operational approval results in REJECTED
        eval_res = self.risk_evaluator.evaluate_intent(intent, "risk_evaluator", str(uuid.uuid4()))
        self.assertEqual(eval_res.decision.value, "REJECTED")
        self.assertIn("NOT_OPERATIONALLY_APPROVED", eval_res.reason)

        # Attempt to create instruction directly from unapproved intent is blocked
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.assertIn("EXECUTION_CHAIN_BREACH", str(ctx.exception))

    # 30. LIVE mode cannot bypass OrderInstruction
    def test_30_live_mode_cannot_bypass_order_instruction(self) -> None:
        raw_dict = {"symbol": "NSE:INFY", "side": "BUY", "quantity": 10}
        with self.assertRaises(TypeError) as ctx:
            self.live_adapter.dispatch(raw_dict)
        self.assertIn("INVALID_INSTRUCTION_TYPE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
