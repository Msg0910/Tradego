"""
Regression tests for H-01 forensic finding remediation.
Guarantees that OrderInstruction cannot bypass canonical ExecutionIntent -> Approval -> Risk Evaluation chain.
"""

import unittest
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from gateway.api.app import create_app
from gateway.broker_adapter import PaperBrokerAdapter
from gateway.contracts import Capability
from gateway.intent import ExecutionIntentManager, IntentState
from gateway.order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from gateway.recovery import GuardState, TradingGuard
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import InMemorySessionStore, Tier1AuditLogger
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState


class TestH01Remediation(unittest.TestCase):
    """
    Authoritative regression tests for H-01 remediation.
    Validates that:
    1. Instructions created via convenience builder are CREATED (draft) and NOT dispatchable.
    2. Missing risk evaluation references are rejected.
    3. Fake 'RISK_APPROVED' string references are strictly rejected.
    4. Canonical instructions created from RISK_APPROVED intents dispatch successfully.
    5. API dispatch endpoint strictly requires VALIDATED instructions from canonical flow.
    """

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.guard._state = GuardState.NORMAL
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.broker_adapter = PaperBrokerAdapter(venue_name="PAPER_H01_VENUE")
        self.instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            broker_adapter=self.broker_adapter,
        )
        self.intent_mgr = ExecutionIntentManager(audit_logger=self.audit_logger)

        self.portfolio_state = PortfolioRuntimeState(
            account_id="H01_TEST_ACCT",
            initial_cash=1000000.0,
        )
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.risk_gate = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            max_order_quantity=1000,
            margin_rate=0.20,
        )

        self.session_store = InMemorySessionStore()
        self.token_admin = self.session_store.create_session(
            operator_id="admin_op",
            roles=["ADMIN"],
            capabilities=set(Capability),
        )
        self.token_dispatcher = self.session_store.create_session(
            operator_id="dispatcher_op",
            roles=["DISPATCHER"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_ORDER_INSTRUCT,
                Capability.CAP_ORDER_DISPATCH,
                Capability.CAP_TRADE_APPROVE,
                Capability.CAP_RISK_EVALUATE,
                Capability.CAP_TRADE_SUBMIT,
            },
        )

        self.app = create_app(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            intent_manager=self.intent_mgr,
            risk_gate=self.risk_gate,
            order_instruction_manager=self.instruction_mgr,
            broker_adapter=self.broker_adapter,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.audit_logger.close()

    def test_create_instruction_without_intent_is_not_dispatchable(self) -> None:
        """1. Convenience builder produces an instruction in CREATED state that cannot be dispatched."""
        ins = self.instruction_mgr.create_instruction(
            intent_id="intent-no-chain",
            symbol="NSE:INFY",
            side="BUY",
            quantity=10,
            limit_price=1500.0,
            order_type="LIMIT",
            operator_id="op_tester",
        )

        # Invariant 1: State must be CREATED (not VALIDATED)
        self.assertEqual(ins.state, InstructionState.CREATED)
        self.assertIsNone(ins.risk_evaluation_reference)
        self.assertIsNone(ins.approval_reference)

        # Invariant 2: Dispatch must be rejected
        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "op_tester")

        self.assertIn("INVALID_INSTRUCTION_STATE", str(ctx.exception))
        self.assertIn("CREATED", str(ctx.exception))

    def test_create_instruction_without_risk_evaluation_is_not_dispatchable(self) -> None:
        """2. An instruction without valid risk evaluation reference cannot be dispatched even if marked VALIDATED."""
        ins = OrderInstruction(
            instruction_id=f"ins-{uuid.uuid4().hex[:12]}",
            intent_id="intent-unapproved",
            symbol="NSE:TCS",
            side="BUY",
            quantity=20,
            order_type="LIMIT",
            limit_price=3500.0,
            correlation_id=str(uuid.uuid4()),
            risk_evaluation_reference=None,  # Missing risk reference
            approval_reference="op_approver",
            provenance={},
            state=InstructionState.VALIDATED,  # Forcibly tampered state
        )
        self.instruction_mgr._instructions[ins.instruction_id] = ins

        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "dispatcher")

        self.assertIn("CANONICAL_CHAIN_BREACH", str(ctx.exception))
        self.assertIn("risk evaluation reference", str(ctx.exception))

    def test_create_instruction_fake_risk_reference_rejected(self) -> None:
        """3. Hardcoded or fake 'RISK_APPROVED' string is rejected as an invalid risk reference."""
        ins = OrderInstruction(
            instruction_id=f"ins-{uuid.uuid4().hex[:12]}",
            intent_id="intent-fake-ref",
            symbol="NSE:RELIANCE",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            limit_price=2500.0,
            correlation_id=str(uuid.uuid4()),
            risk_evaluation_reference="RISK_APPROVED",  # The historical fake reference string
            approval_reference="op_approver",
            provenance={},
            state=InstructionState.VALIDATED,
        )
        self.instruction_mgr._instructions[ins.instruction_id] = ins

        with self.assertRaises(ValueError) as ctx:
            self.instruction_mgr.dispatch_instruction(ins.instruction_id, "dispatcher")

        self.assertIn("CANONICAL_CHAIN_BREACH", str(ctx.exception))
        self.assertIn("fake reference", str(ctx.exception))

    def test_canonical_instruction_is_dispatchable(self) -> None:
        """4. Instruction created via the canonical chain (Intent -> Operational Approval -> Risk Eval) is dispatchable."""
        corr_id = str(uuid.uuid4())
        intent = self.intent_mgr.create_intent(
            creator_id="trader_a",
            symbol="NSE:INFY",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=1500.0,
            correlation_id=corr_id,
        )
        self.intent_mgr.approve_intent(intent.intent_id, "approver_b", corr_id)
        self.risk_gate.evaluate_intent(intent, "risk_officer_c", corr_id)

        self.assertEqual(intent.state, IntentState.RISK_APPROVED)
        self.assertEqual(intent.risk_status, "ALLOWED")

        ins = self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher_d", corr_id)
        self.assertEqual(ins.state, InstructionState.VALIDATED)
        self.assertIsNotNone(ins.risk_evaluation_reference)
        self.assertNotEqual(ins.risk_evaluation_reference, "RISK_APPROVED")
        self.assertEqual(ins.approval_reference, "approver_b")

        dispatched = self.instruction_mgr.dispatch_instruction(ins.instruction_id, "dispatcher_d", corr_id)
        self.assertEqual(dispatched.state, InstructionState.ACKNOWLEDGED)
        self.assertTrue(dispatched.broker_order_id is not None)
        self.assertTrue(dispatched.broker_order_id.startswith("PAPER-"))

    def test_api_dispatch_requires_validated_instruction(self) -> None:
        """5. API dispatch endpoint (/api/v1/order-instructions/{id}/dispatch) rejects draft/unvalidated instructions."""
        # Step A: Create unvalidated instruction directly via builder
        draft_ins = self.instruction_mgr.create_instruction(
            intent_id="intent-api-draft",
            symbol="NSE:WIPRO",
            side="BUY",
            quantity=25,
            limit_price=450.0,
            order_type="LIMIT",
        )

        # Step B: Dispatch via API must fail with HTTP 409 / 400
        resp_fail = self.client.post(
            f"/api/v1/order-instructions/{draft_ins.instruction_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertIn(resp_fail.status_code, [400, 409])
        self.assertIn("INVALID_INSTRUCTION_STATE", str(resp_fail.json()))

        # Step C: Complete canonical flow via API
        r_intent = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
            json={
                "symbol": "NSE:WIPRO",
                "side": "BUY",
                "quantity": 25,
                "order_type": "LIMIT",
                "limit_price": 450.0,
            },
        )
        self.assertEqual(r_intent.status_code, 201)
        intent_id = r_intent.json()["intent_id"]

        # Approve
        r_appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(r_appr.status_code, 200)

        # Evaluate risk
        r_risk = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_risk.status_code, 200)

        # Create instruction canonical
        r_ins = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_ins.status_code, 201)
        canonical_ins_id = r_ins.json()["instruction_id"]
        self.assertEqual(r_ins.json()["state"], "VALIDATED")

        # Dispatch via API succeeds
        r_disp = self.client.post(
            f"/api/v1/order-instructions/{canonical_ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_disp.status_code, 200)
        self.assertEqual(r_disp.json()["state"], "ACKNOWLEDGED")
        self.assertTrue(r_disp.json()["broker_order_id"].startswith("PAPER-"))


if __name__ == "__main__":
    unittest.main()
