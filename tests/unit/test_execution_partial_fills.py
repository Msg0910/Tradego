"""
Unit tests for Partial Fill Accounting and Mathematical Invariants in Phase 7.
Verifies cumulative quantity conservation, volume-weighted average price (VWAP) math,
duplicate fill suppression, and overfill anomaly detection.
"""

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
from services.execution.state import ExecutionState
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestPartialFills(unittest.TestCase):
    """Verifies partial fill aggregation, VWAP calculation, and conservation invariants."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="HDFCBANK",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.request = OrderRequest(
            client_order_id="TG-TEST-20260915-000000000003",
            intent_id="intent-3",
            signal_id="sig-3",
            strategy_id="TEST_STRAT",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1600.0,
            reference_price=1600.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            order_version=1,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k3",
        )

    def test_incremental_multi_fill_aggregation_and_vwap(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # Fill 1: 30 shares @ ₹1600.0
        f1 = Fill(
            fill_id="FILL-101",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=30,
            fill_price=1600.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied = state.apply_fill(f1)
        self.assertTrue(applied)
        self.assertEqual(state.cumulative_filled_quantity, 30)
        self.assertEqual(state.remaining_quantity, 70)
        self.assertAlmostEqual(state.average_execution_price, 1600.0, places=4)
        self.assertEqual(state.status, CanonicalOrderStatus.PARTIALLY_FILLED)

        # Fill 2: 50 shares @ ₹1602.0
        f2 = Fill(
            fill_id="FILL-102",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=50,
            fill_price=1602.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=2000,
        )
        applied = state.apply_fill(f2)
        self.assertTrue(applied)
        self.assertEqual(state.cumulative_filled_quantity, 80)
        self.assertEqual(state.remaining_quantity, 20)
        # Expected VWAP: (30 * 1600 + 50 * 1602) / 80 = (48000 + 80100) / 80 = 128100 / 80 = 1601.25
        self.assertAlmostEqual(state.average_execution_price, 1601.25, places=4)
        self.assertEqual(state.status, CanonicalOrderStatus.PARTIALLY_FILLED)

        # Fill 3: 20 shares @ ₹1605.0 -> Completes order
        f3 = Fill(
            fill_id="FILL-103",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=20,
            fill_price=1605.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=3000,
        )
        applied = state.apply_fill(f3)
        self.assertTrue(applied)
        self.assertEqual(state.cumulative_filled_quantity, 100)
        self.assertEqual(state.remaining_quantity, 0)
        # Expected VWAP: (128100 + 32100) / 100 = 160200 / 100 = 1602.0
        self.assertAlmostEqual(state.average_execution_price, 1602.0, places=4)
        self.assertEqual(state.status, CanonicalOrderStatus.FILLED)
        self.assertTrue(state.is_terminal)

    def test_duplicate_fill_idempotency(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        f1 = Fill(
            fill_id="FILL-DUP-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=40,
            fill_price=1600.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied1 = state.apply_fill(f1)
        self.assertTrue(applied1)
        self.assertEqual(state.cumulative_filled_quantity, 40)

        # Re-apply identical fill
        applied2 = state.apply_fill(f1)
        self.assertFalse(applied2)
        # State and quantity must remain unchanged
        self.assertEqual(state.cumulative_filled_quantity, 40)
        self.assertEqual(len(state.fills), 1)

    def test_overfill_anomaly_triggers_unknown(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # Fill arrives requesting 120 shares when only 100 authorized
        f_over = Fill(
            fill_id="FILL-OVER-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=120,
            fill_price=1600.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied = state.apply_fill(f_over)
        self.assertFalse(applied)
        self.assertEqual(state.status, CanonicalOrderStatus.UNKNOWN)
        self.assertIn("Overfill anomaly", state.error_message or "")


if __name__ == "__main__":
    unittest.main()
