"""
Unit tests for Cancel, Replace, and Race Condition Resolution in Phase 7.
Verifies fill-before-cancel, partial-fill-then-cancel, and multi-version race fill correlation.
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


class TestCancelReplaceRaces(unittest.TestCase):
    """Verifies asynchronous race conditions between matching engine and order state."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="ICICIBANK",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.request = OrderRequest(
            client_order_id="TG-TEST-20260915-000000000004",
            intent_id="intent-4",
            signal_id="sig-4",
            strategy_id="TEST_STRAT",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1000.0,
            reference_price=1000.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            order_version=1,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k4",
        )

    def test_scenario_a_fill_arrives_before_cancel(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # Cancel requested
        state.transition_to(CanonicalOrderStatus.CANCEL_PENDING)

        # Race: Exchange matched order before cancel arrived
        fill = Fill(
            fill_id="FILL-RACE-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-A",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=100,
            fill_price=1000.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied = state.apply_fill(fill)
        self.assertTrue(applied)
        # Authoritative fill takes precedence over cancel request
        self.assertEqual(state.status, CanonicalOrderStatus.FILLED)
        self.assertTrue(state.is_terminal)

    def test_scenario_b_partial_fill_followed_by_cancel(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
        state.transition_to(CanonicalOrderStatus.CANCEL_PENDING)

        # Partial fill arrives while in cancel pending
        fill = Fill(
            fill_id="FILL-PARTIAL-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-A",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=30,
            fill_price=1000.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied = state.apply_fill(fill)
        self.assertTrue(applied)
        self.assertEqual(state.status, CanonicalOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(state.cumulative_filled_quantity, 30)

        # Venue confirms cancellation of remaining 70 shares
        state.transition_to(CanonicalOrderStatus.CANCEL_PENDING)
        state.transition_to(CanonicalOrderStatus.CANCELLED)
        self.assertEqual(state.status, CanonicalOrderStatus.CANCELLED)
        self.assertEqual(state.cumulative_filled_quantity, 30)
        self.assertTrue(state.is_terminal)

    def test_scenario_c_replacement_correlation_and_race_fills(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # Initial working placement assigned broker_order_id A
        state.broker_order_id = "BRK-ORD-A"
        state.broker_id_history["BRK-ORD-A"] = 1

        # Replace requested: transitions to REPLACE_PENDING
        state.initiate_replace()
        self.assertEqual(state.status, CanonicalOrderStatus.REPLACE_PENDING)

        # Race fill arrives matching old broker_order_id A for 50 shares
        fill_a = Fill(
            fill_id="FILL-A-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-ORD-A",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=50,
            fill_price=1000.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000,
        )
        applied_a = state.apply_fill(fill_a)
        self.assertTrue(applied_a)
        self.assertEqual(state.cumulative_filled_quantity, 50)

        # Replace is confirmed by broker, issuing new broker_order_id B
        state.confirm_replace("BRK-ORD-B")
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.order_version, 2)
        self.assertEqual(state.broker_order_id, "BRK-ORD-B")

        # Subsequent fill arrives matching new broker_order_id B for remaining 50 shares
        fill_b = Fill(
            fill_id="FILL-B-1",
            client_order_id=self.request.client_order_id,
            broker_order_id="BRK-ORD-B",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=50,
            fill_price=1005.0,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=2000,
        )
        applied_b = state.apply_fill(fill_b)
        self.assertTrue(applied_b)

        # Total quantity strictly conserved across versions
        self.assertEqual(state.cumulative_filled_quantity, 100)
        self.assertEqual(state.remaining_quantity, 0)
        self.assertEqual(state.status, CanonicalOrderStatus.FILLED)

    def test_replacement_rejection_reverts_to_working_state(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        state.initiate_replace()
        self.assertEqual(state.status, CanonicalOrderStatus.REPLACE_PENDING)

        # Broker rejects replace
        state.reject_replace()
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.order_version, 1)


if __name__ == "__main__":
    unittest.main()
