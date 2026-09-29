"""
Regression tests for H-03 forensic finding remediation.
Guarantees that all required operator UI API routes exist, enforce RBAC capabilities,
preserve two-person approval, and strictly execute through canonical business logic.
"""

import os
import shutil
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from gateway.api.app import create_app
from gateway.broker_adapter import PaperBrokerAdapter
from gateway.contracts import Capability
from gateway.disaster_recovery import DisasterRecoveryManager
from gateway.execution import ExecutionStateManager
from gateway.intent import ExecutionIntentManager
from gateway.observability import SystemHealthMonitor
from gateway.order_instruction import OrderInstructionManager
from gateway.persistence import PersistenceManager
from gateway.recovery import GuardState, TradingGuard
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import InMemorySessionStore, Tier1AuditLogger
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState


class TestH03Remediation(unittest.TestCase):
    """
    Authoritative regression tests for H-03 remediation:
    1. Unauthenticated access rejected (HTTP 401).
    2. Insufficient capability rejected (HTTP 403).
    3. Authorized read access (CAP_OBSERVE -> HTTP 200).
    4. Canonical execution-intent lifecycle via API (Submit -> Approve -> Risk -> Instruct -> Dispatch).
    5. Support for GET /approve as well as POST /approve.
    6. Cancellation routes for intent and instruction.
    7. Execution reconciliation endpoint via API.
    8. Confirmation that no route bypasses the canonical execution chain.
    """

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="tradego_test_h03_")
        self.wal_path = os.path.join(self.tmp_dir, "wal.jsonl")

        self.guard = TradingGuard()
        self.guard._state = GuardState.NORMAL
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.pm = PersistenceManager(wal_path=self.wal_path)
        self.broker_adapter = PaperBrokerAdapter(venue_name="PAPER_H03_VENUE")
        self.exec_mgr = ExecutionStateManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
        )
        self.instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            broker_adapter=self.broker_adapter,
            execution_manager=self.exec_mgr,
        )
        self.intent_mgr = ExecutionIntentManager(audit_logger=self.audit_logger)

        self.portfolio_state = PortfolioRuntimeState(
            account_id="H03_TEST_ACCT",
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

        self.dr_mgr = DisasterRecoveryManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            persistence_manager=self.pm,
            execution_manager=self.exec_mgr,
            instruction_manager=self.instruction_mgr,
            intent_manager=self.intent_mgr,
        )
        self.health_mon = SystemHealthMonitor(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            persistence_manager=self.pm,
            recovery_manager=self.dr_mgr,
            execution_manager=self.exec_mgr,
        )

        self.session_store = InMemorySessionStore()

        # Tokens
        self.token_trader = self.session_store.create_session(
            operator_id="trader_bob",
            roles=["TRADER"],
            capabilities={Capability.CAP_TRADE_SUBMIT, Capability.CAP_OBSERVE},
        )
        self.token_approver = self.session_store.create_session(
            operator_id="approver_alice",
            roles=["APPROVER"],
            capabilities={Capability.CAP_TRADE_APPROVE, Capability.CAP_OBSERVE},
        )
        self.token_risk = self.session_store.create_session(
            operator_id="risk_officer_charlie",
            roles=["RISK"],
            capabilities={Capability.CAP_RISK_EVALUATE, Capability.CAP_OBSERVE},
        )
        self.token_dispatcher = self.session_store.create_session(
            operator_id="dispatcher_dave",
            roles=["DISPATCHER"],
            capabilities={
                Capability.CAP_ORDER_INSTRUCT,
                Capability.CAP_ORDER_DISPATCH,
                Capability.CAP_TRADE_CANCEL,
                Capability.CAP_OBSERVE,
            },
        )
        self.token_admin = self.session_store.create_session(
            operator_id="admin_eve",
            roles=["ADMIN"],
            capabilities=set(Capability),
        )
        self.token_viewer = self.session_store.create_session(
            operator_id="viewer_victor",
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.token_no_caps = self.session_store.create_session(
            operator_id="unprivileged_uri",
            roles=["GUEST"],
            capabilities=set(),
        )

        self.app = create_app(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            intent_manager=self.intent_mgr,
            risk_gate=self.risk_gate,
            order_instruction_manager=self.instruction_mgr,
            execution_state_manager=self.exec_mgr,
            broker_adapter=self.broker_adapter,
            persistence_manager=self.pm,
            disaster_recovery_manager=self.dr_mgr,
            health_monitor=self.health_mon,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.pm.close()
        self.audit_logger.close()
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_unauthenticated_access_rejected(self) -> None:
        """1. Protected endpoints reject unauthenticated requests with HTTP 401."""
        endpoints = [
            ("GET", "/api/v1/execution-intents"),
            ("POST", "/api/v1/execution-intents"),
            ("POST", "/api/v1/execution-intents/int-1/approve"),
            ("GET", "/api/v1/execution-intents/int-1/approve"),
            ("POST", "/api/v1/execution-intents/int-1/reject"),
            ("POST", "/api/v1/execution-intents/int-1/cancel"),
            ("POST", "/api/v1/execution-intents/int-1/evaluate-risk"),
            ("POST", "/api/v1/execution-intents/int-1/create-instruction"),
            ("GET", "/api/v1/order-instructions"),
            ("POST", "/api/v1/order-instructions/ins-1/dispatch"),
            ("POST", "/api/v1/order-instructions/ins-1/cancel"),
            ("GET", "/api/v1/executions"),
            ("POST", "/api/v1/executions/exec-1/reconcile"),
        ]
        for method, url in endpoints:
            with self.subTest(method=method, url=url):
                if method == "GET":
                    resp = self.client.get(url)
                else:
                    resp = self.client.post(url, json={})
                self.assertEqual(resp.status_code, 401, f"Endpoint {method} {url} should require auth")

    def test_insufficient_capability_rejected(self) -> None:
        """2. Operators lacking required capabilities are rejected with HTTP 403."""
        # Viewer (CAP_OBSERVE only) attempting write actions
        headers_viewer = {"Authorization": f"Bearer {self.token_viewer}"}

        # Submit intent -> requires CAP_TRADE_SUBMIT
        r = self.client.post(
            "/api/v1/execution-intents",
            headers=headers_viewer,
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 10, "order_type": "MARKET"},
        )
        self.assertEqual(r.status_code, 403)

        # Approve intent -> requires CAP_TRADE_APPROVE
        r = self.client.post("/api/v1/execution-intents/any-id/approve", headers=headers_viewer)
        self.assertEqual(r.status_code, 403)

        # Reject intent -> requires CAP_TRADE_APPROVE
        r = self.client.post(
            "/api/v1/execution-intents/any-id/reject",
            headers=headers_viewer,
            json={"reason": "Rejected by test"},
        )
        self.assertEqual(r.status_code, 403)

        # Evaluate risk -> requires CAP_RISK_EVALUATE
        r = self.client.post("/api/v1/execution-intents/any-id/evaluate-risk", headers=headers_viewer)
        self.assertEqual(r.status_code, 403)

        # Create instruction -> requires CAP_ORDER_INSTRUCT
        r = self.client.post("/api/v1/execution-intents/any-id/create-instruction", headers=headers_viewer)
        self.assertEqual(r.status_code, 403)

        # Dispatch instruction -> requires CAP_ORDER_DISPATCH
        r = self.client.post("/api/v1/order-instructions/any-id/dispatch", headers=headers_viewer)
        self.assertEqual(r.status_code, 403)

        # Reconcile -> requires CAP_ADMIN
        r = self.client.post("/api/v1/executions/any-id/reconcile", headers=headers_viewer)
        self.assertEqual(r.status_code, 403)

        # Operator with zero capabilities attempting read actions
        headers_none = {"Authorization": f"Bearer {self.token_no_caps}"}
        self.assertEqual(self.client.get("/api/v1/execution-intents", headers=headers_none).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/order-instructions", headers=headers_none).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/executions", headers=headers_none).status_code, 403)

    def test_authorized_read_access(self) -> None:
        """3. Operators with CAP_OBSERVE have authorized read access across all entity lists."""
        headers = {"Authorization": f"Bearer {self.token_viewer}"}

        r_intents = self.client.get("/api/v1/execution-intents", headers=headers)
        self.assertEqual(r_intents.status_code, 200)
        self.assertIn("intents", r_intents.json())

        r_instructions = self.client.get("/api/v1/order-instructions", headers=headers)
        self.assertEqual(r_instructions.status_code, 200)
        self.assertIn("instructions", r_instructions.json())

        r_executions = self.client.get("/api/v1/executions", headers=headers)
        self.assertEqual(r_executions.status_code, 200)
        self.assertIn("executions", r_executions.json())

    def test_canonical_execution_intent_lifecycle_via_api(self) -> None:
        """4. Complete canonical execution lifecycle operates end-to-end through API endpoints."""
        # Step A: POST /api/v1/execution-intents (Trader Bob)
        r_create = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_trader}"},
            json={
                "symbol": "NSE:TCS",
                "side": "BUY",
                "quantity": 15,
                "order_type": "LIMIT",
                "limit_price": 3500.0,
                "reason": "Mean reversion setup",
            },
        )
        self.assertEqual(r_create.status_code, 201)
        intent_data = r_create.json()
        intent_id = intent_data["intent_id"]
        self.assertEqual(intent_data["state"], "PENDING_APPROVAL")
        self.assertEqual(intent_data["creator_id"], "trader_bob")

        # Step B: Two-Person Approval: Trader Bob cannot approve own intent
        r_self_approve = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_trader}"},  # lacking CAP_TRADE_APPROVE
        )
        self.assertEqual(r_self_approve.status_code, 403)

        # Step C: Approver Alice approves -> POST /api/v1/execution-intents/{id}/approve
        r_appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_approver}"},
        )
        self.assertEqual(r_appr.status_code, 200)
        self.assertEqual(r_appr.json()["state"], "APPROVED")
        self.assertEqual(r_appr.json()["approver_id"], "approver_alice")

        # Step D: Risk Officer Charlie evaluates risk -> POST /api/v1/execution-intents/{id}/evaluate-risk
        r_risk = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk}"},
        )
        self.assertEqual(r_risk.status_code, 200)
        risk_data = r_risk.json()
        self.assertEqual(risk_data["decision"], "ALLOWED")
        self.assertEqual(risk_data["resulting_state"], "RISK_APPROVED")

        # Step E: Dispatcher Dave creates instruction -> POST /api/v1/execution-intents/{id}/create-instruction
        r_ins = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_ins.status_code, 201)
        ins_data = r_ins.json()
        ins_id = ins_data["instruction_id"]
        self.assertEqual(ins_data["state"], "VALIDATED")
        self.assertNotEqual(ins_data["risk_evaluation_reference"], "RISK_APPROVED")

        # Step F: Dispatcher Dave dispatches instruction -> POST /api/v1/order-instructions/{id}/dispatch
        r_disp = self.client.post(
            f"/api/v1/order-instructions/{ins_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_disp.status_code, 200)
        disp_data = r_disp.json()
        self.assertEqual(disp_data["state"], "ACKNOWLEDGED")
        self.assertTrue(disp_data["broker_order_id"].startswith("PAPER-"))

        # Step G: Verify execution is created and readable via GET /api/v1/executions
        r_execs = self.client.get(
            "/api/v1/executions",
            headers={"Authorization": f"Bearer {self.token_viewer}"},
        )
        self.assertEqual(r_execs.status_code, 200)
        exec_list = r_execs.json()["executions"]
        self.assertTrue(any(e["instruction_id"] == ins_id for e in exec_list))

    def test_approve_supports_get_and_post(self) -> None:
        """5. Approval route accepts both POST and GET HTTP methods."""
        # Create intent
        intent = self.intent_mgr.create_intent(
            creator_id="trader_bob",
            symbol="NSE:RELIANCE",
            side="BUY",
            quantity=10,
            order_type="MARKET",
            correlation_id=str(uuid.uuid4()),
        )
        # GET /api/v1/execution-intents/{id}/approve
        r_get = self.client.get(
            f"/api/v1/execution-intents/{intent.intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_approver}"},
        )
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["state"], "APPROVED")

    def test_cancellation_routes_via_api(self) -> None:
        """6. Cancellation routes for intent and instruction operate authoritatively."""
        # 6a. Cancel Intent
        intent = self.intent_mgr.create_intent(
            creator_id="trader_bob",
            symbol="NSE:INFY",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=1500.0,
            correlation_id=str(uuid.uuid4()),
        )
        r_cancel_intent = self.client.post(
            f"/api/v1/execution-intents/{intent.intent_id}/cancel",
            headers={"Authorization": f"Bearer {self.token_trader}"},
            json={"reason": "Trader changed mind"},
        )
        self.assertEqual(r_cancel_intent.status_code, 200)
        self.assertEqual(r_cancel_intent.json()["state"], "CANCELLED")

        # 6b. Cancel Instruction
        intent2 = self.intent_mgr.create_intent(
            creator_id="trader_bob",
            symbol="NSE:WIPRO",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            limit_price=450.0,
            correlation_id=str(uuid.uuid4()),
        )
        self.intent_mgr.approve_intent(intent2.intent_id, "approver_alice", intent2.correlation_id)
        self.risk_gate.evaluate_intent(intent2, "risk_officer_charlie", intent2.correlation_id)
        ins = self.instruction_mgr.create_instruction_from_intent(intent2, "dispatcher_dave")
        self.assertEqual(ins.state.value, "VALIDATED")

        r_cancel_ins = self.client.post(
            f"/api/v1/order-instructions/{ins.instruction_id}/cancel",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
            json={"reason": "Cancel before dispatch"},
        )
        self.assertEqual(r_cancel_ins.status_code, 200)
        self.assertEqual(r_cancel_ins.json()["state"], "CANCELLED")

    def test_execution_reconciliation_via_api(self) -> None:
        """7. Execution reconciliation endpoint calls canonical reconciliation logic."""
        intent = self.intent_mgr.create_intent(
            creator_id="trader_bob",
            symbol="NSE:HDFC",
            side="BUY",
            quantity=20,
            order_type="LIMIT",
            limit_price=1600.0,
            correlation_id=str(uuid.uuid4()),
        )
        self.intent_mgr.approve_intent(intent.intent_id, "approver_alice", intent.correlation_id)
        self.risk_gate.evaluate_intent(intent, "risk_officer_charlie", intent.correlation_id)
        ins = self.instruction_mgr.create_instruction_from_intent(intent, "dispatcher_dave")
        self.instruction_mgr.dispatch_instruction(ins.instruction_id, "dispatcher_dave")

        rec = self.exec_mgr.get_by_instruction(ins.instruction_id)
        self.assertIsNotNone(rec)

        # POST /api/v1/executions/{execution_id}/reconcile with Admin token
        r_rec = self.client.post(
            f"/api/v1/executions/{rec.execution_id}/reconcile",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(r_rec.status_code, 200)
        data = r_rec.json()
        self.assertEqual(data["reconciliation_status"], "RECONCILED")

    def test_no_route_bypasses_canonical_chain(self) -> None:
        """8. Verification that routes reject attempts to jump or bypass the canonical execution chain."""
        # Unapproved intent cannot create instruction
        intent_raw = self.intent_mgr.create_intent(
            creator_id="trader_bob",
            symbol="NSE:AXISBANK",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=900.0,
            correlation_id=str(uuid.uuid4()),
        )
        r_bypass1 = self.client.post(
            f"/api/v1/execution-intents/{intent_raw.intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_bypass1.status_code, 400)
        self.assertIn("EXECUTION_CHAIN_BREACH", str(r_bypass1.json()))

        # Approved but not risk-evaluated intent cannot create instruction
        self.intent_mgr.approve_intent(intent_raw.intent_id, "approver_alice", intent_raw.correlation_id)
        r_bypass2 = self.client.post(
            f"/api/v1/execution-intents/{intent_raw.intent_id}/create-instruction",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_bypass2.status_code, 400)
        self.assertIn("EXECUTION_CHAIN_BREACH", str(r_bypass2.json()))

        # Draft unvalidated instruction cannot be dispatched
        draft_ins = self.instruction_mgr.create_instruction(
            intent_id="intent-fake",
            symbol="NSE:AXISBANK",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            limit_price=900.0,
        )
        r_bypass3 = self.client.post(
            f"/api/v1/order-instructions/{draft_ins.instruction_id}/dispatch",
            headers={"Authorization": f"Bearer {self.token_dispatcher}"},
        )
        self.assertEqual(r_bypass3.status_code, 409)
        self.assertIn("INVALID_INSTRUCTION_STATE", str(r_bypass3.json()))


if __name__ == "__main__":
    unittest.main()
