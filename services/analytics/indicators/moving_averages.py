"""
Streaming Moving Averages for Tradego Analytics (Phase 4).

Implements:
- StreamingEMA: Exponential Moving Average with O(1) stateful updates.
- StreamingSMA: Simple Moving Average with O(1) sliding window sum.

Strictly adheres to the Dual-Path Indicator Contract (confirmed_update vs preview).
"""

from collections import deque
from typing import Deque, Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from ..base import BaseIndicator
from ..models import FeatureQuality, FeatureValue


class StreamingEMA(BaseIndicator):
    """
    Streaming Exponential Moving Average.
    O(1) updates with zero historical array re-iteration.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 20,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"EMA period must be positive, got {period}")
        indicator_name = name or f"EMA_{period}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.prev_ema: Optional[float] = None

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        """
        Advances persistent EMA state on a closed candle.
        """
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        price = candle.close
        if self.prev_ema is None:
            self.prev_ema = price
        else:
            self.prev_ema = (self.alpha * price) + ((1.0 - self.alpha) * self.prev_ema)

        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        quality = (
            FeatureQuality.VALID
            if self.periods_observed >= self.min_periods
            else FeatureQuality.WARMING_UP
        )

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(self.prev_ema, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects the EMA value using confirmed state + active forming candle.
        Leaves persistent state completely unmutated.
        """
        price = active_candle.close
        if self.prev_ema is None:
            preview_val = price
            quality = FeatureQuality.WARMING_UP
        else:
            preview_val = (self.alpha * price) + ((1.0 - self.alpha) * self.prev_ema)
            quality = (
                FeatureQuality.VALID
                if (self.periods_observed + 1) >= self.min_periods
                else FeatureQuality.WARMING_UP
            )

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(preview_val, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
        )

    def reset(self) -> None:
        self.prev_ema = None
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None


class StreamingSMA(BaseIndicator):
    """
    Streaming Simple Moving Average using an O(1) sliding window sum and ring buffer.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 20,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"SMA period must be positive, got {period}")
        indicator_name = name or f"SMA_{period}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.buffer: Deque[float] = deque(maxlen=period)
        self.running_sum: float = 0.0

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        price = candle.close
        if len(self.buffer) == self.period:
            evicted = self.buffer.popleft()
            self.running_sum -= evicted

        self.buffer.append(price)
        self.running_sum += price
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        count = len(self.buffer)
        sma_val = self.running_sum / count if count > 0 else price
        quality = (
            FeatureQuality.VALID
            if count >= self.min_periods
            else FeatureQuality.WARMING_UP
        )

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(sma_val, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects the SMA value by substituting the oldest element with the active close.
        Does not mutate the persistent deque buffer or running_sum.
        """
        price = active_candle.close
        count = len(self.buffer)

        if count == 0:
            preview_val = price
            quality = FeatureQuality.WARMING_UP
        elif count == self.period:
            # Ephemeral eviction of oldest element
            oldest = self.buffer[0]
            preview_sum = self.running_sum - oldest + price
            preview_val = preview_sum / self.period
            quality = FeatureQuality.VALID
        else:
            preview_sum = self.running_sum + price
            preview_val = preview_sum / (count + 1)
            quality = (
                FeatureQuality.VALID
                if (count + 1) >= self.min_periods
                else FeatureQuality.WARMING_UP
            )

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(preview_val, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
        )

    def reset(self) -> None:
        self.buffer.clear()
        self.running_sum = 0.0
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None
