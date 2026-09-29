"""
Unit tests for Feature Data Quality and Error Protection in Tradego Analytics (Phase 4).

Tests:
- Incomplete volume degradation across indicators and flow features
- Warm-up threshold accounting (FeatureQuality.WARMING_UP -> VALID)
- Zero-division and flat-market protections
- Missing/invalid input resilience
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.indicators.momentum import StreamingRSI
from services.analytics.indicators.moving_averages import StreamingEMA
from services.analytics.indicators.volatility import RollingBollingerBands
from services.analytics.indicators.volume import StreamingVWAP
from services.analytics.microstructure.order_book import BookImbalance, SpreadBps
from services.analytics.models import FeatureQuality
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.order_book import OrderBookState
from services.market_state.state import InstrumentState


def make_candle(
    dt: datetime,
    close: float,
    volume: float = 100.0,
    volume_quality: VolumeQuality = VolumeQuality.COMPLETE,
) -> Candle:
    iid = InstrumentId("QUALITY_TEST", Exchange.NSE, InstrumentType.EQUITY)
    return Candle(
        instrument_id=iid,
        timeframe=TF_1M,
        start_time=dt - timedelta(minutes=1),
        end_time=dt,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
        ticks=10,
        volume_quality=volume_quality,
        is_closed=True,
    )


class TestFeatureDataQuality(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_volume_quality_degradation(self):
        vwap = StreamingVWAP(timeframe=TF_1M)

        # Bar 1: Authoritative volume -> VALID
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = vwap.confirmed_update(make_candle(t1, 100.0, volume=100.0, volume_quality=VolumeQuality.COMPLETE))
        self.assertEqual(fv1.quality, FeatureQuality.VALID)

        # Bar 2: Partial volume (mid-bucket startup or reconnect) -> DEGRADED
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = vwap.confirmed_update(make_candle(t2, 105.0, volume=50.0, volume_quality=VolumeQuality.PARTIAL))
        self.assertEqual(fv2.quality, FeatureQuality.DEGRADED)

        # Bar 3: Unavailable volume -> INVALID
        t3 = self.base_time + timedelta(minutes=3)
        fv3 = vwap.confirmed_update(make_candle(t3, 110.0, volume=0.0, volume_quality=VolumeQuality.UNAVAILABLE))
        self.assertEqual(fv3.quality, FeatureQuality.INVALID)

    def test_warmup_threshold_accounting(self):
        period = 4
        ema = StreamingEMA(timeframe=TF_1M, period=period)

        for i in range(1, period):
            t = self.base_time + timedelta(minutes=i)
            fv = ema.confirmed_update(make_candle(t, 100.0 + i))
            self.assertEqual(fv.quality, FeatureQuality.WARMING_UP)
            self.assertFalse(ema.is_ready)

        # Bar 4 reaches min_periods
        t4 = self.base_time + timedelta(minutes=period)
        fv4 = ema.confirmed_update(make_candle(t4, 100.0 + period))
        self.assertEqual(fv4.quality, FeatureQuality.VALID)
        self.assertTrue(ema.is_ready)

    def test_divide_by_zero_protection_spread_bps(self):
        sp = SpreadBps()
        iid = InstrumentId("DIV_TEST", Exchange.NSE, InstrumentType.EQUITY)
        state = InstrumentState(instrument_id=iid, provider="TEST", provider_symbol_id="T1", is_resolved=True)

        # Empty order book
        state.order_book = OrderBookState()
        snap_empty = state.create_snapshot()
        fv = sp.compute(snap_empty)
        self.assertEqual(fv.quality, FeatureQuality.INVALID)
        self.assertEqual(fv.value, 0.0)

    def test_divide_by_zero_protection_book_imbalance(self):
        bi = BookImbalance()
        iid = InstrumentId("DIV_TEST", Exchange.NSE, InstrumentType.EQUITY)
        state = InstrumentState(instrument_id=iid, provider="TEST", provider_symbol_id="T1", is_resolved=True)
        state.order_book = OrderBookState()
        snap_empty = state.create_snapshot()
        fv = bi.compute(snap_empty)
        self.assertEqual(fv.quality, FeatureQuality.VALID)
        self.assertEqual(fv.value, 0.0)


if __name__ == "__main__":
    unittest.main()
