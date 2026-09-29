"""
Performance Benchmark for InstrumentStateStore.

Measures throughput (updates/sec) and latency percentiles (p50, p95, p99, p99.9)
under representative market tick loads.
NOTE: As specified in Phase 2 guidelines, >50k updates/sec is a performance
measurement/target, not a strict pass/fail unit test assertion.
"""

import time
import unittest
from typing import List

from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.store import InstrumentStateStore


class TestStatePerformanceBenchmark(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.tokens = []
        for i in range(10):
            sym = f"INST_{i}"
            tok = f"TOKEN_{i}"
            inst = InstrumentId(symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY)
            self.registry.register(inst, {"ATMSTOX": tok})
            self.tokens.append(tok)

        self.store = InstrumentStateStore(registry=self.registry)

    def test_run_benchmark(self):
        depth = MarketDepth(
            bids=[DepthLevel(price=100.0, quantity=50), DepthLevel(price=99.5, quantity=100)],
            asks=[DepthLevel(price=100.5, quantity=60), DepthLevel(price=101.0, quantity=120)],
            total_buy_qty=150,
            total_sell_qty=180,
        )

        # Pre-generate 10,000 cyclic events to avoid benchmarking event generation overhead
        pre_events = []
        for i in range(10000):
            tok = self.tokens[i % len(self.tokens)]
            ev = MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id=tok,
                ltp=100.0 + (i % 20) * 0.05,
                ltp_qty=10 + (i % 5),
                depth=depth,
                tick_volume=15.5,
                total_volume=float(i * 10),
                local_receive_timestamp=time.perf_counter(),
            )
            pre_events.append(ev)

        # 1. Warm-up run (3,000 updates)
        for ev in pre_events[:3000]:
            self.store.on_market_event(ev)

        # 2. Timed benchmark run (50,000 updates)
        total_iterations = 50000
        latencies_ns: List[int] = [0] * total_iterations

        start_time = time.perf_counter()
        for idx in range(total_iterations):
            ev = pre_events[idx % len(pre_events)]
            t0 = time.perf_counter_ns()
            self.store.on_market_event(ev)
            t1 = time.perf_counter_ns()
            latencies_ns[idx] = t1 - t0
        total_elapsed = time.perf_counter() - start_time

        throughput = total_iterations / total_elapsed if total_elapsed > 0 else 0.0

        # Calculate percentiles (in microseconds)
        latencies_ns.sort()
        p50 = latencies_ns[int(total_iterations * 0.50)] / 1000.0
        p95 = latencies_ns[int(total_iterations * 0.95)] / 1000.0
        p99 = latencies_ns[int(total_iterations * 0.99)] / 1000.0
        p999 = latencies_ns[int(total_iterations * 0.999)] / 1000.0
        min_us = latencies_ns[0] / 1000.0
        max_us = latencies_ns[-1] / 1000.0
        avg_us = (sum(latencies_ns) / total_iterations) / 1000.0

        print("\n" + "=" * 60)
        print(">> TRADEGO PHASE 2: STATE STORE PERFORMANCE BENCHMARK")
        print("=" * 60)
        print(f"Total Updates Measured : {total_iterations:,}")
        print(f"Total Elapsed Time     : {total_elapsed:.4f} seconds")
        print(f"Throughput             : {throughput:,.2f} updates/sec")
        print("-" * 60)
        print(f"Latency min            : {min_us:.3f} us")
        print(f"Latency avg            : {avg_us:.3f} us")
        print(f"Latency p50 (median)   : {p50:.3f} us")
        print(f"Latency p95            : {p95:.3f} us")
        print(f"Latency p99            : {p99:.3f} us")
        print(f"Latency p99.9          : {p999:.3f} us")
        print(f"Latency max            : {max_us:.3f} us")
        print("=" * 60 + "\n")

        # Basic sanity assertion that it processed all updates without crashing
        self.assertEqual(self.store.tracked_count, len(self.tokens))
        self.assertGreater(throughput, 1000.0)


if __name__ == "__main__":
    unittest.main()
