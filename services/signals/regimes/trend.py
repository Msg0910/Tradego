"""
Trend Regime Classifiers for Tradego Signal Intelligence (Phase 5).

Implements the deterministic reference regime:
- EMABasedTrendRegime:
    Bullish: EMA20 > EMA50 AND LTP > VWAP
    Bearish: EMA20 < EMA50 AND LTP < VWAP
    Otherwise: NEUTRAL
"""

from typing import Optional

from ..base import BaseRegimeClassifier
from ..context import StrategyContext


class EMABasedTrendRegime(BaseRegimeClassifier):
    """
    Deterministic trend classifier based on EMA crossover alignment and VWAP position.
    Bullish: EMA_fast > EMA_slow AND LTP > VWAP
    Bearish: EMA_fast < EMA_slow AND LTP < VWAP
    Otherwise: NEUTRAL
    """

    def __init__(
        self,
        fast_ema_id: str = "EMA_20_5M",
        slow_ema_id: str = "EMA_50_5M",
        vwap_id: str = "VWAP_5M",
        name: str = "EMA_VWAP_TREND",
    ) -> None:
        super().__init__(name)
        self.fast_ema_id = fast_ema_id
        self.slow_ema_id = slow_ema_id
        self.vwap_id = vwap_id

    def classify(self, context: StrategyContext) -> Optional[str]:
        fast_ema = context.get_feature_value(self.fast_ema_id)
        slow_ema = context.get_feature_value(self.slow_ema_id)
        vwap = context.get_feature_value(self.vwap_id)

        if fast_ema is None or slow_ema is None or vwap is None:
            return None

        ltp = context.market_state.ltp
        if ltp is None and context.active_candle is not None:
            ltp = context.active_candle.close

        if ltp is None:
            return None

        if fast_ema > slow_ema and ltp > vwap:
            return "BULLISH"
        elif fast_ema < slow_ema and ltp < vwap:
            return "BEARISH"
        return "NEUTRAL"
