"""
Streaming Volatility Indicators for Tradego Analytics (Phase 4).

Implements:
- StreamingATR: Average True Range with Wilder's smoothing.
- RollingBollingerBands: Fixed-capacity ring buffer with running sum & sum of squares,
  O(1) updates, and numerical stability safeguards against floating-point cancellation.

Strictly adheres to the Dual-Path Indicator Contract (confirmed_update vs preview).
"""

import math
from collections import deque
from typing import Deque, List, Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from ..base import BaseIndicator
from ..models import FeatureQuality, FeatureValue


class StreamingATR(BaseIndicator):
    """
    Streaming Average True Range (ATR) using Wilder's smoothed moving average.
    O(1) stateful updates.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 14,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"ATR period must be positive, got {period}")
        indicator_name = name or f"ATR_{period}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.prev_atr: Optional[float] = None
        self.last_close: Optional[float] = None
        self._initial_trs: List[float] = []

    def _compute_tr(self, high: float, low: float, last_close: Optional[float]) -> float:
        if last_close is None:
            return high - low
        return max(high - low, abs(high - last_close), abs(low - last_close))

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        tr = self._compute_tr(candle.high, candle.low, self.last_close)
        self.last_close = candle.close
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        if self.periods_observed <= self.period:
            self._initial_trs.append(tr)
            if self.periods_observed == self.period:
                self.prev_atr = sum(self._initial_trs) / self.period
                quality = FeatureQuality.VALID
            else:
                self.prev_atr = sum(self._initial_trs) / len(self._initial_trs)
                quality = FeatureQuality.WARMING_UP
        else:
            assert self.prev_atr is not None
            self.prev_atr = (self.prev_atr * (self.period - 1.0) + tr) / self.period
            quality = FeatureQuality.VALID

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(self.prev_atr, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects the ATR value without mutating prev_atr or last_close.
        """
        tr = self._compute_tr(active_candle.high, active_candle.low, self.last_close)

        if self.periods_observed < self.period:
            temp_trs = list(self._initial_trs) + [tr]
            p_atr = sum(temp_trs) / len(temp_trs)
            quality = (
                FeatureQuality.VALID
                if len(temp_trs) >= self.period
                else FeatureQuality.WARMING_UP
            )
        else:
            assert self.prev_atr is not None
            p_atr = (self.prev_atr * (self.period - 1.0) + tr) / self.period
            quality = FeatureQuality.VALID

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(p_atr, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
        )

    def reset(self) -> None:
        self.prev_atr = None
        self.last_close = None
        self._initial_trs.clear()
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None


class RollingBollingerBands(BaseIndicator):
    """
    Streaming Bollinger Bands using an O(1) sliding window sum and sum of squares.
    Includes explicit numerical stability safeguards (max(0.0, raw_variance))
    to eliminate floating-point catastrophic cancellation.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 20,
        k: float = 2.0,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"Bollinger period must be positive, got {period}")
        indicator_name = name or f"BB_{period}_{k:g}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.k = k
        self.buffer: Deque[float] = deque(maxlen=period)
        self.running_sum: float = 0.0
        self.running_sum_sq: float = 0.0

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        price = candle.close
        if len(self.buffer) == self.period:
            evicted = self.buffer.popleft()
            self.running_sum -= evicted
            self.running_sum_sq -= evicted * evicted

        self.buffer.append(price)
        self.running_sum += price
        self.running_sum_sq += price * price
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        count = len(self.buffer)
        mean = self.running_sum / count

        # O(1) sample variance formula with numerical safeguard
        raw_var = (self.running_sum_sq - ((self.running_sum * self.running_sum) / count)) / count
        var = max(0.0, raw_var)
        std = math.sqrt(var)

        upper = mean + (self.k * std)
        lower = mean - (self.k * std)
        width = upper - lower
        percent_b = ((price - lower) / width) if width > 1e-12 else 0.5
        bandwidth = (width / mean) if mean > 1e-12 else 0.0

        quality = (
            FeatureQuality.VALID
            if count >= self.min_periods
            else FeatureQuality.WARMING_UP
        )

        metadata = {
            "mean": round(mean, 4),
            "std": round(std, 4),
            "upper": round(upper, 4),
            "lower": round(lower, 4),
            "bandwidth": round(bandwidth, 6),
            "percent_b": round(percent_b, 4),
        }

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(mean, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
            metadata=metadata,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects Bollinger Bands without mutating buffer, running_sum, or running_sum_sq.
        """
        price = active_candle.close
        count = len(self.buffer)

        if count == 0:
            preview_mean = price
            preview_std = 0.0
            quality = FeatureQuality.WARMING_UP
        elif count == self.period:
            oldest = self.buffer[0]
            p_sum = self.running_sum - oldest + price
            p_sum_sq = self.running_sum_sq - (oldest * oldest) + (price * price)
            preview_mean = p_sum / self.period
            p_raw_var = (p_sum_sq - ((p_sum * p_sum) / self.period)) / self.period
            preview_std = math.sqrt(max(0.0, p_raw_var))
            quality = FeatureQuality.VALID
        else:
            p_count = count + 1
            p_sum = self.running_sum + price
            p_sum_sq = self.running_sum_sq + (price * price)
            preview_mean = p_sum / p_count
            p_raw_var = (p_sum_sq - ((p_sum * p_sum) / p_count)) / p_count
            preview_std = math.sqrt(max(0.0, p_raw_var))
            quality = (
                FeatureQuality.VALID
                if p_count >= self.min_periods
                else FeatureQuality.WARMING_UP
            )

        upper = preview_mean + (self.k * preview_std)
        lower = preview_mean - (self.k * preview_std)
        width = upper - lower
        percent_b = ((price - lower) / width) if width > 1e-12 else 0.5
        bandwidth = (width / preview_mean) if preview_mean > 1e-12 else 0.0

        metadata = {
            "mean": round(preview_mean, 4),
            "std": round(preview_std, 4),
            "upper": round(upper, 4),
            "lower": round(lower, 4),
            "bandwidth": round(bandwidth, 6),
            "percent_b": round(percent_b, 4),
        }

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(preview_mean, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
            metadata=metadata,
        )

    def reset(self) -> None:
        self.buffer.clear()
        self.running_sum = 0.0
        self.running_sum_sq = 0.0
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None
