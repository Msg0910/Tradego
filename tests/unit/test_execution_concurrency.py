"""
Unit tests for Concurrency and Per-Order Serialization in Phase 7.
Verifies thread-safe lifecycle transitions, race-free fill accumulation under concurrent threads,
and lock striping across the execution registry.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import unittest

from services.execution.models import (
    CanonicalOrderStatus,
    Fill,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionEffect,
    TimeInForce,
)
from services.execution.state import ExecutionState, ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestExecutionConcurrency(unittest.TestCase):
    """Verifies multi-threaded concurrency safety using reentrant per-order locks."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="AXISBANK",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)

    def test_concurrent_partial_fills_on_single_order(self) -> None:
        req = OrderRequest(
            client_order_id="TG-CONCUR-ORD-1",
            intent_id="intent-concur-1",
            signal_id="sig-1",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1200.0,
            reference_price=1200.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_concur_1",
        )
        state = ExecutionState(req)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # 4 distinct fills of 25 shares each = 100 shares total
        fills = [
            Fill(
                fill_id=f"FILL-CONCUR-{i}",
                client_order_id=req.client_order_id,
                broker_order_id="BRK-CONCUR",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                fill_quantity=25,
                fill_price=1200.0,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=1000 + i * 100,
            )
            for i in range(4)
        ]

        def worker_apply(fill: Fill) -> bool:
            return state.apply_fill(fill)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker_apply, f) for f in fills]
            results = [fut.result() for fut in as_completed(futures)]

        self.assertTrue(all(results))
        self.assertEqual(state.cumulative_filled_quantity, 100)
        self.assertEqual(state.remaining_quantity, 0)
        self.assertEqual(state.status, CanonicalOrderStatus.FILLED)
        self.assertEqual(len(state.fills), 4)

    def test_concurrent_registrations_across_distinct_orders(self) -> None:
        registry = ExecutionStateRegistry()

        def register_order(idx: int) -> bool:
            req = OrderRequest(
                client_order_id=f"TG-STRIPE-{idx:04d}",
                intent_id=f"intent-stripe-{idx:04d}",
                signal_id=f"sig-{idx}",
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
                idempotency_key=f"k_stripe_{idx}",
            )
            state = registry.create_state(req)
            return state is not None

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(register_order, i) for i in range(20)]
            results = [fut.result() for fut in as_completed(futures)]

        self.assertTrue(all(results))
        self.assertEqual(len(registry.get_all_states()), 20)


if __name__ == "__main__":
    unittest.main()
