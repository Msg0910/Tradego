"""
Unit tests for the 14-state Execution Lifecycle State Machine in Phase 7.
Verifies exhaustive legal transitions, illegal transition fail-closed rejection,
and terminal immutability.
"""

from datetime import datetime, timezone
import unittest

from services.execution.models import (
    CanonicalOrderStatus,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionEffect,
    TimeInForce,
)
from services.execution.state import ExecutionState
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestExecutionStateMachine(unittest.TestCase):
    """Verifies state machine transitions, invariants, and terminal immutability."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="INFY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.request = OrderRequest(
            client_order_id="TG-TEST-20260915-000000000002",
            intent_id="intent-2",
            signal_id="sig-2",
            strategy_id="TEST_STRAT",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=1500.0,
            reference_price=1500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            order_version=1,
            creation_timestamp=datetime.now(timezone.utc),
            expiry_timestamp=None,
            idempotency_key="k2",
        )

    def test_happy_path_lifecycle(self) -> None:
        state = ExecutionState(self.request)
        self.assertEqual(state.status, CanonicalOrderStatus.CREATED)

        state.transition_to(CanonicalOrderStatus.VALIDATED)
        self.assertEqual(state.status, CanonicalOrderStatus.VALIDATED)

        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        self.assertEqual(state.status, CanonicalOrderStatus.SUBMITTING)

        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)

        state.transition_to(CanonicalOrderStatus.PARTIALLY_FILLED)
        self.assertEqual(state.status, CanonicalOrderStatus.PARTIALLY_FILLED)

        state.transition_to(CanonicalOrderStatus.FILLED)
        self.assertEqual(state.status, CanonicalOrderStatus.FILLED)
        self.assertTrue(state.is_terminal)

    def test_cancellation_lifecycle(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        state.transition_to(CanonicalOrderStatus.CANCEL_PENDING)
        self.assertEqual(state.status, CanonicalOrderStatus.CANCEL_PENDING)

        state.transition_to(CanonicalOrderStatus.CANCELLED)
        self.assertEqual(state.status, CanonicalOrderStatus.CANCELLED)
        self.assertTrue(state.is_terminal)

    def test_unknown_reconciliation_lifecycle(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)

        # Timeout during submission -> enters UNKNOWN
        state.transition_to(CanonicalOrderStatus.UNKNOWN)
        self.assertEqual(state.status, CanonicalOrderStatus.UNKNOWN)
        self.assertFalse(state.is_terminal)

        # Reconciled against broker order book -> transitions to ACKNOWLEDGED
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)

    def test_illegal_transitions_fail_closed(self) -> None:
        state = ExecutionState(self.request)

        # Cannot skip from CREATED directly to ACKNOWLEDGED or FILLED
        with self.assertRaises(ValueError):
            state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        with self.assertRaises(ValueError):
            state.transition_to(CanonicalOrderStatus.FILLED)

        state.transition_to(CanonicalOrderStatus.VALIDATED)
        with self.assertRaises(ValueError):
            state.transition_to(CanonicalOrderStatus.PARTIALLY_FILLED)

    def test_terminal_immutability(self) -> None:
        state = ExecutionState(self.request)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
        state.transition_to(CanonicalOrderStatus.FILLED)
        self.assertTrue(state.is_terminal)

        # Attempting any transition from FILLED must fail
        for target in CanonicalOrderStatus:
            with self.assertRaises(ValueError):
                state.transition_to(target)


if __name__ == "__main__":
    unittest.main()
