"""
Streaming Momentum Indicators for Tradego Analytics (Phase 4).

Implements:
- StreamingRSI: Relative Strength Index with Wilder smoothing.
- StreamingMACD: Moving Average Convergence Divergence with fast/slow/signal EMAs.

Strictly adheres to the Dual-Path Indicator Contract (confirmed_update vs preview).
"""

from typing import List, Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from ..base import BaseIndicator
from ..models import FeatureQuality, FeatureValue
from .moving_averages import StreamingEMA


class StreamingRSI(BaseIndicator):
    """
    Streaming Relative Strength Index (RSI) using Wilder's smoothed moving average.
    O(1) updates with zero historical array re-iteration.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 14,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"RSI period must be positive, got {period}")
        indicator_name = name or f"RSI_{period}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.avg_gain: Optional[float] = None
        self.avg_loss: Optional[float] = None
        self.last_close: Optional[float] = None
        self._initial_gains: List[float] = []
        self._initial_losses: List[float] = []

    def _calculate_rsi(self, avg_gain: float, avg_loss: float) -> float:
        """Helper to calculate bounded RSI with division-by-zero protection."""
        if avg_loss <= 1e-12:
            return 100.0 if avg_gain > 0 else 50.0
        if avg_gain <= 1e-12:
            return 0.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        price = candle.close
        if self.last_close is None:
            self.last_close = price
            self.periods_observed = 1
            self.last_confirmed_timestamp = candle.end_time
            fv = FeatureValue(
                feature_id=self.feature_id,
                value=50.0,
                quality=FeatureQuality.WARMING_UP,
                observation_timestamp=candle.end_time,
                availability_timestamp=candle.end_time,
                is_confirmed=True,
            )
            self.last_confirmed_value = fv
            return fv

        diff = price - self.last_close
        gain = max(0.0, diff)
        loss = max(0.0, -diff)
        self.last_close = price
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        # Initial accumulation phase
        if self.periods_observed <= self.period:
            self._initial_gains.append(gain)
            self._initial_losses.append(loss)
            if self.periods_observed == self.period:
                self.avg_gain = sum(self._initial_gains) / self.period
                self.avg_loss = sum(self._initial_losses) / self.period
                rsi_val = self._calculate_rsi(self.avg_gain, self.avg_loss)
                quality = FeatureQuality.VALID
            else:
                rsi_val = 50.0
                quality = FeatureQuality.WARMING_UP
        else:
            # Wilder's Smoothing
            assert self.avg_gain is not None and self.avg_loss is not None
            self.avg_gain = (self.avg_gain * (self.period - 1.0) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1.0) + loss) / self.period
            rsi_val = self._calculate_rsi(self.avg_gain, self.avg_loss)
            quality = FeatureQuality.VALID

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(rsi_val, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally projects RSI using confirmed state + active close price.
        Does not mutate avg_gain, avg_loss, or last_close.
        """
        price = active_candle.close
        if self.last_close is None:
            return FeatureValue(
                feature_id=self.feature_id,
                value=50.0,
                quality=FeatureQuality.WARMING_UP,
                observation_timestamp=active_candle.end_time,
                availability_timestamp=active_candle.end_time,
                is_confirmed=False,
            )

        diff = price - self.last_close
        gain = max(0.0, diff)
        loss = max(0.0, -diff)

        if self.periods_observed < self.period:
            temp_gains = list(self._initial_gains) + [gain]
            temp_losses = list(self._initial_losses) + [loss]
            if len(temp_gains) >= self.period:
                p_avg_gain = sum(temp_gains[-self.period:]) / self.period
                p_avg_loss = sum(temp_losses[-self.period:]) / self.period
                rsi_val = self._calculate_rsi(p_avg_gain, p_avg_loss)
                quality = FeatureQuality.VALID
            else:
                rsi_val = 50.0
                quality = FeatureQuality.WARMING_UP
        else:
            assert self.avg_gain is not None and self.avg_loss is not None
            p_avg_gain = (self.avg_gain * (self.period - 1.0) + gain) / self.period
            p_avg_loss = (self.avg_loss * (self.period - 1.0) + loss) / self.period
            rsi_val = self._calculate_rsi(p_avg_gain, p_avg_loss)
            quality = FeatureQuality.VALID

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(rsi_val, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
        )

    def reset(self) -> None:
        self.avg_gain = None
        self.avg_loss = None
        self.last_close = None
        self._initial_gains.clear()
        self._initial_losses.clear()
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None


class StreamingMACD(BaseIndicator):
    """
    Moving Average Convergence Divergence (MACD).
    Calculates MACD Line, Signal Line, and MACD Histogram.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        name: Optional[str] = None,
    ) -> None:
        if fast_period >= slow_period:
            raise ValueError(f"fast_period ({fast_period}) must be < slow_period ({slow_period})")
        indicator_name = name or f"MACD_{fast_period}_{slow_period}_{signal_period}"
        super().__init__(indicator_name, timeframe, min_periods=slow_period + signal_period)
        self.fast_ema = StreamingEMA(timeframe, fast_period, name="FAST")
        self.slow_ema = StreamingEMA(timeframe, slow_period, name="SLOW")
        self.signal_ema = StreamingEMA(timeframe, signal_period, name="SIGNAL")
        self.signal_period = signal_period

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        fast_fv = self.fast_ema.confirmed_update(candle)
        slow_fv = self.slow_ema.confirmed_update(candle)
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        macd_line = fast_fv.value - slow_fv.value

        # Signal line only accumulates once slow EMA is ready
        if self.slow_ema.is_ready:
            # Construct synthetic candle for signal EMA with close = macd_line
            from services.candles.models import VolumeQuality
            sig_candle = Candle(
                instrument_id=candle.instrument_id,
                timeframe=self.timeframe,
                start_time=candle.start_time,
                end_time=candle.end_time,
                open=macd_line,
                high=macd_line,
                low=macd_line,
                close=macd_line,
                volume=0.0,
                ticks=0,
                volume_quality=VolumeQuality.COMPLETE,
                is_closed=True,
            )
            sig_fv = self.signal_ema.confirmed_update(sig_candle)
            signal_line = sig_fv.value
            histogram = macd_line - signal_line
            quality = (
                FeatureQuality.VALID
                if self.signal_ema.is_ready
                else FeatureQuality.WARMING_UP
            )
        else:
            signal_line = 0.0
            histogram = 0.0
            quality = FeatureQuality.WARMING_UP

        metadata = {
            "macd": round(macd_line, 4),
            "signal": round(signal_line, 4),
            "histogram": round(histogram, 4),
        }

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(macd_line, 4),
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
        Ephemerally projects MACD without mutating fast, slow, or signal EMA states.
        """
        fast_prev = self.fast_ema.preview(active_candle)
        slow_prev = self.slow_ema.preview(active_candle)
        macd_line = fast_prev.value - slow_prev.value

        if self.slow_ema.is_ready:
            from services.candles.models import VolumeQuality
            sig_candle = Candle(
                instrument_id=active_candle.instrument_id,
                timeframe=self.timeframe,
                start_time=active_candle.start_time,
                end_time=active_candle.end_time,
                open=macd_line,
                high=macd_line,
                low=macd_line,
                close=macd_line,
                volume=0.0,
                ticks=0,
                volume_quality=VolumeQuality.COMPLETE,
                is_closed=False,
            )
            sig_prev = self.signal_ema.preview(sig_candle)
            signal_line = sig_prev.value
            histogram = macd_line - signal_line
            quality = (
                FeatureQuality.VALID
                if self.signal_ema.is_ready
                else FeatureQuality.WARMING_UP
            )
        else:
            signal_line = 0.0
            histogram = 0.0
            quality = FeatureQuality.WARMING_UP

        metadata = {
            "macd": round(macd_line, 4),
            "signal": round(signal_line, 4),
            "histogram": round(histogram, 4),
        }

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(macd_line, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
            metadata=metadata,
        )

    def reset(self) -> None:
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.signal_ema.reset()
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None
