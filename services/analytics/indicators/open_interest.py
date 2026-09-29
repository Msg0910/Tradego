"""
Open Interest Analytics and Quadrant Classification for Tradego Analytics (Phase 4).

Enforces the Same-Timeframe Rule:
price_delta and oi_delta MUST originate from the exact same timeframe stream.
If OI is None, do not manufacture synthetic values (value=None, quality=INVALID).
"""

from typing import Optional

from services.candles.models import Candle
from services.candles.timeframe import TimeFrame
from ..base import BaseIndicator
from ..models import FeatureQuality, FeatureValue

# Numerical encoding for quadrant classifications
QUADRANT_ENCODING = {
    "LONG_BUILDUP": 1.0,
    "SHORT_BUILDUP": -1.0,
    "SHORT_COVERING": 0.5,
    "LONG_UNWINDING": -0.5,
    "NEUTRAL": 0.0,
}


class OpenInterestQuadrant(BaseIndicator):
    """
    Classifies institutional positioning into canonical OI quadrants:
    - Long Buildup: Price Up, OI Up (Bullish aggressive buyers)
    - Short Buildup: Price Down, OI Up (Bearish aggressive sellers)
    - Short Covering: Price Up, OI Down (Short squeeze / profit taking)
    - Long Unwinding: Price Down, OI Down (Long liquidation)

    Strictly adheres to the Same-Timeframe Rule and preserves None semantics.
    """

    def __init__(
        self,
        timeframe: TimeFrame,
        name: Optional[str] = None,
    ) -> None:
        indicator_name = name or "OI_QUADRANT"
        super().__init__(indicator_name, timeframe, min_periods=2)
        self.prev_close: Optional[float] = None
        self.prev_oi: Optional[int] = None

    def _classify_quadrant(
        self, price_delta: float, oi_delta: int
    ) -> str:
        if price_delta > 0 and oi_delta > 0:
            return "LONG_BUILDUP"
        elif price_delta < 0 and oi_delta > 0:
            return "SHORT_BUILDUP"
        elif price_delta > 0 and oi_delta < 0:
            return "SHORT_COVERING"
        elif price_delta < 0 and oi_delta < 0:
            return "LONG_UNWINDING"
        return "NEUTRAL"

    def confirmed_update(self, candle: Candle) -> FeatureValue:
        if not candle.is_closed:
            raise ValueError("confirmed_update requires a closed Candle (is_closed=True)")

        current_close = candle.close
        current_oi = candle.close_oi

        self.periods_observed += 1
        self.last_confirmed_timestamp = candle.end_time

        # Case 1: Instrument has no Open Interest (e.g. Cash Equity)
        if current_oi is None:
            fv = FeatureValue(
                feature_id=self.feature_id,
                value=None,
                quality=FeatureQuality.INVALID,
                observation_timestamp=candle.end_time,
                availability_timestamp=candle.end_time,
                is_confirmed=True,
                metadata={"reason": "OI_UNAVAILABLE"},
            )
            self.last_confirmed_value = fv
            return fv

        # Case 2: First bar with OI observed
        if self.prev_close is None or self.prev_oi is None:
            self.prev_close = current_close
            self.prev_oi = current_oi
            fv = FeatureValue(
                feature_id=self.feature_id,
                value=0.0,
                quality=FeatureQuality.WARMING_UP,
                observation_timestamp=candle.end_time,
                availability_timestamp=candle.end_time,
                is_confirmed=True,
                metadata={"quadrant": "NEUTRAL", "price_delta": 0.0, "oi_delta": 0, "oi": current_oi},
            )
            self.last_confirmed_value = fv
            return fv

        # Case 3: Standard consecutive bar evaluation
        price_delta = current_close - self.prev_close
        oi_delta = current_oi - self.prev_oi
        quadrant = self._classify_quadrant(price_delta, oi_delta)
        encoded_val = QUADRANT_ENCODING[quadrant]

        self.prev_close = current_close
        self.prev_oi = current_oi

        metadata = {
            "quadrant": quadrant,
            "price_delta": round(price_delta, 4),
            "oi_delta": oi_delta,
            "oi": current_oi,
        }

        fv = FeatureValue(
            feature_id=self.feature_id,
            value=encoded_val,
            quality=FeatureQuality.VALID,
            observation_timestamp=candle.end_time,
            availability_timestamp=candle.end_time,
            is_confirmed=True,
            metadata=metadata,
        )
        self.last_confirmed_value = fv
        return fv

    def preview(self, active_candle: Candle) -> FeatureValue:
        """
        Ephemerally evaluates OI quadrant using active forming candle.
        Does not mutate prev_close or prev_oi.
        """
        active_close = active_candle.close
        active_oi = active_candle.close_oi

        if active_oi is None or self.prev_oi is None or self.prev_close is None:
            quality = FeatureQuality.INVALID if active_oi is None else FeatureQuality.WARMING_UP
            return FeatureValue(
                feature_id=self.feature_id,
                value=None if active_oi is None else 0.0,
                quality=quality,
                observation_timestamp=active_candle.end_time,
                availability_timestamp=active_candle.end_time,
                is_confirmed=False,
                metadata={"quadrant": "NEUTRAL", "reason": "INSUFFICIENT_HISTORY_OR_NO_OI"},
            )

        price_delta = active_close - self.prev_close
        oi_delta = active_oi - self.prev_oi
        quadrant = self._classify_quadrant(price_delta, oi_delta)
        encoded_val = QUADRANT_ENCODING[quadrant]

        metadata = {
            "quadrant": quadrant,
            "price_delta": round(price_delta, 4),
            "oi_delta": oi_delta,
            "oi": active_oi,
        }

        return FeatureValue(
            feature_id=self.feature_id,
            value=encoded_val,
            quality=FeatureQuality.VALID,
            observation_timestamp=active_candle.end_time,
            availability_timestamp=active_candle.end_time,
            is_confirmed=False,
            metadata=metadata,
        )

    def reset(self) -> None:
        self.prev_close = None
        self.prev_oi = None
        self.periods_observed = 0
        self.last_confirmed_timestamp = None
        self.last_confirmed_value = None
