"""
Unit tests for InstrumentCandleSeries: Two-Path Architecture, Multi-Timeframe Rollup,
Active Previews, and Bounded Ring Buffers.
"""

import unittest
from datetime import datetime, timedelta

from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.models import VolumeQuality
from services.candles.series import InstrumentCandleSeries
from services.candles.timeframe import (
    TF_1D,
    TF_1H,
    TF_1M,
    TF_1S,
    TF_3M,
    TF_5M,
    TF_15M,
)
from services.candles.volume import IncrementalVolumePolicy
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_tick(dt: datetime, ltp: float, vol: float = 10.0, oi: int = 1000) -> MarketEvent:
    return MarketEvent(
        provider="TEST",
        provider_symbol_id="TOKEN_TCS",
        exchange_timestamp=dt,
        ltp=ltp,
        tick_volume=vol,
        oi=oi,
        local_receive_timestamp=100.0,
    )


class TestCandleSeries(unittest.TestCase):

    def setUp(self):
        self.cal = IndianMarketCalendar()
        self.inst_id = InstrumentId("TCS", Exchange.NSE, InstrumentType.EQUITY)
        self.policy = IncrementalVolumePolicy()
        self.series = InstrumentCandleSeries(
            instrument_id=self.inst_id,
            calendar=self.cal,
            volume_policy=self.policy,
        )

    def test_base_1s_and_1m_builders(self):
        # Two ticks in same second
        t0 = datetime(2026, 9, 14, 9, 15, 0, 100000, tzinfo=INDIA_TZ)
        t1 = datetime(2026, 9, 14, 9, 15, 0, 500000, tzinfo=INDIA_TZ)
        self.series.process_tick(make_tick(t0, ltp=100.0, vol=5.0))
        self.series.process_tick(make_tick(t1, ltp=102.0, vol=15.0))

        # Active 1S candle
        act_1s = self.series.get_active_candle(TF_1S)
        self.assertIsNotNone(act_1s)
        self.assertEqual(act_1s.open, 100.0)
        self.assertEqual(act_1s.close, 102.0)
        self.assertEqual(act_1s.volume, 20.0)
        self.assertEqual(act_1s.ticks, 2)

        # Active 1M candle
        act_1m = self.series.get_active_candle(TF_1M)
        self.assertIsNotNone(act_1m)
        self.assertEqual(act_1m.open, 100.0)
        self.assertEqual(act_1m.close, 102.0)
        self.assertEqual(act_1m.volume, 20.0)

    def test_two_path_architecture_path_b_active_preview(self):
        """
        Path B: Active 5M preview dynamically rolls up closed 1M bars
        plus current forming 1M bar with sub-microsecond latency.
        """
        # Minute 1: 09:15:10 - 09:15:50
        self.series.process_tick(make_tick(datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ), ltp=100.0, vol=10.0))
        self.series.process_tick(make_tick(datetime(2026, 9, 14, 9, 15, 50, tzinfo=INDIA_TZ), ltp=105.0, vol=20.0))

        # Minute 2: 09:16:10 -> Closes Minute 1!
        closed_bars = self.series.process_tick(make_tick(datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ), ltp=108.0, vol=30.0))
        closed_1m = [b for b in closed_bars if b.timeframe == TF_1M]
        self.assertEqual(len(closed_1m), 1)
        self.assertEqual(closed_1m[0].open, 100.0)
        self.assertEqual(closed_1m[0].close, 105.0)

        # In Minute 2: add another tick at 09:16:40
        self.series.process_tick(make_tick(datetime(2026, 9, 14, 9, 16, 40, tzinfo=INDIA_TZ), ltp=95.0, vol=40.0))

        # Query Active 5M Preview during minute 2:
        # Should combine closed Minute 1 (09:15-09:16) + forming Minute 2 (09:16:10-09:16:40)
        active_5m = self.series.get_active_candle(TF_5M)
        self.assertIsNotNone(active_5m)
        self.assertFalse(active_5m.is_closed)
        self.assertEqual(active_5m.start_time, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(active_5m.end_time, datetime(2026, 9, 14, 9, 20, 0, tzinfo=INDIA_TZ))
        self.assertEqual(active_5m.open, 100.0)  # Open from first 1M bar
        self.assertEqual(active_5m.high, 108.0)  # Max high across 105 and 108
        self.assertEqual(active_5m.low, 95.0)    # Min low
        self.assertEqual(active_5m.close, 95.0)  # Latest tick price
        self.assertEqual(active_5m.volume, 10.0 + 20.0 + 30.0 + 40.0)  # 100.0
        self.assertEqual(active_5m.ticks, 4)

    def test_two_path_architecture_path_a_closed_cascade(self):
        """
        Path A: Ingest 5 full minutes of ticks.
        When tick at 09:20:00 arrives, verify closed 5M bar is emitted and stored in history.
        """
        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)

        # Ingest 5 minutes of data (09:15 to 09:19)
        for minute in range(5):
            t_m = base_time + timedelta(minutes=minute, seconds=30)
            self.series.process_tick(make_tick(t_m, ltp=100.0 + minute, vol=100.0))

        # Ingest tick at 09:20:05 to trigger close of 09:19 minute and close of 5M bucket
        t_final = base_time + timedelta(minutes=5, seconds=5)
        closed_candles = self.series.process_tick(make_tick(t_final, ltp=120.0, vol=50.0))

        # Check for finalized 5M bar
        closed_5m = [c for c in closed_candles if c.timeframe == TF_5M]
        self.assertEqual(len(closed_5m), 1)
        bar_5m = closed_5m[0]
        self.assertTrue(bar_5m.is_closed)
        self.assertEqual(bar_5m.start_time, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(bar_5m.end_time, datetime(2026, 9, 14, 9, 20, 0, tzinfo=INDIA_TZ))
        self.assertEqual(bar_5m.open, 100.0)
        self.assertEqual(bar_5m.close, 104.0)
        self.assertEqual(bar_5m.volume, 500.0)  # 5 minutes * 100.0

        # Verify ring buffer history contains the 5M bar
        hist_5m = self.series.get_history(TF_5M)
        self.assertEqual(len(hist_5m), 1)
        self.assertEqual(hist_5m[0], bar_5m)

    def test_ring_buffer_eviction_bounded_capacity(self):
        # Create series with small capacity of 3 bars for TF_1M
        custom_series = InstrumentCandleSeries(
            instrument_id=self.inst_id,
            calendar=self.cal,
            volume_policy=self.policy,
            capacities={TF_1M: 3},
        )

        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)

        # Emit 6 minutes of bars
        for minute in range(6):
            t_tick = base_time + timedelta(minutes=minute, seconds=30)
            custom_series.process_tick(make_tick(t_tick, ltp=100.0 + minute))

        # Close the 6th minute
        t_close = base_time + timedelta(minutes=6, seconds=5)
        custom_series.process_tick(make_tick(t_close, ltp=110.0))

        # History must contain at most 3 bars (the last 3: minutes 3, 4, 5)
        hist = custom_series.get_history(TF_1M)
        self.assertEqual(len(hist), 3)
        self.assertEqual(hist[0].open, 103.0)
        self.assertEqual(hist[1].open, 104.0)
        self.assertEqual(hist[2].open, 105.0)

    def test_snapshot_immutability(self):
        # Process a bar
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        t1 = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)
        self.series.process_tick(make_tick(t0, ltp=100.0))
        self.series.process_tick(make_tick(t1, ltp=105.0))

        hist = self.series.get_history(TF_1M)
        self.assertEqual(len(hist), 1)

        # Mutating the returned list
        hist.clear()
        self.assertEqual(len(hist), 0)

        # Internal buffer must remain unaffected
        hist_refetched = self.series.get_history(TF_1M)
        self.assertEqual(len(hist_refetched), 1)

    def test_finalize_until_and_force_finalize(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.series.process_tick(make_tick(t0, ltp=100.0, vol=50.0))

        # Market time passes 09:16:00
        t_due = datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ)
        closed = self.series.finalize_until(t_due)
        self.assertTrue(any(c.timeframe == TF_1M for c in closed))

        # Force finalize all
        t1 = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)
        self.series.process_tick(make_tick(t1, ltp=102.0))
        forced = self.series.force_finalize_all()
        self.assertTrue(any(c.timeframe == TF_1M for c in forced))


if __name__ == "__main__":
    unittest.main()
