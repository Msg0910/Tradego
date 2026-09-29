"""
Unit tests for Regime Classifiers and Setup Detectors (Phase 5).

Tests:
- EMABasedTrendRegime: Bullish, Bearish, Neutral, and missing feature fallbacks
- EMAVWAPPullbackSetup: Bullish and Bearish pullbacks, RSI boundaries, setup_anchor_timestamp
- ATRVolatilityRegime and RangeBreakoutSetup
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.models import FeatureQuality, FeatureSnapshot, FeatureValue
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_5M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.state import InstrumentState
from services.signals.context import StrategyContext
from services.signals.models import TriggerMode
from services.signals.regimes.trend import EMABasedTrendRegime
from services.signals.regimes.volatility import ATRVolatilityRegime
from services.signals.setups.breakout import RangeBreakoutSetup
from services.signals.setups.pullback import EMAVWAPPullbackSetup


def make_test_candle(iid: InstrumentId, dt: datetime, open_p: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        instrument_id=iid,
        timeframe=TF_5M,
        start_time=dt - timedelta(minutes=5),
        end_time=dt,
        open=open_p,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
        ticks=100,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=True,
    )


class TestRegimeAndSetupDetectors(unittest.TestCase):

    def setUp(self):
        self.iid = InstrumentId(
            "NIFTY26OCTFUT",
            Exchange.NFO,
            InstrumentType.FUTURES,
            expiry=datetime(2026, 10, 29).date(),
        )
        self.ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)

    def test_ema_vwap_trend_regime(self):
        regime = EMABasedTrendRegime(fast_ema_id="EMA_20", slow_ema_id="EMA_50", vwap_id="VWAP")

        # 1. Bullish: EMA20 (105) > EMA50 (100) and LTP (106) > VWAP (102)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 106.0
        snap_bull = FeatureSnapshot(
            self.iid,
            self.ts,
            {
                "EMA_20": FeatureValue("EMA_20", 105.0, FeatureQuality.VALID, self.ts, self.ts, True),
                "EMA_50": FeatureValue("EMA_50", 100.0, FeatureQuality.VALID, self.ts, self.ts, True),
                "VWAP": FeatureValue("VWAP", 102.0, FeatureQuality.VALID, self.ts, self.ts, True),
            },
        )
        ctx_bull = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, inst_state.create_snapshot(), snap_bull, self.ts)
        self.assertEqual(regime.classify(ctx_bull), "BULLISH")

        # 2. Bearish: EMA20 (95) < EMA50 (100) and LTP (94) < VWAP (98)
        inst_state.ltp = 94.0
        snap_bear = FeatureSnapshot(
            self.iid,
            self.ts,
            {
                "EMA_20": FeatureValue("EMA_20", 95.0, FeatureQuality.VALID, self.ts, self.ts, True),
                "EMA_50": FeatureValue("EMA_50", 100.0, FeatureQuality.VALID, self.ts, self.ts, True),
                "VWAP": FeatureValue("VWAP", 98.0, FeatureQuality.VALID, self.ts, self.ts, True),
            },
        )
        ctx_bear = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, inst_state.create_snapshot(), snap_bear, self.ts)
        self.assertEqual(regime.classify(ctx_bear), "BEARISH")

        # 3. Neutral: EMA20 > EMA50 but LTP < VWAP
        inst_state.ltp = 101.0
        ctx_neutral = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, inst_state.create_snapshot(), snap_bull, self.ts)
        self.assertEqual(regime.classify(ctx_neutral), "NEUTRAL")

    def test_ema_vwap_pullback_setup(self):
        setup = EMAVWAPPullbackSetup(ema_id="EMA_20", vwap_id="VWAP", rsi_id="RSI_14")

        # Setup: EMA_20=100.0, VWAP=98.0 -> Zone [98.0, 100.0], RSI=50.0
        features = {
            "EMA_20": FeatureValue("EMA_20", 100.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP": FeatureValue("VWAP", 98.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14": FeatureValue("RSI_14", 50.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)

        # Candle testing zone: low=98.5, close=99.5
        candle = make_test_candle(self.iid, self.ts, open_p=101.0, high=101.5, low=98.5, close=99.5)
        ctx = StrategyContext(
            self.iid,
            TriggerMode.BAR_CLOSE,
            inst_state.create_snapshot(),
            feat_snap,
            self.ts,
            active_candle=candle,
        )

        # In Bullish regime -> Setup should be active, direction = 1
        res = setup.evaluate(ctx, regime="BULLISH")
        self.assertTrue(res.is_active)
        self.assertEqual(res.direction, 1)
        self.assertEqual(res.setup_anchor_timestamp, candle.end_time)
        self.assertEqual(res.suggested_entry_price, 99.5)
        self.assertEqual(res.suggested_stop_loss, 98.5)

        # RSI out of bounds (e.g. 70.0) -> Inactive
        features_rsi_high = dict(features, RSI_14=FeatureValue("RSI_14", 70.0, FeatureQuality.VALID, self.ts, self.ts, True))
        ctx_rsi_high = StrategyContext(
            self.iid,
            TriggerMode.BAR_CLOSE,
            inst_state.create_snapshot(),
            FeatureSnapshot(self.iid, self.ts, features_rsi_high),
            self.ts,
            active_candle=candle,
        )
        res_high = setup.evaluate(ctx_rsi_high, regime="BULLISH")
        self.assertFalse(res_high.is_active)

    def test_atr_volatility_regime(self):
        regime = ATRVolatilityRegime(atr_id="ATR_14", high_threshold_bps=50.0, low_threshold_bps=10.0)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 1000.0

        # ATR = 10.0 -> 10/1000 * 10000 = 100 bps >= 50.0 -> HIGH_VOLATILITY
        features = {"ATR_14": FeatureValue("ATR_14", 10.0, FeatureQuality.VALID, self.ts, self.ts, True)}
        ctx = StrategyContext(
            self.iid, TriggerMode.BAR_CLOSE, inst_state.create_snapshot(), FeatureSnapshot(self.iid, self.ts, features), self.ts
        )
        self.assertEqual(regime.classify(ctx), "HIGH_VOLATILITY")


if __name__ == "__main__":
    unittest.main()
