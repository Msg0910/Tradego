"""
Unit tests for Streaming Volatility Indicators (ATR, RollingBollingerBands) in Tradego Analytics.

Tests:
- ATR True Range calculation and Wilder smoothing
- RollingBollingerBands sliding window sum of squares
- Numerical stability safeguard against floating-point cancellation (max(0.0, raw_var))
- Validation against NumPy reference calculations
- Dual-path preview immutability
"""

import math
import statistics
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.analytics.indicators.volatility import RollingBollingerBands, StreamingATR
from services.analytics.models import FeatureQuality
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_candle(
    dt: datetime,
    close: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    is_closed: bool = True,
) -> Candle:
    iid = InstrumentId("TEST_SYM", Exchange.NSE, InstrumentType.EQUITY)
    h = high if high is not None else close + 1.0
    l = low if low is not None else close - 1.0
    return Candle(
        instrument_id=iid,
        timeframe=TF_1M,
        start_time=dt - timedelta(minutes=1),
        end_time=dt,
        open=close,
        high=h,
        low=l,
        close=close,
        volume=100.0,
        ticks=10,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=is_closed,
    )


class TestStreamingVolatility(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_atr_mathematical_correctness(self):
        atr = StreamingATR(timeframe=TF_1M, period=3)

        # Bar 1: H=12, L=8, C=10 -> TR = 4.0
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = atr.confirmed_update(make_candle(t1, close=10.0, high=12.0, low=8.0))
        self.assertEqual(fv1.value, 4.0)
        self.assertEqual(fv1.quality, FeatureQuality.WARMING_UP)

        # Bar 2: PrevClose=10, H=15, L=9, C=14 -> TR = max(6, |15-10|=5, |9-10|=1) = 6.0
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = atr.confirmed_update(make_candle(t2, close=14.0, high=15.0, low=9.0))
        self.assertEqual(fv2.value, 5.0)  # (4+6)/2

        # Bar 3: PrevClose=14, H=18, L=13, C=17 -> TR = max(5, |18-14|=4, |13-14|=1) = 5.0
        t3 = self.base_time + timedelta(minutes=3)
        fv3 = atr.confirmed_update(make_candle(t3, close=17.0, high=18.0, low=13.0))
        self.assertEqual(fv3.value, 5.0)  # (4+6+5)/3 = 5.0
        self.assertEqual(fv3.quality, FeatureQuality.VALID)

    def test_bollinger_bands_numpy_reference_validation(self):
        period = 5
        k = 2.0
        bb = RollingBollingerBands(timeframe=TF_1M, period=period, k=k)

        prices = [100.0, 102.5, 101.0, 104.0, 103.5, 106.0, 105.0]

        for i, p in enumerate(prices):
            t = self.base_time + timedelta(minutes=i + 1)
            bb.confirmed_update(make_candle(t, close=p))

            if i + 1 >= period:
                # Reference calculation
                window = prices[i + 1 - period : i + 1]
                ref_mean = sum(window) / len(window)
                ref_std = math.sqrt(sum((x - ref_mean)**2 for x in window) / len(window))
                ref_upper = ref_mean + (k * ref_std)
                ref_lower = ref_mean - (k * ref_std)

                fv = bb.last_confirmed_value
                self.assertIsNotNone(fv)
                self.assertAlmostEqual(fv.value, ref_mean, places=3)
                self.assertAlmostEqual(fv.metadata["upper"], ref_upper, places=3)
                self.assertAlmostEqual(fv.metadata["lower"], ref_lower, places=3)
                self.assertAlmostEqual(fv.metadata["std"], ref_std, places=3)

    def test_bollinger_numerical_stability_constant_prices(self):
        """
        Tests against floating point catastrophic cancellation when prices are identical.
        raw_var must not go negative and crash math.sqrt().
        """
        bb = RollingBollingerBands(timeframe=TF_1M, period=10)
        constant_price = 1234.5678

        for i in range(20):
            t = self.base_time + timedelta(minutes=i + 1)
            fv = bb.confirmed_update(make_candle(t, close=constant_price))

        fv = bb.last_confirmed_value
        self.assertIsNotNone(fv)
        self.assertEqual(fv.value, round(constant_price, 4))
        self.assertEqual(fv.metadata["std"], 0.0)
        self.assertEqual(fv.metadata["upper"], round(constant_price, 4))
        self.assertEqual(fv.metadata["lower"], round(constant_price, 4))

    def test_bollinger_preview_immutability(self):
        bb = RollingBollingerBands(timeframe=TF_1M, period=5)
        for i, p in enumerate([10.0, 20.0, 30.0, 40.0, 50.0]):
            bb.confirmed_update(make_candle(self.base_time + timedelta(minutes=i + 1), close=p))

        confirmed_sum = bb.running_sum
        confirmed_sum_sq = bb.running_sum_sq
        confirmed_buffer = list(bb.buffer)

        # Preview with active forming candle close = 100.0
        t_prev = self.base_time + timedelta(minutes=6)
        active_candle = make_candle(t_prev, close=100.0, is_closed=False)
        prev_fv = bb.preview(active_candle)

        self.assertFalse(prev_fv.is_confirmed)
        self.assertEqual(prev_fv.quality, FeatureQuality.VALID)

        # Buffer [20, 30, 40, 50, 100] -> mean = 240 / 5 = 48.0
        self.assertEqual(prev_fv.value, 48.0)

        # Verify persistent state unchanged
        self.assertEqual(bb.running_sum, confirmed_sum)
        self.assertEqual(bb.running_sum_sq, confirmed_sum_sq)
        self.assertEqual(list(bb.buffer), confirmed_buffer)


if __name__ == "__main__":
    unittest.main()
