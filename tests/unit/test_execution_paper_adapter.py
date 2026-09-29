"""
Unit tests for PaperExecutionAdapter and Simulated Matching Engine in Phase 7.
Verifies MARKET execution against Level 1 quotes, slippage calculation,
LIMIT order price-boundary matching, rate limiting, and cancellation/replacement.
"""

from datetime import datetime, timezone
import time
import unittest

from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    Fill,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    PositionEffect,
    SubmissionOutcomeType,
    TimeInForce,
)
from services.execution.paper_adapter import PaperExecutionAdapter, TokenBucketRateLimiter
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestPaperExecutionAdapter(unittest.TestCase):
    """Verifies simulated matching against Phase 1 MarketEvents, slippage, and rate limiting."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.config = ExecutionConfig(
            paper_base_slippage_bps=2.0,  # 2 basis points = 0.02%
            rate_limit_max_queue_depth=5,
            rate_limit_max_wait_ms=50.0,
        )
        self.adapter = PaperExecutionAdapter(config=self.config)

    def test_market_buy_execution_with_depth_and_slippage(self) -> None:
        # Seed depth: Ask = 2500.0, Bid = 2498.0
        depth = MarketDepth(
            bids=[DepthLevel(price=2498.0, quantity=100, orders=2)],
            asks=[DepthLevel(price=2500.0, quantity=100, orders=2)],
        )
        event = MarketEvent(
            provider="SIM",
            provider_symbol_id=self.instrument_id.symbol,
            symbol=self.instrument_id.symbol,
            exchange_timestamp=self.now,
            local_receive_datetime=self.now,
            local_receive_timestamp=time.perf_counter(),
            normalized_timestamp=time.perf_counter(),
            ltp=2499.0,
            bid=2498.0,
            ask=2500.0,
            depth=depth,
        )
        self.adapter.on_market_event(event)

        req = OrderRequest(
            client_order_id="TG-PAPER-MKT-BUY",
            intent_id="intent-paper-1",
            signal_id="sig-1",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.MARKET,
            quantity=50,
            price=None,
            reference_price=2500.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_paper_1",
        )

        captured_fills = []
        self.adapter.register_fill_callback(captured_fills.append)

        result = self.adapter.submit_order(req)
        self.assertEqual(result.outcome, SubmissionOutcomeType.ACKNOWLEDGED)
        self.assertEqual(len(captured_fills), 1)

        fill: Fill = captured_fills[0]
        self.assertEqual(fill.fill_quantity, 50)
        self.assertEqual(fill.side, OrderSide.BUY)
        # Base ask price is 2500.0; slippage is +2 bps -> 2500.0 * 1.0002 = 2500.5
        self.assertAlmostEqual(fill.fill_price, 2500.5, places=3)
        self.assertTrue(fill.fill_id.startswith("FILL-TG-PAPER-MKT-BUY-"))

    def test_limit_order_delayed_matching_on_market_ticks(self) -> None:
        captured_fills = []
        self.adapter.register_fill_callback(captured_fills.append)

        # Seed market LTP = 1005.0
        event1 = MarketEvent(
            provider="SIM",
            provider_symbol_id=self.instrument_id.symbol,
            symbol=self.instrument_id.symbol,
            exchange_timestamp=self.now,
            local_receive_datetime=self.now,
            local_receive_timestamp=time.perf_counter(),
            normalized_timestamp=time.perf_counter(),
            ltp=1005.0,
        )
        self.adapter.on_market_event(event1)

        # Place LIMIT BUY at 1000.0 (passive: LTP 1005 > Limit 1000, should not fill yet)
        req = OrderRequest(
            client_order_id="TG-PAPER-LMT-1",
            intent_id="intent-paper-2",
            signal_id="sig-2",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=20,
            price=1000.0,
            reference_price=1000.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_paper_2",
        )
        result = self.adapter.submit_order(req)
        self.assertEqual(result.outcome, SubmissionOutcomeType.ACKNOWLEDGED)
        self.assertEqual(len(captured_fills), 0)

        # Subsequent market tick arrives with LTP = 999.0 (crosses limit boundary!)
        event2 = MarketEvent(
            provider="SIM",
            provider_symbol_id=self.instrument_id.symbol,
            symbol=self.instrument_id.symbol,
            exchange_timestamp=self.now,
            local_receive_datetime=self.now,
            local_receive_timestamp=time.perf_counter(),
            normalized_timestamp=time.perf_counter(),
            ltp=999.0,
        )
        self.adapter.on_market_event(event2)

        self.assertEqual(len(captured_fills), 1)
        fill = captured_fills[0]
        self.assertEqual(fill.fill_quantity, 20)
        self.assertEqual(fill.fill_price, 1000.0)

    def test_cancellation_and_replacement(self) -> None:
        req = OrderRequest(
            client_order_id="TG-PAPER-CANCEL-1",
            intent_id="intent-paper-3",
            signal_id="sig-3",
            strategy_id="TEST",
            instrument_id=self.instrument_id,
            side=OrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            order_type=OrderType.LIMIT,
            quantity=30,
            price=900.0,
            reference_price=900.0,
            time_in_force=TimeInForce.DAY,
            order_purpose=OrderPurpose.ENTRY,
            creation_timestamp=self.now,
            expiry_timestamp=None,
            idempotency_key="k_paper_3",
        )
        result = self.adapter.submit_order(req)
        broker_id = result.acknowledgement.broker_order_id

        # Replace order
        ack_replace = self.adapter.replace_order(
            client_order_id=req.client_order_id,
            broker_order_id=broker_id,
            new_price=910.0,
            new_quantity=40,
        )
        self.assertEqual(ack_replace.order_version, 2)
        self.assertNotEqual(ack_replace.broker_order_id, broker_id)

        # Cancel modified order
        cancelled = self.adapter.cancel_order(req.client_order_id, ack_replace.broker_order_id)
        self.assertTrue(cancelled)

        status = self.adapter.get_order_status(req.client_order_id, ack_replace.broker_order_id)
        self.assertEqual(status.status, CanonicalOrderStatus.CANCELLED)

    def test_rate_limiter_fail_fast(self) -> None:
        limiter = TokenBucketRateLimiter(
            rate_limit_per_second=1.0,
            max_queue_depth=1,
            max_wait_ms=10.0,
        )
        # Consume available token
        self.assertTrue(limiter.acquire())
        # Immediate subsequent request with tiny wait budget must fail fast
        self.assertFalse(limiter.acquire())


if __name__ == "__main__":
    unittest.main()
