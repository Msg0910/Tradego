"""
Unit tests for Volume and Open Interest Features in Tradego Analytics (Phase 4).

Tests:
- StreamingVWAP and basis point deviation
- VolumeQuality propagation into FeatureQuality (COMPLETE -> VALID, PARTIAL -> DEGRADED)
- VolumeZScore rolling anomaly detection
- OpenInterestQuadrant: Same-Timeframe Rule, 4-quadrant classification, None semantics preservation
- Dual-path preview immutability
"""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.analytics.indicators.open_interest import OpenInterestQuadrant
from services.analytics.indicators.volume import StreamingVWAP, VolumeZScore
from services.analytics.models import FeatureQuality
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_candle(
    dt: datetime,
    close: float,
    volume: float = 100.0,
    volume_quality: VolumeQuality = VolumeQuality.COMPLETE,
    oi: Optional[int] = None,
    vwap: Optional[float] = None,
    is_closed: bool = True,
) -> Candle:
    iid = InstrumentId("TEST_FUT", Exchange.NFO, InstrumentType.FUTURES, expiry=datetime(2026, 10, 29).date())
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
        close_oi=oi,
        vwap=vwap if vwap is not None else close,
        is_closed=is_closed,
    )


class TestVolumeAndOIFeatures(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_vwap_calculation_and_deviation(self):
        vwap = StreamingVWAP(timeframe=TF_1M)

        # Bar 1: vwap=100.0, vol=100 -> pv=10000, vol=100, vwap=100.0, close=100.0 -> dev=0 bps
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = vwap.confirmed_update(make_candle(t1, close=100.0, volume=100.0, vwap=100.0))
        self.assertEqual(fv1.value, 100.0)
        self.assertEqual(fv1.metadata["deviation_bps"], 0.0)
        self.assertEqual(fv1.quality, FeatureQuality.VALID)

        # Bar 2: vwap=105.0, vol=100 -> pv=10500, cum_pv=20500, cum_vol=200 -> vwap=102.50
        # close=104.55 -> dev = ((104.55 - 102.50) / 102.50) * 10000 = 200 bps
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = vwap.confirmed_update(make_candle(t2, close=104.55, volume=100.0, vwap=105.0))
        self.assertEqual(fv2.value, 102.50)
        self.assertEqual(fv2.metadata["deviation_bps"], 200.0)

    def test_vwap_volume_quality_propagation(self):
        vwap = StreamingVWAP(timeframe=TF_1M)

        # Bar 1: Partial volume (e.g. process started mid-bucket)
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = vwap.confirmed_update(make_candle(t1, close=100.0, volume=50.0, volume_quality=VolumeQuality.PARTIAL))
        self.assertEqual(fv1.quality, FeatureQuality.DEGRADED)

        # Bar 2: Complete volume -> but overall session has degraded history
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = vwap.confirmed_update(make_candle(t2, close=102.0, volume=100.0, volume_quality=VolumeQuality.COMPLETE))
        self.assertEqual(fv2.quality, FeatureQuality.DEGRADED)

    def test_vwap_preview_immutability(self):
        vwap = StreamingVWAP(timeframe=TF_1M)
        t1 = self.base_time + timedelta(minutes=1)
        vwap.confirmed_update(make_candle(t1, close=100.0, volume=100.0, vwap=100.0))

        confirmed_pv = vwap.cum_pv
        confirmed_vol = vwap.cum_vol

        # Preview with active forming candle
        t2 = self.base_time + timedelta(minutes=2)
        prev_fv = vwap.preview(make_candle(t2, close=110.0, volume=100.0, vwap=110.0, is_closed=False))

        self.assertFalse(prev_fv.is_confirmed)
        self.assertEqual(prev_fv.value, 105.0)  # (10000 + 11000) / 200 = 105.0

        # Verify persistent state unchanged
        self.assertEqual(vwap.cum_pv, confirmed_pv)
        self.assertEqual(vwap.cum_vol, confirmed_vol)

    def test_volume_zscore(self):
        zscore = VolumeZScore(timeframe=TF_1M, period=5)
        # 5 bars with volume=100.0
        for i in range(5):
            t = self.base_time + timedelta(minutes=i + 1)
            zscore.confirmed_update(make_candle(t, close=100.0, volume=100.0))

        # Bar 6: huge surge volume=500.0
        t6 = self.base_time + timedelta(minutes=6)
        fv6 = zscore.confirmed_update(make_candle(t6, close=105.0, volume=500.0))
        self.assertEqual(fv6.quality, FeatureQuality.VALID)
        self.assertGreater(fv6.value, 1.5)

    def test_open_interest_quadrant_classification(self):
        oi_quad = OpenInterestQuadrant(timeframe=TF_1M)

        # Bar 1: Base (P=100, OI=10000) -> Warming up
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = oi_quad.confirmed_update(make_candle(t1, close=100.0, oi=10000))
        self.assertEqual(fv1.quality, FeatureQuality.WARMING_UP)

        # Bar 2: Long Buildup (P=105 > 100, OI=11000 > 10000)
        t2 = self.base_time + timedelta(minutes=2)
        fv2 = oi_quad.confirmed_update(make_candle(t2, close=105.0, oi=11000))
        self.assertEqual(fv2.quality, FeatureQuality.VALID)
        self.assertEqual(fv2.value, 1.0)
        self.assertEqual(fv2.metadata["quadrant"], "LONG_BUILDUP")

        # Bar 3: Long Unwinding (P=102 < 105, OI=10500 < 11000)
        t3 = self.base_time + timedelta(minutes=3)
        fv3 = oi_quad.confirmed_update(make_candle(t3, close=102.0, oi=10500))
        self.assertEqual(fv3.value, -0.5)
        self.assertEqual(fv3.metadata["quadrant"], "LONG_UNWINDING")

        # Bar 4: Short Buildup (P=98 < 102, OI=12000 > 10500)
        t4 = self.base_time + timedelta(minutes=4)
        fv4 = oi_quad.confirmed_update(make_candle(t4, close=98.0, oi=12000))
        self.assertEqual(fv4.value, -1.0)
        self.assertEqual(fv4.metadata["quadrant"], "SHORT_BUILDUP")

        # Bar 5: Short Covering (P=101 > 98, OI=11500 < 12000)
        t5 = self.base_time + timedelta(minutes=5)
        fv5 = oi_quad.confirmed_update(make_candle(t5, close=101.0, oi=11500))
        self.assertEqual(fv5.value, 0.5)
        self.assertEqual(fv5.metadata["quadrant"], "SHORT_COVERING")

    def test_open_interest_unavailable_preserves_none(self):
        oi_quad = OpenInterestQuadrant(timeframe=TF_1M)

        # Equity with no OI
        t1 = self.base_time + timedelta(minutes=1)
        fv1 = oi_quad.confirmed_update(make_candle(t1, close=100.0, oi=None))
        self.assertIsNone(fv1.value)
        self.assertEqual(fv1.quality, FeatureQuality.INVALID)
        self.assertEqual(fv1.metadata["reason"], "OI_UNAVAILABLE")

    def test_open_interest_preview_immutability(self):
        oi_quad = OpenInterestQuadrant(timeframe=TF_1M)
        t1 = self.base_time + timedelta(minutes=1)
        t2 = self.base_time + timedelta(minutes=2)
        oi_quad.confirmed_update(make_candle(t1, close=100.0, oi=10000))
        oi_quad.confirmed_update(make_candle(t2, close=105.0, oi=11000))

        confirmed_close = oi_quad.prev_close
        confirmed_oi = oi_quad.prev_oi

        # Preview with active forming candle (P=108, OI=12000)
        t3 = self.base_time + timedelta(minutes=3)
        prev_fv = oi_quad.preview(make_candle(t3, close=108.0, oi=12000, is_closed=False))

        self.assertFalse(prev_fv.is_confirmed)
        self.assertEqual(prev_fv.metadata["quadrant"], "LONG_BUILDUP")

        # State check: confirmed state unchanged
        self.assertEqual(oi_quad.prev_close, confirmed_close)
        self.assertEqual(oi_quad.prev_oi, confirmed_oi)


if __name__ == "__main__":
    unittest.main()
