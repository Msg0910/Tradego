"""
Tradego Central Feature Engine (Phase 4).

Coordinates feature stores across instruments, attaches to CandleEngine and
InstrumentStateStore as a listener, and provides clean query APIs for downstream strategies.
"""

import threading
from typing import Callable, Dict, List, Optional

from services.candles.engine import CandleEngine
from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId, InstrumentRegistry
from services.market_state.state import InstrumentStateSnapshot
from .base import BaseIndicator, BaseMicrostructureFeature
from .models import FeatureSnapshot, FeatureValue
from .store import InstrumentFeatureStore


class FeatureEngine:
    """
    Central coordinator managing analytics and feature stores across all instruments.
    """

    def __init__(
        self,
        registry: InstrumentRegistry,
        candle_engine: Optional[CandleEngine] = None,
        history_capacity: int = 500,
    ) -> None:
        self._registry = registry
        self._candle_engine = candle_engine
        self._history_capacity = history_capacity

        # Per-instrument feature stores protected by engine lock for addition
        self._store_lock = threading.Lock()
        self._stores: Dict[InstrumentId, InstrumentFeatureStore] = {}

        # Downstream feature listeners
        self._listener_lock = threading.Lock()
        self._listeners: List[Callable[[FeatureValue], None]] = []

        # Attach automatically to CandleEngine if provided
        if self._candle_engine is not None:
            self._candle_engine.add_candle_listener(self.on_candle_closed)

    # =========================================================================
    # STORE RETRIEVAL & REGISTRATION
    # =========================================================================

    def get_or_create_store(self, instrument_id: InstrumentId) -> InstrumentFeatureStore:
        """Retrieves or lazily initializes the InstrumentFeatureStore for an instrument."""
        store = self._stores.get(instrument_id)
        if store is not None:
            return store

        with self._store_lock:
            store = self._stores.get(instrument_id)
            if store is None:
                store = InstrumentFeatureStore(
                    instrument_id=instrument_id,
                    history_capacity=self._history_capacity,
                )
                self._stores[instrument_id] = store
            return store

    def register_indicator(
        self, instrument_id: InstrumentId, indicator: BaseIndicator
    ) -> None:
        """On-demand registration of an indicator for a specific instrument."""
        store = self.get_or_create_store(instrument_id)
        store.register_indicator(indicator)

    def register_microstructure_feature(
        self, instrument_id: InstrumentId, feature: BaseMicrostructureFeature
    ) -> None:
        """On-demand registration of a microstructure feature for a specific instrument."""
        store = self.get_or_create_store(instrument_id)
        store.register_microstructure_feature(feature)

    # =========================================================================
    # INGESTION CALLBACKS
    # =========================================================================

    def on_candle_closed(self, candle: Candle) -> List[FeatureValue]:
        """
        Listener callback registered with CandleEngine.
        Triggered when a candle closes (Path A closed bar stream).
        """
        store = self._stores.get(candle.instrument_id)
        if store is None:
            return []

        updated = store.process_closed_candle(candle)
        if updated:
            self._dispatch_features(updated)
        return updated

    def on_state_changed(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> List[FeatureValue]:
        """
        Hot-path callback triggered on state updates or ticks.
        Calculates registered microstructure features.
        """
        store = self._stores.get(snapshot.instrument_id)
        if store is None:
            return []

        updated = store.process_state_snapshot(snapshot, event)
        if updated:
            self._dispatch_features(updated)
        return updated

    def _dispatch_features(self, features: List[FeatureValue]) -> None:
        """Dispatches feature values to registered listeners with exception isolation."""
        with self._listener_lock:
            listeners = list(self._listeners)

        if not listeners:
            return

        for fv in features:
            for listener in listeners:
                try:
                    listener(fv)
                except Exception:
                    pass  # Isolate listener failure to protect the pipeline

    # =========================================================================
    # LISTENER REGISTRATION
    # =========================================================================

    def add_feature_listener(self, callback: Callable[[FeatureValue], None]) -> None:
        with self._listener_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_feature_listener(self, callback: Callable[[FeatureValue], None]) -> None:
        with self._listener_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    # =========================================================================
    # QUERY API
    # =========================================================================

    def get_features(self, instrument_id: InstrumentId) -> Optional[FeatureSnapshot]:
        """
        Returns an immutable snapshot of all published features for an instrument.
        """
        store = self._stores.get(instrument_id)
        if store is None:
            return None
        return store.get_snapshot()

    def get_active_preview(
        self,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
    ) -> Dict[str, FeatureValue]:
        """
        On-demand query projecting active indicators for the in-progress forming candle.
        Consumes Phase 3 CandleEngine active preview. Leaves confirmed state unmutated.
        """
        if self._candle_engine is None:
            return {}

        active_candle = self._candle_engine.get_active_candle(instrument_id, timeframe)
        if active_candle is None:
            return {}

        store = self._stores.get(instrument_id)
        if store is None:
            return {}

        return store.get_active_preview(timeframe, active_candle)

    def get_feature_history(
        self,
        instrument_id: InstrumentId,
        feature_id: str,
        count: Optional[int] = None,
    ) -> List[FeatureValue]:
        """Returns historical confirmed feature values."""
        store = self._stores.get(instrument_id)
        if store is None:
            return []
        return store.get_feature_history(feature_id, count)

    def reset_session(self) -> None:
        """Resets all instrument stores on daily session open."""
        with self._store_lock:
            for store in self._stores.values():
                store.reset_session()
