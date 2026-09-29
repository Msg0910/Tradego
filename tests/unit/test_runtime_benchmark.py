"""
Performance Benchmark for Tradego Trading Runtime (Phase 8).

Profiles optimization targets:
- T1: Paper trade path (p50 < 100.0 us)
- T2: Strategy scheduling watermark check (p50 < 15.0 us)
- T3: Telemetry ring buffer insertion (p50 < 2.0 us)

Following Phase 6/7 governance, benchmarks are directional optimization targets,
not rigid contractual failure gates.
"""

from datetime import datetime, timedelta, timezone
import time
from typing import List
import unittest

from services.candles.calendar import IndianMarketCalendar
from services.candles.timeframe import TF_5M, TimeFrame
from services.execution.config import ExecutionConfig
from services.execution.paper_adapter import PaperExecutionAdapter
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentRegistry, InstrumentType
from services.risk.engine import RiskEngine
from services.signals.models import SignalCandidate, SignalType, TriggerMode
from services.runtime.execution_coordinator import ExecutionCoordinator
from services.runtime.guards import TradingGuard
from services.runtime.models import RuntimeCorrelationRecord
from services.runtime.portfolio import PortfolioRuntimeState
from services.runtime.scheduler import StrategyScheduler
from services.runtime.signal_risk_coordinator import SignalRiskCoordinator
from services.runtime.telemetry import TelemetryCollector


class TestRuntimeBenchmark(unittest.TestCase):
    """Performance benchmarks for Phase 8 runtime components."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="BENCH_SYM",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime(2026, 9, 15, 9, 15, 0, tzinfo=timezone.utc)

    def test_t1_paper_trade_path_latency(self) -> None:
        """
        Target T1: Paper trade path latency (Signal -> Risk -> Intent -> Planner -> Router -> PaperAdapter).
        Optimization target: p50 < 100.0 us.
        """
        calendar = IndianMarketCalendar()
        registry = InstrumentRegistry()
        registry.register(self.instrument_id, lot_size=1, tick_size=0.05)

        guard = TradingGuard()
        portfolio_state = PortfolioRuntimeState("BENCH_ACCOUNT", initial_cash=100_000_000.0)
        risk_engine = RiskEngine()
        signal_risk = SignalRiskCoordinator(risk_engine, portfolio_state, guard, registry)

        config = ExecutionConfig(max_order_notional=100_000_000.0, rate_limit_max_queue_depth=100000)
        exec_registry = ExecutionStateRegistry()
        paper_adapter = PaperExecutionAdapter(config=config)
        # Prevent token starvation in benchmark loop
        paper_adapter._rate_limiter._tokens = 1_000_000.0
        paper_adapter._rate_limiter._capacity = 1_000_000.0

        router = ExecutionRouter(paper_adapter, exec_registry, calendar, config)
        router.is_accepting_orders = True
        execution_coord = ExecutionCoordinator(router, portfolio_state, guard)

        iterations = 1000
        latencies_ns: List[int] = []

        # Warm-up (50 iterations)
        for i in range(50):
            sig = SignalCandidate(
                signal_id=f"sig-warmup-{i}",
                fingerprint=f"fp-warmup-{i}",
                reaffirmation_key=f"rk-warmup-{i}",
                strategy_id="BENCH_STRAT",
                strategy_version="1.0.0",
                config_hash="cfg_hash",
                instrument_id=self.instrument_id,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=TriggerMode.BAR_CLOSE,
                confidence_score=0.9,
                suggested_entry_price=1000.0,
                suggested_stop_loss=980.0,
                suggested_take_profit=1040.0,
                risk_reward_ratio=2.0,
                market_timestamp=self.now,
                availability_timestamp=self.now,
                generated_timestamp=self.now,
            )
            intent = signal_risk.evaluate_signal(sig, current_price=1000.0)
            if intent:
                execution_coord.execute_intent(intent)

        # Benchmark iterations
        for i in range(iterations):
            sig = SignalCandidate(
                signal_id=f"sig-bench-{i}",
                fingerprint=f"fp-bench-{i}",
                reaffirmation_key=f"rk-bench-{i}",
                strategy_id="BENCH_STRAT",
                strategy_version="1.0.0",
                config_hash="cfg_hash",
                instrument_id=self.instrument_id,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=TriggerMode.BAR_CLOSE,
                confidence_score=0.9,
                suggested_entry_price=1000.0,
                suggested_stop_loss=980.0,
                suggested_take_profit=1040.0,
                risk_reward_ratio=2.0,
                market_timestamp=self.now,
                availability_timestamp=self.now,
                generated_timestamp=self.now,
            )
            t0 = time.perf_counter_ns()
            intent = signal_risk.evaluate_signal(sig, current_price=1000.0)
            if intent:
                execution_coord.execute_intent(intent)
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0
        max_us = latencies_ns[-1] / 1000.0

        print("\n--- PHASE 8 BENCHMARK: T1 PAPER TRADE PATH LATENCY ---")
        print(f"Iterations: {n}")
        print(f"Trade Path p50: {p50_us:.2f} us (Optimization Target < 100.0 us)")
        print(f"Trade Path p95: {p95_us:.2f} us")
        print(f"Trade Path p99: {p99_us:.2f} us")
        print(f"Trade Path max: {max_us:.2f} us")
        self.assertGreater(n, 0)

    def test_t2_strategy_scheduling_watermark_latency(self) -> None:
        """
        Target T2: Strategy scheduling watermark check latency.
        Optimization target: p50 < 15.0 us.
        """
        scheduler = StrategyScheduler(min_intrabar_interval_ms=100.0)
        iterations = 5000

        # Generate consecutive timestamps
        timestamps = [self.now + timedelta(minutes=5 * i) for i in range(iterations + 500)]

        # Warm-up
        for i in range(500):
            scheduler.should_evaluate_bar_close("STRAT_BENCH", self.instrument_id, TF_5M, timestamps[i])
            scheduler.advance_watermark("STRAT_BENCH", self.instrument_id, TF_5M, timestamps[i])

        latencies_ns: List[int] = []
        for i in range(500, 500 + iterations):
            t0 = time.perf_counter_ns()
            eligible = scheduler.should_evaluate_bar_close(
                "STRAT_BENCH", self.instrument_id, TF_5M, timestamps[i]
            )
            if eligible:
                scheduler.advance_watermark(
                    "STRAT_BENCH", self.instrument_id, TF_5M, timestamps[i]
                )
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0
        max_us = latencies_ns[-1] / 1000.0

        print("\n--- PHASE 8 BENCHMARK: T2 STRATEGY SCHEDULING WATERMARK ---")
        print(f"Iterations: {n}")
        print(f"Scheduling p50: {p50_us:.2f} us (Optimization Target < 15.0 us)")
        print(f"Scheduling p95: {p95_us:.2f} us")
        print(f"Scheduling p99: {p99_us:.2f} us")
        print(f"Scheduling max: {max_us:.2f} us")
        self.assertGreater(n, 0)

    def test_t3_telemetry_insertion_latency(self) -> None:
        """
        Target T3: Telemetry ring buffer insertion latency.
        Optimization target: p50 < 2.0 us.
        """
        collector = TelemetryCollector(capacity=65536)
        iterations = 5000

        record = RuntimeCorrelationRecord(
            client_order_id="TG-BENCH-001",
            intent_id="intent-bench-001",
            signal_id="sig-bench-001",
            signal_fingerprint="fp-bench-001",
            reaffirmation_key="rk-bench-001",
            idempotency_key="idempotency-bench-001",
            instrument_canonical_id="NSE:BENCH_SYM",
            strategy_id="BENCH_STRAT",
            t1_market_receive_ns=1000,
            t2_signal_generated_ns=2000,
            t3_risk_evaluated_ns=3000,
            t4_intent_approved_ns=4000,
            t5_order_planned_ns=5000,
            t6_order_submitted_ns=6000,
            t7_wire_dispatched_ns=7000,
            t8_order_acked_ns=8000,
            t9_fill_received_ns=9000,
            t10_position_updated_ns=10000,
        )

        # Warm-up
        for _ in range(500):
            collector.record_lineage(record)

        collector.clear()

        latencies_ns: List[int] = []
        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            collector.record_lineage(record)
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0
        max_us = latencies_ns[-1] / 1000.0

        print("\n--- PHASE 8 BENCHMARK: T3 TELEMETRY INSERTION LATENCY ---")
        print(f"Iterations: {n}")
        print(f"Telemetry p50: {p50_us:.2f} us (Optimization Target < 2.0 us)")
        print(f"Telemetry p95: {p95_us:.2f} us")
        print(f"Telemetry p99: {p99_us:.2f} us")
        print(f"Telemetry max: {max_us:.2f} us")
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main()
