"""
Tradego Boundary Snapshot Generator & Client Projection Model.
Enforces the authoritative snapshot sequence frontier (S_snap), contiguous sequence
reconciliation, duplicate dropping, confirmed-gap detection, and deterministic state
machine lifecycle (UI-02, UI-03, UI-08, UI-13).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from services.runtime.guards import TradingGuard

from .adapters import MarketStateAdapter
from .broadcaster import SequenceManager
from .contracts import SnapshotPayload, TradegoEventEnvelope


class ProjectionState(str, Enum):
    """
    Client projection synchronization state machine lifecycle (Phase 2).
    """
    CONNECTING = "CONNECTING"
    SNAPSHOT_PENDING = "SNAPSHOT_PENDING"
    RECONCILING = "RECONCILING"
    LIVE = "LIVE"
    STALE = "STALE"
    RESYNCING = "RESYNCING"
    DISCONNECTED = "DISCONNECTED"

    # Backward-compatibility aliases for Phase 1 baseline tests
    STREAMING = "LIVE"
    GAP_DETECTED = "STALE"
    UNINITIALIZED = "DISCONNECTED"
    SYNCING = "RECONCILING"


class SnapshotGenerator:
    """
    Assembles consistent point-in-time state snapshots representing the exact sequence
    frontier S_snap across all engine sub-domains and market data adapters.
    """

    def __init__(
        self,
        trading_guard: TradingGuard,
        sequence_manager: SequenceManager,
        market_adapter: Optional[MarketStateAdapter] = None,
    ) -> None:
        self._guard = trading_guard
        self._seq_mgr = sequence_manager
        self._market_adapter = market_adapter

    def generate_snapshot(
        self,
        runtime_mode: str = "PAPER",
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> SnapshotPayload:
        """
        Captures the exact sequence frontier S_snap and authoritative state boundary.
        """
        # S_snap is the exact sequence frontier of the snapshot
        s_snap = self._seq_mgr.current_sequence()
        guard_state = self._guard.state.value

        details: Dict[str, Any] = dict(extra_details or {})
        if self._market_adapter and self._market_adapter.has_store:
            details["market_state"] = self._market_adapter.get_market_snapshots()
        elif "market_state" not in details:
            details["market_state"] = {}

        return SnapshotPayload(
            snapshot_id=str(uuid.uuid4()),
            snapshot_timestamp=datetime.now(timezone.utc).isoformat(),
            authoritative_sequence=s_snap,
            runtime_mode=runtime_mode,
            guard_state=guard_state,
            details=details,
        )


class ClientProjection:
    """
    Client-side presentation projection and deterministic sequence reconciler.
    Enforces the mandatory invariants:
    - Never fabricates optimistic state (UI-03)
    - Reconciles baseline starting unconditionally at S_snap (UI-08)
    - Buffers incoming stream during snapshot acquisition
    - Drops duplicates (S <= S_last)
    - Applies contiguous updates (S == S_last + 1)
    - Detects confirmed sequence gaps (S > S_last + 1) and quarantines to STALE (UI-13)
    - Supports clean resynchronization from a new authoritative snapshot
    """

    def __init__(self) -> None:
        self._state = ProjectionState.DISCONNECTED
        self._last_processed_sequence = 0
        self._guard_state: Optional[str] = None
        self._market_state: Dict[str, Any] = {}
        self._inbound_buffer: List[TradegoEventEnvelope] = []
        self._applied_events_count = 0
        self._dropped_duplicates_count = 0
        self._detected_gaps_count = 0
        self._resync_count = 0
        self._last_event_timestamp: Optional[str] = None

    @property
    def state(self) -> ProjectionState:
        return self._state

    @property
    def last_processed_sequence(self) -> int:
        return self._last_processed_sequence

    @property
    def guard_state(self) -> Optional[str]:
        return self._guard_state

    @property
    def market_state(self) -> Dict[str, Any]:
        return dict(self._market_state)

    @property
    def applied_events_count(self) -> int:
        return self._applied_events_count

    @property
    def dropped_duplicates_count(self) -> int:
        return self._dropped_duplicates_count

    @property
    def detected_gaps_count(self) -> int:
        return self._detected_gaps_count

    @property
    def resync_count(self) -> int:
        return self._resync_count

    @property
    def last_event_timestamp(self) -> Optional[str]:
        return self._last_event_timestamp

    @property
    def buffered_events_count(self) -> int:
        return len(self._inbound_buffer)

    def connect(self) -> None:
        """Transitions projection state to CONNECTING."""
        self._state = ProjectionState.CONNECTING

    def start_buffering(self) -> None:
        """Transitions to SNAPSHOT_PENDING and initiates inbound event buffering."""
        self._state = ProjectionState.SNAPSHOT_PENDING
        self._inbound_buffer.clear()

    def buffer_event(self, envelope: TradegoEventEnvelope) -> None:
        """Buffers an event during snapshot acquisition or reconciliation."""
        self._inbound_buffer.append(envelope)

    def bootstrap(self, snapshot: SnapshotPayload) -> str:
        """
        Bootstraps client presentation projection from an authoritative snapshot.
        Initializes sequence baseline to S_snap and reconciles buffered events.
        """
        self._last_processed_sequence = snapshot.authoritative_sequence
        self._guard_state = snapshot.guard_state
        self._last_event_timestamp = snapshot.snapshot_timestamp
        if isinstance(snapshot.details, dict) and "market_state" in snapshot.details:
            self._market_state = dict(snapshot.details["market_state"])
        self._state = ProjectionState.RECONCILING
        return self.reconcile_buffer()

    def reconcile_buffer(self) -> str:
        """
        Reconciles all buffered events received during snapshot acquisition:
        - S <= S_snap  -> Duplicate / already in snapshot (drop safely)
        - S == S_last + 1 -> Contiguous (apply and advance S_last)
        - S > S_last + 1  -> Confirmed Sequence Gap (quarantine to STALE)
        """
        # Sort buffer by sequence
        self._inbound_buffer.sort(key=lambda env: env.sequence)
        buffer_copy = list(self._inbound_buffer)
        self._inbound_buffer.clear()

        for env in buffer_copy:
            s = env.sequence
            if s <= self._last_processed_sequence:
                self._dropped_duplicates_count += 1
                continue
            if s == self._last_processed_sequence + 1:
                self._apply_payload(env.payload)
                self._last_processed_sequence = s
                self._applied_events_count += 1
                self._last_event_timestamp = env.server_timestamp
            else:
                # Confirmed sequence gap in buffer!
                self._detected_gaps_count += 1
                self._state = ProjectionState.STALE
                return "GAP_DETECTED"

        self._state = ProjectionState.LIVE
        return "LIVE_RECONCILED"

    def apply_event(self, envelope: TradegoEventEnvelope) -> str:
        """
        Applies live inbound stream events:
        - In SNAPSHOT_PENDING / RECONCILING / RESYNCING: buffers the event
        - In LIVE:
          - S <= S_last  -> Duplicate (drop safely)
          - S == S_last + 1 -> Contiguous (apply and advance S_last)
          - S > S_last + 1  -> Confirmed Sequence Gap (quarantine to STALE)
        """
        if self._state in (
            ProjectionState.SNAPSHOT_PENDING,
            ProjectionState.RECONCILING,
            ProjectionState.RESYNCING,
        ):
            self.buffer_event(envelope)
            return "BUFFERED"

        if self._state != ProjectionState.LIVE:
            return "IGNORED_NOT_LIVE"

        s = envelope.sequence

        if s <= self._last_processed_sequence:
            self._dropped_duplicates_count += 1
            return "DUPLICATE_DROPPED"

        if s == self._last_processed_sequence + 1:
            self._apply_payload(envelope.payload)
            self._last_processed_sequence = s
            self._applied_events_count += 1
            self._last_event_timestamp = envelope.server_timestamp
            return "APPLIED"

        # Confirmed sequence gap (s > self._last_processed_sequence + 1)
        self._detected_gaps_count += 1
        self._state = ProjectionState.STALE
        return "GAP_DETECTED"

    def _apply_payload(self, payload: Any) -> None:
        if isinstance(payload, dict):
            if "guard_state" in payload:
                self._guard_state = payload["guard_state"]
            if "market_state" in payload:
                self._market_state.update(payload["market_state"])
            if "symbol" in payload and "ltp" in payload:
                self._market_state[payload["symbol"]] = payload

    def request_resync(self) -> None:
        """Initiates recovery from STALE state by transitioning to RESYNCING."""
        self._state = ProjectionState.RESYNCING
        self._inbound_buffer.clear()

    def resynchronize(self, new_snapshot: SnapshotPayload) -> str:
        """
        Re-anchors projection to a new snapshot S_snap and reconciles any
        subsequent buffered events.
        """
        self._resync_count += 1
        self._last_processed_sequence = new_snapshot.authoritative_sequence
        self._guard_state = new_snapshot.guard_state
        self._last_event_timestamp = new_snapshot.snapshot_timestamp
        if isinstance(new_snapshot.details, dict) and "market_state" in new_snapshot.details:
            self._market_state = dict(new_snapshot.details["market_state"])
        self._state = ProjectionState.RECONCILING
        return self.reconcile_buffer()

    def disconnect(self) -> None:
        """Transitions projection state to DISCONNECTED and clears local buffer."""
        self._state = ProjectionState.DISCONNECTED
        self._inbound_buffer.clear()
