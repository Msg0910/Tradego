"""
Unit tests for CandleBuilder single-timeframe aggregation logic.
"""

import unittest
from datetime import datetime, timedelta

from services.candles.builder import CandleBuilder
from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.models import VolumeQuality
from services.candles.timeframe import TF_1M
from services.candles.volume import CumulativeVolumePolicy, IncrementalVolumePolicy
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_tick(
    dt: datetime,
    ltp: float,
    tick_vol: float = 10.0,
    total_vol: float = 1000.0,
    oi: int = 5000,
) -> MarketEvent:
    return MarketEvent(
        provider="TEST",
        provider_symbol_id="TOKEN_1",
        exchange_timestamp=dt,
        ltp=ltp,
        tick_volume=tick_vol,
        total_volume=total_vol,
        oi=oi,
        local_receive_timestamp=100.0,
    )


class TestCandleBuilder(unittest.TestCase):

    def setUp(self):
        self.cal = IndianMarketCalendar()
        self.inst_id = InstrumentId("SBIN", Exchange.NSE, InstrumentType.EQUITY)
        self.policy = IncrementalVolumePolicy()
        self.builder = CandleBuilder(
            instrument_id=self.inst_id,
            timeframe=TF_1M,
            calendar=self.cal,
            volume_policy=self.policy,
        )

    def test_first_tick_initialization(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        ev0 = make_tick(t0, ltp=500.0, tick_vol=100.0)

        closed = self.builder.process_tick(ev0)
        self.assertIsNone(closed)
        self.assertTrue(self.builder.is_active)

        active = self.builder.get_active_candle()
        self.assertIsNotNone(active)
        self.assertEqual(active.open, 500.0)
        self.assertEqual(active.high, 500.0)
        self.assertEqual(active.low, 500.0)
        self.assertEqual(active.close, 500.0)
        self.assertEqual(active.volume, 100.0)
        self.assertEqual(active.ticks, 1)
        self.assertFalse(active.is_closed)

    def test_multiple_ticks_ohlc_ratcheting(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t0, ltp=500.0, tick_vol=50.0))

        # Higher price
        t1 = datetime(2026, 9, 14, 9, 15, 20, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t1, ltp=510.0, tick_vol=30.0))

        # Lower price
        t2 = datetime(2026, 9, 14, 9, 15, 40, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t2, ltp=495.0, tick_vol=70.0))

        # Final tick in bucket
        t3 = datetime(2026, 9, 14, 9, 15, 55, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t3, ltp=502.0, tick_vol=50.0))

        active = self.builder.get_active_candle()
        self.assertEqual(active.open, 500.0)
        self.assertEqual(active.high, 510.0)
        self.assertEqual(active.low, 495.0)
        self.assertEqual(active.close, 502.0)
        self.assertEqual(active.volume, 200.0)
        self.assertEqual(active.ticks, 4)

    def test_boundary_transition_and_single_tick_candle(self):
        # 09:15:00 - 09:16:00 bucket
        t0 = datetime(2026, 9, 14, 9, 15, 30, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t0, ltp=100.0, tick_vol=10.0))

        # Next tick arrives in next bucket (09:16:05 >= 09:16:00)
        t1 = datetime(2026, 9, 14, 9, 16, 5, tzinfo=INDIA_TZ)
        closed = self.builder.process_tick(make_tick(t1, ltp=105.0, tick_vol=20.0))

        self.assertIsNotNone(closed)
        self.assertTrue(closed.is_closed)
        self.assertEqual(closed.start_time, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(closed.end_time, datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ))
        self.assertEqual(closed.open, 100.0)
        self.assertEqual(closed.high, 100.0)
        self.assertEqual(closed.low, 100.0)
        self.assertEqual(closed.close, 100.0)
        self.assertEqual(closed.volume, 10.0)
        self.assertEqual(closed.ticks, 1)

        # Active candle is now for the new bucket
        active = self.builder.get_active_candle()
        self.assertEqual(active.start_time, datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ))
        self.assertEqual(active.open, 105.0)

    def test_exact_bucket_boundary(self):
        # Tick exactly at 09:16:00.000000 belongs to [09:16:00, 09:17:00)
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t0, ltp=100.0))

        t_edge = datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ)
        closed = self.builder.process_tick(make_tick(t_edge, ltp=102.0))

        self.assertIsNotNone(closed)
        self.assertEqual(closed.end_time, t_edge)
        active = self.builder.get_active_candle()
        self.assertEqual(active.start_time, t_edge)

    def test_same_second_bursts(self):
        t_same = datetime(2026, 9, 14, 9, 15, 15, tzinfo=INDIA_TZ)
        # Tick 1
        self.builder.process_tick(make_tick(t_same, ltp=100.0, tick_vol=10.0))
        # Tick 2 (same second, higher price)
        self.builder.process_tick(make_tick(t_same, ltp=103.0, tick_vol=20.0))
        # Tick 3 (same second, lower price)
        self.builder.process_tick(make_tick(t_same, ltp=99.0, tick_vol=15.0))

        active = self.builder.get_active_candle()
        self.assertEqual(active.open, 100.0)
        self.assertEqual(active.high, 103.0)
        self.assertEqual(active.low, 99.0)
        self.assertEqual(active.close, 99.0)
        self.assertEqual(active.volume, 45.0)
        self.assertEqual(active.ticks, 3)

    def test_duplicate_tick_filtering(self):
        t0 = datetime(2026, 9, 14, 9, 15, 15, tzinfo=INDIA_TZ)
        ev = make_tick(t0, ltp=100.0, tick_vol=50.0, total_vol=1000.0, oi=5000)

        self.builder.process_tick(ev)
        # Exact duplicate
        self.builder.process_tick(ev)

        self.assertEqual(self.builder.duplicate_ticks_ignored, 1)
        active = self.builder.get_active_candle()
        self.assertEqual(active.volume, 50.0)
        self.assertEqual(active.ticks, 1)

    def test_closed_bucket_late_tick_rejection(self):
        # Establish active bucket [09:16, 09:17)
        t_active = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t_active, ltp=100.0))

        # Late tick from previous already-closed bucket (09:15:30)
        t_late = datetime(2026, 9, 14, 9, 15, 30, tzinfo=INDIA_TZ)
        closed = self.builder.process_tick(make_tick(t_late, ltp=150.0))

        self.assertIsNone(closed)
        self.assertEqual(self.builder.late_ticks_rejected, 1)
        # Active candle must remain completely unchanged
        active = self.builder.get_active_candle()
        self.assertEqual(active.open, 100.0)
        self.assertEqual(active.high, 100.0)
        self.assertEqual(active.ticks, 1)

    def test_quote_only_tick_no_ltp(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        ev_quote = MarketEvent(
            provider="TEST",
            provider_symbol_id="TOKEN_1",
            exchange_timestamp=t0,
            ltp=None,  # Quote-only depth tick
            local_receive_timestamp=100.0,
        )
        closed = self.builder.process_tick(ev_quote)
        self.assertIsNone(closed)
        self.assertFalse(self.builder.is_active)
        self.assertIsNone(self.builder.get_active_candle())

    def test_open_interest_tracking(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t0, ltp=100.0, oi=10000))

        t1 = datetime(2026, 9, 14, 9, 15, 20, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t1, ltp=101.0, oi=12000))

        t2 = datetime(2026, 9, 14, 9, 15, 30, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t2, ltp=102.0, oi=9500))

        active = self.builder.get_active_candle()
        self.assertEqual(active.open_oi, 10000)
        self.assertEqual(active.high_oi, 12000)
        self.assertEqual(active.low_oi, 9500)
        self.assertEqual(active.close_oi, 9500)

    def test_finalize_if_due_and_force_finalize(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t0, ltp=100.0))

        # Not due yet (market time 09:15:30 < 09:16:00 end time)
        not_due = datetime(2026, 9, 14, 9, 15, 30, tzinfo=INDIA_TZ)
        self.assertIsNone(self.builder.finalize_if_due(not_due))
        self.assertTrue(self.builder.is_active)

        # Due (market time 09:16:00 >= 09:16:00 end time)
        is_due = datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ)
        closed = self.builder.finalize_if_due(is_due)
        self.assertIsNotNone(closed)
        self.assertTrue(closed.is_closed)
        self.assertFalse(self.builder.is_active)

        # Force finalize when reopened
        t1 = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)
        self.builder.process_tick(make_tick(t1, ltp=105.0))
        forced = self.builder.force_finalize()
        self.assertIsNotNone(forced)
        self.assertEqual(forced.open, 105.0)
        self.assertFalse(self.builder.is_active)


if __name__ == "__main__":
    unittest.main()
