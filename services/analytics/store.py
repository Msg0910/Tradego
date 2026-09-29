"""
Tradego Instrument Feature Store (Phase 4).

Manages on-demand registered indicators and microstructure features for a single instrument.
Provides fine-grained per-instrument thread isolation and safe reference-sharing snapshots.
"""

import threading
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId
from services.market_state.state import InstrumentStateSnapshot
from .base import BaseIndicator, BaseMicrostructureFeature
from .models import FeatureSnapshot, FeatureValue


class InstrumentFeatureStore:
    """
    Thread-safe container managing indicators and microstructure features for one instrument.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        history_capacity: int = 500,
    ) -> None:
        self.instrument_id = instrument_id
        self.history_capacity = history_capacity
        self._lock = threading.Lock()

        # Registered feature engines (key: feature_id -> instance)
        self._microstructure_features: Dict[str, BaseMicrostructureFeature] = {}
        self._indicators_by_tf: Dict[TimeFrame, List[BaseIndicator]] = {}
        self._indicators_by_id: Dict[str, BaseIndicator] = {}

        # Latest published feature values (key: feature_id -> FeatureValue)
        self._latest_features: Dict[str, FeatureValue] = {}

        # Bounded historical ring buffers for confirmed features (key: feature_id -> Deque[FeatureValue])
        self._history: Dict[str, Deque[FeatureValue]] = {}

    # =========================================================================
    # REGISTRATION API (On-Demand Registration)
    # =========================================================================

    def register_indicator(self, indicator: BaseIndicator) -> None:
        """Registers a stateful streaming indicator for a specific timeframe."""
        with self._lock:
            fid = indicator.feature_id
            self._indicators_by_id[fid] = indicator
            tf_list = self._indicators_by_tf.setdefault(indicator.timeframe, [])
            if indicator not in tf_list:
                tf_list.append(indicator)
            self._history.setdefault(fid, deque(maxlen=self.history_capacity))

    def register_microstructure_feature(self, feature: BaseMicrostructureFeature) -> None:
        """Registers a tick-level microstructure feature."""
        with self._lock:
            fid = feature.feature_id
            self._microstructure_features[fid] = feature
            self._history.setdefault(fid, deque(maxlen=self.history_capacity))

    # =========================================================================
    # INGESTION API
    # =========================================================================

    def process_state_snapshot(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> List[FeatureValue]:
        """
        Hot-path update triggered by live state changes or ticks.
        Evaluates registered microstructure features under per-instrument lock.
        """
        updated: List[FeatureValue] = []
        with self._lock:
            for feature in self._microstructure_features.values():
                fv = feature.compute(snapshot, event)
                self._latest_features[fv.feature_id] = fv
                updated.append(fv)
        return updated

    def process_closed_candle(self, candle: Candle) -> List[FeatureValue]:
        """
        Warm-path update triggered when a candle officially closes (is_closed=True).
        Advances confirmed indicator states and stores confirmed feature values.
        """
        if not candle.is_closed:
            raise ValueError("process_closed_candle requires a confirmed closed Candle")

        updated: List[FeatureValue] = []
        with self._lock:
            indicators = self._indicators_by_tf.get(candle.timeframe, [])
            for ind in indicators:
                fv = ind.confirmed_update(candle)
                self._latest_features[fv.feature_id] = fv
                self._history[fv.feature_id].append(fv)
                updated.append(fv)
        return updated

    # =========================================================================
    # PREVIEW & QUERY API
    # =========================================================================

    def get_active_preview(
        self,
        timeframe: TimeFrame,
        active_candle: Candle,
    ) -> Dict[str, FeatureValue]:
        """
        Ephemerally projects active indicators for the forming candle.
        Leaves persistent indicator states completely unmutated.
        """
        previews: Dict[str, FeatureValue] = {}
        with self._lock:
            indicators = self._indicators_by_tf.get(timeframe, [])
            for ind in indicators:
                fv = ind.preview(active_candle)
                previews[fv.feature_id] = fv
        return previews

    def get_snapshot(self) -> FeatureSnapshot:
        """
        Returns an immutable point-in-time FeatureSnapshot with safe reference sharing.
        Lock hold time is strictly minimal (< 1 us).
        """
        with self._lock:
            # Shallow copy of the dictionary mapping feature_id to immutable FeatureValue instances
            features_copy = dict(self._latest_features)
            ts = datetime.now()

        return FeatureSnapshot(
            instrument_id=self.instrument_id,
            snapshot_timestamp=ts,
            _features=features_copy,
        )

    def get_feature(self, feature_id: str) -> Optional[FeatureValue]:
        """Returns the latest published FeatureValue for a specific feature."""
        with self._lock:
            return self._latest_features.get(feature_id)

    def get_feature_history(
        self,
        feature_id: str,
        count: Optional[int] = None,
    ) -> List[FeatureValue]:
        """Returns an immutable snapshot list of historical confirmed feature values."""
        with self._lock:
            ring = self._history.get(feature_id)
            if not ring:
                return []
            if count is not None and count > 0:
                return list(ring)[-count:]
            return list(ring)

    def reset_session(self) -> None:
        """Resets indicators and accumulators at daily session open."""
        with self._lock:
            for ind in self._indicators_by_id.values():
                ind.reset()
            for feat in self._microstructure_features.values():
                feat.reset()
            self._latest_features.clear()
