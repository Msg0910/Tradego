"""
Unit tests for Feature Engine Concurrency and Thread-Safety (Phase 4).

Tests:
- Multi-instrument parallel state isolation
- Reader-writer concurrency: Ingestion thread updating features while reader threads
  continuously query immutable FeatureSnapshots and active previews without lock contention.
- Basic performance benchmark metrics (hot path, warm update, memory footprint).
"""

import gc
import threading
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.engine import FeatureEngine
from services.analytics.indicators.momentum import StreamingRSI
from services.analytics.indicators.moving_averages import StreamingEMA, StreamingSMA
from services.analytics.indicators.volatility import RollingBollingerBands
from services.analytics.microstructure.order_book import BookImbalance, SpreadBps
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_1M, TF_5M
from services.market_gateway.models import DepthLevel
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.order_book import OrderBookState
from services.market_state.state import InstrumentState


def make_candle(iid: InstrumentId, dt: datetime, close: float) -> Candle:
    return Candle(
        instrument_id=iid,
        timeframe=TF_1M,
        start_time=dt - timedelta(minutes=1),
        end_time=dt,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
        ticks=10,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=True,
    )


class TestFeatureConcurrency(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.engine = FeatureEngine(registry=self.registry)
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_multi_instrument_concurrent_updates(self):
        """
        Tests multiple instruments receiving closed candles and state updates
        concurrently on separate threads with per-instrument isolation.
        """
        num_instruments = 5
        updates_per_inst = 100
        instruments = []

        for i in range(num_instruments):
            iid = InstrumentId(f"CONC_SYM_{i}", Exchange.NSE, InstrumentType.EQUITY)
            self.registry.register(iid, {"ATMSTOX": f"TOK_{i}"})
            # Register indicators for each instrument
            self.engine.register_indicator(iid, StreamingEMA(TF_1M, period=5))
            self.engine.register_indicator(iid, StreamingRSI(TF_1M, period=5))
            self.engine.register_microstructure_feature(iid, BookImbalance())
            instruments.append(iid)

        errors = []

        def worker(iid: InstrumentId, idx: int):
            try:
                for k in range(updates_per_inst):
                    t = self.base_time + timedelta(minutes=k + 1)
                    c = make_candle(iid, t, close=100.0 + (idx * 5) + (k % 10))
                    self.engine.on_candle_closed(c)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(iid, i))
            for i, iid in enumerate(instruments)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Worker encountered errors: {errors}")

        # Verify all stores accumulated features
        for iid in instruments:
            snap = self.engine.get_features(iid)
            self.assertIsNotNone(snap)
            self.assertIn("EMA_5_1M", snap)
            self.assertIn("RSI_5_1M", snap)
            self.assertTrue(snap.is_valid("EMA_5_1M"))

    def test_reader_writer_concurrency(self):
        """
        Writer thread streaming closed candles while concurrent reader threads
        repeatedly inspect FeatureSnapshots with zero lock starvation.
        """
        iid = InstrumentId("READER_WRITER_TEST", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(iid, {"ATMSTOX": "TOK_RW"})
        self.engine.register_indicator(iid, StreamingEMA(TF_1M, period=5))
        self.engine.register_indicator(iid, StreamingSMA(TF_1M, period=10))
        self.engine.register_indicator(iid, RollingBollingerBands(TF_1M, period=10))

        stop_event = threading.Event()
        errors = []
        read_counts = [0]

        def writer():
            try:
                for k in range(200):
                    t = self.base_time + timedelta(minutes=k + 1)
                    c = make_candle(iid, t, close=100.0 + (k % 20))
                    self.engine.on_candle_closed(c)
                    if k % 20 == 0:
                        time.sleep(0.001)
            except Exception as e:
                errors.append(e)
            finally:
                stop_event.set()

        def reader():
            try:
                while not stop_event.is_set():
                    snap = self.engine.get_features(iid)
                    if snap is not None:
                        # Verify snapshot immutability & usability
                        val = snap.get_value("EMA_5_1M")
                        if val is not None:
                            self.assertGreater(val, 0.0)
                    read_counts[0] += 1
            except Exception as e:
                errors.append(e)

        t_writer = threading.Thread(target=writer)
        t_reader = threading.Thread(target=reader)

        t_writer.start()
        t_reader.start()

        t_writer.join()
        t_reader.join()

        self.assertEqual(len(errors), 0, f"Reader-writer concurrency errors: {errors}")
        self.assertGreater(read_counts[0], 50)

    def test_benchmark_hot_and_warm_latencies(self):
        """
        Measures hot-path microstructure and warm-path indicator update latencies.
        """
        iid = InstrumentId("BENCH_TEST", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(iid, {"ATMSTOX": "TOK_BENCH"})

        bi = BookImbalance()
        sp = SpreadBps()
        self.engine.register_microstructure_feature(iid, bi)
        self.engine.register_microstructure_feature(iid, sp)
        self.engine.register_indicator(iid, StreamingEMA(TF_1M, period=20))
        self.engine.register_indicator(iid, StreamingRSI(TF_1M, period=14))
        self.engine.register_indicator(iid, RollingBollingerBands(TF_1M, period=20))

        store = self.engine.get_or_create_store(iid)

        # Microstructure Hot-Path Benchmark
        state = InstrumentState(instrument_id=iid, provider="TEST", provider_symbol_id="TOK_BENCH", is_resolved=True)
        state.ltp = 100.0
        state.order_book = OrderBookState(
            bids=(DepthLevel(100.0, 100),),
            asks=(DepthLevel(100.05, 100),),
        )
        snap = state.create_snapshot()

        hot_latencies_us = []
        gc.disable()
        for _ in range(5000):
            t0 = time.perf_counter_ns()
            store.process_state_snapshot(snap)
            t1 = time.perf_counter_ns()
            hot_latencies_us.append((t1 - t0) / 1000.0)
        gc.enable()

        sorted_hot = sorted(hot_latencies_us)
        n = len(sorted_hot)
        p50 = sorted_hot[int(n * 0.50)]
        p95 = sorted_hot[int(n * 0.95)]
        p99 = sorted_hot[int(n * 0.99)]

        print("\n--- PHASE 4 BENCHMARK: HOT MICROSTRUCTURE ---")
        print(f"Iterations: {n}")
        print(f"Hot p50: {p50:.2f} us")
        print(f"Hot p95: {p95:.2f} us")
        print(f"Hot p99: {p99:.2f} us")

        # Warm-Path Indicator Benchmark
        warm_latencies_us = []
        gc.disable()
        for k in range(2000):
            t = self.base_time + timedelta(minutes=k + 1)
            c = make_candle(iid, t, close=100.0 + (k % 10))
            t0 = time.perf_counter_ns()
            store.process_closed_candle(c)
            t1 = time.perf_counter_ns()
            warm_latencies_us.append((t1 - t0) / 1000.0)
        gc.enable()

        sorted_warm = sorted(warm_latencies_us)
        nw = len(sorted_warm)
        print("\n--- PHASE 4 BENCHMARK: WARM INDICATOR UPDATE ---")
        print(f"Iterations: {nw}")
        print(f"Warm p50: {sorted_warm[int(nw * 0.50)]:.2f} us")
        print(f"Warm p95: {sorted_warm[int(nw * 0.95)]:.2f} us")
        print(f"Warm p99: {sorted_warm[int(nw * 0.99)]:.2f} us")

    def test_memory_footprint_scaling(self):
        """
        Measures actual memory footprint across 50 instruments populated with features.
        """
        tracemalloc.start()
        gc.collect()
        mem_before, _ = tracemalloc.get_traced_memory()

        num_inst = 50
        reg = InstrumentRegistry()
        engine = FeatureEngine(registry=reg)

        for i in range(num_inst):
            iid = InstrumentId(f"MEM_{i:03d}", Exchange.NSE, InstrumentType.EQUITY)
            reg.register(iid, {"ATMSTOX": f"TOK_{i:03d}"})
            engine.register_indicator(iid, StreamingEMA(TF_1M, period=20))
            engine.register_indicator(iid, StreamingRSI(TF_1M, period=14))
            engine.register_indicator(iid, RollingBollingerBands(TF_1M, period=20))
            engine.register_microstructure_feature(iid, BookImbalance())

            # Seed with 30 bars
            store = engine.get_or_create_store(iid)
            for m in range(30):
                t = self.base_time + timedelta(minutes=m + 1)
                store.process_closed_candle(make_candle(iid, t, close=100.0 + m))

        gc.collect()
        mem_after, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        net_mb = (mem_after - mem_before) / (1024 * 1024)
        kb_per_inst = (net_mb * 1024) / num_inst

        print("\n--- PHASE 4 BENCHMARK: MEMORY SCALING ---")
        print(f"Instruments: {num_inst}")
        print(f"Total Net Memory: {net_mb:.2f} MB")
        print(f"Memory Per Instrument: {kb_per_inst:.2f} KB")

        # Must be well below target of 250 KB
        self.assertLess(kb_per_inst, 250.0)


if __name__ == "__main__":
    unittest.main()
