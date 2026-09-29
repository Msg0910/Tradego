"""
Performance Benchmark for Tradego Candle & Time-Series Aggregation Layer.

Measures:
- Ingestion Throughput (ticks/second)
- Latency percentiles: p50, p95, p99, p99.9, max (in microseconds)
- Active Higher-Timeframe Preview latency
- Memory usage per instrument

Scenarios:
1. 1 instrument
2. 10 instruments
3. 100 instruments
"""

import gc
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta
from typing import List, Tuple

from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.timeframe import TF_1M, TF_5M
from services.candles.volume import IncrementalVolumePolicy
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)


def run_ingestion_benchmark(
    num_instruments: int,
    ticks_per_instrument: int,
) -> Tuple[float, List[float]]:
    """
    Runs an ingestion benchmark and returns (throughput, latencies_in_microseconds).
    """
    registry = InstrumentRegistry()
    cal = IndianMarketCalendar()
    engine = CandleEngine(
        registry=registry,
        calendar=cal,
        volume_policy_factory=lambda iid: IncrementalVolumePolicy(),
    )

    tokens = []
    for i in range(num_instruments):
        iid = InstrumentId(f"SYM_{i:04d}", Exchange.NSE, InstrumentType.EQUITY)
        token = f"TOK_{i:04d}"
        registry.register(iid, {"ATMSTOX": token})
        tokens.append((token, iid))

    # Pre-generate ticks
    base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)
    all_events: List[MarketEvent] = []

    for k in range(ticks_per_instrument):
        t = base_time + timedelta(milliseconds=k * 100)
        for token, _ in tokens:
            ev = MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id=token,
                exchange_timestamp=t,
                ltp=100.0 + (k % 50),
                tick_volume=10.0,
                local_receive_timestamp=float(k),
            )
            all_events.append(ev)

    latencies_us: List[float] = []
    on_event = engine.on_market_event

    gc.disable()
    t_start = time.perf_counter()

    for ev in all_events:
        t0 = time.perf_counter_ns()
        on_event(ev)
        t1 = time.perf_counter_ns()
        latencies_us.append((t1 - t0) / 1000.0)

    t_end = time.perf_counter()
    gc.enable()

    total_ticks = len(all_events)
    elapsed = t_end - t_start
    throughput = total_ticks / elapsed if elapsed > 0 else 0.0

    return throughput, latencies_us


def compute_percentiles(latencies: List[float]) -> dict:
    sorted_lats = sorted(latencies)
    n = len(sorted_lats)
    return {
        "p50": sorted_lats[int(n * 0.50)],
        "p95": sorted_lats[int(n * 0.95)],
        "p99": sorted_lats[int(n * 0.99)],
        "p99.9": sorted_lats[min(int(n * 0.999), n - 1)],
        "max": sorted_lats[-1],
    }


class TestCandleBenchmark(unittest.TestCase):

    def test_benchmark_1_instrument(self):
        ticks = 20000
        throughput, latencies = run_ingestion_benchmark(num_instruments=1, ticks_per_instrument=ticks)
        stats = compute_percentiles(latencies)

        print("\n--- BENCHMARK: 1 INSTRUMENT ---")
        print(f"Total Ticks: {ticks}")
        print(f"Throughput: {throughput:,.0f} ticks/sec")
        print(f"Latency p50 : {stats['p50']:.2f} us")
        print(f"Latency p95 : {stats['p95']:.2f} us")
        print(f"Latency p99 : {stats['p99']:.2f} us")
        print(f"Latency p99.9: {stats['p99.9']:.2f} us")
        print(f"Latency max : {stats['max']:.2f} us")
        self.assertGreater(throughput, 10000)

    def test_benchmark_10_instruments(self):
        num_inst = 10
        ticks_per_inst = 3000
        total_ticks = num_inst * ticks_per_inst
        throughput, latencies = run_ingestion_benchmark(num_instruments=num_inst, ticks_per_instrument=ticks_per_inst)
        stats = compute_percentiles(latencies)

        print("\n--- BENCHMARK: 10 INSTRUMENTS ---")
        print(f"Total Ticks: {total_ticks}")
        print(f"Throughput: {throughput:,.0f} ticks/sec")
        print(f"Latency p50 : {stats['p50']:.2f} us")
        print(f"Latency p95 : {stats['p95']:.2f} us")
        print(f"Latency p99 : {stats['p99']:.2f} us")
        print(f"Latency p99.9: {stats['p99.9']:.2f} us")
        print(f"Latency max : {stats['max']:.2f} us")
        self.assertGreater(throughput, 10000)

    def test_benchmark_100_instruments(self):
        num_inst = 100
        ticks_per_inst = 500
        total_ticks = num_inst * ticks_per_inst
        throughput, latencies = run_ingestion_benchmark(num_instruments=num_inst, ticks_per_instrument=ticks_per_inst)
        stats = compute_percentiles(latencies)

        print("\n--- BENCHMARK: 100 INSTRUMENTS ---")
        print(f"Total Ticks: {total_ticks}")
        print(f"Throughput: {throughput:,.0f} ticks/sec")
        print(f"Latency p50 : {stats['p50']:.2f} us")
        print(f"Latency p95 : {stats['p95']:.2f} us")
        print(f"Latency p99 : {stats['p99']:.2f} us")
        print(f"Latency p99.9: {stats['p99.9']:.2f} us")
        print(f"Latency max : {stats['max']:.2f} us")
        self.assertGreater(throughput, 10000)

    def test_benchmark_active_preview_latency(self):
        registry = InstrumentRegistry()
        cal = IndianMarketCalendar()
        engine = CandleEngine(registry, cal)
        iid = InstrumentId("PREVIEW_BENCH", Exchange.NSE, InstrumentType.EQUITY)
        registry.register(iid, {"ATMSTOX": "TOK_PREV"})

        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)
        # Prime series with some ticks and closed bars
        for m in range(4):
            t = base_time + timedelta(minutes=m, seconds=30)
            engine.on_market_event(MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="TOK_PREV",
                exchange_timestamp=t,
                ltp=100.0 + m,
                tick_volume=10.0,
                local_receive_timestamp=float(m),
            ))

        iterations = 20000
        latencies_us = []

        gc.disable()
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            preview = engine.get_active_candle(iid, TF_5M)
            t1 = time.perf_counter_ns()
            latencies_us.append((t1 - t0) / 1000.0)
        gc.enable()

        stats = compute_percentiles(latencies_us)
        print("\n--- BENCHMARK: ACTIVE 5M PREVIEW LATENCY ---")
        print(f"Iterations: {iterations}")
        print(f"Preview p50 : {stats['p50']:.2f} us")
        print(f"Preview p95 : {stats['p95']:.2f} us")
        print(f"Preview p99 : {stats['p99']:.2f} us")
        print(f"Preview max : {stats['max']:.2f} us")

    def test_benchmark_memory_footprint_100_instruments(self):
        tracemalloc.start()
        gc.collect()
        mem_before, _ = tracemalloc.get_traced_memory()

        registry = InstrumentRegistry()
        cal = IndianMarketCalendar()
        engine = CandleEngine(registry, cal)

        num_inst = 100
        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)

        for i in range(num_inst):
            iid = InstrumentId(f"MEM_SYM_{i:04d}", Exchange.NSE, InstrumentType.EQUITY)
            tok = f"MEM_TOK_{i:04d}"
            registry.register(iid, {"ATMSTOX": tok})
            # Add ticks to populate 1S and 1M buffers
            for s in range(50):
                t = base_time + timedelta(seconds=s)
                engine.on_market_event(MarketEvent(
                    provider="ATMSTOX",
                    provider_symbol_id=tok,
                    exchange_timestamp=t,
                    ltp=100.0 + s,
                    tick_volume=1.0,
                    local_receive_timestamp=float(s),
                ))

        gc.collect()
        mem_after, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        net_mem_mb = (mem_after - mem_before) / (1024 * 1024)
        mem_per_instrument_kb = (net_mem_mb * 1024) / num_inst

        print("\n--- BENCHMARK: MEMORY FOOTPRINT ---")
        print(f"Instruments: {num_inst}")
        print(f"Total Net Memory: {net_mem_mb:.2f} MB")
        print(f"Memory Per Instrument: {mem_per_instrument_kb:.2f} KB ({mem_per_instrument_kb / 1024:.3f} MB)")
        # Must be well below 2 MB per instrument
        self.assertLess(mem_per_instrument_kb / 1024, 2.0)


if __name__ == "__main__":
    unittest.main()
