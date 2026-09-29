"""
Pullback Setup Detectors for Tradego Signal Intelligence (Phase 5).

Implements:
- EMAVWAPPullbackSetup: Reference setup detecting pullbacks into the value zone between
  EMA20 and VWAP with balanced RSI (40 <= RSI <= 60) and deterministic setup_anchor_timestamp.
"""

from datetime import datetime
from typing import Optional

from ..base import BaseSetupDetector, SetupResult
from ..context import StrategyContext


class EMAVWAPPullbackSetup(BaseSetupDetector):
    """
    Reference Pullback Setup:
    1. Regime must be BULLISH or BEARISH.
    2. Price retraces into the value zone between EMA20 and VWAP.
    3. RSI is in balanced momentum territory (40 <= RSI <= 60).
    4. setup_anchor_timestamp is the timestamp of the confirmed 5M candle establishing the active setup.
    """

    def __init__(
        self,
        ema_id: str = "EMA_20_5M",
        vwap_id: str = "VWAP_5M",
        rsi_id: str = "RSI_14_5M",
        rsi_lower: float = 40.0,
        rsi_upper: float = 60.0,
        name: str = "EMA_VWAP_PULLBACK",
    ) -> None:
        super().__init__(name)
        self.ema_id = ema_id
        self.vwap_id = vwap_id
        self.rsi_id = rsi_id
        self.rsi_lower = rsi_lower
        self.rsi_upper = rsi_upper

    def evaluate(self, context: StrategyContext, regime: Optional[str] = None) -> SetupResult:
        default_inactive = SetupResult(
            is_active=False,
            direction=0,
            setup_anchor_timestamp=context.evaluation_timestamp,
        )

        if regime not in ("BULLISH", "BEARISH"):
            return default_inactive

        ema = context.get_feature_value(self.ema_id)
        vwap = context.get_feature_value(self.vwap_id)
        rsi = context.get_feature_value(self.rsi_id)

        if ema is None or vwap is None or rsi is None:
            return default_inactive

        # 1. RSI Condition: 40 <= RSI <= 60
        if not (self.rsi_lower <= rsi <= self.rsi_upper):
            return default_inactive

        candle = context.active_candle
        if candle is None:
            return default_inactive

        # Setup anchor is the timestamp of the confirmed 5M candle establishing the active setup
        setup_anchor_timestamp = candle.end_time

        zone_low = min(ema, vwap)
        zone_high = max(ema, vwap)

        # 2. Price Pullback into Value Zone [zone_low, zone_high]
        if regime == "BULLISH":
            # For a bullish pullback, price tests the support zone
            in_zone = (candle.low <= zone_high) and (candle.close >= zone_low * 0.998)
            if in_zone:
                # Suggested entry at close, stop loss below candle low
                risk = max(candle.close - candle.low, candle.close * 0.002)
                target = candle.close + (2.0 * risk)
                return SetupResult(
                    is_active=True,
                    direction=1,
                    setup_anchor_timestamp=setup_anchor_timestamp,
                    confidence=0.85,
                    suggested_entry_price=round(candle.close, 4),
                    suggested_stop_loss=round(candle.low, 4),
                    suggested_take_profit=round(target, 4),
                    metadata={
                        "zone_low": zone_low,
                        "zone_high": zone_high,
                        "rsi": rsi,
                    },
                )
        elif regime == "BEARISH":
            # For a bearish pullback, price rallies into the resistance zone
            in_zone = (candle.high >= zone_low) and (candle.close <= zone_high * 1.002)
            if in_zone:
                # Suggested entry at close, stop loss above candle high
                risk = max(candle.high - candle.close, candle.close * 0.002)
                target = candle.close - (2.0 * risk)
                return SetupResult(
                    is_active=True,
                    direction=-1,
                    setup_anchor_timestamp=setup_anchor_timestamp,
                    confidence=0.85,
                    suggested_entry_price=round(candle.close, 4),
                    suggested_stop_loss=round(candle.high, 4),
                    suggested_take_profit=round(target, 4),
                    metadata={
                        "zone_low": zone_low,
                        "zone_high": zone_high,
                        "rsi": rsi,
                    },
                )

        return default_inactive
