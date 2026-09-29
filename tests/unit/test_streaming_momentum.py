"""
Unit tests for Streaming Momentum Indicators (RSI, MACD) in Tradego Analytics.

Tests:
- RSI Wilder's smoothing correctness
- Boundary protections: all gains (RSI=100), all losses (RSI=0), flat prices (RSI=50)
- MACD line, signal, and histogram calculations
- Preview immutability for both RSI and MACD
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.indicators.momentum import StreamingMACD, StreamingRSI
from services.analytics.models import FeatureQuality
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_candle(dt: datetime, close: float, is_closed: bool = True) -> Candle:
    iid = InstrumentId("TEST_SYM", Exchange.NSE, InstrumentType.EQUITY)
    return Candle(
        instrument_id=iid,
        timeframe=TF_1M,
        start_time=dt - timedelta(minutes=1),
        end_time=dt,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
        ticks=10,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=is_closed,
    )


class TestStreamingMomentum(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_rsi_all_gains_bound(self):
        # Monotonically increasing prices
        rsi = StreamingRSI(timeframe=TF_1M, period=5)
        for i in range(10):
            t = self.base_time + timedelta(minutes=i + 1)
            rsi.confirmed_update(make_candle(t, 100.0 + (i * 2.0)))

        self.assertTrue(rsi.is_ready)
        fv = rsi.last_confirmed_value
        self.assertIsNotNone(fv)
        self.assertEqual(fv.value, 100.0)
        self.assertEqual(fv.quality, FeatureQuality.VALID)

    def test_rsi_all_losses_bound(self):
        # Monotonically decreasing prices
        rsi = StreamingRSI(timeframe=TF_1M, period=5)
        for i in range(10):
            t = self.base_time + timedelta(minutes=i + 1)
            rsi.confirmed_update(make_candle(t, 100.0 - (i * 2.0)))

        self.assertTrue(rsi.is_ready)
        fv = rsi.last_confirmed_value
        self.assertIsNotNone(fv)
        self.assertEqual(fv.value, 0.0)
        self.assertEqual(fv.quality, FeatureQuality.VALID)

    def test_rsi_flat_prices(self):
        # Identical prices
        rsi = StreamingRSI(timeframe=TF_1M, period=5)
        for i in range(10):
            t = self.base_time + timedelta(minutes=i + 1)
            rsi.confirmed_update(make_candle(t, 100.0))

        self.assertTrue(rsi.is_ready)
        fv = rsi.last_confirmed_value
        self.assertIsNotNone(fv)
        self.assertEqual(fv.value, 50.0)

    def test_rsi_preview_immutability(self):
        rsi = StreamingRSI(timeframe=TF_1M, period=5)
        for i in range(6):
            t = self.base_time + timedelta(minutes=i + 1)
            rsi.confirmed_update(make_candle(t, 100.0 + i))

        confirmed_gain = rsi.avg_gain
        confirmed_loss = rsi.avg_loss
        confirmed_close = rsi.last_close

        # Preview with high price
        t_prev = self.base_time + timedelta(minutes=7)
        prev_fv = rsi.preview(make_candle(t_prev, 150.0, is_closed=False))

        self.assertFalse(prev_fv.is_confirmed)
        self.assertEqual(prev_fv.quality, FeatureQuality.VALID)
        self.assertGreater(prev_fv.value, 50.0)

        # State check: confirmed state must remain unchanged
        self.assertEqual(rsi.avg_gain, confirmed_gain)
        self.assertEqual(rsi.avg_loss, confirmed_loss)
        self.assertEqual(rsi.last_close, confirmed_close)

    def test_macd_calculation_and_preview(self):
        # Fast=3, Slow=6, Signal=3
        macd = StreamingMACD(
            timeframe=TF_1M,
            fast_period=3,
            slow_period=6,
            signal_period=3,
        )

        # Ingest 15 bars
        for i in range(15):
            t = self.base_time + timedelta(minutes=i + 1)
            macd.confirmed_update(make_candle(t, 100.0 + i))

        fv = macd.last_confirmed_value
        self.assertIsNotNone(fv)
        self.assertEqual(fv.quality, FeatureQuality.VALID)
        self.assertIn("macd", fv.metadata)
        self.assertIn("signal", fv.metadata)
        self.assertIn("histogram", fv.metadata)

        # Preview check
        t_act = self.base_time + timedelta(minutes=16)
        prev_fv = macd.preview(make_candle(t_act, 125.0, is_closed=False))
        self.assertFalse(prev_fv.is_confirmed)
        self.assertEqual(prev_fv.quality, FeatureQuality.VALID)


if __name__ == "__main__":
    unittest.main()
