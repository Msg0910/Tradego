"""
Breakout Setup Detectors for Tradego Signal Intelligence (Phase 5).

Implements:
- RangeBreakoutSetup: Detects breakouts beyond Bollinger Bands with expanding volume.
"""

from typing import Optional

from ..base import BaseSetupDetector, SetupResult
from ..context import StrategyContext


class RangeBreakoutSetup(BaseSetupDetector):
    """
    Detects range breakouts closing outside Bollinger Bands with volume surge.
    """

    def __init__(
        self,
        bb_id: str = "BB_20_2",
        vol_z_id: str = "VOL_ZSCORE_20",
        min_zscore: float = 1.0,
        name: str = "RANGE_BREAKOUT",
    ) -> None:
        super().__init__(name)
        self.bb_id = bb_id
        self.vol_z_id = vol_z_id
        self.min_zscore = min_zscore

    def evaluate(self, context: StrategyContext, regime: Optional[str] = None) -> SetupResult:
        default_inactive = SetupResult(
            is_active=False,
            direction=0,
            setup_anchor_timestamp=context.evaluation_timestamp,
        )

        bb_fv = context.features.get(self.bb_id)
        if bb_fv is None or bb_fv.metadata is None:
            return default_inactive

        upper = bb_fv.metadata.get("upper")
        lower = bb_fv.metadata.get("lower")
        if upper is None or lower is None:
            return default_inactive

        vol_z = context.get_feature_value(self.vol_z_id, default=0.0) or 0.0
        if vol_z < self.min_zscore:
            return default_inactive

        candle = context.active_candle
        if candle is None:
            return default_inactive

        if candle.close > upper:
            risk = candle.close - lower
            return SetupResult(
                is_active=True,
                direction=1,
                setup_anchor_timestamp=candle.end_time,
                confidence=0.80,
                suggested_entry_price=round(candle.close, 4),
                suggested_stop_loss=round(lower, 4),
                suggested_take_profit=round(candle.close + (2.0 * risk), 4),
                metadata={"vol_zscore": vol_z, "upper": upper},
            )
        elif candle.close < lower:
            risk = upper - candle.close
            return SetupResult(
                is_active=True,
                direction=-1,
                setup_anchor_timestamp=candle.end_time,
                confidence=0.80,
                suggested_entry_price=round(candle.close, 4),
                suggested_stop_loss=round(upper, 4),
                suggested_take_profit=round(candle.close - (2.0 * risk), 4),
                metadata={"vol_zscore": vol_z, "lower": lower},
            )

        return default_inactive
