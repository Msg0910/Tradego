"""
TradeGo Milestone 2 — Realtime Runtime Latency & Pipeline Micro-Benchmark.

Exercises the complete in-memory pipeline:
MarketEvent -> StateStore -> CandleEngine -> FeatureEngine -> SignalEngine
-> RiskEngine -> ExecutionPlanner -> ExecutionRouter -> PaperExecutionAdapter -> Telemetry.

Measures T1 through T10 latency distributions across multiple synthetic workloads:
1. Low tick rate (50 ticks)
2. Moderate tick rate (500 ticks)
3. High tick burst (2,500 ticks)
4. Multi-symbol workload (5 symbols, 1,000 ticks)
5. Multi-strategy workload (3 strategies per symbol, 500 ticks)
"""

from datetime import datetime, timedelta, timezone
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from services.analytics.engine import FeatureEngine
from services.candles.calendar import IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.timeframe import TF_1M
from services.execution.config import ExecutionConfig
from services.execution.models import CanonicalOrderStatus, OrderSide
from services.execution.paper_adapter import PaperExecutionAdapter
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentRegistry, InstrumentType
from services.market_state.store import InstrumentStateStore
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.limits import RiskLimits
from services.signals.base import BaseStrategy
from services.signals.context import StrategyContext
from services.signals.engine import SignalEngine
from services.signals.models import DecisionType, SignalCandidate, SignalType, StrategyDecision, TriggerMode
from services.runtime.config import RuntimeConfig
from services.runtime.coordinator import PipelineCoordinator
from services.runtime.models import RuntimeCorrelationRecord, RuntimeMode


from services.candles.calendar import INDIA_TZ, IndianMarketCalendar

class SyntheticBreakoutStrategy(BaseStrategy):
    """Deterministic synthetic strategy that emits actionable signals periodically on bar close."""

    def __init__(
        self,
        strategy_id: str,
        trigger_mode: TriggerMode = TriggerMode.BAR_CLOSE,
        trigger_frequency: int = 1,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            config_hash=f"hash_{strategy_id}",
            trigger_mode=trigger_mode,
        )
        self.trigger_frequency = trigger_frequency
        self._eval_count = 0

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        self._eval_count += 1
        if self._eval_count % self.trigger_frequency != 0:
            return StrategyDecision(decision=DecisionType.NO_TRADE)

        now = context.evaluation_timestamp
        inst = context.instrument_id
        current_price = context.active_candle.close if context.active_candle else 1000.0

        is_in_position = context.position is not None and context.position.is_long
        signal_type = SignalType.EXIT_LONG if is_in_position else SignalType.ENTRY_LONG
        direction = -1 if is_in_position else 1

        candidate = SignalCandidate(
            signal_id=f"SIG_{self.strategy_id}_{self._eval_count}_{inst.symbol}",
            fingerprint=f"FP_{self.strategy_id}_{self._eval_count}_{inst.symbol}",
            reaffirmation_key=f"REAF_{self.strategy_id}_{self._eval_count}_{inst.symbol}",
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_hash=self.config_hash,
            instrument_id=inst,
            signal_type=signal_type,
            direction=direction,
            trigger_mode=self.trigger_mode,
            confidence_score=0.88,
            suggested_entry_price=current_price,
            suggested_stop_loss=None if is_in_position else current_price * 0.98,
            suggested_take_profit=None if is_in_position else current_price * 1.04,
            risk_reward_ratio=None if is_in_position else 2.0,
            market_timestamp=now,
            availability_timestamp=now,
            generated_timestamp=now,
        )
        return StrategyDecision(decision=DecisionType.TRADE, candidate=candidate)


def compute_distribution(latencies_ns: List[int]) -> Dict[str, float]:
    if not latencies_ns:
        return {
            "count": 0,
            "min_us": 0.0,
            "mean_us": 0.0,
            "p50_us": 0.0,
            "p90_us": 0.0,
            "p95_us": 0.0,
            "p99_us": 0.0,
            "p999_us": 0.0,
            "max_us": 0.0,
        }

    us = [d / 1000.0 for d in latencies_ns]
    us.sort()
    n = len(us)
    return {
        "count": n,
        "min_us": round(us[0], 2),
        "mean_us": round(statistics.mean(us), 2),
        "p50_us": round(us[int(n * 0.50)], 2),
        "p90_us": round(us[int(n * 0.90)], 2),
        "p95_us": round(us[int(n * 0.95)], 2),
        "p99_us": round(us[int(n * 0.99)], 2),
        "p999_us": round(us[int(n * 0.999)], 2) if n >= 1000 else round(us[-1], 2),
        "max_us": round(us[-1], 2),
    }


def build_pipeline(
    symbols: List[str],
    strategies_per_symbol: int = 1,
    min_intrabar_interval_ms: float = 0.0,  # 0 ms for benchmarking raw engine throughput
) -> Tuple[PipelineCoordinator, InstrumentRegistry, List[InstrumentId]]:
    registry = InstrumentRegistry()
    instrument_ids: List[InstrumentId] = []

    for sym in symbols:
        iid = InstrumentId(symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY)
        instrument_ids.append(iid)
        registry.register(
            instrument_id=iid,
            lot_size=1,
            tick_size=0.05,
            provider_tokens={"ATMSTOX": sym},
        )

    gateway = MarketDataGateway()
    state_store = InstrumentStateStore(registry=registry)
    calendar = IndianMarketCalendar()
    candle_engine = CandleEngine(registry=registry, calendar=calendar)
    feature_engine = FeatureEngine(registry=registry, candle_engine=candle_engine)

    for iid in instrument_ids:
        feature_engine.get_or_create_store(iid)

    signal_engine = SignalEngine(
        registry=registry,
        feature_engine=feature_engine,
        candle_engine=candle_engine,
        state_store=state_store,
    )

    for iid in instrument_ids:
        for s_idx in range(strategies_per_symbol):
            strat = SyntheticBreakoutStrategy(
                strategy_id=f"BENCH_STRAT_{s_idx}_{iid.symbol}",
                trigger_mode=TriggerMode.BAR_CLOSE,
                trigger_frequency=1,
            )
            signal_engine.register_strategy(iid, strat)

    risk_limits = RiskLimits(max_concurrent_positions=100)
    admission_state = AdmissionState()
    risk_engine = RiskEngine(limits=risk_limits, admission_state=admission_state)

    exec_config = ExecutionConfig(max_order_notional=100_000_000.0)
    exec_registry = ExecutionStateRegistry()
    paper_adapter = PaperExecutionAdapter(config=exec_config)
    router = ExecutionRouter(
        adapter=paper_adapter,
        registry=exec_registry,
        calendar=calendar,
        config=exec_config,
    )

    runtime_config = RuntimeConfig(
        runtime_mode=RuntimeMode.PAPER,
        active_symbols=tuple(symbols),
        min_intrabar_eval_interval_ms=min_intrabar_interval_ms,
        max_daily_loss=100_000_000.0,
        telemetry_ring_buffer_size=131072,
    )

    coordinator = PipelineCoordinator(
        config=runtime_config,
        gateway=gateway,
        state_store=state_store,
        candle_engine=candle_engine,
        feature_engine=feature_engine,
        signal_engine=signal_engine,
        risk_engine=risk_engine,
        router=router,
        paper_adapter=paper_adapter,
        registry=registry,
        initial_cash=100_000_000.0,
    )
    coordinator.start()

    return coordinator, registry, instrument_ids


def make_synthetic_tick(
    symbol: str,
    price: float,
    tick_index: int,
    base_time: datetime,
) -> MarketEvent:
    current_time = base_time + timedelta(seconds=tick_index * 2)
    now_perf = time.perf_counter()

    depth = MarketDepth(
        bids=[
            DepthLevel(price=round(price - 0.05, 2), quantity=100),
            DepthLevel(price=round(price - 0.10, 2), quantity=200),
        ],
        asks=[
            DepthLevel(price=round(price + 0.05, 2), quantity=100),
            DepthLevel(price=round(price + 0.10, 2), quantity=200),
        ],
        total_buy_qty=300,
        total_sell_qty=300,
    )

    return MarketEvent(
        provider="ATMSTOX",
        provider_symbol_id=symbol,
        symbol=symbol,
        exchange_timestamp=current_time,
        provider_timestamp=current_time,
        local_receive_timestamp=now_perf,
        local_receive_datetime=current_time,
        normalized_timestamp=now_perf + 0.000005,
        ltp=price,
        ltp_qty=10,
        bid=round(price - 0.05, 2),
        bid_qty=100,
        ask=round(price + 0.05, 2),
        ask_qty=100,
        spread=0.10,
        depth=depth,
        tick_volume=10.0,
        total_volume=1000.0 + tick_index * 10.0,
        atp=price,
        open=price - 1.0,
        high=price + 2.0,
        low=price - 2.0,
        previous_close=price - 1.5,
        oi=50000,
        raw={},
    )


def run_benchmark_workload(
    name: str,
    symbols: List[str],
    tick_count: int,
    strategies_per_symbol: int = 1,
    min_intrabar_interval_ms: float = 0.0,
) -> Dict[str, Any]:
    coordinator, registry, instrument_ids = build_pipeline(
        symbols=symbols,
        strategies_per_symbol=strategies_per_symbol,
        min_intrabar_interval_ms=min_intrabar_interval_ms,
    )

    # Regular trading hours: 09:30:00 AM IST on a Monday
    base_time = datetime(2026, 9, 28, 9, 30, 0, tzinfo=INDIA_TZ)

    # Warmup (100 ticks)
    for i in range(100):
        sym = symbols[i % len(symbols)]
        warmup_ev = make_synthetic_tick(sym, 1000.0 + (i % 20) * 0.5, i, base_time)
        coordinator.on_market_event(warmup_ev)

    coordinator.telemetry.clear()

    # Pre-generate ticks to eliminate generation overhead during measurement
    ticks: List[MarketEvent] = []
    for i in range(tick_count):
        sym = symbols[i % len(symbols)]
        price = 1000.0 + (i % 50) * 0.10
        ticks.append(make_synthetic_tick(sym, price, i, base_time))

    # Execute burst measurement
    t_start = time.perf_counter_ns()
    for ev in ticks:
        coordinator.on_market_event(ev)
    t_end = time.perf_counter_ns()

    total_duration_ms = (t_end - t_start) / 1e6
    throughput = (tick_count / (total_duration_ms / 1000.0)) if total_duration_ms > 0 else 0.0

    records: List[RuntimeCorrelationRecord] = coordinator.telemetry.get_records()

    # Extract latency vectors from both telemetry records and in-flight correlations
    # (In paper trading, synchronous immediate fills store T1-T8 in _in_flight_correlations)
    t1_t2: List[int] = []
    t2_t4: List[int] = []
    t4_t6: List[int] = []
    t6_t7: List[int] = []
    t1_t7: List[int] = []
    t1_t10: List[int] = []

    t2_t3: List[int] = []
    t3_t4: List[int] = []
    t4_t5: List[int] = []
    t5_t6: List[int] = []
    t7_t8: List[int] = []
    t8_t9: List[int] = []
    t9_t10: List[int] = []

    # 1. From telemetry records (if emitted)
    for r in records:
        if r.t7_wire_dispatched_ns >= r.t1_market_receive_ns:
            t1_t2.append(r.t2_signal_generated_ns - r.t1_market_receive_ns)
            t2_t4.append(r.t4_intent_approved_ns - r.t2_signal_generated_ns)
            t4_t6.append(r.t6_order_submitted_ns - r.t4_intent_approved_ns)
            t6_t7.append(r.t7_wire_dispatched_ns - r.t6_order_submitted_ns)
            t1_t7.append(r.t7_wire_dispatched_ns - r.t1_market_receive_ns)
            t1_t10.append(r.t10_position_updated_ns - r.t1_market_receive_ns)

            t2_t3.append(r.t3_risk_evaluated_ns - r.t2_signal_generated_ns)
            t3_t4.append(r.t4_intent_approved_ns - r.t3_risk_evaluated_ns)
            t4_t5.append(r.t5_order_planned_ns - r.t4_intent_approved_ns)
            t5_t6.append(r.t6_order_submitted_ns - r.t5_order_planned_ns)
            t7_t8.append(r.t8_order_acked_ns - r.t7_wire_dispatched_ns)
            t8_t9.append(r.t9_fill_received_ns - r.t8_order_acked_ns)
            t9_t10.append(r.t10_position_updated_ns - r.t9_fill_received_ns)

    # 2. From in-flight correlations (authoritative T1-T8 record stored in coordinator)
    with coordinator._correlation_lock:
        in_flight = list(coordinator._in_flight_correlations.values())

    for corr in in_flight:
        t1 = corr["t1_ns"]
        t2 = corr["t2_ns"]
        t3 = corr["t3_ns"]
        t4 = corr["t4_ns"]
        t5 = corr["t5_ns"]
        t6 = corr["t6_ns"]
        t7 = corr["t7_ns"]
        t8 = corr["t8_ns"]

        if t7 >= t1:
            t1_t2.append(t2 - t1)
            t2_t4.append(t4 - t2)
            t4_t6.append(t6 - t4)
            t6_t7.append(t7 - t6)
            t1_t7.append(t7 - t1)

            t2_t3.append(t3 - t2)
            t3_t4.append(t4 - t3)
            t4_t5.append(t5 - t4)
            t5_t6.append(t6 - t5)
            t7_t8.append(t8 - t7)

    coordinator.stop()

    return {
        "workload_name": name,
        "symbols": symbols,
        "symbol_count": len(symbols),
        "strategies_per_symbol": strategies_per_symbol,
        "total_ticks": tick_count,
        "orders_executed": len(t1_t7),
        "total_wall_duration_ms": round(total_duration_ms, 2),
        "throughput_ticks_per_sec": round(throughput, 1),
        "avg_tick_processing_us": round((total_duration_ms * 1000.0) / tick_count, 2),
        "primary_metric_t1_t7": compute_distribution(t1_t7),
        "full_lineage_t1_t10": compute_distribution(t1_t10),
        "stages": {
            "T1_to_T2_tick_to_signal": compute_distribution(t1_t2),
            "T2_to_T4_signal_to_risk": compute_distribution(t2_t4),
            "T4_to_T6_risk_to_plan": compute_distribution(t4_t6),
            "T6_to_T7_plan_to_dispatch": compute_distribution(t6_t7),
            "sub_stages": {
                "T2_to_T3_signal_to_risk_handoff": compute_distribution(t2_t3),
                "T3_to_T4_risk_engine_evaluation": compute_distribution(t3_t4),
                "T4_to_T5_risk_to_planner_handoff": compute_distribution(t4_t5),
                "T5_to_T6_order_planning": compute_distribution(t5_t6),
                "T7_to_T8_broker_ack": compute_distribution(t7_t8),
                "T8_to_T9_fill_delivery": compute_distribution(t8_t9),
                "T9_to_T10_position_accounting": compute_distribution(t9_t10),
            },
        },
    }


def run_all_benchmarks() -> Dict[str, Any]:
    print("=" * 80)
    print("🚀 TRADEGO MILESTONE 2: RUNTIME LATENCY & PIPELINE MICRO-BENCHMARK")
    print("=" * 80)

    workloads = [
        ("Workload 1: Low Tick Rate", ["RELIANCE"], 50, 1),
        ("Workload 2: Moderate Tick Rate", ["RELIANCE"], 500, 1),
        ("Workload 3: High Tick Burst", ["RELIANCE"], 2500, 1),
        ("Workload 4: Multi-Symbol Workload (5 Symbols)", ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"], 1000, 1),
        ("Workload 5: Multi-Strategy Workload (3 Strats)", ["RELIANCE"], 500, 3),
    ]

    all_results = {}
    for name, syms, ticks, strats in workloads:
        print(f"\nRunning {name} ({ticks} ticks, {len(syms)} symbols, {strats} strats)...")
        res = run_benchmark_workload(
            name=name,
            symbols=syms,
            tick_count=ticks,
            strategies_per_symbol=strats,
        )
        all_results[name] = res
        t1_t7 = res["primary_metric_t1_t7"]
        print(f"  • Throughput    : {res['throughput_ticks_per_sec']} ticks/sec")
        print(f"  • Avg Tick Time : {res['avg_tick_processing_us']} µs")
        print(f"  • T1->T7 P50    : {t1_t7['p50_us']} µs")
        print(f"  • T1->T7 P95    : {t1_t7['p95_us']} µs")
        print(f"  • T1->T7 P99    : {t1_t7['p99_us']} µs")
        print(f"  • T1->T7 Max    : {t1_t7['max_us']} µs")
        print(f"  • Orders Placed : {res['orders_executed']}")

    return all_results


if __name__ == "__main__":
    results = run_all_benchmarks()
    with open("scripts/benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved detailed benchmark results to scripts/benchmark_results.json")
