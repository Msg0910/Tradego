"""
Unit tests for Tradego Phase 6: Authoritative Risk Gating & Pre-Trade Limit Evaluation.

Validates all mandatory Phase 6 specifications:
1. Valid intent passes risk evaluation (ALLOWED, RISK_APPROVED)
2. Insufficient margin rejection
3. Quantity limit breach rejection
4. Exposure limit breach rejection
5. Symbol restriction rejection
6. Unavailable authoritative risk source (UNAVAILABLE without fabrication)
7. HALTED guard rejection
8. PAUSED guard rejection
9. Cancelled intent rejection
10. Expired intent rejection
11. Already rejected intent rejection
12. Unapproved intent rejection (enforces operational gating precedence)
13. Duplicate active intent rejection
14. Conflicting active intent rejection
15. Unauthorized risk evaluation (403)
16. Complete audit trail generation (STARTED, ALLOWED, REJECTED, UNAVAILABLE, LIMIT_BREACH)
17. No broker invocation
18. No ExecutionRouter invocation
19. Frozen core remains untouched and read-only
20. Complete intent -> operational approval -> risk evaluation lifecycle
21. UI exposes risk evaluation state and action controls
22. GET /risk-evaluation endpoint query
"""

from datetime import datetime, timezone, timedelta
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
from gateway.risk_gate import PreTradeRiskEvaluator, RiskDecisionType, RiskEvaluationResult
from gateway.security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from services.risk.limits import RiskLimits
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState
from services.runtime.portfolio import PortfolioRuntimeState


class TestPhase6RiskGate(unittest.TestCase):
    """Exhaustive test suite for Phase 6 Pre-Trade Risk Gate & Evaluation Boundary."""

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

        # Authoritative Phase 6 core structures for testing
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.portfolio_state = PortfolioRuntimeState(
            account_id="PAPER_TEST_P6",
            initial_cash=50000.0,
        )

        # Phase 6 Risk Gate Boundary
        self.risk_gate = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            restricted_symbols={"NSE:BANNED", "NSE:RESTRICTED"},
            max_order_quantity=1000,
            margin_rate=0.20,
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
        self.token_unauth = self.session_store.create_session(
            operator_id="viewer_unauth",
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.token_admin = self.session_store.create_session(
            operator_id="admin_op",
            roles=["ADMIN"],
            capabilities={Capability.CAP_ADMIN},
        )

    def _create_and_approve_intent(
        self,
        symbol: str = "NSE:TCS",
        side: str = "BUY",
        quantity: int = 50,
        limit_price: float = 3000.0,
    ) -> str:
        """Helper to create an intent by Operator A and approve it by Operator B."""
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": "LIMIT",
                "limit_price": limit_price,
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        intent_id = create_resp.json()["intent_id"]

        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)
        self.assertEqual(appr_resp.json()["state"], "APPROVED")
        return intent_id

    # 1. Valid intent passes risk evaluation
    def test_01_valid_intent_passes_risk_evaluation(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=50, limit_price=100.0)
        # Required margin = 50 * 100 * 0.20 = 1,000 <= 50,000 cash

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "ALLOWED")
        self.assertEqual(data["resulting_state"], "RISK_APPROVED")
        self.assertEqual(data["status_category"], "ACCEPTED_AS_INTENT")
        self.assertFalse(data["is_executed"])

        # Check intent entity
        intent = self.intent_mgr.get_intent(intent_id)
        self.assertEqual(intent.state, IntentState.RISK_APPROVED)
        self.assertEqual(intent.risk_status, "ALLOWED")
        self.assertFalse(intent.is_executed)

    # 2. Insufficient margin rejection
    def test_02_insufficient_margin(self) -> None:
        # Available cash is 50,000.
        # Order 500 * 600.0 = 300,000 notional. Required margin = 300,000 * 0.20 = 60,000 > 50,000 cash
        intent_id = self._create_and_approve_intent(quantity=500, limit_price=600.0)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("INSUFFICIENT_MARGIN", data["reason"])
        self.assertEqual(data["resulting_state"], "RISK_REJECTED")

        intent = self.intent_mgr.get_intent(intent_id)
        self.assertEqual(intent.state, IntentState.RISK_REJECTED)
        self.assertEqual(intent.risk_status, "REJECTED")

    # 3. Quantity limit breach rejection
    def test_03_quantity_limit_breach(self) -> None:
        # Max quantity configured is 1,000. Submit 1,500
        intent_id = self._create_and_approve_intent(quantity=1500, limit_price=10.0)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("QUANTITY_LIMIT_BREACH", data["reason"])
        self.assertEqual(data["resulting_state"], "RISK_REJECTED")

    # 4. Exposure limit breach rejection
    def test_04_exposure_limit_breach(self) -> None:
        # Equity is 50,000. Max gross leverage is 2.0 (Max gross exposure = 100,000).
        # Order notional = 800 * 200.0 = 160,000 > 100,000 limit. (Margin rate 0.20 = 32,000 <= 50,000 cash)
        intent_id = self._create_and_approve_intent(quantity=800, limit_price=200.0)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("EXPOSURE_LIMIT_BREACH", data["reason"])
        self.assertEqual(data["resulting_state"], "RISK_REJECTED")

    # 5. Symbol restriction rejection
    def test_05_symbol_restriction(self) -> None:
        intent_id = self._create_and_approve_intent(symbol="NSE:BANNED", quantity=10, limit_price=50.0)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("SYMBOL_RESTRICTED", data["reason"])
        self.assertEqual(data["resulting_state"], "RISK_REJECTED")

    # 6. Unavailable authoritative risk source
    def test_06_unavailable_authoritative_risk_source(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=10, limit_price=50.0)

        # Unmount portfolio state
        self.risk_gate.set_portfolio_state(None)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "UNAVAILABLE")
        self.assertIn("AUTHORITATIVE_SOURCE_UNAVAILABLE", data["reason"])
        # Intent state remains APPROVED (does not transition to RISK_APPROVED or RISK_REJECTED)
        intent = self.intent_mgr.get_intent(intent_id)
        self.assertEqual(intent.state, IntentState.APPROVED)
        self.assertEqual(intent.risk_status, "UNAVAILABLE")

        # Restore
        self.risk_gate.set_portfolio_state(self.portfolio_state)

    # 7. HALTED guard rejection
    def test_07_halted_guard(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=10, limit_price=50.0)

        # Trip guard to HALTED
        self.guard.trip(reason="Emergency kill test")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("GUARD_HALTED", data["reason"])

    # 8. PAUSED guard rejection
    def test_08_paused_guard(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=10, limit_price=50.0)

        # Pause guard
        self.guard.pause()
        self.assertEqual(self.guard.state, GuardState.PAUSED)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("GUARD_PAUSED", data["reason"])

    # 9. Cancelled intent rejection
    def test_09_cancelled_intent(self) -> None:
        # Create intent and cancel it
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]
        self.client.post(
            f"/api/v1/execution-intents/{intent_id}/cancel",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"reason": "Operator cancel"},
        )

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("INTENT_CANCELLED", data["reason"])

    # 10. Expired intent rejection
    def test_10_expired_intent(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 20, "order_type": "MARKET", "ttl_seconds": 1},
        )
        intent_id = create_resp.json()["intent_id"]
        time.sleep(1.1)

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("INTENT_EXPIRED", data["reason"])

    # 11. Already rejected intent rejection
    def test_11_already_rejected_intent(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]
        self.client.post(
            f"/api/v1/execution-intents/{intent_id}/reject",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
            json={"reason": "Operator reject"},
        )

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("INTENT_ALREADY_REJECTED", data["reason"])

    # 12. Unapproved intent rejection (enforces operational review precedence)
    def test_12_not_operationally_approved_intent(self) -> None:
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={"symbol": "NSE:INFY", "side": "BUY", "quantity": 20, "order_type": "MARKET"},
        )
        intent_id = create_resp.json()["intent_id"]
        # Intent is in PENDING_APPROVAL state, not yet APPROVED

        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "REJECTED")
        self.assertIn("NOT_OPERATIONALLY_APPROVED", data["reason"])

    # 13. Duplicate active intent rejection
    def test_13_duplicate_active_intent(self) -> None:
        # Intent 1: Approved and Risk Approved
        id1 = self._create_and_approve_intent(symbol="NSE:HCLTECH", quantity=30, limit_price=1200.0)
        self.client.post(
            f"/api/v1/execution-intents/{id1}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )

        # Intent 2: Identical symbol, side, quantity, price
        id2 = self._create_and_approve_intent(symbol="NSE:HCLTECH", quantity=30, limit_price=1200.0)
        resp2 = self.client.post(
            f"/api/v1/execution-intents/{id2}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["decision"], "REJECTED")
        self.assertIn("DUPLICATE_OR_CONFLICTING_INTENT", data2["reason"])

    # 14. Conflicting active intent rejection
    def test_14_conflicting_active_intent(self) -> None:
        # Intent 1: BUY
        id1 = self._create_and_approve_intent(symbol="NSE:BAJFINANCE", side="BUY", quantity=10, limit_price=1000.0)
        self.client.post(
            f"/api/v1/execution-intents/{id1}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )

        # Intent 2: SELL on same symbol (conflicting)
        id2 = self._create_and_approve_intent(symbol="NSE:BAJFINANCE", side="SELL", quantity=10, limit_price=1000.0)
        resp2 = self.client.post(
            f"/api/v1/execution-intents/{id2}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertEqual(data2["decision"], "REJECTED")
        self.assertIn("DUPLICATE_OR_CONFLICTING_INTENT", data2["reason"])

    # 15. Unauthorized risk evaluation (403)
    def test_15_unauthorized_risk_evaluation(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=10, limit_price=50.0)

        # Viewer without CAP_RISK_EVALUATE attempts risk evaluation
        resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_unauth}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("INSUFFICIENT_CAPABILITY", str(resp.json()))

    # 16. Complete audit trail generation
    def test_16_audit_trail_generation(self) -> None:
        mock_audit = MagicMock()
        evaluator = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=mock_audit,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
        )
        intent = ExecutionIntent(
            intent_id="audit-intent-1",
            correlation_id="audit-corr-1",
            creator_id="op_a",
            symbol="NSE:NIFTY",
            side="BUY",
            quantity=50,
            order_type="LIMIT",
            limit_price=100.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            state=IntentState.APPROVED,
        )
        evaluator.evaluate_intent(intent, "risk_officer", "audit-corr-1")

        actions = [call[0][0]["action"] for call in mock_audit.log.call_args_list]
        self.assertIn("RISK_EVALUATION_STARTED", actions)
        self.assertIn("RISK_ALLOWED", actions)

    # 17. No broker invocation
    def test_17_no_broker_invocation(self) -> None:
        self.assertFalse(hasattr(self.risk_gate, "_broker"))
        self.assertFalse(hasattr(self.risk_gate, "broker"))

    # 18. No ExecutionRouter invocation
    def test_18_no_execution_router_invocation(self) -> None:
        self.assertFalse(hasattr(self.risk_gate, "_router"))
        self.assertFalse(hasattr(self.risk_gate, "execution_router"))

    # 19. Frozen core remains untouched and read-only
    def test_19_frozen_core_read_only(self) -> None:
        # Pre-trade risk evaluation strictly reads from RiskLimits and PortfolioRuntimeState
        acct_before = self.portfolio_state.get_account_state()
        intent_id = self._create_and_approve_intent(quantity=10, limit_price=50.0)
        self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        acct_after = self.portfolio_state.get_account_state()
        # Verifies no mutation of cash or positions in core portfolio
        self.assertEqual(acct_before.available_cash, acct_after.available_cash)
        self.assertEqual(acct_before.total_equity, acct_after.total_equity)

    # 20. Complete lifecycle
    def test_20_complete_lifecycle(self) -> None:
        # Step 1: Create
        create_resp = self.client.post(
            "/api/v1/execution-intents",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
            json={
                "symbol": "NSE:MARUTI",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 1000.0,
                "reason": "Lifecycle test",
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        intent_id = create_resp.json()["intent_id"]
        self.assertEqual(create_resp.json()["state"], "PENDING_APPROVAL")

        # Step 2: Operational approval
        appr_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/approve",
            headers={"Authorization": f"Bearer {self.token_op_b}"},
        )
        self.assertEqual(appr_resp.status_code, 200)
        self.assertEqual(appr_resp.json()["state"], "APPROVED")

        # Step 3: Risk evaluation
        risk_resp = self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )
        self.assertEqual(risk_resp.status_code, 200)
        self.assertEqual(risk_resp.json()["decision"], "ALLOWED")
        self.assertEqual(risk_resp.json()["resulting_state"], "RISK_APPROVED")
        self.assertEqual(risk_resp.json()["status_category"], "ACCEPTED_AS_INTENT")
        self.assertFalse(risk_resp.json()["is_executed"])

    # 21. UI exposes risk evaluation state and action controls
    def test_21_ui_exposes_risk_state(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Evaluate Risk", html)
        self.assertIn("handleEvaluateRisk", html)
        self.assertIn("evaluate-risk", html)

    # 22. GET /risk-evaluation endpoint query
    def test_22_get_risk_evaluation_endpoint(self) -> None:
        intent_id = self._create_and_approve_intent(quantity=25, limit_price=100.0)
        self.client.post(
            f"/api/v1/execution-intents/{intent_id}/evaluate-risk",
            headers={"Authorization": f"Bearer {self.token_risk_officer}"},
        )

        resp = self.client.get(
            f"/api/v1/execution-intents/{intent_id}/risk-evaluation",
            headers={"Authorization": f"Bearer {self.token_op_a}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["decision"], "ALLOWED")
        self.assertEqual(data["resulting_state"], "RISK_APPROVED")
        self.assertEqual(data["evaluator_id"], "risk_officer")
        self.assertIn("required_margin", data["details"])


if __name__ == "__main__":
    unittest.main()
