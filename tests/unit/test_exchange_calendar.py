"""
Unit tests for ExchangeCalendar, IndianMarketCalendar, and Session-Anchored Bucket Alignment.
"""

import unittest
from datetime import date, datetime, time, timedelta

from services.candles.calendar import (
    INDIA_TZ,
    UTC_TZ,
    DefaultContinuousCalendar,
    ExchangeCalendar,
    IndianMarketCalendar,
    MarketSegment,
    SessionSegment,
    SessionSegmentType,
    TradingSession,
)
from services.candles.timeframe import (
    TF_1D,
    TF_1H,
    TF_1M,
    TF_1S,
    TF_5M,
    TF_15M,
    TF_TICK,
)
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestExchangeCalendar(unittest.TestCase):

    def setUp(self):
        self.cal = IndianMarketCalendar()
        self.eq_id = InstrumentId("INFY", Exchange.NSE, InstrumentType.EQUITY)
        self.nfo_id = InstrumentId("NIFTY", Exchange.NFO, InstrumentType.INDEX)
        self.mcx_id = InstrumentId("CRUDEOIL", Exchange.MCX, InstrumentType.COMMODITY)

    def test_trading_day_and_weekends(self):
        # 2026-09-14 is Monday
        monday = date(2026, 9, 14)
        self.assertTrue(self.cal.is_trading_day(Exchange.NSE, monday))

        # 2026-09-19 is Saturday
        saturday = date(2026, 9, 19)
        self.assertFalse(self.cal.is_trading_day(Exchange.NSE, saturday))

        # 2026-09-20 is Sunday
        sunday = date(2026, 9, 20)
        self.assertFalse(self.cal.is_trading_day(Exchange.NSE, sunday))

    def test_holidays(self):
        holiday = date(2026, 10, 2)  # Gandhi Jayanti
        cal_with_holiday = IndianMarketCalendar(holidays={Exchange.NSE: {holiday}})
        self.assertFalse(cal_with_holiday.is_trading_day(Exchange.NSE, holiday))

    def test_session_segments_nse_equity(self):
        dt_monday = date(2026, 9, 14)
        session = self.cal.get_session(Exchange.NSE, MarketSegment.EQUITY, dt_monday)

        self.assertFalse(session.is_holiday)
        self.assertEqual(len(session.segments), 4)

        # Pre-open 09:00 - 09:08 (not active for continuous candle aggregation)
        self.assertEqual(session.segments[0].segment_type, SessionSegmentType.PRE_OPEN)
        self.assertFalse(session.segments[0].is_trading_active)

        # Regular 09:15 - 15:30 (active)
        self.assertEqual(session.segments[2].segment_type, SessionSegmentType.REGULAR)
        self.assertTrue(session.segments[2].is_trading_active)
        self.assertEqual(session.segments[2].start_time, time(9, 15))
        self.assertEqual(session.segments[2].end_time, time(15, 30))

        # Regular bounds
        reg_start = session.get_regular_start()
        reg_end = session.get_regular_end()
        self.assertEqual(reg_start.time(), time(9, 15))
        self.assertEqual(reg_end.time(), time(15, 30))

    def test_session_anchored_bucket_alignment_5m(self):
        # Indian markets open regular trading at 09:15.
        # 5M bars must align as 09:15-09:20, 09:20-09:25 (NOT 09:10-09:15 or 09:00-anchored).
        dt = datetime(2026, 9, 14, 9, 16, 23, tzinfo=INDIA_TZ)
        b_start, b_end = self.cal.get_bucket_bounds(dt, TF_5M, self.eq_id)

        self.assertEqual(b_start, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(b_end, datetime(2026, 9, 14, 9, 20, 0, tzinfo=INDIA_TZ))

        # Exact boundary: 09:20:00 belongs to the next bucket [09:20, 09:25)
        dt_edge = datetime(2026, 9, 14, 9, 20, 0, tzinfo=INDIA_TZ)
        b_start_edge, b_end_edge = self.cal.get_bucket_bounds(dt_edge, TF_5M, self.eq_id)
        self.assertEqual(b_start_edge, datetime(2026, 9, 14, 9, 20, 0, tzinfo=INDIA_TZ))
        self.assertEqual(b_end_edge, datetime(2026, 9, 14, 9, 25, 0, tzinfo=INDIA_TZ))

    def test_session_anchored_bucket_alignment_15m_and_1h(self):
        dt = datetime(2026, 9, 14, 9, 25, 0, tzinfo=INDIA_TZ)

        # 15M: 09:15 - 09:30
        b_start_15, b_end_15 = self.cal.get_bucket_bounds(dt, TF_15M, self.eq_id)
        self.assertEqual(b_start_15, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(b_end_15, datetime(2026, 9, 14, 9, 30, 0, tzinfo=INDIA_TZ))

        # 1H: 09:15 - 10:15
        b_start_1h, b_end_1h = self.cal.get_bucket_bounds(dt, TF_1H, self.eq_id)
        self.assertEqual(b_start_1h, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(b_end_1h, datetime(2026, 9, 14, 10, 15, 0, tzinfo=INDIA_TZ))

    def test_daily_bucket_alignment(self):
        dt = datetime(2026, 9, 14, 11, 45, 0, tzinfo=INDIA_TZ)
        b_start_1d, b_end_1d = self.cal.get_bucket_bounds(dt, TF_1D, self.eq_id)
        self.assertEqual(b_start_1d, datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ))
        self.assertEqual(b_end_1d, datetime(2026, 9, 14, 15, 30, 0, tzinfo=INDIA_TZ))

    def test_mcx_and_cds_trading_hours(self):
        dt_monday = date(2026, 9, 14)
        mcx_session = self.cal.get_session(Exchange.MCX, MarketSegment.COMMODITY, dt_monday)
        self.assertEqual(mcx_session.get_regular_start().time(), time(9, 0))
        self.assertEqual(mcx_session.get_regular_end().time(), time(23, 30))

        cds_session = self.cal.get_session(Exchange.CDS, MarketSegment.CURRENCY, dt_monday)
        self.assertEqual(cds_session.get_regular_start().time(), time(9, 0))
        self.assertEqual(cds_session.get_regular_end().time(), time(17, 0))

    def test_default_continuous_calendar(self):
        cont_cal = DefaultContinuousCalendar(tz=UTC_TZ)
        dt = datetime(2026, 9, 14, 14, 23, 45, tzinfo=UTC_TZ)
        b_start, b_end = cont_cal.get_bucket_bounds(dt, TF_1M, self.eq_id)
        self.assertEqual(b_start, datetime(2026, 9, 14, 14, 23, 0, tzinfo=UTC_TZ))
        self.assertEqual(b_end, datetime(2026, 9, 14, 14, 24, 0, tzinfo=UTC_TZ))


if __name__ == "__main__":
    unittest.main()
