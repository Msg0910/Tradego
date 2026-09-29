"""
Tradego Strategy Context (Phase 5).

Defines the immutable StrategyContext aggregating market state, quantitative features,
active candle previews, and read-only position views for strategy evaluation.
"""

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Optional

from services.analytics.models import FeatureSnapshot, FeatureValue
from services.candles.models import Candle
from services.market_state.instrument import InstrumentId
from services.market_state.state import InstrumentStateSnapshot
from .models import PositionView, TriggerMode


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """
    Immutable, point-in-time contextual view supplied to strategies.
    Aggregates Phase 2 market state, Phase 4 quantitative features,
    and an optional read-only PositionView.

    evaluation_timestamp MUST be explicitly supplied:
    - Live trading: SignalEngine supplies the current market/system clock.
    - Backtest replay: Replay driver supplies the historical simulation clock.
    """
    instrument_id: InstrumentId
    trigger_mode: TriggerMode
    market_state: InstrumentStateSnapshot
    features: FeatureSnapshot
    evaluation_timestamp: datetime
    active_preview_features: Optional[Mapping[str, FeatureValue]] = None
    active_candle: Optional[Candle] = None
    position: Optional[PositionView] = None

    def __post_init__(self) -> None:
        # Enforce read-only mapping wrapper if mutable dict was passed
        if self.active_preview_features is not None and isinstance(self.active_preview_features, dict):
            object.__setattr__(
                self, "active_preview_features", MappingProxyType(self.active_preview_features)
            )

    def get_feature_value(self, feature_id: str, default: Optional[float] = None) -> Optional[float]:
        """Convenience method to query confirmed feature scalar values."""
        return self.features.get_value(feature_id, default=default)

    def get_preview_feature_value(self, feature_id: str, default: Optional[float] = None) -> Optional[float]:
        """Convenience method to query active preview feature values."""
        if self.active_preview_features is None:
            return default
        fv = self.active_preview_features.get(feature_id)
        if fv is not None and fv.value is not None:
            return fv.value
        return default
