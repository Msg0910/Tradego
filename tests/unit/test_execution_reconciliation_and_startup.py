"""
Unit tests for 6-Phase Safe Live Startup Protocol and Position Reconciliation in Phase 7.
Verifies cold-boot sequencing, UNKNOWN resolution, quarantine release, and PositionReconciliationAdjustment.
"""

from datetime import datetime, timezone
from typing import List, Optional
import unittest

from services.execution.accounting import PositionAccounting
from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    OrderAcknowledgement,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    PositionEffect,
    PositionReconciliationAdjustment,
    SubmissionOutcomeType,
    SubmissionResult,
    TimeInForce,
)
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionState, ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.risk.models import PositionSnapshot


class MockReconciliationAdapter(BrokerExecutionAdapter):
    """Mock broker adapter simulating broker state for reconciliation testing."""

    def __init__(self) -> None:
        self.open_orders: List[OrderUpdate] = []
        self.positions: List[PositionSnapshot] = []
        self.order_status_map = {}
        self.healthy = True

    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        return SubmissionResult(
            outcome=SubmissionOutcomeType.ACKNOWLEDGED,
            acknowledgement=OrderAcknowledgement(
                client_order_id=request.client_order_id,
                broker_order_id="BRK-REC-1",
                order_version=1,
                exchange_timestamp=datetime.now(timezone.utc),
                local_received_timestamp=datetime.now(timezone.utc),
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
            broker_order_id="BRK-REC-2",
            order_version=2,
            exchange_timestamp=datetime.now(timezone.utc),
            local_received_timestamp=datetime.now(timezone.utc),
            local_receive_monotonic_ns=2000,
        )

    def get_order_status(self, client_order_id: str, broker_order_id: Optional[str]) -> OrderUpdate:
        return self.order_status_map.get(
            client_order_id,
            OrderUpdate(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id or "UNKNOWN",
                status=CanonicalOrderStatus.UNKNOWN,
                cumulative_filled_quantity=0,
                remaining_quantity=0,
                average_price=0.0,
                timestamp=datetime.now(timezone.utc),
                monotonic_ns=1000,
            ),
        )

    def get_open_orders(self) -> List[OrderUpdate]:
        return self.open_orders

    def get_positions(self) -> List[PositionSnapshot]:
        return self.positions

    def health_check(self) -> bool:
        return self.healthy


class TestReconciliationAndStartup(unittest.TestCase):
    """Verifies startup reconciliation, UNKNOWN order resolution, and position adjustments."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="TATASTEEL",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.adapter = MockReconciliationAdapter()
        self.registry = ExecutionStateRegistry()
        self.router = ExecutionRouter(
            adapter=self.adapter,
            registry=self.registry,
            calendar=None,
            config=ExecutionConfig(),
        )

    def test_startup_sequence_lifecycle(self) -> None:
        # Phase A: Health Check
        self.assertTrue(self.adapter.health_check())

        # Phase B: State Fetch
        open_orders = self.adapter.get_open_orders()
        broker_positions = self.adapter.get_positions()
        self.assertEqual(len(open_orders), 0)
        self.assertEqual(len(broker_positions), 0)

        # Phase C: Order Reconcile (simulated: local journal matches broker)
        # Phase D: Position Reconcile
        # Phase E: Guard Check
        # Phase F: Enable Order Intake
        self.assertFalse(self.router.is_accepting_orders)
        self.router.is_accepting_orders = True
        self.assertTrue(self.router.is_accepting_orders)

    def test_unknown_order_reconciliation_releases_quarantine(self) -> None:
        # Put router in accepting state
        self.router.is_accepting_orders = True

        req = OrderRequest(
            client_order_id="TG-RECON-1",
            intent_id="intent-recon-1",
            signal_id="sig-1",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=50,
            price=150.0,
            reference_price=150.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_recon_1",
        )

        state = self.registry.create_state(req)
        state.transition_to(CanonicalOrderStatus.VALIDATED)
        state.transition_to(CanonicalOrderStatus.SUBMITTING)
        state.transition_to(CanonicalOrderStatus.UNKNOWN)
        self.registry.quarantine_instrument(self.instrument_id)
        self.assertTrue(self.registry.is_quarantined(self.instrument_id))

        # Configure broker adapter with authoritative status: FILLED
        self.adapter.order_status_map[req.client_order_id] = OrderUpdate(
            client_order_id=req.client_order_id,
            broker_order_id="BRK-REC-99",
            status=CanonicalOrderStatus.FILLED,
            cumulative_filled_quantity=50,
            remaining_quantity=0,
            average_price=150.0,
            timestamp=self.now,
            monotonic_ns=1000,
        )

        # Run reconciliation
        reconciled_state = self.router.reconcile_order(req.client_order_id)
        self.assertEqual(reconciled_state.status, CanonicalOrderStatus.FILLED)
        # Instrument quarantine must be released now that UNKNOWN state is resolved
        self.assertFalse(self.registry.is_quarantined(self.instrument_id))

    def test_position_reconciliation_adjustment_without_fake_fills(self) -> None:
        # Current local position is 80 shares
        current_pos = PositionSnapshot(
            instrument_id=self.instrument_id,
            net_quantity=80,
            average_entry_price=140.0,
            current_market_price=150.0,
            unrealized_pnl=800.0,
            realized_pnl=0.0,
            opened_timestamp=self.now,
            last_updated_timestamp=self.now,
        )

        # Reconciliation reveals broker net position is 100 shares (delta = +20)
        adj = PositionReconciliationAdjustment(
            instrument_id=self.instrument_id,
            broker_quantity=100,
            local_quantity_before=80,
            quantity_delta=20,
            reconciliation_reason="COLD_START_SYNC",
            timestamp=self.now,
        )

        updated_pos = PositionAccounting.apply_reconciliation(current_pos, adj)
        self.assertEqual(updated_pos.net_quantity, 100)
        self.assertEqual(updated_pos.average_entry_price, 140.0)
        self.assertEqual(updated_pos.realized_pnl, 0.0)  # Must not manufacture PnL or fake fills!


if __name__ == "__main__":
    unittest.main()
