"""
Streaming Volume Indicators for Tradego Analytics (Phase 4).

Implements:
- StreamingVWAP: Volume-Weighted Average Price and VWAP deviation in basis points.
- VolumeZScore: Rolling volume standard score (anomaly/surge detection).

Adheres strictly to the Dual-Path Indicator Contract and VolumeQuality propagation rules.
"""

import math
from collections import deque
from typing import Deque, Optional

from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TimeFrame
from ..base import BaseIndicator
from ..models import FeatureQuality, FeatureValue


class StreamingVWAP(BaseIndicator):
    """
    Streaming Volume-Weighted Average Price (VWAP).
    Propagates Phase 3 VolumeQuality into FeatureQuality:
    - COMPLETE -> VALID
    - PARTIAL / RECONSTRUCTED / ESTIMATED -> DEGRADED
    - UNAVAILABLE / Zero Volume -> INVALID
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        name: Optional[str] = None,
    ) -> None:
        indicator_name = name or "VWAP"
        super().__init__(indicator_name, timeframe, min_periods=1)
        self.cum_pv: float = 0.0
        self.cum_vol: float = 0.0
        self._has_degraded_volume: bool = False

    def _determine_quality(self, candle_vol_quality: VolumeQuality, vol: float) -> FeatureQuality:
        if vol <= 0.0 or candle_vol_quality == VolumeQuality.UNAVAILABLE:
            return FeatureQuality.INVALID
        if self._has_degraded_volume or candle_vol_quality != VolumeQuality.COMPLETE:
            return FeatureQuality.DEGRADED
        return FeatureQuality.VALID

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        bar_vwap = candle.vwap if candle.vwap is not None else candle.close
        bar_vol = candle.volume

        if candle.volume_quality != VolumeQuality.COMPLETE:
            self._has_degraded_volume = True

        if bar_vol > 0:
            self.cum_pv += bar_vwap * bar_vol
            self.cum_vol += bar_vol

        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        if self.cum_vol > 0:
            vwap_val = self.cum_pv / self.cum_vol
            dev_bps = ((candle.close - vwap_val) / vwap_val) * 10000.0
            quality = self._determine_quality(candle.volume_quality, self.cum_vol)
        else:
            vwap_val = candle.close
            dev_bps = 0.0
            quality = FeatureQuality.INVALID

        metadata = {
            "vwap": round(vwap_val, 4),
            "deviation_bps": round(dev_bps, 2),
            "cumulative_volume": self.cum_vol,
        }

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(vwap_val, 4),
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
        Ephemerally projects VWAP using confirmed state + active forming candle.
        Does not mutate cum_pv or cum_vol.
        """
        bar_vwap = active_candle.vwap if active_candle.vwap is not None else active_candle.close
        bar_vol = active_candle.volume

        p_pv = self.cum_pv + (bar_vwap * bar_vol if bar_vol > 0 else 0.0)
        p_vol = self.cum_vol + (bar_vol if bar_vol > 0 else 0.0)

        if p_vol > 0:
            p_vwap = p_pv / p_vol
            dev_bps = ((active_candle.close - p_vwap) / p_vwap) * 10000.0
            quality = self._determine_quality(active_candle.volume_quality, p_vol)
        else:
            p_vwap = active_candle.close
            dev_bps = 0.0
            quality = FeatureQuality.INVALID

        metadata = {
            "vwap": round(p_vwap, 4),
            "deviation_bps": round(dev_bps, 2),
            "cumulative_volume": p_vol,
        }

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(p_vwap, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
            metadata=metadata,
        )

    def reset(self) -> None:
        self.cum_pv = 0.0
        self.cum_vol = 0.0
        self._has_degraded_volume = False
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None


class VolumeZScore(BaseIndicator):
    """
    Streaming rolling Z-score of candle volume.
    Measures volume anomalies and surges in terms of standard deviations from the rolling mean.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        period: int = 20,
        name: Optional[str] = None,
    ) -> None:
        if period <= 0:
            raise ValueError(f"VolumeZScore period must be positive, got {period}")
        indicator_name = name or f"VOL_ZSCORE_{period}"
        super().__init__(indicator_name, timeframe, min_periods=period)
        self.period = period
        self.buffer: Deque[float] = deque(maxlen=period)
        self.running_sum: float = 0.0
        self.running_sum_sq: float = 0.0

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        vol = candle.volume
        if len(self.buffer) == self.period:
            evicted = self.buffer.popleft()
            self.running_sum -= evicted
            self.running_sum_sq -= evicted * evicted

        self.buffer.append(vol)
        self.running_sum += vol
        self.running_sum_sq += vol * vol
        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        count = len(self.buffer)
        mean = self.running_sum / count
        raw_var = (self.running_sum_sq - ((self.running_sum * self.running_sum) / count)) / count
        std = math.sqrt(max(0.0, raw_var))

        z_score = ((vol - mean) / std) if std > 1e-6 else 0.0
        quality = (
            FeatureQuality.VALID
            if count >= self.min_periods and candle.volume_quality == VolumeQuality.COMPLETE
            else (FeatureQuality.DEGRADED if count >= self.min_periods else FeatureQuality.WARMING_UP)
        )

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=round(z_score, 4),
            quality=quality,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        vol = active_candle.volume
        count = len(self.buffer)

        if count == 0:
            p_z = 0.0
            quality = FeatureQuality.WARMING_UP
        elif count == self.period:
            oldest = self.buffer[0]
            p_sum = self.running_sum - oldest + vol
            p_sum_sq = self.running_sum_sq - (oldest * oldest) + (vol * vol)
            p_mean = p_sum / self.period
            p_raw_var = (p_sum_sq - ((p_sum * p_sum) / self.period)) / self.period
            p_std = math.sqrt(max(0.0, p_raw_var))
            p_z = ((vol - p_mean) / p_std) if p_std > 1e-6 else 0.0
            quality = (
                FeatureQuality.VALID
                if active_candle.volume_quality == VolumeQuality.COMPLETE
                else FeatureQuality.DEGRADED
            )
        else:
            p_count = count + 1
            p_sum = self.running_sum + vol
            p_sum_sq = self.running_sum_sq + (vol * vol)
            p_mean = p_sum / p_count
            p_raw_var = (p_sum_sq - ((p_sum * p_sum) / p_count)) / p_count
            p_std = math.sqrt(max(0.0, p_raw_var))
            p_z = ((vol - p_mean) / p_std) if p_std > 1e-6 else 0.0
            quality = FeatureQuality.WARMING_UP

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(p_z, 4),
            quality=quality,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
        )

    def reset(self) -> None:
        self.buffer.clear()
        self.running_sum = 0.0
        self.running_sum_sq = 0.0
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None
