"""
Unit tests for Tradego Presentation / API Boundary Vertical Slice 1.
Verifies end-to-end flow:
Client -> Authenticated Command Boundary -> Capability Authorization ->
TradingGuard Core -> Authoritative Event Broadcaster -> Client Projection.
Enforces UI-01, UI-02, UI-03, UI-08, UI-11, UI-13, UI-14, SEC-01, SEC-02, SEC-05, SEC-09, SEC-14.
"""

import time
import unittest

from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState

from gateway.contracts import (
    Capability,
    CommandRequest,
    CommandStatus,
    CommandType,
)
from gateway.security import (
    InMemorySessionStore,
    Tier1AuditLogger,
)
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.command import CommandGateway
from gateway.projection import ClientProjection, ProjectionState, SnapshotGenerator


class TestBoundarySlice(unittest.TestCase):
    """Test suite for the boundary slice."""

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.session_store = InMemorySessionStore(default_ttl_seconds=3600)
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.seq_mgr = SequenceManager(initial_sequence=0)
        self.broadcaster = EventBroadcaster(sequence_manager=self.seq_mgr)
        self.gateway = CommandGateway(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            broadcaster=self.broadcaster,
        )
        self.snapshot_gen = SnapshotGenerator(
            trading_guard=self.guard,
            sequence_manager=self.seq_mgr,
        )

    def tearDown(self) -> None:
        self.audit_logger.close()

    def test_unauthenticated_command_rejected(self) -> None:
        """SEC-01: Inbound command without valid token is rejected."""
        req = CommandRequest(command_type=CommandType.PAUSE, operator_id="trader1")
        result = self.gateway.dispatch(token="invalid_token", request=req)

        self.assertEqual(result.status, CommandStatus.REJECTED)
        self.assertIn("UNAUTHENTICATED", result.reason)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    def test_unauthorized_command_rejected(self) -> None:
        """SEC-02: Authenticated user without required capability is rejected."""
        token = self.session_store.create_session(
            operator_id="observer1",
            roles=["OBSERVER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        req = CommandRequest(command_type=CommandType.PAUSE, operator_id="observer1")
        result = self.gateway.dispatch(token=token, request=req)

        self.assertEqual(result.status, CommandStatus.REJECTED)
        self.assertIn("UNAUTHORIZED", result.reason)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

    def test_authorized_pause_resume_flatten_lifecycle(self) -> None:
        """
        Tests authorized command execution, core guard mutation, and sequence event emission.
        """
        token = self.session_store.create_session(
            operator_id="supervisor1",
            roles=["RISK_SUPERVISOR"],
            capabilities={
                Capability.CAP_CONTROL_PAUSE,
                Capability.CAP_CONTROL_RESUME,
                Capability.CAP_CONTROL_FLATTEN,
            },
        )
        sub_queue = self.broadcaster.subscribe()

        # 1. PAUSE command
        req1 = CommandRequest(command_type=CommandType.PAUSE, operator_id="supervisor1")
        res1 = self.gateway.dispatch(token=token, request=req1)
        self.assertEqual(res1.status, CommandStatus.ACCEPTED)
        self.assertEqual(res1.guard_state, "PAUSED")
        self.assertEqual(res1.sequence, 1)
        self.assertEqual(self.guard.state, GuardState.PAUSED)

        event1 = sub_queue.get(timeout=1.0)
        self.assertEqual(event1.sequence, 1)
        self.assertEqual(event1.event_type, "GUARD_STATE_CHANGED")
        self.assertEqual(event1.payload["guard_state"], "PAUSED")

        # 2. RESUME command
        req2 = CommandRequest(command_type=CommandType.RESUME, operator_id="supervisor1")
        res2 = self.gateway.dispatch(token=token, request=req2)
        self.assertEqual(res2.status, CommandStatus.ACCEPTED)
        self.assertEqual(res2.guard_state, "NORMAL")
        self.assertEqual(res2.sequence, 2)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

        event2 = sub_queue.get(timeout=1.0)
        self.assertEqual(event2.sequence, 2)
        self.assertEqual(event2.payload["guard_state"], "NORMAL")

        # 3. EMERGENCY_FLATTEN command
        req3 = CommandRequest(
            command_type=CommandType.EMERGENCY_FLATTEN, operator_id="supervisor1"
        )
        res3 = self.gateway.dispatch(token=token, request=req3)
        self.assertEqual(res3.status, CommandStatus.ACCEPTED)
        self.assertEqual(res3.guard_state, "EMERGENCY_FLATTEN")
        self.assertEqual(res3.sequence, 3)
        self.assertEqual(self.guard.state, GuardState.EMERGENCY_FLATTEN)

        event3 = sub_queue.get(timeout=1.0)
        self.assertEqual(event3.sequence, 3)
        self.assertEqual(event3.payload["guard_state"], "EMERGENCY_FLATTEN")

    def test_audit_logger_hash_chain_integrity(self) -> None:
        """
        SEC-09: Tier 1 audit logger writes serialized records with SHA-256 hash chaining.
        """
        token = self.session_store.create_session(
            operator_id="admin1",
            roles=["ADMIN"],
            capabilities={Capability.CAP_ADMIN},
        )
        req = CommandRequest(command_type=CommandType.PAUSE, operator_id="admin1")
        self.gateway.dispatch(token=token, request=req)

        self.audit_logger.flush()
        records = self.audit_logger.in_memory_records
        self.assertGreaterEqual(len(records), 2)

        # Verify hash chain continuity
        prev_hash = Tier1AuditLogger.GENESIS_HASH
        for rec in records:
            self.assertEqual(rec["prev_hash"], prev_hash)
            self.assertTrue("hash" in rec)
            prev_hash = rec["hash"]

    def test_client_projection_snapshot_and_sequence_reconciliation(self) -> None:
        """
        UI-08, UI-13: Client bootstraps from S_snap, applies contiguous updates,
        drops duplicates, and detects sequence gaps.
        """
        # Step 1: Advance sequence to 5 with some events
        for i in range(5):
            self.broadcaster.publish("PING", {"idx": i})

        # Step 2: Take authoritative snapshot
        snapshot = self.snapshot_gen.generate_snapshot()
        self.assertEqual(snapshot.authoritative_sequence, 5)

        # Step 3: Client bootstraps from snapshot
        client = ClientProjection()
        client.bootstrap(snapshot)
        self.assertEqual(client.state, ProjectionState.STREAMING)
        self.assertEqual(client.last_processed_sequence, 5)

        # Step 4: Stale / duplicate event with S=4 arrives
        stale_env = self.broadcaster.publish("DUMMY", {})
        # Manually create envelope with S=4 to simulate network replay
        from gateway.contracts import TradegoEventEnvelope
        dup_envelope = TradegoEventEnvelope(
            event_id="e-dup",
            event_type="GUARD_STATE_CHANGED",
            sequence=4,
            correlation_id="c1",
            server_timestamp="ts",
            payload={"guard_state": "PAUSED"},
        )
        status_dup = client.apply_event(dup_envelope)
        self.assertEqual(status_dup, "DUPLICATE_DROPPED")
        self.assertEqual(client.last_processed_sequence, 5)
        self.assertEqual(client.dropped_duplicates_count, 1)

        # Step 5: Contiguous event S=6 arrives
        env6 = TradegoEventEnvelope(
            event_id="e-6",
            event_type="GUARD_STATE_CHANGED",
            sequence=6,
            correlation_id="c2",
            server_timestamp="ts",
            payload={"guard_state": "PAUSED"},
        )
        status6 = client.apply_event(env6)
        self.assertEqual(status6, "APPLIED")
        self.assertEqual(client.last_processed_sequence, 6)
        self.assertEqual(client.guard_state, "PAUSED")
        self.assertEqual(client.applied_events_count, 1)

        # Step 6: Sequence gap event S=8 arrives (S=7 was lost)
        env8 = TradegoEventEnvelope(
            event_id="e-8",
            event_type="GUARD_STATE_CHANGED",
            sequence=8,
            correlation_id="c3",
            server_timestamp="ts",
            payload={"guard_state": "NORMAL"},
        )
        status8 = client.apply_event(env8)
        self.assertEqual(status8, "GAP_DETECTED")
        self.assertEqual(client.state, ProjectionState.GAP_DETECTED)
        # Client does NOT update state to NORMAL because sequence gap occurred
        self.assertEqual(client.guard_state, "PAUSED")

    def test_session_invalidation(self) -> None:
        """SEC-14: Session revocation invalidates token immediately."""
        token = self.session_store.create_session(
            operator_id="op_to_revoke",
            roles=["OPERATOR"],
            capabilities={Capability.CAP_CONTROL_PAUSE},
        )
        self.assertIsNotNone(self.session_store.validate_token(token))

        revoked = self.session_store.revoke_session(token)
        self.assertTrue(revoked)
        self.assertIsNone(self.session_store.validate_token(token))

    def test_backpressure_isolation(self) -> None:
        """UI-11: Slow subscriber queue saturation does not block broadcaster."""
        slow_queue = self.broadcaster.subscribe(max_queue_size=2)

        # Publish 5 events into broadcaster with a queue size of 2
        for i in range(5):
            self.broadcaster.publish("TEST_EVENT", {"num": i})

        # Queue should hold at most 2 items, publisher completed with zero delay
        self.assertEqual(slow_queue.qsize(), 2)


if __name__ == "__main__":
    unittest.main()
