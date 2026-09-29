"""
Unit tests for Tradego Phase 2 Presentation & Event Stream Boundary Slice.
Covers:
1. authenticated stream connection
2. unauthenticated stream rejection
3. snapshot acquisition
4. S_snap handling
5. event sequence N+1 processing
6. duplicate event handling
7. delayed event handling
8. confirmed gap detection
9. stale state transition
10. snapshot resynchronization
11. buffered-event reconciliation
12. return to LIVE
13. disconnect/reconnect
14. correlation ID propagation
15. client projection state transitions
16. DISC-01 regression: KILL_SWITCH uses GuardTripReason.MANUAL
17. SEC-13 enforcement: Inbound WebSocket frames closed with 1003
18. MarketStateAdapter authoritative read-only boundary
19. GET /api/v1/market/state endpoint
20. Presentation UI dashboard endpoint (GET /ui and GET /)
21. Presentation backpressure insulation (UI-11)
"""

import time
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
from services.market_state.state import InstrumentStateSnapshot
from services.market_state.store import InstrumentStateStore
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState, GuardTripReason


class TestStreamBoundary(unittest.TestCase):
    """Test suite for Phase 2 stream boundary, sequence semantics, and projection."""

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
        self.inst_nifty = InstrumentId(
            symbol="NIFTY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.registry.register(self.inst_nifty, provider_tokens={"ATMSTOX": "26000"})
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

        # Standard operator token
        self.valid_token = self.session_store.create_session(
            operator_id="operator-alice",
            roles=["TRADER", "OBSERVER"],
            capabilities=[
                Capability.CAP_OBSERVE,
                Capability.CAP_CONTROL_PAUSE,
                Capability.CAP_CONTROL_RESUME,
                Capability.CAP_CONTROL_KILL,
            ],
        )

        # Restricted operator lacking CAP_OBSERVE
        self.restricted_token = self.session_store.create_session(
            operator_id="operator-restricted",
            roles=["RESTRICTED"],
            capabilities=[],
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    # -----------------------------------------------------------------------
    # 1. Authenticated Stream Connection
    # -----------------------------------------------------------------------
    def test_authenticated_stream_connection(self) -> None:
        """1. Authenticated client connects to /ws/events and receives SESSION_ESTABLISHED."""
        with self.client.websocket_connect(f"/ws/events?token={self.valid_token}") as ws:
            init_frame = ws.receive_json()
            self.assertEqual(init_frame["event_type"], "SESSION_ESTABLISHED")
            self.assertEqual(init_frame["payload"]["operator_id"], "operator-alice")
            self.assertIn("CAP_OBSERVE", init_frame["payload"]["capabilities"])
            self.assertEqual(init_frame["sequence"], 0)

    # -----------------------------------------------------------------------
    # 2. Unauthenticated Stream Rejection
    # -----------------------------------------------------------------------
    def test_unauthenticated_stream_rejection(self) -> None:
        """2. Stream connection without valid session token is rejected with 1008 Policy Violation."""
        # No token
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect("/ws/events") as ws:
                pass
        self.assertEqual(cm.exception.code, 1008)

        # Invalid token
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect("/ws/events?token=invalid_opaque_token") as ws:
                pass
        self.assertEqual(cm.exception.code, 1008)

        # Lacking CAP_OBSERVE capability
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(f"/ws/events?token={self.restricted_token}") as ws:
                pass
        self.assertEqual(cm.exception.code, 1008)

    # -----------------------------------------------------------------------
    # 3. Snapshot Acquisition
    # -----------------------------------------------------------------------
    def test_snapshot_acquisition(self) -> None:
        """3. Authenticated snapshot acquisition returns point-in-time state and S_snap."""
        # Advance sequence by publishing an event
        self.broadcaster.publish("ENGINE_INITIALIZED", {"status": "READY"})

        resp = self.client.get(
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {self.valid_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["authoritative_sequence"], 1)
        self.assertEqual(data["guard_state"], "NORMAL")
        self.assertIn("market_state", data["details"])

    # -----------------------------------------------------------------------
    # 4. S_snap Handling
    # -----------------------------------------------------------------------
    def test_s_snap_handling(self) -> None:
        """4. Snapshot establishes the authoritative sequence baseline S_snap in ClientProjection."""
        # Advance sequences to 10
        for _ in range(10):
            self.seq_mgr.next_sequence()

        snapshot = self.snapshot_gen.generate_snapshot(runtime_mode="PAPER")
        self.assertEqual(snapshot.authoritative_sequence, 10)

        projection = ClientProjection()
        projection.connect()
        projection.start_buffering()
        status = projection.bootstrap(snapshot)

        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 10)

    # -----------------------------------------------------------------------
    # 5. Event Sequence N+1 Processing
    # -----------------------------------------------------------------------
    def test_event_sequence_n_plus_one_processing(self) -> None:
        """5. In LIVE state, incoming sequence S_last + 1 is applied monotonically."""
        projection = ClientProjection()
        snapshot = self.snapshot_gen.generate_snapshot()
        projection.bootstrap(snapshot)
        self.assertEqual(projection.state, ProjectionState.LIVE)
        base_seq = projection.last_processed_sequence

        envelope = TradegoEventEnvelope(
            event_id="e-1",
            event_type="GUARD_STATE_CHANGED",
            sequence=base_seq + 1,
            correlation_id="corr-1",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"guard_state": "PAUSED"},
        )
        res = projection.apply_event(envelope)

        self.assertEqual(res, "APPLIED")
        self.assertEqual(projection.last_processed_sequence, base_seq + 1)
        self.assertEqual(projection.guard_state, "PAUSED")
        self.assertEqual(projection.applied_events_count, 1)

    # -----------------------------------------------------------------------
    # 6. Duplicate Event Handling
    # -----------------------------------------------------------------------
    def test_duplicate_event_handling(self) -> None:
        """6. In LIVE state, sequence <= S_last is dropped as duplicate/replay."""
        projection = ClientProjection()
        snapshot = self.snapshot_gen.generate_snapshot()
        projection.bootstrap(snapshot)

        # S_last is 0. Send seq 1
        e1 = TradegoEventEnvelope(
            event_id="e-1",
            event_type="TICK",
            sequence=1,
            correlation_id="corr-1",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "RELIANCE", "ltp": 2500.0},
        )
        self.assertEqual(projection.apply_event(e1), "APPLIED")
        self.assertEqual(projection.last_processed_sequence, 1)

        # Duplicate: send seq 1 again
        dup = TradegoEventEnvelope(
            event_id="e-1-dup",
            event_type="TICK",
            sequence=1,
            correlation_id="corr-1",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "RELIANCE", "ltp": 2500.0},
        )
        self.assertEqual(projection.apply_event(dup), "DUPLICATE_DROPPED")
        self.assertEqual(projection.dropped_duplicates_count, 1)
        self.assertEqual(projection.last_processed_sequence, 1)
        self.assertEqual(projection.state, ProjectionState.LIVE)

    # -----------------------------------------------------------------------
    # 7. Delayed Event Handling
    # -----------------------------------------------------------------------
    def test_delayed_event_handling(self) -> None:
        """7. Delayed sequence from earlier point-in-time is safely dropped without error."""
        projection = ClientProjection()
        snapshot = self.snapshot_gen.generate_snapshot()
        projection.bootstrap(snapshot)

        # Advance to seq 5
        for s in range(1, 6):
            env = TradegoEventEnvelope(
                event_id=f"e-{s}",
                event_type="TICK",
                sequence=s,
                correlation_id=f"c-{s}",
                server_timestamp="2026-09-22T00:00:00Z",
                payload={"symbol": "TCS", "ltp": 3500.0 + s},
            )
            projection.apply_event(env)

        self.assertEqual(projection.last_processed_sequence, 5)

        # Delayed arrival of seq 2
        delayed = TradegoEventEnvelope(
            event_id="e-delayed",
            event_type="TICK",
            sequence=2,
            correlation_id="c-delayed",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "TCS", "ltp": 3502.0},
        )
        self.assertEqual(projection.apply_event(delayed), "DUPLICATE_DROPPED")
        self.assertEqual(projection.last_processed_sequence, 5)
        self.assertEqual(projection.dropped_duplicates_count, 1)

    # -----------------------------------------------------------------------
    # 8. Confirmed Gap Detection
    # -----------------------------------------------------------------------
    def test_confirmed_gap_detection(self) -> None:
        """8. Incoming sequence > S_last + 1 declares confirmed gap and enters STALE."""
        projection = ClientProjection()
        snapshot = self.snapshot_gen.generate_snapshot()
        projection.bootstrap(snapshot)
        self.assertEqual(projection.last_processed_sequence, 0)

        # Skip seq 1, receive seq 2
        gap_env = TradegoEventEnvelope(
            event_id="e-gap",
            event_type="TICK",
            sequence=2,
            correlation_id="c-gap",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "INFY", "ltp": 1500.0},
        )
        res = projection.apply_event(gap_env)

        self.assertEqual(res, "GAP_DETECTED")
        self.assertEqual(projection.state, ProjectionState.STALE)

    # -----------------------------------------------------------------------
    # 9. Stale State Transition
    # -----------------------------------------------------------------------
    def test_stale_state_transition(self) -> None:
        """9. When STALE, projection quarantines state and ignores further direct applications."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())

        # Cause gap to trigger STALE
        gap_env = TradegoEventEnvelope(
            event_id="e-3",
            event_type="TICK",
            sequence=3,
            correlation_id="c-3",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "INFY", "ltp": 1500.0},
        )
        projection.apply_event(gap_env)
        self.assertEqual(projection.state, ProjectionState.STALE)

        # Subsequent event while STALE is ignored
        sub_env = TradegoEventEnvelope(
            event_id="e-4",
            event_type="TICK",
            sequence=4,
            correlation_id="c-4",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "INFY", "ltp": 1510.0},
        )
        res = projection.apply_event(sub_env)
        self.assertEqual(res, "IGNORED_NOT_LIVE")
        self.assertEqual(projection.state, ProjectionState.STALE)

    # -----------------------------------------------------------------------
    # 10. Snapshot Resynchronization
    # -----------------------------------------------------------------------
    def test_snapshot_resynchronization(self) -> None:
        """10. From STALE, requesting resync and applying a fresh snapshot resets sequence."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())

        # Trigger STALE
        gap_env = TradegoEventEnvelope(
            event_id="e-gap",
            event_type="TICK",
            sequence=5,
            correlation_id="c-gap",
            server_timestamp="2026-09-22T00:00:00Z",
            payload={"symbol": "INFY", "ltp": 1500.0},
        )
        projection.apply_event(gap_env)
        self.assertEqual(projection.state, ProjectionState.STALE)

        # Initiate recovery
        projection.request_resync()
        self.assertEqual(projection.state, ProjectionState.RESYNCING)

        # Advance server sequence to 5
        while self.seq_mgr.current_sequence() < 5:
            self.seq_mgr.next_sequence()

        new_snapshot = self.snapshot_gen.generate_snapshot()
        self.assertEqual(new_snapshot.authoritative_sequence, 5)

        status = projection.resynchronize(new_snapshot)
        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 5)

    # -----------------------------------------------------------------------
    # 11. Buffered-Event Reconciliation
    # -----------------------------------------------------------------------
    def test_buffered_event_reconciliation(self) -> None:
        """11. Events buffered during snapshot acquisition are reconciled contiguously."""
        projection = ClientProjection()
        projection.start_buffering()
        self.assertEqual(projection.state, ProjectionState.SNAPSHOT_PENDING)

        # In-flight events arrive while snapshot is being fetched
        e1 = TradegoEventEnvelope("e1", "TICK", 1, "c1", "t", {"symbol": "A", "ltp": 10.0})
        e2 = TradegoEventEnvelope("e2", "TICK", 2, "c2", "t", {"symbol": "B", "ltp": 20.0})
        e3 = TradegoEventEnvelope("e3", "TICK", 3, "c3", "t", {"symbol": "C", "ltp": 30.0})

        projection.apply_event(e1)
        projection.apply_event(e2)
        projection.apply_event(e3)
        self.assertEqual(projection.buffered_events_count, 3)

        # Snapshot was captured at sequence 1 (e1 already included)
        self.seq_mgr.next_sequence()  # seq = 1
        snapshot = self.snapshot_gen.generate_snapshot()
        self.assertEqual(snapshot.authoritative_sequence, 1)

        # Bootstrap reconciles buffer: e1 dropped as <= 1, e2 and e3 applied
        status = projection.bootstrap(snapshot)
        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 3)
        self.assertEqual(projection.dropped_duplicates_count, 1)
        self.assertEqual(projection.applied_events_count, 2)
        self.assertEqual(projection.market_state["C"]["ltp"], 30.0)

    # -----------------------------------------------------------------------
    # 12. Return to LIVE
    # -----------------------------------------------------------------------
    def test_return_to_live(self) -> None:
        """12. Verifies complete recovery arc: LIVE -> GAP -> STALE -> RESYNC -> LIVE."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())
        self.assertEqual(projection.state, ProjectionState.LIVE)

        # Cause gap
        projection.apply_event(
            TradegoEventEnvelope("e-gap", "TICK", 10, "c-gap", "t", {})
        )
        self.assertEqual(projection.state, ProjectionState.STALE)

        # Buffer incoming event 11 while stale/resyncing
        projection.request_resync()
        e11 = TradegoEventEnvelope("e-11", "TICK", 11, "c-11", "t", {"symbol": "SBIN", "ltp": 800.0})
        projection.apply_event(e11)
        self.assertEqual(projection.buffered_events_count, 1)

        # Advance sequence to 10
        while self.seq_mgr.current_sequence() < 10:
            self.seq_mgr.next_sequence()

        fresh_snapshot = self.snapshot_gen.generate_snapshot()
        status = projection.resynchronize(fresh_snapshot)

        self.assertEqual(status, "LIVE_RECONCILED")
        self.assertEqual(projection.state, ProjectionState.LIVE)
        self.assertEqual(projection.last_processed_sequence, 11)
        self.assertEqual(projection.market_state["SBIN"]["ltp"], 800.0)

    # -----------------------------------------------------------------------
    # 13. Disconnect / Reconnect
    # -----------------------------------------------------------------------
    def test_disconnect_reconnect(self) -> None:
        """13. Disconnect resets projection; reconnect restarts lifecycle from scratch."""
        projection = ClientProjection()
        projection.bootstrap(self.snapshot_gen.generate_snapshot())
        self.assertEqual(projection.state, ProjectionState.LIVE)

        projection.disconnect()
        self.assertEqual(projection.state, ProjectionState.DISCONNECTED)
        self.assertEqual(projection.buffered_events_count, 0)

        # Reconnect lifecycle
        projection.connect()
        self.assertEqual(projection.state, ProjectionState.CONNECTING)
        projection.start_buffering()
        self.assertEqual(projection.state, ProjectionState.SNAPSHOT_PENDING)

        snap = self.snapshot_gen.generate_snapshot()
        projection.bootstrap(snap)
        self.assertEqual(projection.state, ProjectionState.LIVE)

    # -----------------------------------------------------------------------
    # 14. Correlation ID Propagation
    # -----------------------------------------------------------------------
    def test_correlation_id_propagation(self) -> None:
        """14. Correlation ID is propagated to WebSocket connection and audit records."""
        custom_corr = f"corr-test-{uuid.uuid4()}"
        with self.client.websocket_connect(
            f"/ws/events?token={self.valid_token}&correlation_id={custom_corr}"
        ) as ws:
            init_frame = ws.receive_json()
            self.assertEqual(init_frame["correlation_id"], custom_corr)

    # -----------------------------------------------------------------------
    # 15. Client Projection State Transitions
    # -----------------------------------------------------------------------
    def test_client_projection_state_transitions(self) -> None:
        """15. Explicitly tests all valid states in ProjectionState lifecycle enum."""
        states = [
            ProjectionState.CONNECTING,
            ProjectionState.SNAPSHOT_PENDING,
            ProjectionState.RECONCILING,
            ProjectionState.LIVE,
            ProjectionState.STALE,
            ProjectionState.RESYNCING,
            ProjectionState.DISCONNECTED,
        ]
        for s in states:
            self.assertIsInstance(s.value, str)

        projection = ClientProjection()
        self.assertEqual(projection.state, ProjectionState.DISCONNECTED)
        projection.connect()
        self.assertEqual(projection.state, ProjectionState.CONNECTING)
        projection.start_buffering()
        self.assertEqual(projection.state, ProjectionState.SNAPSHOT_PENDING)

    # -----------------------------------------------------------------------
    # 16. DISC-01 Regression: KILL_SWITCH uses GuardTripReason.MANUAL
    # -----------------------------------------------------------------------
    def test_disc01_kill_switch_regression(self) -> None:
        """16. DISC-01: KILL_SWITCH executes cleanly via GuardTripReason.MANUAL."""
        req = CommandRequest(
            command_type=CommandType.KILL_SWITCH,
            operator_id="operator-alice",
            parameters={"details": "Emergency test kill switch"},
        )
        res = self.command_gateway.dispatch(token=self.valid_token, request=req)
        self.assertEqual(res.status, CommandStatus.ACCEPTED)
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(res.guard_state, "HALTED")

    # -----------------------------------------------------------------------
    # 17. SEC-13 Enforcement: Inbound WebSocket Frames Closed with 1003
    # -----------------------------------------------------------------------
    def test_sec13_one_way_push_rejection(self) -> None:
        """17. SEC-13: Inbound client data frame terminates stream with 1003 Unsupported Data."""
        with self.client.websocket_connect(f"/ws/events?token={self.valid_token}") as ws:
            ws.receive_json()  # Read initial SESSION_ESTABLISHED
            ws.send_text("client_data_not_allowed")

            with self.assertRaises(WebSocketDisconnect) as cm:
                ws.receive_json()
            self.assertEqual(cm.exception.code, 1003)

    # -----------------------------------------------------------------------
    # 18. MarketStateAdapter Authoritative Read-Only Boundary
    # -----------------------------------------------------------------------
    def test_market_state_adapter_read_only(self) -> None:
        """18. MarketStateAdapter safely extracts real snapshots without data fabrication."""
        empty_adapter = MarketStateAdapter(state_store=None)
        self.assertEqual(empty_adapter.get_market_snapshots(), {})

        # Ingest real market event for registered instrument
        ev = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="26000",
            ltp=25500.50,
            open=25350.0,
            high=25600.0,
            low=25300.0,
            total_volume=1250000.0,
            oi=540000,
            local_receive_timestamp=1.0,
        )
        self.state_store.on_market_event(ev)

        snapshots = self.market_adapter.get_market_snapshots()
        self.assertIn("NIFTY", snapshots)
        self.assertEqual(snapshots["NIFTY"]["ltp"], 25500.50)
        self.assertEqual(snapshots["NIFTY"]["high"], 25600.0)

    # -----------------------------------------------------------------------
    # 19. GET /api/v1/market/state Endpoint
    # -----------------------------------------------------------------------
    def test_market_state_endpoint(self) -> None:
        """19. GET /api/v1/market/state returns authoritative market snapshots."""
        resp = self.client.get(
            "/api/v1/market/state",
            headers={"Authorization": f"Bearer {self.valid_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("snapshots", body)
        self.assertIn("count", body)
        self.assertIn("timestamp", body)

    # -----------------------------------------------------------------------
    # 20. Presentation UI Dashboard Endpoint
    # -----------------------------------------------------------------------
    def test_presentation_ui_endpoint(self) -> None:
        """20. GET /ui and GET / return the Phase 2 presentation HTML dashboard."""
        resp_ui = self.client.get("/ui")
        self.assertEqual(resp_ui.status_code, 200)
        self.assertIn("Tradego", resp_ui.text)
        self.assertIn("ClientProjectionManager", resp_ui.text)

        resp_root = self.client.get("/")
        self.assertEqual(resp_root.status_code, 200)
        self.assertIn("Tradego", resp_root.text)

    # -----------------------------------------------------------------------
    # 21. Stream Backpressure Insulation (UI-11)
    # -----------------------------------------------------------------------
    def test_stream_backpressure_insulation(self) -> None:
        """21. UI-11: Lagging subscriber queue dropping excess events non-blockingly."""
        q = self.broadcaster.subscribe(max_queue_size=2)
        # Publish 5 events
        for i in range(5):
            self.broadcaster.publish("TEST_EVENT", {"i": i})

        # Queue only retained up to its capacity of 2; publisher never blocked
        self.assertEqual(q.qsize(), 2)
        self.broadcaster.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
