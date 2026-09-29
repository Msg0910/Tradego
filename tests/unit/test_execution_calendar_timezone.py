"""
Unit tests for ExchangeCalendar Integration and Timezone Normalization in Phase 7.
Verifies that UTC and naive creation_timestamps are correctly converted to exchange-local time
(Asia/Kolkata) before evaluating trading session regular hours.
"""

from datetime import datetime, timezone
from typing import Optional
import unittest
from zoneinfo import ZoneInfo

from services.candles.calendar import IndianMarketCalendar
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
    SubmissionOutcomeType,
    SubmissionResult,
    TimeInForce,
)
from services.execution.router import ExecutionRouter
from services.execution.state import ExecutionStateRegistry
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.risk.models import PositionSnapshot


class DummyAdapter(BrokerExecutionAdapter):
    """Dummy adapter returning immediate acknowledgement."""

    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        return SubmissionResult(
            outcome=SubmissionOutcomeType.ACKNOWLEDGED,
            acknowledgement=OrderAcknowledgement(
                client_order_id=request.client_order_id,
                broker_order_id="BRK-CAL-1",
                order_version=1,
                exchange_timestamp=datetime.now(timezone.utc),
                local_received_timestamp=datetime.now(timezone.utc),
                local_receive_monotonic_ns=1000,
            ),
        )

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
            broker_order_id="BRK-CAL-2",
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
            broker_order_id=broker_order_id or "BRK-CAL",
            status=CanonicalOrderStatus.ACKNOWLEDGED,
            cumulative_filled_quantity=0,
            remaining_quantity=0,
            average_price=0.0,
            timestamp=datetime.now(timezone.utc),
            monotonic_ns=1000,
        )

    def get_open_orders(self) -> list:
        return []

    def get_positions(self) -> list:
        return []

    def health_check(self) -> bool:
        return True


class TestCalendarTimezoneContract(unittest.TestCase):
    """Verifies exchange timezone normalization during calendar session evaluation."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="INFY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.calendar = IndianMarketCalendar()
        self.adapter = DummyAdapter()
        self.registry = ExecutionStateRegistry()
        self.router = ExecutionRouter(
            adapter=self.adapter,
            registry=self.registry,
            calendar=self.calendar,
            config=ExecutionConfig(max_order_notional=500000.0),
        )
        self.router.is_accepting_orders = True

    def test_utc_timestamp_converted_to_ist_during_regular_trading(self) -> None:
        # A Monday at 04:30 UTC -> 10:00 AM IST (regular trading is 09:15 to 15:30 IST)
        # 2026-09-14 is a Monday
        creation_utc = datetime(2026, 9, 14, 4, 30, 0, tzinfo=timezone.utc)
        req = OrderRequest(
            client_order_id="TG-CAL-OK-1",
            intent_id="intent-cal-1",
            signal_id="sig-cal-1",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=1500.0,
            reference_price=1500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=creation_utc,
            expiry_timestamp=None,
            idempotency_key="k_cal_1",
        )
        state = self.router.submit(req)
        self.assertEqual(state.status, CanonicalOrderStatus.ACKNOWLEDGED)

    def test_utc_timestamp_converted_to_ist_outside_regular_trading_fails(self) -> None:
        # A Monday at 03:00 UTC -> 08:30 AM IST (market is closed, pre-open is at 09:00, regular at 09:15)
        creation_utc = datetime(2026, 9, 14, 3, 0, 0, tzinfo=timezone.utc)
        req = OrderRequest(
            client_order_id="TG-CAL-FAIL-1",
            intent_id="intent-cal-2",
            signal_id="sig-cal-2",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=1500.0,
            reference_price=1500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=creation_utc,
            expiry_timestamp=None,
            idempotency_key="k_cal_2",
        )
        with self.assertRaises(ValueError) as ctx:
            self.router.submit(req)
        self.assertIn("not in regular trading session", str(ctx.exception))

    def test_weekend_rejected_by_calendar(self) -> None:
        # A Sunday at 10:00 AM IST (2026-09-13 is Sunday)
        creation_utc = datetime(2026, 9, 13, 4, 30, 0, tzinfo=timezone.utc)
        req = OrderRequest(
            client_order_id="TG-CAL-WEEKEND-1",
            intent_id="intent-cal-3",
            signal_id="sig-cal-3",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=1500.0,
            reference_price=1500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=creation_utc,
            expiry_timestamp=None,
            idempotency_key="k_cal_3",
        )
        with self.assertRaises(ValueError) as ctx:
            self.router.submit(req)
        self.assertIn("not in regular trading session", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
