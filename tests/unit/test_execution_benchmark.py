"""
Performance benchmarks and microsecond telemetry measurement for Phase 7 Execution Layer.
Profiles execution planning, router dispatch, fill processing, and memory scaling using time.perf_counter_ns().
Performance numbers are optimization targets, NOT rigid acceptance gates.
"""

from datetime import datetime, timezone
import gc
import sys
import time
from typing import List, Optional
import unittest

from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    Fill,
    OrderAcknowledgement,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    PositionEffect,
    SubmissionOutcomeType,
    SubmissionResult,
    TimeInForce,
)
from services.execution.planner import ExecutionPlanner
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionState, ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.risk.models import ApprovedTradeIntent, PositionSnapshot
from services.signals.models import SignalType


class FastBenchmarkAdapter(BrokerExecutionAdapter):
    """Zero-overhead mock adapter for pure CPU latency benchmarking."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        return SubmissionResult(
            outcome=SubmissionOutcomeType.ACKNOWLEDGED,
            acknowledgement=OrderAcknowledgement(
                client_order_id=request.client_order_id,
                broker_order_id="BRK-BENCH",
                order_version=1,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=1000,
            ),
        )

    def cancel_order(self, client_order_id: str, broker_order_id: str) -> bool:
        return True

    def replace_order(
        self, client_order_id: str, broker_order_id: str, new_price: Optional[float], new_quantity: Optional[int]
    ) -> OrderAcknowledgement:
        return OrderAcknowledgement(
            client_order_id=client_order_id,
            broker_order_id="BRK-BENCH-2",
            order_version=2,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=2000,
        )

    def get_order_status(self, client_order_id: str, broker_order_id: Optional[str]) -> OrderUpdate:
        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id or "BRK-BENCH",
            status=CanonicalOrderStatus.ACKNOWLEDGED,
            cumulative_filled_quantity=0,
            remaining_quantity=0,
            average_price=0.0,
            timestamp=self.now,
            monotonic_ns=1000,
        )

    def get_open_orders(self) -> List[OrderUpdate]:
        return []

    def get_positions(self) -> List[PositionSnapshot]:
        return []

    def health_check(self) -> bool:
        return True


class TestExecutionBenchmark(unittest.TestCase):
    """Profiles execution stage durations via monotonic integer nanoseconds (time.perf_counter_ns())."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="TCS",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)

    def test_execution_planning_latency_benchmark(self) -> None:
        intents = [
            ApprovedTradeIntent(
                intent_id=f"intent-bench-{i:06d}",
                signal_id=f"sig-{i}",
                fingerprint=f"fp-{i}",
                reaffirmation_key=f"reaff-{i}",
                strategy_id="MOMENTUM_ALPHA",
                strategy_version="1.0.0",
                risk_config_version="1.0.0",
                risk_config_hash="hash-bench",
                instrument_id=self.instrument_id,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                permitted_quantity=10,
                approved_entry_price=3500.0,
                approved_stop_loss=3400.0,
                approved_take_profit=3700.0,
                risk_reward_ratio=2.0,
                calculated_monetary_risk=1000.0,
                allocated_capital=35000.0,
                market_timestamp=self.now,
                signal_generated_timestamp=self.now,
                intent_generated_timestamp=self.now,
                expiry_timestamp=None,
                binding_constraint="TEST",
            )
            for i in range(2000)
        ]

        # Warm-up iterations
        for i in range(200):
            ExecutionPlanner.plan_order(intents[i], current_time=self.now)

        latencies_ns: List[int] = []
        for intent in intents[200:]:
            t0 = time.perf_counter_ns()
            ExecutionPlanner.plan_order(intent, current_time=self.now)
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0

        print(f"\n--- PHASE 7 BENCHMARK: EXECUTION PLANNING LATENCY ---")
        print(f"Iterations: {n}")
        print(f"Planning p50: {p50_us:.2f} us (Optimization Target < 10.0 us)")
        print(f"Planning p95: {p95_us:.2f} us (Optimization Target < 20.0 us)")
        print(f"Planning p99: {p99_us:.2f} us")
        self.assertGreater(n, 0)

    def test_router_dispatch_latency_benchmark(self) -> None:
        adapter = FastBenchmarkAdapter()
        registry = ExecutionStateRegistry()
        router = ExecutionRouter(
            adapter=adapter,
            registry=registry,
            calendar=None,
            config=ExecutionConfig(max_order_notional=10000000.0),
        )
        router.is_accepting_orders = True

        requests = [
            OrderRequest(
                client_order_id=f"TG-BENCH-{i:06d}",
                intent_id=f"intent-bench-router-{i:06d}",
                signal_id=f"sig-{i}",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=3000.0,
                reference_price=3000.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key=f"k_bench_{i}",
            )
            for i in range(2000)
        ]

        # Warm-up
        for i in range(200):
            router.submit(requests[i])

        latencies_ns: List[int] = []
        for req in requests[200:]:
            t0 = time.perf_counter_ns()
            router.submit(req)
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0

        print(f"\n--- PHASE 7 BENCHMARK: ROUTER DISPATCH LATENCY ---")
        print(f"Iterations: {n}")
        print(f"Router p50: {p50_us:.2f} us (Optimization Target < 15.0 us)")
        print(f"Router p95: {p95_us:.2f} us (Optimization Target < 30.0 us)")
        print(f"Router p99: {p99_us:.2f} us")
        self.assertGreater(n, 0)

    def test_fill_processing_latency_benchmark(self) -> None:
        req = OrderRequest(
            client_order_id="TG-FILL-BENCH",
            intent_id="intent-fill-bench",
            signal_id="sig-fill",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10000,
            price=100.0,
            reference_price=100.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_fill_bench",
        )
        state = ExecutionState(req)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        fills = [
            Fill(
                fill_id=f"FILL-BENCH-{i}",
                client_order_id=req.client_order_id,
                broker_order_id="BRK-FILL",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                fill_quantity=1,
                fill_price=100.0,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=1000,
            )
            for i in range(2000)
        ]

        # Warm-up
        for i in range(200):
            state.apply_fill(fills[i])

        latencies_ns: List[int] = []
        for f in fills[200:]:
            t0 = time.perf_counter_ns()
            state.apply_fill(f)
            t1 = time.perf_counter_ns()
            latencies_ns.append(t1 - t0)

        latencies_ns.sort()
        n = len(latencies_ns)
        p50_us = latencies_ns[int(n * 0.50)] / 1000.0
        p95_us = latencies_ns[int(n * 0.95)] / 1000.0
        p99_us = latencies_ns[int(n * 0.99)] / 1000.0

        print(f"\n--- PHASE 7 BENCHMARK: FILL PROCESSING LATENCY ---")
        print(f"Iterations: {n}")
        print(f"Fill p50: {p50_us:.2f} us (Optimization Target < 10.0 us)")
        print(f"Fill p95: {p95_us:.2f} us (Optimization Target < 20.0 us)")
        print(f"Fill p99: {p99_us:.2f} us")
        self.assertGreater(n, 0)

    def test_memory_scaling_benchmark(self) -> None:
        gc.collect()
        initial_objects = len(gc.get_objects())

        states = []
        for i in range(1000):
            req = OrderRequest(
                client_order_id=f"TG-MEM-{i:05d}",
                intent_id=f"intent-mem-{i:05d}",
                signal_id=f"sig-{i}",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=100.0,
                reference_price=100.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key=f"k_mem_{i}",
            )
            state = ExecutionState(req)
            states.append(state)

        gc.collect()
        final_objects = len(gc.get_objects())
        approx_bytes_per_state = sys.getsizeof(states[0]) + sys.getsizeof(states[0].request)

        print(f"\n--- PHASE 7 BENCHMARK: MEMORY FOOTPRINT ---")
        print(f"Active States Created: {len(states)}")
        print(f"Net Object Overhead: {final_objects - initial_objects} objects")
        print(f"Approx Slotted State Footprint: {approx_bytes_per_state} bytes per state")
        print(f"Total 1,000 States Footprint: {(approx_bytes_per_state * 1000) / 1024:.2f} KB (Target < 100.0 KB)")
        self.assertEqual(len(states), 1000)


if __name__ == "__main__":
    unittest.main()
