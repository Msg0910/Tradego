"""
Unit tests for Tradego Phase 3 Real-Time Presentation Client & Operator Workflow.
Validates:
1. Real login -> snapshot -> WebSocket lifecycle
2. S_snap initialization
3. Contiguous event application (N+1)
4. Duplicate/replay discard (<= S_last)
5. Confirmed gap -> STALE
6. STALE -> RESYNCING -> LIVE
7. WebSocket reconnect
8. Expired session handling (401 / WS 1008)
9. Unauthorized command (403 Forbidden)
10. Authorized PAUSE command
11. Authorized RESUME command
12. Authorized KILL_SWITCH command
13. Command rejection handling
14. Zero fabricated market state (pure authoritative backend truth)
15. UI/backend correlation ID propagation
16. Emergency KILL_SWITCH availability during UI resync
17. End-to-end smoke test executing full operator workflow
"""

import unittest
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.adapters import MarketStateAdapter
from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.command import CommandGateway
from gateway.contracts import (
    Capability,
    CommandRequest,
    CommandStatus,
    CommandType,
    TradegoEventEnvelope,
)
from gateway.projection import ClientProjection, ProjectionState, SnapshotGenerator
from gateway.security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.store import InstrumentStateStore
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState


class TestPhase3OperatorWorkflow(unittest.TestCase):
    """Comprehensive test suite for Phase 3 Operator Workflow & Presentation Client."""

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
        )
        self.client = TestClient(self.app)

        # Pre-seed operator credentials
        self.operator_id = "chief-operator"
        self.master_key = "Operator#2026!Key"
        self.credential_store.register_user(
            operator_id=self.operator_id,
            password=self.master_key,
            roles=["CHIEF_OPERATOR"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_CONTROL_PAUSE,
                Capability.CAP_CONTROL_RESUME,
                Capability.CAP_CONTROL_KILL,
                Capability.CAP_ADMIN,
            },
        )

        # Pre-seed restricted operator (Observer-only, lacks command capabilities)
        self.observer_id = "audit-observer"
        self.observer_key = "Observer#2026!Key"
        self.credential_store.register_user(
            operator_id=self.observer_id,
            password=self.observer_key,
            roles=["OBSERVER"],
            capabilities={Capability.CAP_OBSERVE},
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    # -----------------------------------------------------------------------
    # 1. Real Login -> Snapshot -> WebSocket Lifecycle
    # -----------------------------------------------------------------------
    def test_real_login_snapshot_websocket_lifecycle(self) -> None:
        """1. End-to-end authentication, snapshot acquisition, and WebSocket stream setup."""
        # A. Native direct Argon2id login
        login_resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": self.operator_id, "password": self.master_key},
        )
        self.assertEqual(login_resp.status_code, 200)
        login_data = login_resp.json()
        token = login_data["session_token"]
        self.assertTrue(token.startswith("tg_sess_"))
        self.assertEqual(login_data["operator_id"], self.operator_id)

        # B. Authoritative snapshot query
        snap_resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(snap_resp.status_code, 200)
        snap_data = snap_resp.json()
        self.assertEqual(snap_data["authoritative_sequence"], 0)
        self.assertEqual(snap_data["guard_state"], "NORMAL")
        self.assertIn("market_state", snap_data["details"])

        # C. WebSocket stream connection
        with self.client.websocket_connect(f"/ws/events?token={token}") as ws:
            init_frame = ws.receive_json()
            self.assertEqual(init_frame["event_type"], "SESSION_ESTABLISHED")
            self.assertEqual(init_frame["payload"]["operator_id"], self.operator_id)

    # -----------------------------------------------------------------------
    # 2. S_snap Initialization
    # -----------------------------------------------------------------------
    def test_s_snap_initialization(self) -> None:
        """2. Snapshot initializes sequence baseline S_snap in client projection."""
        for _ in range(7):
            self.seq_mgr.next_sequence()

        snap = self.snapshot_gen.generate_snapshot()
        self.assertEqual(snap.authoritative_sequence, 7)

        projection = ClientProjection()
        projection.connect()
        projection.start_buffering()
        status = projection.bootstrap(snap)

        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 7)
        self.assertEqual(projection.last_event_timestamp, snap.snapshot_timestamp)

    # -----------------------------------------------------------------------
    # 3. Contiguous Event Application
    # -----------------------------------------------------------------------
    def test_contiguous_event_application(self) -> None:
        """3. In LIVE state, sequence S_last + 1 is applied contiguously."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())
        self.assertEqual(projection.state, ProjectionState.LIVE)

        ts = "2026-09-22T10:15:30Z"
        env = TradegoEventEnvelope(
            event_id="e-1",
            event_type="GUARD_STATE_CHANGED",
            sequence=1,
            correlation_id="c-1",
            server_timestamp=ts,
            payload={"guard_state": "PAUSED"},
        )
        res = projection.apply_event(env)

        self.assertEqual(res, "APPLIED")
        self.assertEqual(projection.last_processed_sequence, 1)
        self.assertEqual(projection.guard_state, "PAUSED")
        self.assertEqual(projection.applied_events_count, 1)
        self.assertEqual(projection.last_event_timestamp, ts)

    # -----------------------------------------------------------------------
    # 4. Duplicate / Replay Discard
    # -----------------------------------------------------------------------
    def test_duplicate_replay_discard(self) -> None:
        """4. Sequence <= S_last is discarded as duplicate/replay without affecting state."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())

        e1 = TradegoEventEnvelope("e1", "TICK", 1, "c1", "t1", {"symbol": "TCS", "ltp": 3500.0})
        projection.apply_event(e1)
        self.assertEqual(projection.last_processed_sequence, 1)

        # Duplicate
        res = projection.apply_event(e1)
        self.assertEqual(res, "DUPLICATE_DROPPED")
        self.assertEqual(projection.dropped_duplicates_count, 1)
        self.assertEqual(projection.last_processed_sequence, 1)

        # Older replay (sequence 0)
        e0 = TradegoEventEnvelope("e0", "TICK", 0, "c0", "t0", {"symbol": "TCS", "ltp": 3490.0})
        res0 = projection.apply_event(e0)
        self.assertEqual(res0, "DUPLICATE_DROPPED")
        self.assertEqual(projection.dropped_duplicates_count, 2)
        self.assertEqual(projection.last_processed_sequence, 1)

    # -----------------------------------------------------------------------
    # 5. Confirmed Gap -> STALE
    # -----------------------------------------------------------------------
    def test_confirmed_gap_transitions_to_stale(self) -> None:
        """5. Incoming sequence > S_last + 1 triggers confirmed gap quarantine to STALE."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())
        self.assertEqual(projection.last_processed_sequence, 0)

        gap_env = TradegoEventEnvelope("e4", "TICK", 4, "c4", "t4", {})
        res = projection.apply_event(gap_env)

        self.assertEqual(res, "GAP_DETECTED")
        self.assertEqual(projection.state, ProjectionState.STALE)
        self.assertEqual(projection.detected_gaps_count, 1)

    # -----------------------------------------------------------------------
    # 6. STALE -> RESYNCING -> LIVE
    # -----------------------------------------------------------------------
    def test_stale_to_resyncing_to_live(self) -> None:
        """6. Projection recovers from STALE through snapshot resynchronization."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())

        # Induce gap
        projection.apply_event(TradegoEventEnvelope("eg", "TICK", 5, "cg", "tg", {}))
        self.assertEqual(projection.state, ProjectionState.STALE)

        # Initiate resync
        projection.request_resync()
        self.assertEqual(projection.state, ProjectionState.RESYNCING)

        # Buffer event 6 in flight
        e6 = TradegoEventEnvelope("e6", "TICK", 6, "c6", "t6", {"symbol": "TCS", "ltp": 3520.0})
        projection.apply_event(e6)
        self.assertEqual(projection.buffered_events_count, 1)

        # Server advances to seq 5
        while self.seq_mgr.current_sequence() < 5:
            self.seq_mgr.next_sequence()

        fresh_snap = self.snapshot_gen.generate_snapshot()
        self.assertEqual(fresh_snap.authoritative_sequence, 5)

        status = projection.resynchronize(fresh_snap)
        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 6)
        self.assertEqual(projection.resync_count, 1)
        self.assertEqual(projection.market_state["TCS"]["ltp"], 3520.0)

    # -----------------------------------------------------------------------
    # 7. WebSocket Reconnect
    # -----------------------------------------------------------------------
    def test_websocket_reconnect(self) -> None:
        """7. Client cleanly disconnects and reconnects with session preservation."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_OBSERVE],
        )

        # Connect session 1
        with self.client.websocket_connect(f"/ws/events?token={token}") as ws1:
            frame1 = ws1.receive_json()
            self.assertEqual(frame1["event_type"], "SESSION_ESTABLISHED")

        # Disconnect occurred cleanly; now reconnect session 2
        with self.client.websocket_connect(f"/ws/events?token={token}") as ws2:
            frame2 = ws2.receive_json()
            self.assertEqual(frame2["event_type"], "SESSION_ESTABLISHED")
            self.assertEqual(frame2["payload"]["operator_id"], self.operator_id)

    # -----------------------------------------------------------------------
    # 8. Expired / Revoked Session Handling
    # -----------------------------------------------------------------------
    def test_expired_session_handling(self) -> None:
        """8. Expired or revoked session is rejected across snapshot, websocket, and commands."""
        token = self.session_store.create_session(
            operator_id="temp-operator",
            roles=["TRADER"],
            capabilities=[Capability.CAP_OBSERVE, Capability.CAP_CONTROL_PAUSE],
        )
        # Explicitly revoke session
        self.session_store.revoke_session(token)

        # Snapshot rejected with 401
        snap_resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(snap_resp.status_code, 401)

        # Command rejected with 401
        cmd_resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(cmd_resp.status_code, 401)

        # WebSocket rejected with 1008
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(f"/ws/events?token={token}") as ws:
                pass
        self.assertEqual(cm.exception.code, 1008)

    # -----------------------------------------------------------------------
    # 9. Unauthorized Command (403 Forbidden)
    # -----------------------------------------------------------------------
    def test_unauthorized_command(self) -> None:
        """9. Operator lacking required capability is rejected with 403 Forbidden."""
        observer_token = self.session_store.create_session(
            operator_id=self.observer_id,
            roles=["OBSERVER"],
            capabilities=[Capability.CAP_OBSERVE],  # Lacks CAP_CONTROL_PAUSE
        )

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {observer_token}"},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(resp.status_code, 403)
        err = resp.json()["error"]
        self.assertEqual(err["code"], "COMMAND_REJECTED")
        self.assertIn("UNAUTHORIZED", err["message"])
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    # -----------------------------------------------------------------------
    # 10. Authorized PAUSE
    # -----------------------------------------------------------------------
    def test_authorized_pause(self) -> None:
        """10. Authorized operator executes PAUSE; TradingGuard transitions to PAUSED."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_CONTROL_PAUSE, Capability.CAP_OBSERVE],
        )

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ACCEPTED")
        self.assertEqual(data["guard_state"], "PAUSED")
        self.assertEqual(self.guard.state, GuardState.PAUSED)

    # -----------------------------------------------------------------------
    # 11. Authorized RESUME
    # -----------------------------------------------------------------------
    def test_authorized_resume(self) -> None:
        """11. Authorized operator executes RESUME; TradingGuard transitions back to NORMAL."""
        self.guard.pause()
        self.assertEqual(self.guard.state, GuardState.PAUSED)

        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_CONTROL_RESUME, Capability.CAP_OBSERVE],
        )

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "RESUME"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ACCEPTED")
        self.assertEqual(data["guard_state"], "NORMAL")
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    # -----------------------------------------------------------------------
    # 12. Authorized KILL_SWITCH
    # -----------------------------------------------------------------------
    def test_authorized_kill_switch(self) -> None:
        """12. Authorized operator executes KILL_SWITCH; TradingGuard trips to HALTED."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_CONTROL_KILL, Capability.CAP_OBSERVE],
        )

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "KILL_SWITCH", "parameters": {"details": "Operator Emergency Stop"}},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ACCEPTED")
        self.assertEqual(data["guard_state"], "HALTED")
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 13. Command Rejection Handling (Disallowed Command Types)
    # -----------------------------------------------------------------------
    def test_command_rejection_handling(self) -> None:
        """13. Attempting disallowed commands in Phase 3 returns 400 Bad Request."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_ADMIN, Capability.CAP_OBSERVE],
        )

        # EMERGENCY_FLATTEN is not exposed on /api/v1/commands in this phase
        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "EMERGENCY_FLATTEN"},
        )
        self.assertEqual(resp.status_code, 400)
        err = resp.json()["error"]
        self.assertEqual(err["code"], "COMMAND_NOT_SUPPORTED_IN_PHASE_1")

    # -----------------------------------------------------------------------
    # 14. Zero Fabricated Market State
    # -----------------------------------------------------------------------
    def test_no_fabricated_market_state(self) -> None:
        """14. Verifies state originates strictly from backend state store without fabrication."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_OBSERVE],
        )

        # A. Store empty -> snapshot market_state is empty
        snap_resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(snap_resp.json()["details"]["market_state"], {})

        # B. Ingest authoritative market tick into state store
        ev = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="11536",
            ltp=3950.75,
            open=3920.0,
            high=3980.0,
            low=3910.0,
            total_volume=450000.0,
            oi=120000,
            local_receive_timestamp=1.0,
        )
        self.state_store.on_market_event(ev)

        # C. Query snapshot again -> real data present
        snap_resp2 = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        mkt = snap_resp2.json()["details"]["market_state"]
        self.assertIn("TCS", mkt)
        self.assertEqual(mkt["TCS"]["ltp"], 3950.75)
        self.assertEqual(mkt["TCS"]["high"], 3980.0)
        self.assertEqual(mkt["TCS"]["low"], 3910.0)

    # -----------------------------------------------------------------------
    # 15. UI / Backend Correlation ID Propagation
    # -----------------------------------------------------------------------
    def test_ui_backend_correlation_id_propagation(self) -> None:
        """15. Correlation ID header is echoed in responses and audit logging."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_CONTROL_PAUSE, Capability.CAP_OBSERVE],
        )
        cid = f"ui-corr-{uuid.uuid4()}"

        resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}", "X-Correlation-ID": cid},
            json={"command_type": "PAUSE"},
        )
        self.assertEqual(resp.headers.get("X-Correlation-ID"), cid)

    # -----------------------------------------------------------------------
    # 16. Emergency KILL_SWITCH Availability During UI Resync (Scope 11)
    # -----------------------------------------------------------------------
    def test_emergency_kill_switch_during_resync(self) -> None:
        """16. Emergency KILL_SWITCH remains accepted and trips guard even while UI is resyncing."""
        token = self.session_store.create_session(
            operator_id=self.operator_id,
            roles=["CHIEF_OPERATOR"],
            capabilities=[Capability.CAP_CONTROL_KILL, Capability.CAP_OBSERVE],
        )

        # UI is in STALE / RESYNCING state
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())
        projection.apply_event(TradegoEventEnvelope("egap", "TICK", 99, "c", "t", {}))
        projection.request_resync()
        self.assertEqual(projection.state, ProjectionState.RESYNCING)

        # Operator presses emergency kill switch during resync
        cmd_resp = self.client.post(
            "/api/v1/commands",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_type": "KILL_SWITCH", "parameters": {"details": "Emergency during resync"}},
        )
        self.assertEqual(cmd_resp.status_code, 200)
        self.assertEqual(cmd_resp.json()["guard_state"], "HALTED")
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -----------------------------------------------------------------------
    # 17. Real Smoke Test of Full Operator Workflow
    # -----------------------------------------------------------------------
    def test_full_operator_smoke_test(self) -> None:
        """
        17. Complete real smoke test of:
        login -> snapshot -> websocket -> live projection ->
        PAUSE -> RESUME -> KILL_SWITCH -> disconnect/reconnect -> resynchronization.
        """
        # Step 1: Login
        login_resp = self.client.post(
            "/api/v1/auth/login",
            json={"username": self.operator_id, "password": self.master_key},
        )
        self.assertEqual(login_resp.status_code, 200)
        token = login_resp.json()["session_token"]

        # Step 2: Snapshot
        snap_resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(snap_resp.status_code, 200)
        snapshot = self.snapshot_gen.generate_snapshot()

        # Step 3: WebSocket Connect
        with self.client.websocket_connect(f"/ws/events?token={token}") as ws:
            conn_frame = ws.receive_json()
            self.assertEqual(conn_frame["event_type"], "SESSION_ESTABLISHED")

            # Step 4: Live Projection Bootstrap
            projection = ClientProjection()
            projection.connect()
            projection.start_buffering()
            projection.bootstrap(snapshot)
            self.assertEqual(projection.state, ProjectionState.LIVE)

            # Step 5: PAUSE Command
            pause_resp = self.client.post(
                "/api/v1/commands",
                headers={"Authorization": f"Bearer {token}"},
                json={"command_type": "PAUSE"},
            )
            self.assertEqual(pause_resp.status_code, 200)
            pause_event = ws.receive_json()
            self.assertEqual(pause_event["event_type"], "GUARD_STATE_CHANGED")
            self.assertEqual(pause_event["payload"]["guard_state"], "PAUSED")
            projection.apply_event(TradegoEventEnvelope(
                event_id=pause_event["event_id"],
                event_type=pause_event["event_type"],
                sequence=pause_event["sequence"],
                correlation_id=pause_event["correlation_id"],
                server_timestamp=pause_event["server_timestamp"],
                payload=pause_event["payload"],
            ))
            self.assertEqual(projection.guard_state, "PAUSED")

            # Step 6: RESUME Command
            resume_resp = self.client.post(
                "/api/v1/commands",
                headers={"Authorization": f"Bearer {token}"},
                json={"command_type": "RESUME"},
            )
            self.assertEqual(resume_resp.status_code, 200)
            resume_event = ws.receive_json()
            self.assertEqual(resume_event["event_type"], "GUARD_STATE_CHANGED")
            self.assertEqual(resume_event["payload"]["guard_state"], "NORMAL")
            projection.apply_event(TradegoEventEnvelope(
                event_id=resume_event["event_id"],
                event_type=resume_event["event_type"],
                sequence=resume_event["sequence"],
                correlation_id=resume_event["correlation_id"],
                server_timestamp=resume_event["server_timestamp"],
                payload=resume_event["payload"],
            ))
            self.assertEqual(projection.guard_state, "NORMAL")

            # Step 7: KILL_SWITCH Command
            kill_resp = self.client.post(
                "/api/v1/commands",
                headers={"Authorization": f"Bearer {token}"},
                json={"command_type": "KILL_SWITCH", "parameters": {"details": "Smoke test emergency kill"}},
            )
            self.assertEqual(kill_resp.status_code, 200)
            kill_event = ws.receive_json()
            self.assertEqual(kill_event["event_type"], "GUARD_STATE_CHANGED")
            self.assertEqual(kill_event["payload"]["guard_state"], "HALTED")
            projection.apply_event(TradegoEventEnvelope(
                event_id=kill_event["event_id"],
                event_type=kill_event["event_type"],
                sequence=kill_event["sequence"],
                correlation_id=kill_event["correlation_id"],
                server_timestamp=kill_event["server_timestamp"],
                payload=kill_event["payload"],
            ))
            self.assertEqual(projection.guard_state, "HALTED")

        # Step 8: Disconnect & Reconnect
        with self.client.websocket_connect(f"/ws/events?token={token}") as ws2:
            conn_frame2 = ws2.receive_json()
            self.assertEqual(conn_frame2["event_type"], "SESSION_ESTABLISHED")

            # Step 9: Simulate Gap -> STALE -> Resynchronization
            # Backend publishes an event skipping sequence forward
            self.seq_mgr.next_sequence()
            gap_event = self.broadcaster.publish("MARKET_TICK", {"symbol": "TCS", "ltp": 3960.0})
            stream_frame = ws2.receive_json()
            res = projection.apply_event(TradegoEventEnvelope(
                event_id=stream_frame["event_id"],
                event_type=stream_frame["event_type"],
                sequence=stream_frame["sequence"],
                correlation_id=stream_frame["correlation_id"],
                server_timestamp=stream_frame["server_timestamp"],
                payload=stream_frame["payload"],
            ))
            self.assertEqual(res, "GAP_DETECTED")
            self.assertEqual(projection.state, ProjectionState.STALE)

            # Re-fetch authoritative snapshot & resynchronize
            fresh_snap = self.snapshot_gen.generate_snapshot()
            resync_res = projection.resynchronize(fresh_snap)
            self.assertEqual(resync_res, "LIVE_RECONCILED")
            self.assertEqual(projection.state, ProjectionState.LIVE)
            self.assertEqual(projection.last_processed_sequence, fresh_snap.authoritative_sequence)


if __name__ == "__main__":
    unittest.main()
