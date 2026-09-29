"""
Volatility Regime Classifiers for Tradego Signal Intelligence (Phase 5).

Implements:
- ATRVolatilityRegime: Classifies market volatility based on ATR thresholds or ATR/Price ratios.
"""

from typing import Optional

from ..base import BaseRegimeClassifier
from ..context import StrategyContext


class ATRVolatilityRegime(BaseRegimeClassifier):
    """
    Classifies market volatility regime based on ATR in basis points or absolute thresholds.
    HIGH_VOLATILITY, NORMAL_VOLATILITY, LOW_VOLATILITY
    """

    def __init__(
        self,
        atr_id: str = "ATR_14_5M",
        high_threshold_bps: float = 80.0,
        low_threshold_bps: float = 20.0,
        name: str = "ATR_VOLATILITY",
    ) -> None:
        super().__init__(name)
        self.atr_id = atr_id
        self.high_threshold_bps = high_threshold_bps
        self.low_threshold_bps = low_threshold_bps

    def classify(self, context: StrategyContext) -> Optional[str]:
        atr = context.get_feature_value(self.atr_id)
        if atr is None or atr <= 0.0:
            return None

        price = context.market_state.ltp
        if price is None and context.active_candle is not None:
            price = context.active_candle.close

        if price is None or price <= 0.0:
            return None

        atr_bps = (atr / price) * 10000.0
        if atr_bps >= self.high_threshold_bps:
            return "HIGH_VOLATILITY"
        elif atr_bps <= self.low_threshold_bps:
            return "LOW_VOLATILITY"
        return "NORMAL_VOLATILITY"
