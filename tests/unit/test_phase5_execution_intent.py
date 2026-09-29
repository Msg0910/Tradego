"""
Unit tests for Tradego Phase 5: Operational Gating & Execution Intent Boundary.

Validates all 24 mandatory architectural specifications:
1. Authenticated intent creation
2. Unauthenticated rejection (401)
3. Capability rejection (403 for unauthorized operator)
4. Malformed request rejection (400/422)
5. Unique intent ID
6. Correlation ID propagation
7. Correct initial lifecycle state
8. Operator A creates intent
9. Operator B can approve when authorized
10. Operator A cannot self-approve (409 Conflict)
11. Unauthorized approval rejected (403)
12. Approval is single-use
13. Rejection is single-use
14. Cancelled intent cannot be approved
15. Expired intent cannot be approved
16. Invalid state transition rejected
17. Every transition produces audit record
18. Intent does NOT call Broker
19. Intent does NOT call ExecutionRouter
20. Intent does NOT call Market Provider
21. No order execution occurs
22. Response explicitly distinguishes intent acceptance from execution
23. UI exposes intent state correctly
24. Full intent lifecycle smoke test
"""

from datetime import datetime, timezone
import time
import unittest
from unittest.mock import MagicMock
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient

from gateway.adapters import MarketStateAdapter, PortfolioRiskAdapter
from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.command import CommandGateway
from gateway.contracts import Capability
from gateway.intent import ExecutionIntent, ExecutionIntentManager, IntentState
from gateway.projection import SnapshotGenerator
from gateway.recovery import RecoveryManager
from gateway.security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from services.runtime.guards import TradingGuard


class TestPhase5ExecutionIntent(unittest.TestCase):
    """Comprehensive test suite for Phase 5 Operational Gating & Execution Intent Boundary."""

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

        # Build FastAPI application
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
        )
        self.client = TestClient(self.app)

        # Setup distinct test operator sessions
        self.token_op_a = self.session_store.create_session(
            operator_id="operator_a",
            roles=["TRADER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_SUBMIT},
        )
        self.token_op_b = self.session_store.create_session(
            operator_id="operator_b",
            roles=["RISK_OFFICER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_APPROVE},
        )
        self.token_op_c = self.session_store.create_session(
            operator_id="operator_c",
            roles=["OPERATOR"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_CANCEL},
        )
        self.token_unauth = self.session_store.create_session(
            operator_id="operator_viewer",
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.token_admin = self.session_store.create_session(
            operator_id="admin_op",
            roles=["ADMIN"],
            capabilities={Capability.CAP_ADMIN},
        )

    # 1. Authenticated intent creation
    def test_01_authenticated_intent_creation(self) -> None:
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:NIFTY50",
                "side": "BUY",
                "quantity": 50,
                "order_type": "LIMIT",
                "limit_price": 24500.50,
                "reason": "Mean reversion trigger",
            },
        )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["intent_id"].startswith("intent-"))
        self.assertEqual(data["symbol"], "NSE:NIFTY50")
        self.assertEqual(data["side"], "BUY")
        self.assertEqual(data["quantity"], 50)
        self.assertEqual(data["order_type"], "LIMIT")
        self.assertEqual(data["limit_price"], 24500.50)
        self.assertEqual(data["creator_id"], "operator_a")
        self.assertEqual(data["state"], "PENDING_APPROVAL")
        self.assertEqual(data["status_category"], "ACCEPTED_AS_INTENT")
        self.assertFalse(data["is_executed"])

    # 2. Unauthenticated rejection
    def test_02_unauthenticated_rejection(self) -> None:
        resp = self.client.post(
            "/api/v1/execution-intents",
            json={"symbol": "NSE:NIFTY50", "side": "BUY", "quantity": 10},
        )
        self.assertEqual(resp.status_code, 401)

    # 3. Capability rejection
    def test_03_capability_rejection(self) -> None:
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_unauth}"},
            json={
                "symbol": "NSE:NIFTY50",
                "side": "BUY",
                "quantity": 25,
                "order_type": "MARKET",
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("INSUFFICIENT_CAPABILITY", str(resp.json()))

    # 4. Malformed request rejection
    def test_04_malformed_request_rejection(self) -> None:
        # Invalid quantity (<= 0)
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:NIFTY50", "side": "BUY", "quantity": 0},
        )
        self.assertEqual(resp.status_code, 400)

        # Invalid side
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:NIFTY50", "side": "HOLD", "quantity": 10},
        )
        self.assertEqual(resp.status_code, 400)

        # Empty symbol
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "  ", "side": "BUY", "quantity": 10},
        )
        self.assertEqual(resp.status_code, 400)

        # LIMIT without positive limit_price
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:NIFTY50",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 0.0,
            },
        )
        self.assertEqual(resp.status_code, 400)

    # 5. Unique intent ID
    def test_05_unique_intent_id(self) -> None:
        resp1 = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 10, "order_type": "MARKET"},
        )
        resp2 = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 10, "order_type": "MARKET"},
        )
        self.assertEqual(resp1.status_code, 201)
        self.assertEqual(resp2.status_code, 201)
        id1 = resp1.json()["intent_id"]
        id2 = resp2.json()["intent_id"]
        self.assertNotEqual(id1, id2)

    # 6. Correlation ID propagation
    def test_06_correlation_id_propagation(self) -> None:
        custom_corr = f"test-corr-{uuid.uuid4().hex[:8]}"
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={
                "Authorization": f"Bearer {self.token_op_a}",
                "X-Correlation-ID": custom_corr,
            },
            json={"symbol": "NSE:TCS", "side": "SELL", "quantity": 15, "order_type": "MARKET"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.headers.get("X-Correlation-ID"), custom_corr)
        data = resp.json()
        self.assertEqual(data["correlation_id"], custom_corr)

    # 7. Correct initial lifecycle state
    def test_07_correct_initial_lifecycle_state(self) -> None:
        # Default is PENDING_APPROVAL
        resp1 = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:SBIN", "side": "BUY", "quantity": 100, "order_type": "MARKET"},
        )
        self.assertEqual(resp1.json()["state"], "PENDING_APPROVAL")

        # Explicit DRAFT
        resp2 = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:SBIN",
                "side": "BUY",
                "quantity": 100,
                "order_type": "MARKET",
                "initial_state": "DRAFT",
            },
        )
        self.assertEqual(resp2.json()["state"], "DRAFT")

    # 8. Operator A creates intent
    def test_08_operator_a_creates_intent(self) -> None:
        resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:RELIANCE", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["creator_id"], "operator_a")

    # 9. Operator B can approve when authorized
    def test_09_operator_b_can_approve_when_authorized(self) -> None:
        # Create by Operator A
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:RELIANCE", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # Approve by Operator B
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)
        data = appr_resp.json()
        self.assertEqual(data["state"], "APPROVED")
        self.assertEqual(data["creator_id"], "operator_a")
        self.assertEqual(data["approver_id"], "operator_b")
        self.assertEqual(data["status_category"], "ACCEPTED_AS_INTENT")
        self.assertFalse(data["is_executed"])

    # 10. Operator A cannot self-approve
    def test_10_operator_a_cannot_self_approve(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:RELIANCE", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # Add approve cap to operator A temporarily or check with admin attempting own intent
        admin_create = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_admin}"},
            json={"symbol": "NSE:TCS", "side": "BUY", "quantity": 10, "order_type": "MARKET"},
        )
        admin_intent_id = admin_create.json()["intent_id"]

        # Admin attempting self-approval (has CAP_ADMIN, but same operator constraint must block)
        self_appr_resp = self.client.post(
            f"/api/v1/execution-intents/{admin_intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(self_appr_resp.status_code, 409)
        self.assertIn("SAME_OPERATOR_APPROVAL_REJECTED", str(self_appr_resp.json()))

    # 11. Unauthorized approval rejected
    def test_11_unauthorized_approval_rejected(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:HDFC", "side": "SELL", "quantity": 30, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # Operator viewer attempts approval
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_unauth}"},
        )
        self.assertEqual(appr_resp.status_code, 403)
        self.assertIn("INSUFFICIENT_CAPABILITY", str(appr_resp.json()))

    # 12. Approval is single-use
    def test_12_approval_is_single_use(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:HDFC", "side": "SELL", "quantity": 30, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # First approval succeeds
        appr1 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr1.status_code, 200)

        # Second approval attempt fails with 409 Conflict
        appr2 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr2.status_code, 409)
        self.assertIn("INTENT_ALREADY_TERMINAL", str(appr2.json()))

    # 13. Rejection is single-use
    def test_13_rejection_is_single_use(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:WIPRO", "side": "BUY", "quantity": 100, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # First rejection succeeds
        rej1 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/reject",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
            json={"reason": "Excess volatility"},
        )
        self.assertEqual(rej1.status_code, 200)
        self.assertEqual(rej1.json()["state"], "REJECTED")

        # Second rejection attempt fails
        rej2 = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/reject",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
            json={"reason": "Second rejection"},
        )
        self.assertEqual(rej2.status_code, 409)
        self.assertIn("INTENT_ALREADY_TERMINAL", str(rej2.json()))

        # Attempting to approve rejected intent also fails
        appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr.status_code, 409)

    # 14. Cancelled intent cannot be approved
    def test_14_cancelled_intent_cannot_be_approved(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:AXISBANK", "side": "BUY", "quantity": 50, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]

        # Creator cancels intent
        canc = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/cancel",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"reason": "Market shifted"},
        )
        self.assertEqual(canc.status_code, 200)
        self.assertEqual(canc.json()["state"], "CANCELLED")

        # Approval of cancelled intent fails
        appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr.status_code, 409)
        self.assertIn("INTENT_CANCELLED", str(appr.json()))

    # 15. Expired intent cannot be approved
    def test_15_expired_intent_cannot_be_approved(self) -> None:
        # Create intent with short 1s TTL
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:KOTAKBANK",
                "side": "BUY",
                "quantity": 10,
                "order_type": "MARKET",
                "ttl_seconds": 1,
            },
        )
        intent_id = create_resp.json()["intent_id"]

        # Sleep past TTL
        time.sleep(1.1)

        # Attempt approval
        appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr.status_code, 409)
        self.assertIn("INTENT_EXPIRED", str(appr.json()))

    # 16. Invalid state transition rejected
    def test_16_invalid_state_transition_rejected(self) -> None:
        # Create DRAFT intent
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:LT",
                "side": "BUY",
                "quantity": 15,
                "order_type": "MARKET",
                "initial_state": "DRAFT",
            },
        )
        intent_id = create_resp.json()["intent_id"]

        # Directly approving DRAFT without submission should be rejected
        appr = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr.status_code, 409)
        self.assertIn("INVALID_STATE_TRANSITION", str(appr.json()))

    # 17. Every transition produces audit record
    def test_17_every_transition_produces_audit_record(self) -> None:
        # Create mock audit logger to intercept calls
        mock_audit = MagicMock()
        mgr = ExecutionIntentManager(audit_logger=mock_audit)
        corr_id = "audit-corr-test"

        # 1. Create
        intent = mgr.create_intent(
            creator_id="op_a",
            symbol="NSE:NIFTY",
            side="BUY",
            quantity=50,
            order_type="MARKET",
            correlation_id=corr_id,
        )
        # Should log INTENT_CREATED and INTENT_APPROVAL_REQUESTED
        actions = [call[0][0]["action"] for call in mock_audit.log.call_args_list]
        self.assertIn("INTENT_CREATED", actions)
        self.assertIn("INTENT_APPROVAL_REQUESTED", actions)

        # 2. Self-approval rejection
        with self.assertRaises(ValueError):
            mgr.approve_intent(intent.intent_id, "op_a", correlation_id=corr_id)
        actions = [call[0][0]["action"] for call in mock_audit.log.call_args_list]
        self.assertIn("INTENT_SAME_OPERATOR_REJECTED", actions)

        # 3. Valid approval
        mgr.approve_intent(intent.intent_id, "op_b", correlation_id=corr_id)
        actions = [call[0][0]["action"] for call in mock_audit.log.call_args_list]
        self.assertIn("INTENT_APPROVED", actions)

    # 18. Intent does NOT call Broker
    def test_18_intent_does_not_call_broker(self) -> None:
        # Verify that ExecutionIntentManager has no reference to any broker or broker connectivity
        self.assertFalse(hasattr(self.intent_mgr, "_broker"))
        self.assertFalse(hasattr(self.intent_mgr, "broker"))

        # Create and approve an intent
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:NIFTY", "side": "BUY", "quantity": 50, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)
        # Confirmation that no broker was invoked or referenced
        self.assertNotIn("broker_order_id", appr_resp.json())

    # 19. Intent does NOT call ExecutionRouter
    def test_19_intent_does_not_call_execution_router(self) -> None:
        self.assertFalse(hasattr(self.intent_mgr, "_router"))
        self.assertFalse(hasattr(self.intent_mgr, "execution_router"))

    # 20. Intent does NOT call Market Provider
    def test_20_intent_does_not_call_market_provider(self) -> None:
        self.assertFalse(hasattr(self.intent_mgr, "_provider"))
        self.assertFalse(hasattr(self.intent_mgr, "market_provider"))

    # 21. No order execution occurs
    def test_21_no_order_execution_occurs(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:TATASTEEL", "side": "BUY", "quantity": 100, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        data = appr_resp.json()
        self.assertFalse(data["is_executed"])
        self.assertEqual(data["status_category"], "ACCEPTED_AS_INTENT")

    # 22. Response explicitly distinguishes intent acceptance from execution
    def test_22_response_distinguishes_intent_acceptance_from_execution(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:TATASTEEL", "side": "BUY", "quantity": 100, "order_type": "LIMIT", "limit_price": 150.0},
        )
        data = create_resp.json()
        self.assertEqual(data["status_category"], "ACCEPTED_AS_INTENT")
        self.assertIn("NOT EXECUTED", data["note"])
        self.assertFalse(data["is_executed"])

    # 23. UI exposes intent state correctly
    def test_23_ui_exposes_intent_state_correctly(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        # Check that UI elements exist
        self.assertIn("execution-intent-panel", html)
        self.assertIn("INTENT ONLY — NOT EXECUTED", html)
        self.assertIn("btn-submit-intent", html)
        self.assertIn("intent-table-body", html)
        self.assertIn("fetchExecutionIntents", html)
        self.assertIn("renderExecutionIntents", html)

    # 24. Full intent lifecycle smoke test
    def test_24_full_intent_lifecycle_smoke_test(self) -> None:
        # Step A: Operator A drafts intent
        draft_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:ITC",
                "side": "BUY",
                "quantity": 200,
                "order_type": "LIMIT",
                "limit_price": 450.25,
                "reason": "Dividend accumulation",
                "initial_state": "DRAFT",
            },
        )
        self.assertEqual(draft_resp.status_code, 201)
        intent_id = draft_resp.json()["intent_id"]
        self.assertEqual(draft_resp.json()["state"], "DRAFT")

        # Step B: Operator A submits intent for review
        sub_intent = self.intent_mgr.submit_intent(intent_id, "operator_a", "smoke-test-corr")
        self.assertEqual(sub_intent.state, IntentState.PENDING_APPROVAL)

        # Step C: Read single intent
        get_resp = self.client.get(
            f"/api/v1/execution-intents/{intent_id}",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(get_resp.json()["state"], "PENDING_APPROVAL")

        # Step D: List intents
        list_resp = self.client.get(
            "/api/v1/execution-intents?state=PENDING_APPROVAL",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(list_resp.status_code, 200)
        ids = [i["intent_id"] for i in list_resp.json()["intents"]]
        self.assertIn(intent_id, ids)

        # Step E: Operator B approves intent
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)
        self.assertEqual(appr_resp.json()["state"], "APPROVED")
        self.assertEqual(appr_resp.json()["approver_id"], "operator_b")
        self.assertEqual(appr_resp.json()["status_category"], "ACCEPTED_AS_INTENT")
        self.assertFalse(appr_resp.json()["is_executed"])


if __name__ == "__main__":
    unittest.main()
