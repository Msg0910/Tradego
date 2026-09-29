"""
Unit tests for Tradego Strategy Scheduler and Monotonic Evaluation Watermarks (Phase 8).
Verifies Scenarios A, B, C, D, E, multi-timeframe independence, and watermark non-regression.
"""

from datetime import datetime, timedelta, timezone
import unittest

from services.candles.timeframe import TF_1M, TF_5M, TimeFrame
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.runtime.scheduler import StrategyScheduler


class TestRuntimeSchedulerAndWatermarks(unittest.TestCase):
    """Verifies monotonic watermark progression and intrabar rate limiting."""

    def setUp(self) -> None:
        self.scheduler = StrategyScheduler(min_intrabar_interval_ms=100.0)
        self.instrument_id = InstrumentId(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.strategy_id = "TREND_CONT_01"
        self.base_time = datetime(2026, 9, 15, 9, 15, 0, tzinfo=timezone.utc)

    def test_scenario_a_duplicate_closed_candle_suppression(self) -> None:
        """
        SCENARIO A: Same closed candle delivered twice.
        Second BAR_CLOSE evaluation must be suppressed.
        """
        candle_end_time = self.base_time + timedelta(minutes=5)  # 09:20

        # First delivery: Should evaluate
        self.assertTrue(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id,
                self.instrument_id,
                TF_5M,
                candle_end_time,
            )
        )
        # Advance watermark after evaluation
        self.scheduler.advance_watermark(
            self.strategy_id,
            self.instrument_id,
            TF_5M,
            candle_end_time,
        )

        # Second delivery of identical closed candle: Must be suppressed!
        self.assertFalse(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id,
                self.instrument_id,
                TF_5M,
                candle_end_time,
            )
        )

    def test_scenario_b_older_late_candle_cannot_regress_watermark(self) -> None:
        """
        SCENARIO B: Older closed candle arrives late.
        Cannot move the evaluation watermark backwards.
        """
        bar_0920 = self.base_time + timedelta(minutes=5)   # 09:20
        bar_0915 = self.base_time                          # 09:15 (older)

        # 09:20 bar arrives first
        self.scheduler.advance_watermark(self.strategy_id, self.instrument_id, TF_5M, bar_0920)
        self.assertEqual(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_5M),
            bar_0920,
        )

        # Older 09:15 bar arrives late: Must be suppressed
        self.assertFalse(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id,
                self.instrument_id,
                TF_5M,
                bar_0915,
            )
        )

        # Even if advance_watermark is invoked with older bar, watermark must NOT regress
        self.scheduler.advance_watermark(self.strategy_id, self.instrument_id, TF_5M, bar_0915)
        self.assertEqual(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_5M),
            bar_0920,
        )

    def test_scenario_c_consecutive_closed_candles(self) -> None:
        """
        SCENARIO C: Two consecutive closed candles.
        Both are independently evaluated and advance the watermark sequentially.
        """
        bar_1 = self.base_time + timedelta(minutes=5)   # 09:20
        bar_2 = self.base_time + timedelta(minutes=10)  # 09:25

        # Bar 1 evaluates
        self.assertTrue(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id, self.instrument_id, TF_5M, bar_1
            )
        )
        self.scheduler.advance_watermark(self.strategy_id, self.instrument_id, TF_5M, bar_1)
        self.assertEqual(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_5M),
            bar_1,
        )

        # Bar 2 evaluates
        self.assertTrue(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id, self.instrument_id, TF_5M, bar_2
            )
        )
        self.scheduler.advance_watermark(self.strategy_id, self.instrument_id, TF_5M, bar_2)
        self.assertEqual(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_5M),
            bar_2,
        )

    def test_multi_timeframe_independence(self) -> None:
        """
        Verifies M1 bar close does not affect M5 watermark for the same instrument.
        """
        m1_bar = self.base_time + timedelta(minutes=1)
        m5_bar = self.base_time + timedelta(minutes=5)

        # M1 advances
        self.scheduler.advance_watermark(self.strategy_id, self.instrument_id, TF_1M, m1_bar)
        self.assertEqual(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_1M),
            m1_bar,
        )

        # M5 watermark remains uninitialized
        self.assertIsNone(
            self.scheduler.get_watermark(self.strategy_id, self.instrument_id, TF_5M)
        )
        # M5 should evaluate
        self.assertTrue(
            self.scheduler.should_evaluate_bar_close(
                self.strategy_id, self.instrument_id, TF_5M, m5_bar
            )
        )

    def test_multi_strategy_independence(self) -> None:
        """
        Verifies watermark for Strategy A does not affect Strategy B on the same instrument.
        """
        strat_a = "STRAT_A"
        strat_b = "STRAT_B"
        bar = self.base_time + timedelta(minutes=5)

        self.scheduler.advance_watermark(strat_a, self.instrument_id, TF_5M, bar)
        self.assertFalse(
            self.scheduler.should_evaluate_bar_close(strat_a, self.instrument_id, TF_5M, bar)
        )

        # Strat B should still be allowed to evaluate this bar
        self.assertTrue(
            self.scheduler.should_evaluate_bar_close(strat_b, self.instrument_id, TF_5M, bar)
        )

    def test_intrabar_rate_limiting_cadence_control(self) -> None:
        """
        Verifies the 100 ms rate limiter prevents rapid evaluation within the threshold.
        """
        t0_ns = 1_000_000_000  # 1.000s
        self.assertTrue(
            self.scheduler.should_evaluate_intrabar(self.strategy_id, self.instrument_id, t0_ns)
        )
        self.scheduler.record_intrabar_eval(self.strategy_id, self.instrument_id, t0_ns)

        # Tick 50 ms later (< 100 ms): Must be throttled
        t_50ms_later = t0_ns + 50_000_000
        self.assertFalse(
            self.scheduler.should_evaluate_intrabar(self.strategy_id, self.instrument_id, t_50ms_later)
        )

        # Tick 110 ms later (>= 100 ms): Allowed
        t_110ms_later = t0_ns + 110_000_000
        self.assertTrue(
            self.scheduler.should_evaluate_intrabar(self.strategy_id, self.instrument_id, t_110ms_later)
        )


if __name__ == "__main__":
    unittest.main()
