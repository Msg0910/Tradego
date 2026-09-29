"""
Unit tests for FeedHealthMetrics and SymbolMetrics.
"""

import unittest

from services.market_gateway.metrics import FeedHealthMetrics, SymbolMetrics


class TestFeedHealthMetrics(unittest.TestCase):

    def test_symbol_metrics_lifecycle(self):
        sm = SymbolMetrics(symbol_id="13061")
        self.assertEqual(sm.ticks_received, 0)
        self.assertEqual(sm.ticks_normalized, 0)
        self.assertEqual(sm.ticks_dropped, 0)
        self.assertEqual(sm.errors_count, 0)

        sm.record_received(perf_now=10.0, wall_now=100.0)
        self.assertEqual(sm.ticks_received, 1)
        self.assertEqual(sm.last_tick_perf_time, 10.0)
        self.assertEqual(sm.last_tick_wall_time, 100.0)

        sm.record_normalized(process_latency_ms=0.5)
        sm.record_normalized(process_latency_ms=1.5)
        self.assertEqual(sm.ticks_normalized, 2)
        self.assertEqual(sm.min_gateway_latency_ms, 0.5)
        self.assertEqual(sm.max_gateway_latency_ms, 1.5)
        self.assertEqual(sm.avg_gateway_latency_ms, 1.0)

        sm.record_dropped()
        self.assertEqual(sm.ticks_dropped, 1)

        sm.record_error()
        self.assertEqual(sm.errors_count, 1)

        d = sm.to_dict()
        self.assertEqual(d["symbol_id"], "13061")
        self.assertEqual(d["ticks_received"], 1)
        self.assertEqual(d["ticks_normalized"], 2)
        self.assertEqual(d["ticks_dropped"], 1)
        self.assertEqual(d["errors_count"], 1)

    def test_feed_health_metrics_aggregation(self):
        metrics = FeedHealthMetrics()
        metrics.record_tick_received("13061", 10.0, 100.0)
        metrics.record_tick_normalized("13061", 0.4)
        metrics.record_tick_received("466583", 10.1, 100.1)
        metrics.record_tick_dropped("466583")
        metrics.record_error("13061")

        summary = metrics.get_summary()
        self.assertEqual(summary["total_ticks_received"], 2)
        self.assertEqual(summary["total_ticks_normalized"], 1)
        self.assertEqual(summary["total_ticks_dropped"], 1)
        self.assertEqual(summary["total_errors"], 1)
        self.assertEqual(summary["active_symbols_count"], 2)

        sm_13061 = metrics.get_symbol_metrics("13061")
        self.assertIsNotNone(sm_13061)
        self.assertEqual(sm_13061.ticks_received, 1)
        self.assertEqual(sm_13061.ticks_normalized, 1)

        self.assertIsNone(metrics.get_symbol_metrics("non_existent"))


if __name__ == "__main__":
    unittest.main()
