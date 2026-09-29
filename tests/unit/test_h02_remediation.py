"""
Regression tests for H-02 forensic finding remediation.
Guarantees that /api/v1/health/readyz correctly unpacks (bool, dict) from health_mon.check_readiness().
"""

import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from gateway.api.app import create_app
from gateway.contracts import Capability
from gateway.disaster_recovery import DisasterRecoveryManager, RecoveryState
from gateway.observability import SystemHealthMonitor
from gateway.recovery import GuardState, TradingGuard
from gateway.security import InMemorySessionStore, Tier1AuditLogger
from gateway.persistence import PersistenceManager


class TestH02Remediation(unittest.TestCase):
    """
    Authoritative regression tests for H-02 remediation.
    Validates that:
    1. test_readyz_returns_200_when_healthy
    2. test_readyz_returns_503_when_guard_halted
    3. test_readyz_returns_503_when_quarantine_active
    4. test_livez_always_200
    5. test_detailed_health_requires_cap_observe
    """

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="tradego_test_h02_")
        self.wal_path = os.path.join(self.tmp_dir, "wal.jsonl")

        self.guard = TradingGuard()
        self.guard._state = GuardState.NORMAL
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.pm = PersistenceManager(wal_path=self.wal_path)
        self.dr_mgr = DisasterRecoveryManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            persistence_manager=self.pm,
        )
        self.health_mon = SystemHealthMonitor(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            persistence_manager=self.pm,
            recovery_manager=self.dr_mgr,
        )
        self.session_store = InMemorySessionStore()

        self.token_observer = self.session_store.create_session(
            operator_id="op_obs",
            roles=["VIEWER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.token_no_obs = self.session_store.create_session(
            operator_id="op_no_obs",
            roles=["NO_PERMS"],
            capabilities=set(),
        )

        self.app = create_app(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
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

    def test_readyz_returns_200_when_healthy(self) -> None:
        """1. When system is healthy and unquarantined, readyz returns HTTP 200 READY."""
        resp = self.client.get("/api/v1/health/readyz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "READY")
        self.assertIsNone(data["reason"])
        self.assertEqual(data["guard_state"], "NORMAL")
        self.assertEqual(data["recovery_state"], "CLEAN")
        self.assertIn("timestamp", data)

    def test_readyz_returns_503_when_guard_halted(self) -> None:
        """2. When TradingGuard is tripped/HALTED, readyz returns HTTP 503 SERVICE UNAVAILABLE."""
        self.guard.trip("H-02 Test emergency trip")
        self.assertEqual(self.guard.state, GuardState.HALTED)

        resp = self.client.get("/api/v1/health/readyz")
        self.assertEqual(resp.status_code, 503)
        err = resp.json()["error"]
        self.assertEqual(err["code"], "NOT_READY")
        self.assertIn("GUARD_NOT_NORMAL", err["message"])
        self.assertEqual(err["details"]["guard_state"], "HALTED")

    def test_readyz_returns_503_when_quarantine_active(self) -> None:
        """3. When disaster recovery quarantine is active, readyz returns HTTP 503 SERVICE UNAVAILABLE."""
        self.dr_mgr._state = RecoveryState.QUARANTINED
        self.assertTrue(self.dr_mgr.is_quarantine_locked)

        resp = self.client.get("/api/v1/health/readyz")
        self.assertEqual(resp.status_code, 503)
        err = resp.json()["error"]
        self.assertEqual(err["code"], "NOT_READY")
        self.assertIn("DISASTER_RECOVERY_QUARANTINE_ACTIVE", err["message"])
        self.assertEqual(err["details"]["recovery_state"], "QUARANTINED")

    def test_livez_always_200(self) -> None:
        """4. Livez always returns HTTP 200 UP even if the system is halted or quarantined."""
        # Baseline
        resp = self.client.get("/api/v1/health/livez")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "UP")

        # Even if tripped
        self.guard.trip("Liveness trip")
        self.dr_mgr._state = RecoveryState.FAILED
        resp_degraded = self.client.get("/api/v1/health/livez")
        self.assertEqual(resp_degraded.status_code, 200)
        self.assertEqual(resp_degraded.json()["status"], "UP")

    def test_detailed_health_requires_cap_observe(self) -> None:
        """5. Detailed health probe strictly requires CAP_OBSERVE authorization."""
        # Unauthenticated -> 401
        r_unauth = self.client.get("/api/v1/health/detailed")
        self.assertEqual(r_unauth.status_code, 401)

        # Authenticated without CAP_OBSERVE -> 403
        r_forbidden = self.client.get(
            "/api/v1/health/detailed",
            headers={"Authorization": f"Bearer {self.token_no_obs}"},
        )
        self.assertEqual(r_forbidden.status_code, 403)
        self.assertEqual(r_forbidden.json()["error"]["code"], "FORBIDDEN")

        # Authenticated with CAP_OBSERVE -> 200
        r_ok = self.client.get(
            "/api/v1/health/detailed",
            headers={"Authorization": f"Bearer {self.token_observer}"},
        )
        self.assertEqual(r_ok.status_code, 200)
        det = r_ok.json()
        self.assertIn("status", det)
        self.assertIn("execution_mode", det)
        self.assertIn("trading_guard_state", det)
        self.assertIn("wal_health", det)
        self.assertIn("audit_chain_status", det)


if __name__ == "__main__":
    unittest.main()
