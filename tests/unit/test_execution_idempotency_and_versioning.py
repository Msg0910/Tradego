"""
Unit tests for Idempotency and Replacement Order Versioning in Phase 7.
Verifies client_order_id stability, registry deduplication, multi-version mapping,
and active intent lifecycle tracking.
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
from services.execution.planner import ExecutionPlanner
from services.execution.state import ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.risk.models import ApprovedTradeIntent
from services.signals.models import SignalType


class TestIdempotencyAndVersioning(unittest.TestCase):
    """Verifies stable client_order_id, registry idempotency, and replacement versioning."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="TCS",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)
        self.intent = ApprovedTradeIntent(
            intent_id="intent-123456789012",
            signal_id="sig-123",
            fingerprint="fp-123",
            reaffirmation_key="reaff-123",
            strategy_id="MOMENTUM_ALPHA",
            strategy_version="1.0.0",
            risk_config_version="1.0.0",
            risk_config_hash="hash-123",
            instrument_id=self.instrument_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            permitted_quantity=50,
            approved_entry_price=3500.0,
            approved_stop_loss=3400.0,
            approved_take_profit=3700.0,
            risk_reward_ratio=2.0,
            calculated_monetary_risk=5000.0,
            allocated_capital=175000.0,
            market_timestamp=self.now,
            signal_generated_timestamp=self.now,
            intent_generated_timestamp=self.now,
            expiry_timestamp=None,
            binding_constraint="TEST",
        )

    def test_stable_client_order_id_across_retries(self) -> None:
        req1 = ExecutionPlanner.plan_order(self.intent, current_time=self.now)
        req2 = ExecutionPlanner.plan_order(self.intent, current_time=self.now)

        self.assertEqual(req1.client_order_id, req2.client_order_id)
        self.assertEqual(req1.idempotency_key, req2.idempotency_key)
        self.assertEqual(req1.client_order_id, "TG-MOME-20260915-intent-12345")

    def test_registry_prevents_duplicate_active_intent(self) -> None:
        registry = ExecutionStateRegistry()
        req = ExecutionPlanner.plan_order(self.intent, current_time=self.now)

        state = registry.create_state(req)
        self.assertIsNotNone(state)
        self.assertTrue(registry.has_active_intent(self.intent.intent_id))

        # Attempting to register another order for same active intent must fail
        req_dup = OrderRequest(
            client_order_id="TG-OTHER-ID",
            intent_id=self.intent.intent_id,
            signal_id=self.intent.signal_id,
            strategy_id=self.intent.strategy_id,
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=50,
            price=3500.0,
            reference_price=3500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_dup",
        )
        with self.assertRaises(ValueError):
            registry.create_state(req_dup)

    def test_replacement_versioning_and_multi_broker_id_mapping(self) -> None:
        registry = ExecutionStateRegistry()
        req = ExecutionPlanner.plan_order(self.intent, current_time=self.now)
        state = registry.create_state(req)

        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

        # Initial placement assigned broker_order_id A
        state.broker_order_id = "BRK-ORD-A"
        state.broker_id_history["BRK-ORD-A"] = 1
        registry.register_broker_order_id(req.client_order_id, "BRK-ORD-A")

        # Replace order -> transitions REPLACE_PENDING -> REPLACED -> ACKNOWLEDGED
        state.initiate_replace()
        self.assertEqual(state.status, CanonicalOrderStatus.REPLACE_PENDING)

        state.confirm_replace("BRK-ORD-B")
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)
        self.assertEqual(state.order_version, 2)
        self.assertEqual(state.broker_order_id, "BRK-ORD-B")
        self.assertEqual(state.broker_id_history["BRK-ORD-A"], 1)
        self.assertEqual(state.broker_id_history["BRK-ORD-B"], 2)

        registry.register_broker_order_id(req.client_order_id, "BRK-ORD-B")

        # Both broker order IDs map back to the identical Tradego logical order
        found_a = registry.get_by_broker_id("BRK-ORD-A")
        found_b = registry.get_by_broker_id("BRK-ORD-B")
        self.assertIsNotNone(found_a)
        self.assertIsNotNone(found_b)
        self.assertEqual(found_a.client_order_id, req.client_order_id)
        self.assertEqual(found_b.client_order_id, req.client_order_id)


if __name__ == "__main__":
    unittest.main()
