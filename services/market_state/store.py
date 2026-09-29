"""
Thread-safe In-Memory Instrument State Store for Tradego.

Serves as the central repository for all live instrument states.
Subscribes to MarketDataGateway events, maps provider tokens to canonical
InstrumentIds (with explicit unresolved tracking, never guessing NSE), and
provides immutable read-only snapshots to downstream components.
"""

import logging
import threading
from typing import Callable, Dict, List, Optional, Set, Tuple

from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent

from .instrument import InstrumentId, InstrumentRegistry
from .state import InstrumentState, InstrumentStateSnapshot

logger = logging.getLogger("market_state.store")

# Listener signature for state change notifications:
# callback(snapshot: InstrumentStateSnapshot) -> None
StateChangeListener = Callable[[InstrumentStateSnapshot], None]


class InstrumentStateStore:
    """
    Central in-memory state repository for all tracked instruments.
    """

    def __init__(self, registry: Optional[InstrumentRegistry] = None) -> None:
        self._registry = registry or InstrumentRegistry()

        # Primary storage: (provider_normalized, token_normalized) -> InstrumentState
        self._states_by_token: Dict[Tuple[str, str], InstrumentState] = {}
        # Secondary index: InstrumentId -> InstrumentState
        self._states_by_id: Dict[InstrumentId, InstrumentState] = {}

        # Concurrency: Fine-grained per-instrument locks to avoid bottlenecking
        self._locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._global_lock = threading.Lock()

        # Listeners for state change events
        self._listeners: List[StateChangeListener] = []

    @property
    def registry(self) -> InstrumentRegistry:
        return self._registry

    def add_listener(self, listener: StateChangeListener) -> None:
        """Register a callback for state change notifications."""
        with self._global_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: StateChangeListener) -> None:
        """Unregister a state change listener."""
        with self._global_lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def attach_to_gateway(self, gateway: MarketDataGateway) -> None:
        """Connects this store to a MarketDataGateway instance."""
        gateway.add_listener(self.on_market_event)

    def detach_from_gateway(self, gateway: MarketDataGateway) -> None:
        """Disconnects this store from a MarketDataGateway instance."""
        gateway.remove_listener(self.on_market_event)

    def _get_or_create_state(
        self, provider: str, token: str
    ) -> Tuple[InstrumentState, threading.Lock]:
        """
        Retrieves or creates the InstrumentState and its associated lock.
        CRITICAL: Never silently fall back to Exchange.NSE.
        """
        p_norm = provider.strip().upper()
        t_norm = str(token).strip()
        key = (p_norm, t_norm)

        with self._global_lock:
            state = self._states_by_token.get(key)
            if state is None:
                # Attempt to resolve via registry
                inst_id = self._registry.resolve(p_norm, t_norm)
                is_resolved = inst_id is not None

                state = InstrumentState(
                    provider=p_norm,
                    provider_symbol_id=t_norm,
                    instrument_id=inst_id,
                    is_resolved=is_resolved,
                )
                self._states_by_token[key] = state
                self._locks[key] = threading.Lock()

                if inst_id is not None:
                    self._states_by_id[inst_id] = state

            lock = self._locks[key]

            # Re-check resolution if state was previously unresolved
            if not state.is_resolved:
                inst_id = self._registry.resolve(p_norm, t_norm)
                if inst_id is not None:
                    state.instrument_id = inst_id
                    state.is_resolved = True
                    self._states_by_id[inst_id] = state

            return state, lock

    def on_market_event(self, event: MarketEvent) -> None:
        """
        Hot path ingestion callback invoked by MarketDataGateway.
        Non-blocking, allocation-conscious.
        """
        if not event or not event.provider or not event.provider_symbol_id:
            return

        state, lock = self._get_or_create_state(event.provider, event.provider_symbol_id)

        with lock:
            updated = state.update_from_event(event)
            if not updated:
                return
            snapshot = state.create_snapshot() if self._listeners else None

        # Dispatch snapshot to listeners outside the per-instrument lock
        if snapshot and self._listeners:
            for listener in list(self._listeners):
                try:
                    listener(snapshot)
                except Exception as e:
                    logger.error(
                        f"[InstrumentStateStore] Error in state listener for {snapshot.provider_symbol_id}: {e}",
                        exc_info=True,
                    )

    def get_state(self, instrument_id: InstrumentId) -> Optional[InstrumentState]:
        """
        Returns direct reference to mutable InstrumentState for internal engines.
        Thread-safety: Read/modify within lock or use get_snapshot().
        """
        with self._global_lock:
            return self._states_by_id.get(instrument_id)

    def get_state_by_token(self, provider: str, token: str) -> Optional[InstrumentState]:
        """Returns direct reference to mutable InstrumentState by provider token."""
        p_norm = provider.strip().upper()
        t_norm = str(token).strip()
        with self._global_lock:
            return self._states_by_token.get((p_norm, t_norm))

    def get_snapshot(self, instrument_id: InstrumentId) -> Optional[InstrumentStateSnapshot]:
        """
        Returns a thread-safe, immutable point-in-time snapshot of the instrument.
        """
        with self._global_lock:
            state = self._states_by_id.get(instrument_id)
            if state is None:
                return None
            key = (state.provider, state.provider_symbol_id)
            lock = self._locks.get(key)

        if lock:
            with lock:
                return state.create_snapshot()
        return state.create_snapshot()

    def get_snapshot_by_token(
        self, provider: str, token: str
    ) -> Optional[InstrumentStateSnapshot]:
        """
        Returns a thread-safe, immutable point-in-time snapshot by provider token.
        Works even for unresolved instruments.
        """
        p_norm = provider.strip().upper()
        t_norm = str(token).strip()
        key = (p_norm, t_norm)

        with self._global_lock:
            state = self._states_by_token.get(key)
            if state is None:
                return None
            lock = self._locks.get(key)

        if lock:
            with lock:
                return state.create_snapshot()
        return state.create_snapshot()

    def get_all_snapshots(self) -> Dict[InstrumentId, InstrumentStateSnapshot]:
        """
        Returns point-in-time snapshots for all resolved instruments.
        """
        with self._global_lock:
            items = list(self._states_by_id.items())

        snapshots: Dict[InstrumentId, InstrumentStateSnapshot] = {}
        for inst_id, state in items:
            key = (state.provider, state.provider_symbol_id)
            with self._global_lock:
                lock = self._locks.get(key)
            if lock:
                with lock:
                    snapshots[inst_id] = state.create_snapshot()
            else:
                snapshots[inst_id] = state.create_snapshot()
        return snapshots

    def reset_session(self, instrument_id: Optional[InstrumentId] = None) -> None:
        """
        Explicitly resets session statistics.
        If instrument_id is provided, resets only that instrument.
        If None, resets all tracked instruments.
        """
        with self._global_lock:
            if instrument_id:
                state = self._states_by_id.get(instrument_id)
                targets = [state] if state else []
            else:
                targets = list(self._states_by_token.values())

        for s in targets:
            key = (s.provider, s.provider_symbol_id)
            with self._global_lock:
                lock = self._locks.get(key)
            if lock:
                with lock:
                    s.reset_session()
            else:
                s.reset_session()

    @property
    def tracked_count(self) -> int:
        """Total number of tracked instruments (both resolved and unresolved)."""
        with self._global_lock:
            return len(self._states_by_token)

    @property
    def resolved_count(self) -> int:
        """Number of tracked instruments mapped to an InstrumentId."""
        with self._global_lock:
            return len(self._states_by_id)

    @property
    def unresolved_tokens(self) -> Set[Tuple[str, str]]:
        """Tokens received from feed that remain unresolved."""
        with self._global_lock:
            return {
                key for key, state in self._states_by_token.items()
                if not state.is_resolved
            }
