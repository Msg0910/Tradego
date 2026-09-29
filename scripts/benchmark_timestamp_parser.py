"""
TradeGo Milestone 2 — Timestamp Parser Micro-Benchmark.

Measures the latency distribution, fallback frequency, and CPU cost of
ATMStoxNormalizer._parse_timestamp_field across all supported format patterns.
"""

import math
import statistics
import time
from typing import Any, Dict, List, Tuple
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.market_gateway.atmstox.normalizer import _parse_timestamp_field


def run_parser_benchmark(iterations_per_format: int = 50000) -> Dict[str, Any]:
    test_cases: List[Tuple[str, Any, str]] = [
        ("Format 1 (%d-%m-%Y %H:%M:%S)", "28-09-2026 15:30:00", "First branch match"),
        ("Format 2 (%Y-%m-%d %H:%M:%S)", "2026-09-28 15:30:00", "Second branch (1 fallback)"),
        ("Format 3 (%d-%m-%Y %H:%M:%S.%f)", "28-09-2026 15:30:00.123456", "Third branch (2 fallbacks)"),
        ("Format 4 (%Y-%m-%d %H:%M:%S.%f)", "2026-09-28 15:30:00.123456", "Fourth branch (3 fallbacks)"),
        ("Format 5 (%Y-%m-%dT%H:%M:%S)", "2026-09-28T15:30:00", "Fifth branch (4 fallbacks)"),
        ("Format 6 (%Y-%m-%dT%H:%M:%SZ)", "2026-09-28T15:30:00Z", "Sixth branch (5 fallbacks)"),
        ("Epoch Milliseconds (float > 1e11)", 1790610600123.0, "All strptime failed -> Epoch ms branch"),
        ("Epoch Seconds (float > 1e8)", 1790610600.0, "All strptime failed -> Epoch sec branch"),
        ("Invalid/Non-matching String", "28/09/2026 15:30:00", "All strptime failed -> None"),
        ("Empty/Null string", "", "Immediate early return None"),
    ]

    results: Dict[str, Any] = {}

    # Warmup
    for _, val, _ in test_cases:
        for _ in range(1000):
            _parse_timestamp_field(val)

    for name, val, desc in test_cases:
        durations_ns: List[int] = []
        t_start = time.perf_counter_ns()
        for _ in range(iterations_per_format):
            t0 = time.perf_counter_ns()
            res = _parse_timestamp_field(val)
            t1 = time.perf_counter_ns()
            durations_ns.append(t1 - t0)
        t_total = time.perf_counter_ns() - t_start

        durations_us = [d / 1000.0 for d in durations_ns]
        durations_us.sort()
        n = len(durations_us)

        results[name] = {
            "description": desc,
            "sample_input": str(val),
            "iterations": n,
            "total_time_ms": t_total / 1e6,
            "throughput_calls_per_sec": n / (t_total / 1e9),
            "min_us": durations_us[0],
            "mean_us": statistics.mean(durations_us),
            "median_p50_us": durations_us[int(n * 0.50)],
            "p90_us": durations_us[int(n * 0.90)],
            "p95_us": durations_us[int(n * 0.95)],
            "p99_us": durations_us[int(n * 0.99)],
            "p999_us": durations_us[int(n * 0.999)],
            "max_us": durations_us[-1],
        }

    return results


if __name__ == "__main__":
    import json
    data = run_parser_benchmark(50000)
    print(json.dumps(data, indent=2))
