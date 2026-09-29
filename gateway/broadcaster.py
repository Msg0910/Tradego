"""
Tradego Boundary Event Broadcaster & Sequence Manager.
Enforces monotonic 64-bit sequence ordering and non-blocking event distribution
with strict presentation backpressure insulation (UI-01, UI-11).
"""

from datetime import datetime, timezone
import queue
import threading
from typing import Any, List, Optional
import uuid

from .contracts import TradegoEventEnvelope


class SequenceManager:
    """
    Thread-safe monotonic sequence generator.
    Guarantees strictly increasing integer sequence numbers for authoritative state events.
    """

    def __init__(self, initial_sequence: int = 0) -> None:
        self._lock = threading.Lock()
        self._sequence = initial_sequence

    def next_sequence(self) -> int:
        """Atomically increments and returns the next monotonic sequence number."""
        with self._lock:
            self._sequence += 1
            return self._sequence

    def current_sequence(self) -> int:
        """Returns current sequence counter without incrementing."""
        with self._lock:
            return self._sequence


class EventBroadcaster:
    """
    Distributes authoritative state change events to connected client stream channels.
    Insulates the trading core by ensuring subscriber backpressure never propagates upstream.
    """

    def __init__(self, sequence_manager: Optional[SequenceManager] = None) -> None:
        self._lock = threading.RLock()
        self._seq_mgr = sequence_manager or SequenceManager()
        self._subscribers: List[queue.Queue] = []

    @property
    def sequence_manager(self) -> SequenceManager:
        return self._seq_mgr

    def subscribe(self, max_queue_size: int = 1000) -> queue.Queue:
        """Registers a new client distribution queue."""
        q: queue.Queue = queue.Queue(maxsize=max_queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Removes a client distribution queue."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(
        self,
        event_type: str,
        payload: Any,
        correlation_id: Optional[str] = None,
    ) -> TradegoEventEnvelope:
        """
        Assigns the next monotonic sequence, packages the event inside an authoritative envelope,
        and fans out non-blockingly to all active subscriber queues.
        """
        seq = self._seq_mgr.next_sequence()
        envelope = TradegoEventEnvelope(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            sequence=seq,
            correlation_id=correlation_id or str(uuid.uuid4()),
            server_timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )

        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(envelope)
                except queue.Full:
                    # UI-11: Backpressure insulation. Dropping message for lagging client
                    # rather than blocking the core event distribution.
                    pass

        return envelope
