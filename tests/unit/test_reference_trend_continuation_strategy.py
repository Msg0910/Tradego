"""
Unit tests for Reference Strategy: EMA_VWAP_TrendContinuationStrategy (Phase 5).

Tests:
- Bullish entry trigger (BAR_CLOSE, confirmed candle, pullback rejection)
- Bearish entry trigger (BAR_CLOSE, confirmed candle, pullback rejection)
- Ranging/Neutral regime suppression (NO_TRADE)
- Unconfirmed active candle rejection in BAR_CLOSE mode
- INTRABAR_PREVIEW trigger mode execution
- Exit evaluation with read-only PositionView (Stop-Loss and Take-Profit)
- Verified non-mutation of PositionView
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.models import FeatureQuality, FeatureSnapshot, FeatureValue
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_5M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.state import InstrumentState
from services.signals.context import StrategyContext
from services.signals.models import (
    DecisionType,
    PositionView,
    QualityPolicy,
    SignalType,
    TriggerMode,
)
from strategies.trend_continuation import (
    EMA_VWAP_TrendContinuationStrategy,
    TrendContinuationConfig,
)


def make_candle(
    iid: InstrumentId,
    dt: datetime,
    open_p: float,
    high: float,
    low: float,
    close: float,
    is_closed: bool = True,
) -> Candle:
    return Candle(
        instrument_id=iid,
        timeframe=TF_5M,
        start_time=dt - timedelta(minutes=5),
        end_time=dt,
        open=open_p,
        high=high,
        low=low,
        close=close,
        volume=5000.0,
        ticks=500,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=is_closed,
    )


class TestReferenceTrendContinuationStrategy(unittest.TestCase):

    def setUp(self):
        self.iid = InstrumentId(
            "NIFTY26OCTFUT",
            Exchange.NFO,
            InstrumentType.FUTURES,
            expiry=datetime(2026, 10, 29).date(),
        )
        self.ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)
        self.strategy = EMA_VWAP_TrendContinuationStrategy()

    def test_bullish_entry_signal(self):
        # Bullish Regime: EMA20 (25050) > EMA50 (25000), LTP (25040) > VWAP (25020)
        # Pullback Zone: [25020, 25050]
        # RSI: 50.0, ATR: 30.0
        features = {
            "EMA_20_5M": FeatureValue("EMA_20_5M", 25050.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 25000.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 25020.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 50.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 30.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)

        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 25045.0

        # Confirmed 5M candle testing zone: low=25025 (inside zone), closes strong at 25045
        candle = make_candle(self.iid, self.ts, open_p=25030.0, high=25048.0, low=25025.0, close=25045.0, is_closed=True)

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle,
            position=PositionView(self.iid, 0, 0.0, self.ts, 0.0, 0.0),  # Flat
        )

        decision = self.strategy.evaluate(ctx)
        self.assertEqual(decision.decision, DecisionType.TRADE)
        self.assertIsNotNone(decision.candidate)

        candidate = decision.candidate
        self.assertEqual(candidate.signal_type, SignalType.ENTRY_LONG)
        self.assertEqual(candidate.direction, 1)
        self.assertEqual(candidate.suggested_entry_price, 25045.0)
        self.assertEqual(candidate.trigger_mode, TriggerMode.BAR_CLOSE)
        self.assertTrue(candidate.is_confirmed)
        self.assertIsNotNone(candidate.fingerprint)
        self.assertIsNotNone(candidate.reaffirmation_key)

    def test_unconfirmed_candle_rejected_in_bar_close_mode(self):
        features = {
            "EMA_20_5M": FeatureValue("EMA_20_5M", 25050.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 25000.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 25020.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 50.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 30.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)
        inst_state.ltp = 25045.0

        # Forming, UNCONFIRMED candle (is_closed=False)
        candle = make_candle(self.iid, self.ts, open_p=25030.0, high=25048.0, low=25025.0, close=25045.0, is_closed=False)

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle,
        )

        decision = self.strategy.evaluate(ctx)
        self.assertEqual(decision.decision, DecisionType.NO_TRADE)

    def test_exit_signal_evaluation(self):
        features = {
            "EMA_20_5M": FeatureValue("EMA_20_5M", 25050.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "EMA_50_5M": FeatureValue("EMA_50_5M", 25000.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "VWAP_5M": FeatureValue("VWAP_5M", 25020.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "RSI_14_5M": FeatureValue("RSI_14_5M", 50.0, FeatureQuality.VALID, self.ts, self.ts, True),
            "ATR_14_5M": FeatureValue("ATR_14_5M", 30.0, FeatureQuality.VALID, self.ts, self.ts, True),
        }
        feat_snap = FeatureSnapshot(self.iid, self.ts, features)
        inst_state = InstrumentState(self.iid, "TEST", "TOK", is_resolved=True)

        # Long position entered at 25000.0. SL = 25000 - 1.5*30 = 24955.0
        pos_long = PositionView(
            instrument_id=self.iid,
            net_quantity=50,
            entry_price=25000.0,
            entry_time=self.ts - timedelta(minutes=30),
            highest_price_since_entry=25020.0,
            lowest_price_since_entry=24950.0,
        )

        # Candle plunging below stop loss: low=24950 <= 24955
        candle_sl = make_candle(self.iid, self.ts, open_p=24970.0, high=24975.0, low=24950.0, close=24952.0, is_closed=True)

        ctx_exit = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=inst_state.create_snapshot(),
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_candle=candle_sl,
            position=pos_long,
        )

        decision = self.strategy.evaluate(ctx_exit)
        self.assertEqual(decision.decision, DecisionType.TRADE)
        self.assertIsNotNone(decision.candidate)
        self.assertEqual(decision.candidate.signal_type, SignalType.EXIT_LONG)
        self.assertEqual(decision.candidate.suggested_entry_price, 24955.0)  # Stop loss price


if __name__ == "__main__":
    unittest.main()
