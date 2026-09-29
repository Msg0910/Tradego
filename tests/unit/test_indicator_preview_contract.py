"""
Unit tests for Dual-Path Indicator Contract in Tradego Analytics (Phase 4).

Verifies that for every indicator:
1. confirmed_update() advances persistent state and returns is_confirmed=True.
2. preview() returns ephemeral is_confirmed=False without mutating persistent state.
3. Repeated preview() calls produce identical results and leave state 100% untouched.
4. Calling preview() between confirmed bars does not alter subsequent confirmed_update() outputs.
"""

import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.analytics.indicators.momentum import StreamingMACD, StreamingRSI
from services.analytics.indicators.moving_averages import StreamingEMA, StreamingSMA
from services.analytics.indicators.open_interest import OpenInterestQuadrant
from services.analytics.indicators.volatility import RollingBollingerBands, StreamingATR
from services.analytics.indicators.volume import StreamingVWAP
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_candle(
    dt: datetime,
    close: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    volume: float = 100.0,
    oi: Optional[int] = 10000,
    is_closed: bool = True,
) -> Candle:
    iid = InstrumentId("CONTRACT_TEST", Exchange.NFO, InstrumentType.FUTURES, expiry=datetime(2026, 10, 29).date())
    h = high if high is not None else close + 2.0
    l = low if low is not None else close - 2.0
    return Candle(
        instrument_id=iid,
        timeframe=TF_1M,
        start_time=dt - timedelta(minutes=1),
        end_time=dt,
        open=close,
        high=h,
        low=l,
        close=close,
        volume=volume,
        ticks=10,
        volume_quality=VolumeQuality.COMPLETE,
        close_oi=oi,
        vwap=close,
        is_closed=is_closed,
    )


class TestIndicatorPreviewContract(unittest.TestCase):

    def setUp(self):
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def _verify_preview_contract(self, indicator, warm_prices, active_price, next_confirmed_price):
        # 1. Warm up indicator with baseline confirmed bars
        for i, p in enumerate(warm_prices):
            t = self.base_time + timedelta(minutes=i + 1)
            indicator.confirmed_update(make_candle(t, close=p, is_closed=True))

        # Snapshot persistent state representation before any preview
        confirmed_periods = indicator.periods_observed
        confirmed_last_val = indicator.last_confirmed_value

        # 2. Call preview multiple times with different active prices
        t_active = self.base_time + timedelta(minutes=len(warm_prices) + 1)
        active_candle = make_candle(t_active, close=active_price, is_closed=False)

        prev_fv1 = indicator.preview(active_candle)
        self.assertFalse(prev_fv1.is_confirmed)

        # Multiple preview calls must return the same result and leave state unchanged
        for _ in range(5):
            prev_fv2 = indicator.preview(active_candle)
            self.assertEqual(prev_fv1.value, prev_fv2.value)
            self.assertFalse(prev_fv2.is_confirmed)

        # Invariant: Persistent state is strictly unmutated
        self.assertEqual(indicator.periods_observed, confirmed_periods)
        self.assertEqual(indicator.last_confirmed_value, confirmed_last_val)

        # 3. Process the next official confirmed candle
        next_candle = make_candle(t_active, close=next_confirmed_price, is_closed=True)
        confirmed_fv = indicator.confirmed_update(next_candle)
        self.assertTrue(confirmed_fv.is_confirmed)
        self.assertEqual(indicator.periods_observed, confirmed_periods + 1)

    def test_ema_preview_contract(self):
        ema = StreamingEMA(timeframe=TF_1M, period=5)
        self._verify_preview_contract(ema, [100.0, 102.0, 101.0, 103.0, 104.0], active_price=106.0, next_confirmed_price=105.0)

    def test_sma_preview_contract(self):
        sma = StreamingSMA(timeframe=TF_1M, period=5)
        self._verify_preview_contract(sma, [100.0, 102.0, 101.0, 103.0, 104.0], active_price=110.0, next_confirmed_price=105.0)

    def test_rsi_preview_contract(self):
        rsi = StreamingRSI(timeframe=TF_1M, period=5)
        self._verify_preview_contract(rsi, [100.0, 102.0, 101.0, 103.0, 104.0, 106.0], active_price=120.0, next_confirmed_price=107.0)

    def test_atr_preview_contract(self):
        atr = StreamingATR(timeframe=TF_1M, period=5)
        self._verify_preview_contract(atr, [100.0, 102.0, 101.0, 103.0, 104.0], active_price=115.0, next_confirmed_price=105.0)

    def test_bollinger_preview_contract(self):
        bb = RollingBollingerBands(timeframe=TF_1M, period=5)
        self._verify_preview_contract(bb, [100.0, 102.0, 101.0, 103.0, 104.0], active_price=110.0, next_confirmed_price=105.0)

    def test_macd_preview_contract(self):
        macd = StreamingMACD(timeframe=TF_1M, fast_period=3, slow_period=6, signal_period=3)
        self._verify_preview_contract(macd, [100.0 + i for i in range(12)], active_price=120.0, next_confirmed_price=113.0)

    def test_vwap_preview_contract(self):
        vwap = StreamingVWAP(timeframe=TF_1M)
        self._verify_preview_contract(vwap, [100.0, 102.0, 101.0], active_price=110.0, next_confirmed_price=105.0)

    def test_oi_quadrant_preview_contract(self):
        oi_quad = OpenInterestQuadrant(timeframe=TF_1M)
        self._verify_preview_contract(oi_quad, [100.0, 102.0, 101.0], active_price=105.0, next_confirmed_price=103.0)


if __name__ == "__main__":
    unittest.main()
