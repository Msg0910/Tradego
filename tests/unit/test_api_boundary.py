"""
Unit tests for Tradego Phase 1 API Application & Security Boundary Foundation.
Validates:
- Health & readiness probes (Zone 2)
- Native Argon2id authentication and worker threadpool isolation (SEC-TECH-ARGON2-01)
- Brute-force lockout protection (5-attempt threshold)
- High-entropy opaque bearer sessions and instant revocation (SEC-14)
- Correlation ID propagation across all inbound/outbound responses
- Structured error response contracts (400, 401, 403, 422, 500)
- Safe read-only authoritative state snapshot query (S_snap sequence frontier)
- Authenticated command dispatch boundary (PAUSE, RESUME) through TradingGuard
- Tier 1 serialized audit logging with SHA-256 hash chaining (SEC-09)
"""

import unittest
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient

from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.command import CommandGateway
from gateway.contracts import Capability, CommandStatus, CommandType
from gateway.projection import ClientProjection, ProjectionState, SnapshotGenerator
from gateway.security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState


class TestApiBoundary(unittest.TestCase):
    """Test suite for the API application and security boundary."""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.guard = TradingGuard()
        self.seq_mgr = SequenceManager(initial_sequence=0)
        self.broadcaster = EventBroadcaster(sequence_manager=self.seq_mgr)
        self.session_store = InMemorySessionStore(default_ttl_seconds=3600)
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        # Fast test hasher to avoid saturating host CPU during concurrent test runs
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
        self.snapshot_gen = SnapshotGenerator(
            trading_guard=self.guard,
            sequence_manager=self.seq_mgr,
        )

        # Register standard test operators
        self.credential_store.register_user(
            operator_id="operator1",
            password="SecurePassword123!",
            roles=["OPERATOR"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_CONTROL_PAUSE,
                Capability.CAP_CONTROL_RESUME,
            },
        )
        self.credential_store.register_user(
            operator_id="observer1",
            password="ObserverPass123!",
            roles=["OBSERVER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.credential_store.register_user(
            operator_id="admin1",
            password="AdminMasterPass123!",
            roles=["ADMIN"],
            capabilities={Capability.CAP_ADMIN},
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
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.credential_store.close()
        self.audit_logger.close()

    def test_health_and_readiness_probes(self) -> None:
        """Verifies liveness and readiness endpoints."""
        # Liveness probe
        r_health = self.client.get("/health")
        self.assertEqual(r_health.status_code, 200)
        data_h = r_health.json()
        self.assertEqual(data_h["status"], "UP")
        self.assertEqual(data_h["version"], "1.0.0")
        self.assertTrue("timestamp" in data_h)

        # Readiness probe
        r_ready = self.client.get("/ready")
        self.assertEqual(r_ready.status_code, 200)
        data_r = r_ready.json()
        self.assertEqual(data_r["status"], "READY")
        self.assertEqual(data_r["guard_state"], "NORMAL")
        self.assertEqual(data_r["audit_logger"], "HEALTHY")

    def test_correlation_id_propagation_and_generation(self) -> None:
        """Verifies correlation ID is preserved when supplied and generated when absent."""
        # 1. Custom correlation ID passed via header
        custom_id = f"corr-cust-{uuid.uuid4()}"
        r1 = self.client.get("/health", headers={"X-Correlation-ID": custom_id})
        self.assertEqual(r1.headers.get("X-Correlation-ID"), custom_id)

        # 2. Omitted correlation ID automatically generated
        r2 = self.client.get("/health")
        gen_id = r2.headers.get("X-Correlation-ID")
        self.assertIsNotNone(gen_id)
        self.assertTrue(len(gen_id) > 10)

        # 3. Propagated into error envelopes
        r3 = self.client.get("/api/v1/snapshot", headers={"X-Correlation-ID": custom_id})
        self.assertEqual(r3.status_code, 401)
        self.assertEqual(r3.headers.get("X-Correlation-ID"), custom_id)
        err_body = r3.json()
        self.assertEqual(err_body["error"]["correlation_id"], custom_id)

    def test_structured_error_contracts(self) -> None:
        """Verifies platform structured error format across 401, 403, 422."""
        # 401 Unauthenticated
        r_401 = self.client.get("/api/v1/snapshot")
        self.assertEqual(r_401.status_code, 401)
        err = r_401.json()["error"]
        self.assertEqual(err["code"], "UNAUTHENTICATED")
        self.assertTrue("message" in err)
        self.assertTrue("correlation_id" in err)

        # 422 Request Validation Error (missing fields)
        r_422 = self.client.post("/api/v1/auth/login", json={"username": ""})
        self.assertEqual(r_422.status_code, 422)
        err_val = r_422.json()["error"]
        self.assertEqual(err_val["code"], "VALIDATION_ERROR")
        self.assertTrue("validation_errors" in err_val["details"])

    def test_native_argon2id_authentication_and_thread_isolation(self) -> None:
        """
        SEC-TECH-ARGON2-01: Argon2id verification executes strictly outside async event loop
        in a dedicated worker threadpool.
        """
        # Valid login
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "SecurePassword123!"},
        )
        self.assertEqual(r_login.status_code, 200)
        data = r_login.json()
        self.assertTrue(data["session_token"].startswith("tg_sess_"))
        self.assertEqual(data["operator_id"], "operator1")
        self.assertIn("CAP_CONTROL_PAUSE", data["capabilities"])

        # INVARIANT-SEC-TECH-ARGON2-01: Verify thread name was a worker thread
        self.assertIsNotNone(self.credential_store.last_verification_thread)
        self.assertIn("Argon2Worker", self.credential_store.last_verification_thread)

        # Invalid password
        r_bad = self.client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "WrongPassword!"},
        )
        self.assertEqual(r_bad.status_code, 401)
        self.assertEqual(r_bad.json()["error"]["code"], "INVALID_CREDENTIALS")

        # Unknown operator
        r_unknown = self.client.post(
            "/api/v1/auth/login",
            json={"username": "nonexistent", "password": "AnyPassword!"},
        )
        self.assertEqual(r_unknown.status_code, 401)
        self.assertEqual(r_unknown.json()["error"]["code"], "INVALID_CREDENTIALS")

    def test_brute_force_lockout_defense(self) -> None:
        """Verifies account is locked out after 5 consecutive failed attempts."""
        # 5 consecutive failures
        for _ in range(5):
            r = self.client.post(
                "/api/v1/auth/login",
                json={"username": "observer1", "password": "BadPassword"},
            )
            self.assertEqual(r.status_code, 401)

        self.assertTrue(self.credential_store.is_locked("observer1"))

        # 6th attempt is blocked with ACCOUNT_LOCKED even with correct password
        r_lock = self.client.post(
            "/api/v1/auth/login",
            json={"username": "observer1", "password": "ObserverPass123!"},
        )
        self.assertEqual(r_lock.status_code, 401)
        self.assertEqual(r_lock.json()["error"]["code"], "ACCOUNT_LOCKED")

        # Unlock user and verify success
        self.credential_store.unlock_user("observer1")
        r_unlocked = self.client.post(
            "/api/v1/auth/login",
            json={"username": "observer1", "password": "ObserverPass123!"},
        )
        self.assertEqual(r_unlocked.status_code, 200)

    def test_session_issuance_and_instant_revocation(self) -> None:
        """SEC-14: Session revocation via logout invalidates token immediately."""
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "SecurePassword123!"},
        )
        token = r_login.json()["session_token"]

        # Token allows snapshot query
        r_snap = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r_snap.status_code, 200)

        # Logout immediately invalidates token
        r_logout = self.client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r_logout.status_code, 200)
        self.assertEqual(r_logout.json()["status"], "LOGGED_OUT")

        # Subsequent query with revoked token returns 401 INVALID_TOKEN
        r_post_logout = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r_post_logout.status_code, 401)
        self.assertEqual(r_post_logout.json()["error"]["code"], "INVALID_TOKEN")

    def test_safe_readonly_authoritative_engine_query(self) -> None:
        """
        UI-02, UI-08: /api/v1/snapshot returns exact sequence frontier S_snap
        anchoring client projection without mutating engine state.
        """
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "observer1", "password": "ObserverPass123!"},
        )
        token = r_login.json()["session_token"]

        # Advance sequence by broadcasting a couple of events
        self.broadcaster.publish("PING", {"seq": 1})
        self.broadcaster.publish("PING", {"seq": 2})

        r_snap = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r_snap.status_code, 200)
        snap = r_snap.json()
        self.assertEqual(snap["authoritative_sequence"], 2)  # S_snap == 2
        self.assertEqual(snap["guard_state"], "NORMAL")
        self.assertEqual(snap["runtime_mode"], "PAPER")

        # Bootstrap client projection from snapshot payload
        from gateway.contracts import SnapshotPayload
        client_proj = ClientProjection()
        client_proj.bootstrap(
            SnapshotPayload(
                snapshot_id=snap["snapshot_id"],
                snapshot_timestamp=snap["snapshot_timestamp"],
                authoritative_sequence=snap["authoritative_sequence"],
                runtime_mode=snap["runtime_mode"],
                guard_state=snap["guard_state"],
            )
        )
        self.assertEqual(client_proj.state, ProjectionState.STREAMING)
        self.assertEqual(client_proj.last_processed_sequence, 2)

    def test_authenticated_command_dispatch_and_engine_mutation(self) -> None:
        """
        UI-14, SEC-01, SEC-02: Authenticated operator dispatches PAUSE and RESUME;
        TradingGuard transitions deterministically and sequence increments.
        """
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "SecurePassword123!"},
        )
        token = r_login.json()["session_token"]

        # 1. Dispatch PAUSE command
        r_pause = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(r_pause.status_code, 200)
        res_p = r_pause.json()
        self.assertEqual(res_p["status"], "ACCEPTED")
        self.assertEqual(res_p["guard_state"], "PAUSED")
        self.assertEqual(res_p["sequence"], 1)
        self.assertEqual(self.guard.state, GuardState.PAUSED)

        # 2. Dispatch RESUME command
        r_resume = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "RESUME"},
        )
        self.assertEqual(r_resume.status_code, 200)
        res_r = r_resume.json()
        self.assertEqual(res_r["status"], "ACCEPTED")
        self.assertEqual(res_r["guard_state"], "NORMAL")
        self.assertEqual(res_r["sequence"], 2)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    def test_command_authorization_capability_enforcement(self) -> None:
        """
        SEC-02: Observer lacking CAP_CONTROL_PAUSE is denied command execution;
        TradingGuard remains unmodified in NORMAL state.
        """
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "observer1", "password": "ObserverPass123!"},
        )
        token = r_login.json()["session_token"]

        r_cmd = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(r_cmd.status_code, 403)
        self.assertEqual(r_cmd.json()["error"]["code"], "COMMAND_REJECTED")
        self.assertIn("UNAUTHORIZED", r_cmd.json()["error"]["message"])
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    def test_unexposed_trading_commands_rejected_in_phase_1(self) -> None:
        """
        Phase 1 vertical slice strictly restricts commands to administrative controls.
        Emergency Flatten and mutating order commands are rejected.
        """
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "operator1", "password": "SecurePassword123!"},
        )
        token = r_login.json()["session_token"]

        r_fl = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "EMERGENCY_FLATTEN"},
        )
        self.assertEqual(r_fl.status_code, 400)
        self.assertEqual(
            r_fl.json()["error"]["code"], "COMMAND_NOT_SUPPORTED_IN_PHASE_1"
        )
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    def test_tier1_audit_logging_and_hash_chain(self) -> None:
        """
        SEC-09: Tier 1 audit logger writes sequential events with SHA-256 hash chaining.
        """
        # Execute login, command, and snapshot
        r_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin1", "password": "AdminMasterPass123!"},
        )
        token = r_login.json()["session_token"]

        self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "PAUSE"},
        )
        self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.audit_logger.flush()
        records = self.audit_logger.in_memory_records
        self.assertGreaterEqual(len(records), 3)

        # Validate hash chain integrity
        prev_hash = Tier1AuditLogger.GENESIS_HASH
        for rec in records:
            self.assertEqual(rec["prev_hash"], prev_hash)
            self.assertTrue("hash" in rec)
            prev_hash = rec["hash"]


if __name__ == "__main__":
    unittest.main()
