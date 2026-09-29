"""
Tradego Base Interfaces for Streaming Indicators and Microstructure Features (Phase 4).

Enforces:
- Dual-Path Indicator Contract (confirmed_update vs preview)
- Hot-Path Microstructure Feature Contract
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from services.market_gateway.models import MarketEvent
from services.market_state.state import InstrumentStateSnapshot
from .models import FeatureValue


class BaseIndicator(ABC):
    """
    Abstract contract for stateful streaming technical indicators.

    Mandates the Dual-Path Contract:
    - confirmed_update(candle): Invoked ONLY when a candle officially closes (is_closed=True).
      Advances persistent running state and returns a confirmed FeatureValue (is_confirmed=True).
    - preview(active_candle): Invoked on-demand to inspect live forming bar state.
      MUST NEVER mutate persistent state. Returns ephemeral FeatureValue (is_confirmed=False).
    """

    def __init__(self, name: str, timeframe: TimeFrame, min_periods: int) -> None:
        self.name = name
        self.timeframe = timeframe
        self.min_periods = min_periods
        self.periods_observed: int = 0
        self.last_confirmed_timestamp: Optional[datetime] = None
        self.last_confirmed_value: Optional[FeatureValue] = None

    @property
    def is_ready(self) -> bool:
        """True if the indicator has observed enough periods to be mathematically valid."""
        return self.periods_observed >= self.min_periods

    @property
    def feature_id(self) -> str:
        """Canonical feature identifier (e.g. 'EMA_20_5M', 'RSI_14_1M')."""
        return f"{self.name}_{self.timeframe.label}"

    @abstractmethod
    def confirmed_update(self, candle: Candle) -> FeatureValue:
        """
        Processes a confirmed closed candle.
        Advances internal state and returns confirmed FeatureValue (is_confirmed=True).
        """
        pass

    @abstractmethod
    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects the indicator value using confirmed state + active candle.
        MUST NOT mutate any internal state or buffers.
        Returns preview FeatureValue (is_confirmed=False).
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """Resets the indicator state (e.g. on session open for session-bounded indicators)."""
        pass


class BaseMicrostructureFeature(ABC):
    """
    Abstract contract for high-frequency market microstructure features on the tick hot path.
    Evaluated directly on InstrumentStateSnapshot (Level-4 Depth) or MarketEvent.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def feature_id(self) -> str:
        return self.name

    @abstractmethod
    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        """
        Computes the microstructure feature value in O(1) time without blocking I/O or allocations.
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """Resets any rolling tick-level counters or session accumulators."""
        pass
