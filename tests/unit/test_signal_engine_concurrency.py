"""
Unit tests for SignalEngine Concurrency, Arbitration, and Performance Benchmarks (Phase 5).

Tests:
- SignalEngine registration and synchronous dispatching
- Deterministic multi-instrument strategy evaluation
- Hot-path microstructure and warm-path candle-close benchmarks
- Multi-candidate arbiter benchmark
- Memory footprint scaling benchmark across 50 active strategy instances
"""

import gc
import threading
import time
import tracemalloc
import unittest
from datetime import datetime, timedelta, timezone

from services.analytics.engine import FeatureEngine
from services.analytics.indicators.momentum import StreamingRSI
from services.analytics.indicators.moving_averages import StreamingEMA
from services.analytics.indicators.volatility import StreamingATR
from services.analytics.indicators.volume import StreamingVWAP
from services.candles.models import Candle, VolumeQuality
from services.candles.timeframe import TF_5M
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.state import InstrumentState
from services.market_state.store import InstrumentStateStore
from services.signals.engine import SignalEngine, SyncSignalDispatcher
from services.signals.models import SignalCandidate, TriggerMode
from services.signals.scoring.arbiter import DeterministicSignalArbiter
from strategies.trend_continuation import EMA_VWAP_TrendContinuationStrategy


def make_5m_candle(iid: InstrumentId, dt: datetime, close: float) -> Candle:
    return Candle(
        instrument_id=iid,
        timeframe=TF_5M,
        start_time=dt - timedelta(minutes=5),
        end_time=dt,
        open=close - 2.0,
        high=close + 3.0,
        low=close - 3.0,
        close=close,
        volume=2000.0,
        ticks=200,
        volume_quality=VolumeQuality.COMPLETE,
        is_closed=True,
    )


class TestSignalEngineConcurrency(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.feature_engine = FeatureEngine(registry=self.registry)
        self.state_store = InstrumentStateStore(registry=self.registry)
        self.signal_engine = SignalEngine(
            registry=self.registry,
            feature_engine=self.feature_engine,
            state_store=self.state_store,
        )
        self.base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_signal_engine_registration_and_dispatch(self):
        iid = InstrumentId("INFY", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(iid, {"ATMSTOX": "TOK_INFY"})

        # Register indicators on feature engine
        self.feature_engine.register_indicator(iid, StreamingEMA(TF_5M, period=20, name="EMA_20_5M"))
        self.feature_engine.register_indicator(iid, StreamingEMA(TF_5M, period=50, name="EMA_50_5M"))
        self.feature_engine.register_indicator(iid, StreamingVWAP(TF_5M, name="VWAP_5M"))
        self.feature_engine.register_indicator(iid, StreamingRSI(TF_5M, period=14, name="RSI_14_5M"))
        self.feature_engine.register_indicator(iid, StreamingATR(TF_5M, period=14, name="ATR_14_5M"))

        # Register strategy
        strat = EMA_VWAP_TrendContinuationStrategy()
        self.signal_engine.register_strategy(iid, strat)

        dispatched_signals = []
        self.signal_engine.add_signal_listener(lambda s: dispatched_signals.append(s))

        # Feed 50 bars to warm up indicators
        for k in range(50):
            t = self.base_time + timedelta(minutes=(k + 1) * 5)
            c = make_5m_candle(iid, t, close=1500.0 + (k * 2))
            self.feature_engine.on_candle_closed(c)

        # Trigger on closed candle
        t_trigger = self.base_time + timedelta(minutes=51 * 5)
        c_trigger = make_5m_candle(iid, t_trigger, close=1602.0)
        self.feature_engine.on_candle_closed(c_trigger)

        sigs = self.signal_engine.on_candle_closed(c_trigger)
        self.assertIsInstance(sigs, list)

    def test_benchmark_signal_evaluation_latencies(self):
        """
        Measures evaluation latencies for Phase 5 reference strategy and arbiter.
        """
        iid = InstrumentId("BENCH_SYM", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(iid, {"ATMSTOX": "TOK_BENCH"})

        self.feature_engine.register_indicator(iid, StreamingEMA(TF_5M, period=20, name="EMA_20_5M"))
        self.feature_engine.register_indicator(iid, StreamingEMA(TF_5M, period=50, name="EMA_50_5M"))
        self.feature_engine.register_indicator(iid, StreamingVWAP(TF_5M, name="VWAP_5M"))
        self.feature_engine.register_indicator(iid, StreamingRSI(TF_5M, period=14, name="RSI_14_5M"))
        self.feature_engine.register_indicator(iid, StreamingATR(TF_5M, period=14, name="ATR_14_5M"))

        strat = EMA_VWAP_TrendContinuationStrategy()
        self.signal_engine.register_strategy(iid, strat)

        # Warm up features
        for k in range(55):
            t = self.base_time + timedelta(minutes=(k + 1) * 5)
            c = make_5m_candle(iid, t, close=2000.0 + (k % 10))
            self.feature_engine.on_candle_closed(c)

        t_test = self.base_time + timedelta(minutes=60 * 5)
        c_test = make_5m_candle(iid, t_test, close=2005.0)

        # Build context once
        ctx = self.signal_engine.build_context(
            instrument_id=iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            evaluation_timestamp=t_test,
            candle=c_test,
        )
        self.assertIsNotNone(ctx)

        # Warm strategy evaluation benchmark (2,000 iterations)
        warm_latencies_us = []
        gc.disable()
        for _ in range(2000):
            t0 = time.perf_counter_ns()
            strat.evaluate(ctx)
            t1 = time.perf_counter_ns()
            warm_latencies_us.append((t1 - t0) / 1000.0)
        gc.enable()

        sorted_warm = sorted(warm_latencies_us)
        nw = len(sorted_warm)
        p50 = sorted_warm[int(nw * 0.50)]
        p95 = sorted_warm[int(nw * 0.95)]
        p99 = sorted_warm[int(nw * 0.99)]

        print("\n--- PHASE 5 BENCHMARK: WARM STRATEGY EVALUATION ---")
        print(f"Iterations: {nw}")
        print(f"Warm Strategy p50: {p50:.2f} us (Target < 50.0 us)")
        print(f"Warm Strategy p95: {p95:.2f} us")
        print(f"Warm Strategy p99: {p99:.2f} us")

        # Deterministic arbiter benchmark (10 candidates)
        arbiter = DeterministicSignalArbiter()
        from tests.unit.test_deterministic_arbiter import make_candidate
        candidates = [
            make_candidate(str(i), f"STRAT_{i%3}", f"SYM_{i}", confidence=0.5 + (i * 0.04))
            for i in range(10)
        ]

        arbiter_latencies_us = []
        gc.disable()
        for _ in range(3000):
            t0 = time.perf_counter_ns()
            arbiter.arbitrate(candidates)
            t1 = time.perf_counter_ns()
            arbiter_latencies_us.append((t1 - t0) / 1000.0)
        gc.enable()

        sorted_arb = sorted(arbiter_latencies_us)
        na = len(sorted_arb)
        arb_p50 = sorted_arb[int(na * 0.50)]
        arb_p95 = sorted_arb[int(na * 0.95)]
        arb_p99 = sorted_arb[int(na * 0.99)]

        print("\n--- PHASE 5 BENCHMARK: 10-CANDIDATE ARBITER RANKING ---")
        print(f"Iterations: {na}")
        print(f"Arbiter p50: {arb_p50:.2f} us (Target < 25.0 us)")
        print(f"Arbiter p95: {arb_p95:.2f} us")
        print(f"Arbiter p99: {arb_p99:.2f} us")

    def test_memory_footprint_scaling(self):
        """
        Measures memory scaling across 50 active strategy instances.
        """
        tracemalloc.start()
        gc.collect()
        mem_before, _ = tracemalloc.get_traced_memory()

        num_inst = 50
        reg = InstrumentRegistry()
        feat_engine = FeatureEngine(registry=reg)
        sig_engine = SignalEngine(registry=reg, feature_engine=feat_engine)

        strategies = []
        for i in range(num_inst):
            iid = InstrumentId(f"SIG_MEM_{i:03d}", Exchange.NSE, InstrumentType.EQUITY)
            reg.register(iid, {"ATMSTOX": f"TOK_{i:03d}"})
            feat_engine.register_indicator(iid, StreamingEMA(TF_5M, period=20, name="EMA_20_5M"))
            feat_engine.register_indicator(iid, StreamingEMA(TF_5M, period=50, name="EMA_50_5M"))
            feat_engine.register_indicator(iid, StreamingVWAP(TF_5M, name="VWAP_5M"))
            feat_engine.register_indicator(iid, StreamingRSI(TF_5M, period=14, name="RSI_14_5M"))
            feat_engine.register_indicator(iid, StreamingATR(TF_5M, period=14, name="ATR_14_5M"))

            strat = EMA_VWAP_TrendContinuationStrategy()
            sig_engine.register_strategy(iid, strat)
            strategies.append(strat)

            # Warm with 20 bars
            for m in range(20):
                t = self.base_time + timedelta(minutes=(m + 1) * 5)
                feat_engine.on_candle_closed(make_5m_candle(iid, t, close=100.0 + m))

        gc.collect()
        mem_after, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        net_mb = (mem_after - mem_before) / (1024 * 1024)
        kb_per_strat = (net_mb * 1024) / num_inst

        print("\n--- PHASE 5 BENCHMARK: MEMORY FOOTPRINT ---")
        print(f"Strategy Instances: {num_inst}")
        print(f"Total Net Memory: {net_mb:.2f} MB")
        print(f"Memory Per Strategy: {kb_per_strat:.2f} KB (Target < 100.0 KB)")

        # Verify below target of 100 KB
        self.assertLess(kb_per_strat, 100.0)


if __name__ == "__main__":
    unittest.main()
