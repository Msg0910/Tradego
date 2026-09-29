"""
Unit tests for Phase 7 Execution Layer Domain Models and Contracts.
Verifies immutability, validation constraints, and post-init guards.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import unittest

from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
    Fill,
    OrderAcknowledgement,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    PositionEffect,
    PositionReconciliationAdjustment,
    SubmissionOutcomeType,
    SubmissionResult,
    TimeInForce,
)
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestExecutionModels(unittest.TestCase):
    """Verifies frozen immutability and contract invariants."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)

    def test_order_request_immutability(self) -> None:
        req = OrderRequest(
            client_order_id="TG-TEST-20260915-000000000001",
            intent_id="intent-1",
            signal_id="sig-1",
            strategy_id="TEST_STRAT",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=2500.0,
            reference_price=2500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            order_version=1,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="idemp-key-1",
        )
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            req.quantity = 20  # type: ignore

    def test_order_request_validation_guards(self) -> None:
        # Invalid quantity
        with self.assertRaises(ValueError):
            OrderRequest(
                client_order_id="TG-TEST-1",
                intent_id="intent-1",
                signal_id="sig-1",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.LIMIT,
                quantity=0,
                price=100.0,
                reference_price=100.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key="k1",
            )

        # Invalid reference price <= 0
        with self.assertRaises(ValueError):
            OrderRequest(
                client_order_id="TG-TEST-1",
                intent_id="intent-1",
                signal_id="sig-1",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=100.0,
                reference_price=0.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key="k1",
            )

        # LIMIT order with price=None
        with self.assertRaises(ValueError):
            OrderRequest(
                client_order_id="TG-TEST-1",
                intent_id="intent-1",
                signal_id="sig-1",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=None,
                reference_price=100.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key="k1",
            )

        # MARKET order with price set
        with self.assertRaises(ValueError):
            OrderRequest(
                client_order_id="TG-TEST-1",
                intent_id="intent-1",
                signal_id="sig-1",
                strategy_id="TEST",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                position_effect=PositionEffect.OPEN,
                order_type=OrderType.MARKET,
                quantity=10,
                price=100.0,  # Must be None for MARKET
                reference_price=100.0,
                time_in_force=TimeInForce.DAY,
                order_purpose=OrderPurpose.ENTRY,
                creation_timestamp=self.now,
                expiry_timestamp=None,
                idempotency_key="k1",
            )

    def test_fill_validation_and_immutability(self) -> None:
        fill = Fill(
            fill_id="FILL-1",
            client_order_id="TG-1",
            broker_order_id="BRK-1",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            fill_quantity=5,
            fill_price=2500.50,
            exchange_timestamp=self.now,
            local_received_timestamp=self.now,
            local_receive_monotonic_ns=1000000,
        )
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            fill.fill_quantity = 10  # type: ignore

        # Invalid fill quantity
        with self.assertRaises(ValueError):
            Fill(
                fill_id="FILL-2",
                client_order_id="TG-1",
                broker_order_id="BRK-1",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                fill_quantity=0,
                fill_price=2500.0,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=1000000,
            )

        # Invalid monotonic ns <= 0
        with self.assertRaises(ValueError):
            Fill(
                fill_id="FILL-3",
                client_order_id="TG-1",
                broker_order_id="BRK-1",
                instrument_id=self.instrument_id,
                side=OrderSide.BUY,
                fill_quantity=5,
                fill_price=2500.0,
                exchange_timestamp=self.now,
                local_received_timestamp=self.now,
                local_receive_monotonic_ns=0,
            )

    def test_position_reconciliation_adjustment_contract(self) -> None:
        adj = PositionReconciliationAdjustment(
            instrument_id=self.instrument_id,
            broker_quantity=100,
            local_quantity_before=80,
            quantity_delta=20,
            reconciliation_reason="COLD_START_SYNC",
            timestamp=self.now,
        )
        self.assertEqual(adj.quantity_delta, 20)
        self.assertEqual(adj.broker_quantity, 100)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            adj.quantity_delta = 30  # type: ignore


if __name__ == "__main__":
    unittest.main()
