"""
Unit tests for Streaming Moving Averages (EMA, SMA) in Tradego Analytics.

Tests:
- Mathematical reference correctness
- Warm-up tracking (is_ready, FeatureQuality.WARMING_UP -> VALID)
- Dual-Path contract: confirmed_update advances state, preview leaves state unmutated
- Sliding window eviction in SMA
- Reset semantics
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.indicators.moving_averages import StreamingEMA, StreamingSMA
from services.analytics.models import FeatureQuality
from services.candles.models import Candle
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_candle(dt: datetime, close: float, is_closed: bool = True) -> Candle:
    from services.candles.models import VolumeQuality
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


class TestStreamingMovingAverages(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_ema_mathematical_correctness(self):
        # Period = 3, alpha = 2 / (3 + 1) = 0.5
        ema = StreamingEMA(timeframe=TF_1M, period=3)
        prices = [10.0, 20.0, 30.0, 40.0]

        # Bar 1: close=10.0 -> EMA=10.0 (warming up)
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = ema.confirmed_update(make_candle(t1, 10.0))
        self.assertEqual(fv1.value, 10.0)
        self.assertEqual(fv1.quality, FeatureQuality.WARMING_UP)
        self.assertFalse(ema.is_ready)

        # Bar 2: close=20.0 -> EMA = 0.5*20 + 0.5*10 = 15.0 (warming up)
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = ema.confirmed_update(make_candle(t2, 20.0))
        self.assertEqual(fv2.value, 15.0)
        self.assertEqual(fv2.quality, FeatureQuality.WARMING_UP)

        # Bar 3: close=30.0 -> EMA = 0.5*30 + 0.5*15 = 22.5 (VALID, ready)
        t3 = self.base_time + timedelta(minutes=3)
        fv3 = ema.confirmed_update(make_candle(t3, 30.0))
        self.assertEqual(fv3.value, 22.5)
        self.assertEqual(fv3.quality, FeatureQuality.VALID)
        self.assertTrue(ema.is_ready)

        # Bar 4: close=40.0 -> EMA = 0.5*40 + 0.5*22.5 = 31.25
        t4 = self.base_time + timedelta(minutes=4)
        fv4 = ema.confirmed_update(make_candle(t4, 40.0))
        self.assertEqual(fv4.value, 31.25)
        self.assertEqual(fv4.quality, FeatureQuality.VALID)

    def test_ema_preview_immutability(self):
        ema = StreamingEMA(timeframe=TF_1M, period=3)
        # Advance with 2 confirmed bars
        ema.confirmed_update(make_candle(self.base_time + timedelta(minutes=1), 10.0))
        ema.confirmed_update(make_candle(self.base_time + timedelta(minutes=2), 20.0))

        confirmed_state = ema.prev_ema
        self.assertEqual(confirmed_state, 15.0)
        self.assertEqual(ema.periods_observed, 2)

        # Preview with active forming bar (close = 30.0)
        active_candle = make_candle(self.base_time + timedelta(minutes=3), 30.0, is_closed=False)
        preview_fv = ema.preview(active_candle)

        # Preview value: 0.5*30 + 0.5*15 = 22.5
        self.assertEqual(preview_fv.value, 22.5)
        self.assertFalse(preview_fv.is_confirmed)

        # Verify persistent state was NOT mutated
        self.assertEqual(ema.prev_ema, confirmed_state)
        self.assertEqual(ema.periods_observed, 2)

    def test_sma_mathematical_correctness_and_eviction(self):
        # Period = 3
        sma = StreamingSMA(timeframe=TF_1M, period=3)
        prices = [10.0, 20.0, 30.0, 40.0, 50.0]

        # Bar 1: [10] -> sum=10, mean=10.0
        fv1 = sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=1), 10.0))
        self.assertEqual(fv1.value, 10.0)
        self.assertEqual(fv1.quality, FeatureQuality.WARMING_UP)

        # Bar 2: [10, 20] -> sum=30, mean=15.0
        fv2 = sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=2), 20.0))
        self.assertEqual(fv2.value, 15.0)
        self.assertEqual(fv2.quality, FeatureQuality.WARMING_UP)

        # Bar 3: [10, 20, 30] -> sum=60, mean=20.0 (ready)
        fv3 = sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=3), 30.0))
        self.assertEqual(fv3.value, 20.0)
        self.assertEqual(fv3.quality, FeatureQuality.VALID)
        self.assertTrue(sma.is_ready)

        # Bar 4: evicts 10 -> [20, 30, 40] -> sum=90, mean=30.0
        fv4 = sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=4), 40.0))
        self.assertEqual(fv4.value, 30.0)
        self.assertEqual(fv4.quality, FeatureQuality.VALID)

        # Bar 5: evicts 20 -> [30, 40, 50] -> sum=120, mean=40.0
        fv5 = sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=5), 50.0))
        self.assertEqual(fv5.value, 40.0)

    def test_sma_preview_immutability(self):
        sma = StreamingSMA(timeframe=TF_1M, period=3)
        # Populate with [10, 20, 30] (sum=60)
        for i, p in enumerate([10.0, 20.0, 30.0]):
            sma.confirmed_update(make_candle(self.base_time + timedelta(minutes=i + 1), p))

        self.assertEqual(len(sma.buffer), 3)
        self.assertEqual(sma.running_sum, 60.0)

        # Preview with active forming candle close = 40.0
        # Should ephemerally evict 10.0 -> (60 - 10 + 40) / 3 = 30.0
        active_candle = make_candle(self.base_time + timedelta(minutes=4), 40.0, is_closed=False)
        prev_fv = sma.preview(active_candle)

        self.assertEqual(prev_fv.value, 30.0)
        self.assertFalse(prev_fv.is_confirmed)

        # Verify persistent state unchanged
        self.assertEqual(len(sma.buffer), 3)
        self.assertEqual(sma.buffer[0], 10.0)
        self.assertEqual(sma.running_sum, 60.0)


if __name__ == "__main__":
    unittest.main()
