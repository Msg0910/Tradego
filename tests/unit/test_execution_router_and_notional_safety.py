"""
Unit tests for ExecutionRouter, Reference-Price Notional Safety, and Typed Submission Outcomes.
Verifies LIMIT and MARKET notional ceiling enforcement, retry behavior, and UNKNOWN quarantine.
"""

from datetime import datetime, timezone
from typing import List, Optional
import unittest

from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
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
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.risk.models import PositionSnapshot


class MockBrokerAdapter(BrokerExecutionAdapter):
    """Configurable mock adapter for testing router outcome branches."""

    def __init__(self) -> None:
        self.outcome_to_return: SubmissionResult = SubmissionResult(
            outcome=SubmissionOutcomeType.ACKNOWLEDGED,
            acknowledgement=OrderAcknowledgement(
                client_order_id="TG-TEST",
                broker_order_id="BRK-MOCK-1",
                order_version=1,
                exchange_timestamp=datetime.now(timezone.utc),
                local_received_timestamp=datetime.now(timezone.utc),
                local_receive_monotonic_ns=1000,
            ),
        )
        self.submit_call_count = 0

    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        self.submit_call_count += 1
        return self.outcome_to_return

    def cancel_order(self, client_order_id: str, broker_order_id: str) -> bool:
        return True

    def replace_order(
        self,
        client_order_id: str,
        broker_order_id: str,
        new_price: Optional[float],
        new_quantity: Optional[int],
    ) -> OrderAcknowledgement:
        return OrderAcknowledgement(
            client_order_id=client_order_id,
            broker_order_id="BRK-REPLACE",
            order_version=2,
            exchange_timestamp=datetime.now(timezone.utc),
            local_received_timestamp=datetime.now(timezone.utc),
            local_receive_monotonic_ns=2000,
        )

    def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str]
    ) -> OrderUpdate:
        return OrderUpdate(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id or "BRK-MOCK",
            status=CanonicalOrderStatus.ACKNOWLEDGED,
            cumulative_filled_quantity=0,
            remaining_quantity=100,
            average_price=0.0,
            timestamp=datetime.now(timezone.utc),
            monotonic_ns=1000,
        )

    def get_open_orders(self) -> List[OrderUpdate]:
        return []

    def get_positions(self) -> List[PositionSnapshot]:
        return []

    def health_check(self) -> bool:
        return True


class TestRouterAndNotionalSafety(unittest.TestCase):
    """Verifies router notional safety checks, typed outcome dispatch, and retry behavior."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="SBIN",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.config = ExecutionConfig(
            max_order_notional=100000.0,  # ₹1 Lakh limit
            max_submission_retries=2,
            retry_base_delay_ms=10.0,
        )
        self.adapter = MockBrokerAdapter()
        self.registry = ExecutionStateRegistry()
        self.router = ExecutionRouter(
            adapter=self.adapter,
            registry=self.registry,
            calendar=None,
            config=self.config,
        )
        self.router.is_accepting_orders = True

    def test_limit_order_notional_safety_exceeded(self) -> None:
        # 100 shares @ ₹1200 = ₹120,000 (exceeds ₹100,000 cap)
        req = OrderRequest(
            client_order_id="TG-NOTIONAL-LIMIT-1",
            intent_id="intent-notional-1",
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
            idempotency_key="k1",
        )
        with self.assertRaises(ValueError) as ctx:
            self.router.submit(req)
        self.assertIn("exceeds max limit", str(ctx.exception))

    def test_market_order_notional_safety_exceeded(self) -> None:
        # MARKET order with price=None, reference_price=₹1500, quantity=100 -> ₹150,000 > ₹100,000
        req = OrderRequest(
            client_order_id="TG-NOTIONAL-MKT-1",
            intent_id="intent-notional-2",
            signal_id="sig-2",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.MARKET,
            quantity=100,
            price=None,
            reference_price=1500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k2",
        )
        with self.assertRaises(ValueError) as ctx:
            self.router.submit(req)
        self.assertIn("exceeds max limit", str(ctx.exception))

    def test_successful_acknowledged_submission(self) -> None:
        req = OrderRequest(
            client_order_id="TG-OK-1",
            intent_id="intent-ok-1",
            signal_id="sig-3",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=50,
            price=800.0,  # ₹40,000 <= ₹100,000
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k3",
        )
        state = self.router.submit(req)
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.broker_order_id, "BRK-MOCK-1")

    def test_rejected_outcome_releases_intent_lock(self) -> None:
        self.adapter.outcome_to_return = SubmissionResult(
            outcome=SubmissionOutcomeType.REJECTED,
            rejection_reason=ExecutionFailureReason.INSUFFICIENT_MARGIN,
            error_message="Insufficient funds for order margin.",
        )
        req = OrderRequest(
            client_order_id="TG-REJ-1",
            intent_id="intent-rej-1",
            signal_id="sig-4",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=800.0,
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k4",
        )
        state = self.router.submit(req)
        self.assertEqual(state.status, CanonicalOrderStatus.REJECTED)
        self.assertTrue(state.is_terminal)
        # Intent lock must be released so subsequent intents are not blocked
        self.assertFalse(self.registry.has_active_intent(req.intent_id))

    def test_retryable_failure_bounded_exhaustion(self) -> None:
        self.adapter.outcome_to_return = SubmissionResult(
            outcome=SubmissionOutcomeType.RETRYABLE_FAILURE,
            rejection_reason=ExecutionFailureReason.RATE_LIMIT_EXCEEDED,
            error_message="Rate limit hit, retry backoff.",
        )
        req = OrderRequest(
            client_order_id="TG-RETRY-1",
            intent_id="intent-retry-1",
            signal_id="sig-5",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=800.0,
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k5",
        )
        state = self.router.submit(req)
        # 1 initial attempt + 2 retries = 3 calls
        self.assertEqual(self.adapter.submit_call_count, 3)
        self.assertEqual(state.status, CanonicalOrderStatus.FAILED)
        self.assertTrue(state.is_terminal)
        self.assertFalse(self.registry.has_active_intent(req.intent_id))

    def test_ambiguous_unknown_quarantines_new_entries(self) -> None:
        self.adapter.outcome_to_return = SubmissionResult(
            outcome=SubmissionOutcomeType.AMBIGUOUS_UNKNOWN,
            error_message="Read timeout waiting for gateway HTTP response.",
        )
        req = OrderRequest(
            client_order_id="TG-UNK-1",
            intent_id="intent-unk-1",
            signal_id="sig-6",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=800.0,
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k6",
        )
        state = self.router.submit(req)
        self.assertEqual(state.status, CanonicalOrderStatus.UNKNOWN)
        self.assertTrue(self.registry.is_quarantined(self.instrument_id))

        # A new ENTRY order on same instrument must be rejected
        req_entry2 = OrderRequest(
            client_order_id="TG-UNK-2",
            intent_id="intent-entry-2",
            signal_id="sig-7",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=800.0,
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k7",
        )
        with self.assertRaises(ValueError) as ctx:
            self.router.submit(req_entry2)
        self.assertIn("is quarantined", str(ctx.exception))

        # An EXIT order must NOT be blocked by quarantine
        self.adapter.outcome_to_return = SubmissionResult(
            outcome=SubmissionOutcomeType.ACKNOWLEDGED,
            acknowledgement=OrderAcknowledgement(
                client_order_id="TG-EXIT-1",
                broker_order_id="BRK-EXIT-1",
                order_version=1,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=2000,
            ),
        )
        req_exit = OrderRequest(
            client_order_id="TG-EXIT-1",
            intent_id="intent-exit-1",
            signal_id="sig-8",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.SELL,
            position_effect=PositionEffect.CLOSE,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=800.0,
            reference_price=800.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.EXIT,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k8",
        )
        exit_state = self.router.submit(req_exit)
        self.assertEqual(exit_state.status, CanonicalOrderStatus.ACKNOWLEDGED)


if __name__ == "__main__":
    unittest.main()
