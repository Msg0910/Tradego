"""
Regression tests for Step 11 Idempotency Verification:
- Verifies exact key composition of LiveBrokerAdapter._compute_idempotency_key()
- Verifies stability of idempotency keys across retries and temporal delays
- Verifies sensitivity to instruction attributes (no unintended collisions)
- Verifies LiveBrokerAdapter duplicate dispatch rejection (DUPLICATE_DISPATCH_BLOCKED)
- Verifies MultiVenueRouter venue-isolated idempotency key derivation
"""

import hashlib
import time
import unittest

from gateway.broker_adapter import LiveBrokerAdapter
from gateway.broker_connectivity import BrokerConnectivityManager
from gateway.broker_credentials import BrokerCredentialsConfig
from gateway.multi_venue import BrokerVenue, BrokerVenueRegistry, MultiVenueRouter, VenueRoutingPolicy
from gateway.order_instruction import InstructionState, OrderInstruction
from gateway.recovery import TradingGuard
from gateway.security import Tier1AuditLogger


class TestStep11IdempotencyVerification(unittest.TestCase):
    """Step 11: Comprehensive idempotency verification suite."""

    def setUp(self) -> None:
        self.guard = TradingGuard()
        self.audit = Tier1AuditLogger(log_file_path=None)
        self.conn_mgr = BrokerConnectivityManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            risk_gate_available=True,
            active_broker="TEST_VENUE_IDEMP",
        )
        self.creds = BrokerCredentialsConfig(
            venue_name="TEST_VENUE_IDEMP",
            client_id="OP_CLIENT_IDEMP",
            access_token="TEST_TOKEN_IDEMP",
            account_id="ACC_IDEMP_01",
        )
        self.conn_mgr.set_credentials(self.creds, "op_idemp")
        self.conn_mgr.connect("op_idemp")

        self.adapter = LiveBrokerAdapter(
            credentials=self.creds,
            connectivity_manager=self.conn_mgr,
            venue_name="TEST_VENUE_IDEMP",
        )

        self.instruction = OrderInstruction(
            instruction_id="INS-IDEMP-001",
            intent_id="INT-IDEMP-001",
            symbol="NSE:INFY",
            side="BUY",
            quantity=25,
            order_type="LIMIT",
            limit_price=1600.0,
            correlation_id="CORR-IDEMP-ABC",
            risk_evaluation_reference="RISK_REF_IDEMP",
            approval_reference="APP_REF_IDEMP",
            state=InstructionState.VALIDATED,
        )

    def test_live_adapter_idempotency_key_composition_matches_specification(self) -> None:
        """Verifies exact key composition matches SHA-256(ins_id:symbol:side:qty:corr_id)."""
        expected_raw = "INS-IDEMP-001:NSE:INFY:BUY:25:CORR-IDEMP-ABC"
        expected_hash = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()

        computed_key = self.adapter._compute_idempotency_key(self.instruction)
        self.assertEqual(computed_key, expected_hash)
        self.assertEqual(len(computed_key), 64)

    def test_idempotency_key_stability_across_retries(self) -> None:
        """Verifies that idempotency key derivation is time-invariant and perfectly stable across retries."""
        key_initial = self.adapter._compute_idempotency_key(self.instruction)

        # Simulate delay / subsequent retries
        time.sleep(0.01)
        key_retry_1 = self.adapter._compute_idempotency_key(self.instruction)

        time.sleep(0.01)
        key_retry_2 = self.adapter._compute_idempotency_key(self.instruction)

        self.assertEqual(key_initial, key_retry_1)
        self.assertEqual(key_initial, key_retry_2)

    def test_idempotency_key_sensitivity_to_attributes(self) -> None:
        """Verifies that changing any key attribute alters the hash (no false collision)."""
        base_key = self.adapter._compute_idempotency_key(self.instruction)

        # Vary quantity
        ins_qty = OrderInstruction(
            instruction_id=self.instruction.instruction_id,
            intent_id=self.instruction.intent_id,
            symbol=self.instruction.symbol,
            side=self.instruction.side,
            quantity=50,  # changed
            order_type=self.instruction.order_type,
            limit_price=self.instruction.limit_price,
            correlation_id=self.instruction.correlation_id,
            state=InstructionState.VALIDATED,
        )
        self.assertNotEqual(base_key, self.adapter._compute_idempotency_key(ins_qty))

        # Vary symbol
        ins_sym = OrderInstruction(
            instruction_id=self.instruction.instruction_id,
            intent_id=self.instruction.intent_id,
            symbol="NSE:TCS",  # changed
            side=self.instruction.side,
            quantity=self.instruction.quantity,
            order_type=self.instruction.order_type,
            limit_price=self.instruction.limit_price,
            correlation_id=self.instruction.correlation_id,
            state=InstructionState.VALIDATED,
        )
        self.assertNotEqual(base_key, self.adapter._compute_idempotency_key(ins_sym))

        # Vary side
        ins_side = OrderInstruction(
            instruction_id=self.instruction.instruction_id,
            intent_id=self.instruction.intent_id,
            symbol=self.instruction.symbol,
            side="SELL",  # changed
            quantity=self.instruction.quantity,
            order_type=self.instruction.order_type,
            limit_price=self.instruction.limit_price,
            correlation_id=self.instruction.correlation_id,
            state=InstructionState.VALIDATED,
        )
        self.assertNotEqual(base_key, self.adapter._compute_idempotency_key(ins_side))

        # Vary correlation_id
        ins_corr = OrderInstruction(
            instruction_id=self.instruction.instruction_id,
            intent_id=self.instruction.intent_id,
            symbol=self.instruction.symbol,
            side=self.instruction.side,
            quantity=self.instruction.quantity,
            order_type=self.instruction.order_type,
            limit_price=self.instruction.limit_price,
            correlation_id="DIFFERENT-CORR-ID",  # changed
            state=InstructionState.VALIDATED,
        )
        self.assertNotEqual(base_key, self.adapter._compute_idempotency_key(ins_corr))

    def test_duplicate_dispatch_rejected_by_idempotency_guard(self) -> None:
        """Verifies duplicate dispatch attempts are blocked by the idempotency guard."""
        # First dispatch attempts execution and registers key
        res1 = self.adapter.dispatch(self.instruction)
        # Even though transport is uninitialized, the key has been registered in _dispatched_idempotency_keys
        self.assertEqual(res1.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")

        # Second dispatch with identical instruction is blocked by idempotency
        res2 = self.adapter.dispatch(self.instruction)
        self.assertFalse(res2.success)
        self.assertEqual(res2.outcome, "FAILED")
        self.assertIn("DUPLICATE_DISPATCH_BLOCKED", res2.failure_reason)
        self.assertIn("Duplicate dispatch detected", res2.raw_payload.get("error", ""))

    def test_multi_venue_idempotency_key_structure(self) -> None:
        """Verifies MultiVenueRouter derives venue-isolated idempotency keys formatted as TG-DISPATCH-{venue_id}-{instruction_id}."""
        registry = BrokerVenueRegistry()
        venue = BrokerVenue(
            venue_id="VENUE_NSE_EQUITY",
            adapter=self.adapter,
            operational_status="ACTIVE",
            connectivity_state="CONNECTED",
        )
        registry.register_venue(venue)

        policy = VenueRoutingPolicy()
        policy.set_default_venue("VENUE_NSE_EQUITY")
        router = MultiVenueRouter(registry, policy)

        routed_venue, idemp_key = router.route(self.instruction)
        self.assertEqual(routed_venue.venue_id, "VENUE_NSE_EQUITY")
        self.assertEqual(idemp_key, f"TG-DISPATCH-VENUE_NSE_EQUITY-{self.instruction.instruction_id}")


if __name__ == "__main__":
    unittest.main()
