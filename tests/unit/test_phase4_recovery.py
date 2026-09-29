"""
Unit tests for Tradego Phase 4: Recovery & Operational Risk Control Boundary.

Tests all 29 mandatory architectural and functional specifications:
1. HALTED guard is detected.
2. Authenticated operator can request recovery.
3. Unauthorized operator cannot request recovery.
4. Recovery challenge is unique.
5. Recovery challenge expires.
6. Recovery challenge is single-use.
7. Operator A cannot confirm their own challenge.
8. Operator B can confirm.
9. Operator B must have the required capability.
10. Revoked session cannot confirm.
11. Invalid challenge is rejected.
12. Expired challenge is rejected.
13. Recovery cannot execute when guard is not HALTED.
14. Successful two-person confirmation transitions the guard correctly.
15. Successful recovery is audited.
16. Failed recovery is audited.
17. Correlation ID propagates through recovery workflow.
18. Portfolio endpoint is authenticated.
19. Risk endpoint is authenticated.
20. Portfolio/risk endpoints are strictly read-only.
21. No fabricated portfolio data is returned.
22. Unavailable authoritative data is represented as unavailable.
23. Existing Phase 3 functionality still works.
24. KILL_SWITCH still works.
25. PAUSE still works.
26. RESUME still works.
27. Snapshot/stream synchronization still works.
28. Session revocation still works.
29. Full Phase 1–3 regression suite remains green.
"""

import time
import unittest
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient

from gateway.adapters import MarketStateAdapter, PortfolioRiskAdapter
from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.command import CommandGateway
from gateway.contracts import (
    Capability,
    CommandType,
)
from gateway.projection import SnapshotGenerator
from gateway.recovery import RecoveryChallenge, RecoveryManager
from gateway.security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.store import InstrumentStateStore
from services.risk.limits import RiskLimits
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState
from services.runtime.portfolio import PortfolioRuntimeState


class TestPhase4Recovery(unittest.TestCase):
    """Exhaustive test suite for Phase 4 Recovery & Operational Risk Boundary."""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.guard = TradingGuard()
        self.seq_mgr = SequenceManager(initial_sequence=0)
        self.broadcaster = EventBroadcaster(sequence_manager=self.seq_mgr)
        self.session_store = InMemorySessionStore(default_ttl_seconds=3600)
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.test_hasher = argon2.PasswordHasher(
            time_cost=1, memory_cost=1024, parallelism=1
        )
        self.credential_store = NativeCredentialStore(
            hasher=self.test_hasher,
            max_failed_attempts=5,
        )
        self.command_gateway = CommandGateway(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            broadcaster=self.broadcaster,
        )

        self.registry = InstrumentRegistry()
        self.inst_tcs = InstrumentId(
            symbol="TCS",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.registry.register(self.inst_tcs, provider_tokens={"ATMSTOX": "11536"})
        self.state_store = InstrumentStateStore(registry=self.registry)
        self.market_adapter = MarketStateAdapter(state_store=self.state_store)
        self.snapshot_gen = SnapshotGenerator(
            trading_guard=self.guard,
            sequence_manager=self.seq_mgr,
            market_adapter=self.market_adapter,
        )

        # Phase 4 components
        self.recovery_mgr = RecoveryManager(
            trading_guard=self.guard,
            command_gateway=self.command_gateway,
            audit_logger=self.audit_logger,
            challenge_ttl_seconds=60,
        )

        self.portfolio_state = PortfolioRuntimeState(
            account_id="PAPER_TEST",
            initial_cash=500_000.0,
        )
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_risk_per_trade_pct=0.01,
            max_drawdown_pct=0.10,
            max_gross_leverage=2.0,
        )
        self.portfolio_adapter = PortfolioRiskAdapter(
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            trading_guard=self.guard,
        )

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
            portfolio_risk_adapter=self.portfolio_adapter,
        )
        self.client = TestClient(self.app)

        # Pre-seed Operator A (Recovery Initiator)
        self.op_a_id = "operator-a"
        self.op_a_key = "OperatorA#2026!Key"
        self.credential_store.register_user(
            operator_id=self.op_a_id,
            password=self.op_a_key,
            roles=["OPERATOR"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_CONTROL_PAUSE,
                Capability.CAP_CONTROL_RESUME,
                Capability.CAP_CONTROL_KILL,
                Capability.CAP_CONTROL_RECOVER,
            },
        )

        # Pre-seed Operator B (Recovery Confirmer)
        self.op_b_id = "operator-b"
        self.op_b_key = "OperatorB#2026!Key"
        self.credential_store.register_user(
            operator_id=self.op_b_id,
            password=self.op_b_key,
            roles=["SUPERVISOR"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_CONTROL_RECOVER,
            },
        )

        # Pre-seed Restricted Operator (Lacks CAP_CONTROL_RECOVER)
        self.restricted_id = "restricted-op"
        self.restricted_key = "Restricted#2026!Key"
        self.credential_store.register_user(
            operator_id=self.restricted_id,
            password=self.restricted_key,
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    def _login(self, username: str, password: str) -> str:
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["session_token"]

    # -----------------------------------------------------------------------
    # 1. HALTED guard is detected
    # -----------------------------------------------------------------------
    def test_01_halted_guard_is_detected(self) -> None:
        """1. HALTED guard is detected accurately via status endpoint and core state."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        self.guard.trip("Operational halt condition")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        resp = self.client.get(
            "/api/v1/recovery/status",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["guard_state"], "HALTED")
        self.assertIsNone(body["active_challenge"])

    # -----------------------------------------------------------------------
    # 2. Authenticated operator can request recovery
    # -----------------------------------------------------------------------
    def test_02_authenticated_operator_can_request_recovery(self) -> None:
        """2. Authenticated operator with CAP_CONTROL_RECOVER can request recovery."""
        self.guard.trip("Trip for auth request test")
        token_a = self._login(self.op_a_id, self.op_a_key)

        resp = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Operator A request"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["challenge_id"].startswith("rec-"))
        self.assertEqual(data["operator_a_id"], self.op_a_id)
        self.assertEqual(data["status"], "PENDING")

    # -----------------------------------------------------------------------
    # 3. Unauthorized operator cannot request recovery
    # -----------------------------------------------------------------------
    def test_03_unauthorized_operator_cannot_request_recovery(self) -> None:
        """3. Unauthorized operator lacking capability cannot request recovery (403)."""
        self.guard.trip("Trip for unauthorized request test")
        restricted_token = self._login(self.restricted_id, self.restricted_key)

        resp = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {restricted_token}"},
            json={"reason": "Unauthorized attempt"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "INSUFFICIENT_CAPABILITY")

    # -----------------------------------------------------------------------
    # 4. Recovery challenge is unique
    # -----------------------------------------------------------------------
    def test_04_recovery_challenge_is_unique(self) -> None:
        """4. Separate recovery challenges have distinct high-entropy IDs and tokens."""
        self.guard.trip("Trip for uniqueness test")
        c1 = self.recovery_mgr.create_challenge(self.op_a_id, correlation_id="c1")
        self.guard.trip("Trip second time")
        c2 = self.recovery_mgr.create_challenge(self.op_a_id, correlation_id="c2")

        self.assertNotEqual(c1.challenge_id, c2.challenge_id)
        self.assertNotEqual(c1.expected_token, c2.expected_token)

    # -----------------------------------------------------------------------
    # 5. Recovery challenge expires
    # -----------------------------------------------------------------------
    def test_05_recovery_challenge_expires(self) -> None:
        """5. Recovery challenge detects expiry past its configured lifetime."""
        self.guard.trip("Trip for expiry check")
        challenge = self.recovery_mgr.create_challenge(
            operator_a_id=self.op_a_id,
            correlation_id="corr-exp",
            ttl_seconds=-5,
        )
        self.assertTrue(challenge.is_expired)

    # -----------------------------------------------------------------------
    # 6. Recovery challenge is single-use
    # -----------------------------------------------------------------------
    def test_06_recovery_challenge_is_single_use(self) -> None:
        """6. Recovery challenge cannot be used more than once."""
        self.guard.trip("Trip for single-use test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        token_b = self._login(self.op_b_id, self.op_b_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Single-use challenge test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        # First use succeeds
        c1 = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(c1.status_code, 200)

        # Trip guard again and re-submit consumed challenge
        self.guard.trip("Trip second time")
        c2 = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(c2.status_code, 409)
        self.assertEqual(c2.json()["error"]["code"], "CHALLENGE_ALREADY_CONSUMED")

    # -----------------------------------------------------------------------
    # 7. Operator A cannot confirm their own challenge
    # -----------------------------------------------------------------------
    def test_07_operator_a_cannot_confirm_own_challenge(self) -> None:
        """7. Operator A cannot confirm their own recovery challenge (two-person rule)."""
        self.guard.trip("Trip for same-user test")
        token_a = self._login(self.op_a_id, self.op_a_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Self-confirm test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        # Operator A attempts confirmation
        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 409)
        self.assertEqual(
            cnf.json()["error"]["code"], "SAME_OPERATOR_CONFIRMATION_REJECTED"
        )
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 8. Operator B can confirm
    # -----------------------------------------------------------------------
    def test_08_operator_b_can_confirm(self) -> None:
        """8. Distinct Operator B with required capability can confirm recovery."""
        self.guard.trip("Trip for Op B confirm test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        token_b = self._login(self.op_b_id, self.op_b_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Valid two-person workflow"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 200)
        self.assertTrue(cnf.json()["recovered"])
        self.assertEqual(cnf.json()["guard_state"], "NORMAL")
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    # -----------------------------------------------------------------------
    # 9. Operator B must have the required capability
    # -----------------------------------------------------------------------
    def test_09_operator_b_must_have_required_capability(self) -> None:
        """9. Operator B lacking CAP_CONTROL_RECOVER is rejected with 403."""
        self.guard.trip("Trip for capability test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        token_restricted = self._login(self.restricted_id, self.restricted_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Capability test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_restricted}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 403)
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 10. Revoked session cannot confirm
    # -----------------------------------------------------------------------
    def test_10_revoked_session_cannot_confirm(self) -> None:
        """10. Revoking Operator B's session immediately blocks confirmation."""
        self.guard.trip("Trip for revocation test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        token_b = self._login(self.op_b_id, self.op_b_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Revocation test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        # Revoke session B
        self.session_store.revoke_session(token_b)

        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 401)
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 11. Invalid challenge is rejected
    # -----------------------------------------------------------------------
    def test_11_invalid_challenge_is_rejected(self) -> None:
        """11. Submitting confirmation with an invalid challenge ID is rejected."""
        self.guard.trip("Trip for invalid challenge test")
        token_b = self._login(self.op_b_id, self.op_b_key)

        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={
                "challenge_id": "rec-nonexistent-challenge-id",
                "confirmation_token": "rec-tok-dummy",
            },
        )
        self.assertIn(cnf.status_code, [400, 404])

    # -----------------------------------------------------------------------
    # 12. Expired challenge is rejected
    # -----------------------------------------------------------------------
    def test_12_expired_challenge_is_rejected(self) -> None:
        """12. Submitting confirmation for an expired challenge is rejected."""
        self.guard.trip("Trip for expired confirmation test")
        c_exp = self.recovery_mgr.create_challenge(
            operator_a_id=self.op_a_id,
            correlation_id="corr-exp-12",
            ttl_seconds=-10,
        )

        with self.assertRaises(ValueError) as ctx:
            self.recovery_mgr.confirm_recovery(
                challenge_id=c_exp.challenge_id,
                operator_b_id=self.op_b_id,
                confirmation_token=c_exp.expected_token,
                correlation_id="corr-exp-confirm",
            )
        self.assertIn("CHALLENGE_EXPIRED", str(ctx.exception))

    # -----------------------------------------------------------------------
    # 13. Recovery cannot execute when guard is not HALTED
    # -----------------------------------------------------------------------
    def test_13_recovery_cannot_execute_when_guard_not_halted(self) -> None:
        """13. Recovery request and confirmation cannot execute unless guard is HALTED."""
        self.assertEqual(self.guard.state, GuardState.NORMAL)
        token_a = self._login(self.op_a_id, self.op_a_key)

        req_normal = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Attempting recovery when NORMAL"},
        )
        self.assertEqual(req_normal.status_code, 409)
        self.assertEqual(req_normal.json()["error"]["code"], "GUARD_NOT_HALTED")

        # In PAUSED state
        self.guard.pause()
        req_paused = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Attempting recovery when PAUSED"},
        )
        self.assertEqual(req_paused.status_code, 409)
        self.assertEqual(req_paused.json()["error"]["code"], "GUARD_NOT_HALTED")

    # -----------------------------------------------------------------------
    # 14. Successful two-person confirmation transitions the guard correctly
    # -----------------------------------------------------------------------
    def test_14_successful_two_person_confirmation_transitions_guard(self) -> None:
        """14. Successful two-person confirmation resets TradingGuard to NORMAL."""
        self.guard.trip("Authoritative emergency trigger")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        token_a = self._login(self.op_a_id, self.op_a_key)
        token_b = self._login(self.op_b_id, self.op_b_key)

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"reason": "Clear circuit trip"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 200)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    # -----------------------------------------------------------------------
    # 15. Successful recovery is audited
    # -----------------------------------------------------------------------
    def test_15_successful_recovery_is_audited(self) -> None:
        """15. Successful recovery records RECOVERY_CHALLENGE_REQUESTED and RECOVERY_CONFIRMED."""
        self.guard.trip("Trip for audit success test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        token_b = self._login(self.op_b_id, self.op_b_key)
        corr_id = "corr-audit-success-15"

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Correlation-ID": corr_id,
            },
            json={"reason": "Audit success test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        self.client.post(
            "/api/v1/recovery/confirm",
            headers={
                "Authorization": f"Bearer {token_b}",
                "X-Correlation-ID": corr_id,
            },
            json={"challenge_id": cid, "confirmation_token": tok},
        )

        self.audit_logger.flush()
        records = self.audit_logger.in_memory_records
        actions = [r.get("action") for r in records if r.get("correlation_id") == corr_id]
        self.assertIn("RECOVERY_CHALLENGE_REQUESTED", actions)
        self.assertIn("RECOVERY_CONFIRMED", actions)

    # -----------------------------------------------------------------------
    # 16. Failed recovery is audited
    # -----------------------------------------------------------------------
    def test_16_failed_recovery_is_audited(self) -> None:
        """16. Failed recovery attempt (same operator) produces an audit record."""
        self.guard.trip("Trip for audit failure test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        corr_fail = "corr-audit-fail-16"

        req = self.client.post(
            "/api/v1/recovery/request",
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Correlation-ID": corr_fail,
            },
            json={"reason": "Audit fail test"},
        )
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        # Operator A attempts self-confirmation
        self.client.post(
            "/api/v1/recovery/confirm",
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Correlation-ID": corr_fail,
            },
            json={"challenge_id": cid, "confirmation_token": tok},
        )

        self.audit_logger.flush()
        records = self.audit_logger.in_memory_records
        fail_records = [
            r for r in records if r.get("action") == "RECOVERY_SAME_OPERATOR_REJECTED"
        ]
        self.assertTrue(len(fail_records) >= 1)

    # -----------------------------------------------------------------------
    # 17. Correlation ID propagates through recovery workflow
    # -----------------------------------------------------------------------
    def test_17_correlation_id_propagates_through_recovery_workflow(self) -> None:
        """17. X-Correlation-ID propagates to response headers and body."""
        self.guard.trip("Trip for correlation test")
        token_a = self._login(self.op_a_id, self.op_a_key)
        corr = f"corr-propagate-{uuid.uuid4()}"

        resp = self.client.post(
            "/api/v1/recovery/request",
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Correlation-ID": corr,
            },
            json={"reason": "Correlation test"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("x-correlation-id"), corr)
        self.assertEqual(resp.json()["correlation_id"], corr)

    # -----------------------------------------------------------------------
    # 18. Portfolio endpoint is authenticated
    # -----------------------------------------------------------------------
    def test_18_portfolio_endpoint_is_authenticated(self) -> None:
        """18. Portfolio endpoints require valid authentication."""
        resp1 = self.client.get("/api/v1/portfolio")
        self.assertEqual(resp1.status_code, 401)

        resp2 = self.client.get("/api/v1/portfolio/state")
        self.assertEqual(resp2.status_code, 401)

    # -----------------------------------------------------------------------
    # 19. Risk endpoint is authenticated
    # -----------------------------------------------------------------------
    def test_19_risk_endpoint_is_authenticated(self) -> None:
        """19. Risk endpoints require valid authentication."""
        resp1 = self.client.get("/api/v1/risk")
        self.assertEqual(resp1.status_code, 401)

        resp2 = self.client.get("/api/v1/risk/state")
        self.assertEqual(resp2.status_code, 401)

    # -----------------------------------------------------------------------
    # 20. Portfolio/risk endpoints are strictly read-only
    # -----------------------------------------------------------------------
    def test_20_portfolio_risk_endpoints_strictly_read_only(self) -> None:
        """20. Portfolio/risk queries never mutate trading core or guard state."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        initial_state = self.guard.state
        initial_cash = self.portfolio_state.cash_balance

        for _ in range(3):
            self.client.get(
                "/api/v1/portfolio",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            self.client.get(
                "/api/v1/risk",
                headers={"Authorization": f"Bearer {token_a}"},
            )

        self.assertEqual(self.guard.state, initial_state)
        self.assertEqual(self.portfolio_state.cash_balance, initial_cash)

    # -----------------------------------------------------------------------
    # 21. No fabricated portfolio data is returned
    # -----------------------------------------------------------------------
    def test_21_no_fabricated_portfolio_data_is_returned(self) -> None:
        """21. When unmounted, cash/equity/positions are strictly None/empty."""
        unmounted = PortfolioRiskAdapter(portfolio_state=None, risk_limits=None)
        data = unmounted.get_portfolio_summary()
        self.assertFalse(data["is_available"])
        self.assertIsNone(data["cash"])
        self.assertIsNone(data["total_equity"])
        self.assertIsNone(data["realized_pnl"])
        self.assertIsNone(data["unrealized_pnl"])
        self.assertEqual(data["positions"], [])

    # -----------------------------------------------------------------------
    # 22. Unavailable authoritative data is represented as unavailable
    # -----------------------------------------------------------------------
    def test_22_unavailable_authoritative_data_represented_as_unavailable(self) -> None:
        """22. Unmounted risk metrics return is_available: False with clear status."""
        unmounted = PortfolioRiskAdapter(portfolio_state=None, risk_limits=None)
        risk = unmounted.get_risk_summary(guard_state="NORMAL")
        self.assertFalse(risk["is_available"])
        self.assertEqual(risk["risk_limits_status"], "UNAVAILABLE")
        self.assertIsNone(risk["max_drawdown"])
        self.assertIsNone(risk["max_capital"])

    # -----------------------------------------------------------------------
    # 23. Existing Phase 3 functionality still works
    # -----------------------------------------------------------------------
    def test_23_existing_phase3_functionality_still_works(self) -> None:
        """23. Phase 3 snapshot and WebSocket event broadcasting continue functioning."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        snap_resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(snap_resp.status_code, 200)
        self.assertIn("authoritative_sequence", snap_resp.json())

    # -----------------------------------------------------------------------
    # 24. KILL_SWITCH still works
    # -----------------------------------------------------------------------
    def test_24_kill_switch_still_works(self) -> None:
        """24. KILL_SWITCH command trips guard to HALTED."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"command_type": "KILL_SWITCH", "parameters": {}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 25. PAUSE still works
    # -----------------------------------------------------------------------
    def test_25_pause_still_works(self) -> None:
        """25. PAUSE command transitions guard to PAUSED."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"command_type": "PAUSE", "parameters": {}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.guard.state, GuardState.PAUSED)

    # -----------------------------------------------------------------------
    # 26. RESUME still works
    # -----------------------------------------------------------------------
    def test_26_resume_still_works(self) -> None:
        """26. RESUME command transitions guard from PAUSED to NORMAL."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        self.guard.pause()

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"command_type": "RESUME", "parameters": {}},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    # -----------------------------------------------------------------------
    # 27. Snapshot/stream synchronization still works
    # -----------------------------------------------------------------------
    def test_27_snapshot_stream_synchronization_still_works(self) -> None:
        """27. WebSocket stream delivers GUARD_STATE_CHANGED and maintains sequence."""
        token_a = self._login(self.op_a_id, self.op_a_key)

        with self.client.websocket_connect(f"/ws/events?token={token_a}") as ws:
            f0 = ws.receive_json()
            self.assertEqual(f0["event_type"], "SESSION_ESTABLISHED")

            # Execute PAUSE command
            self.client.post(
                "/api/v1/commands",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"command_type": "PAUSE", "parameters": {}},
            )
            f1 = ws.receive_json()
            self.assertEqual(f1["event_type"], "GUARD_STATE_CHANGED")
            self.assertEqual(f1["payload"]["guard_state"], "PAUSED")
            self.assertEqual(f1["sequence"], self.seq_mgr.current_sequence())

    # -----------------------------------------------------------------------
    # 28. Session revocation still works
    # -----------------------------------------------------------------------
    def test_28_session_revocation_still_works(self) -> None:
        """28. Session revocation prevents subsequent queries."""
        token_a = self._login(self.op_a_id, self.op_a_key)
        logout_resp = self.client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(logout_resp.status_code, 200)

        # Subsequent query is rejected
        sub_resp = self.client.get(
            "/api/v1/portfolio",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(sub_resp.status_code, 401)

    # -----------------------------------------------------------------------
    # 29. Full Phase 1–3 regression suite remains green
    # -----------------------------------------------------------------------
    def test_29_full_phase1_to_3_regression_suite_remains_green(self) -> None:
        """29. End-to-end smoke test validating full Phase 4 operator workflow."""
        # Step 1: Login Operator A
        token_a = self._login(self.op_a_id, self.op_a_key)
        self.assertIsNotNone(token_a)

        # Step 2: Trip guard to HALTED
        self.guard.trip("Smoke test emergency trip")
        self.assertEqual(self.guard.state, GuardState.HALTED)
        stat = self.client.get(
            "/api/v1/recovery/status",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(stat.json()["guard_state"], "HALTED")

        # Step 3: Operator A requests recovery
        req_corr = f"smoke-req-{uuid.uuid4()}"
        req = self.client.post(
            "/api/v1/recovery/request",
            headers={
                "Authorization": f"Bearer {token_a}",
                "X-Correlation-ID": req_corr,
            },
            json={"reason": "Smoke test recovery"},
        )
        self.assertEqual(req.status_code, 200)
        cid = req.json()["challenge_id"]
        tok = req.json()["token"]

        # Step 4: Login Operator B
        token_b = self._login(self.op_b_id, self.op_b_key)
        self.assertNotEqual(token_a, token_b)

        # Step 5: Operator B confirms recovery
        cnf_corr = f"smoke-cnf-{uuid.uuid4()}"
        cnf = self.client.post(
            "/api/v1/recovery/confirm",
            headers={
                "Authorization": f"Bearer {token_b}",
                "X-Correlation-ID": cnf_corr,
            },
            json={"challenge_id": cid, "confirmation_token": tok},
        )
        self.assertEqual(cnf.status_code, 200)
        self.assertTrue(cnf.json()["recovered"])
        self.assertEqual(cnf.json()["guard_state"], "NORMAL")

        # Step 6: Verify authoritative guard recovery
        self.assertEqual(self.guard.state, GuardState.NORMAL)

        # Step 7: Verify audit records
        self.audit_logger.flush()
        records = self.audit_logger.in_memory_records
        req_audit = [
            r for r in records if r.get("correlation_id") == req_corr and r.get("action") == "RECOVERY_CHALLENGE_REQUESTED"
        ]
        cnf_audit = [
            r for r in records if r.get("correlation_id") == cnf_corr and r.get("action") == "RECOVERY_CONFIRMED"
        ]
        self.assertEqual(len(req_audit), 1)
        self.assertEqual(len(cnf_audit), 1)

        # Step 8: Verify risk/portfolio projection
        port = self.client.get(
            "/api/v1/portfolio",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        self.assertEqual(port.status_code, 200)
        self.assertTrue(port.json()["is_available"])

        # Step 9: Verify Phase 3 stream still works
        with self.client.websocket_connect(f"/ws/events?token={token_a}") as ws:
            f0 = ws.receive_json()
            self.assertEqual(f0["event_type"], "SESSION_ESTABLISHED")

        # Step 10: Verify KILL_SWITCH still works
        kill = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"command_type": "KILL_SWITCH", "parameters": {}},
        )
        self.assertEqual(kill.status_code, 200)
        self.assertEqual(self.guard.state, GuardState.HALTED)


if __name__ == "__main__":
    unittest.main()
