"""
Tradego Phase 7 Execution Layer — Paper Execution Adapter.

Implements the simulated matching engine against Phase 1 MarketEvent ticks, Level 1 depth,
slippage modeling, Token Bucket rate limiting, and Tier 3 deterministic synthetic fill IDs.
"""

from datetime import datetime, timezone
import threading
import time
from typing import Callable, Dict, List, Optional

from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
    Fill,
    OrderAcknowledgement,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    SubmissionOutcomeType,
    SubmissionResult,
)
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import InstrumentId
from services.risk.models import PositionSnapshot


class TokenBucketRateLimiter:
    """
    Adapter-owned Token Bucket rate limiter with bounded wait and fail-fast rejection.
    """

    def __init__(
        self,
        rate_limit_per_second: float = 20.0,
        max_queue_depth: int = 50,
        max_wait_ms: float = 500.0,
    ) -> None:
        self._capacity = rate_limit_per_second
        self._tokens = rate_limit_per_second
        self._fill_rate = rate_limit_per_second  # tokens per second
        self._last_time = time.perf_counter()
        self._max_queue_depth = max_queue_depth
        self._max_wait_ms = max_wait_ms
        self._waiting_count = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """
        Attempts to acquire an execution token.
        If tokens unavailable, waits up to max_wait_ms if queue allows.
        Fails fast if wait exceeds max_wait_ms or queue is full.
        """
        with self._lock:
            now = time.perf_counter()
            elapsed = now - self._last_time
            self._last_time = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._fill_rate)

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True

            # Calculate required wait duration
            needed = 1.0 - self._tokens
            wait_seconds = needed / self._fill_rate
            wait_ms = wait_seconds * 1000.0

            if wait_ms > self._max_wait_ms or self._waiting_count >= self._max_queue_depth:
                return False

            self._waiting_count += 1

        # Perform bounded sleep outside lock
        time.sleep(wait_seconds)

        with self._lock:
            self._waiting_count -= 1
            now = time.perf_counter()
            elapsed = now - self._last_time
            self._last_time = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._fill_rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class PaperExecutionAdapter(BrokerExecutionAdapter):
    """
    Deterministic simulated execution adapter for paper trading and backtesting.
    Consumes Phase 1 MarketEvents, simulates matching, models slippage, and emits canonical Fills.
    """

    def __init__(
        self,
        config: Optional[ExecutionConfig] = None,
        rate_limiter: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self._config = config or ExecutionConfig()
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            rate_limit_per_second=20.0,
            max_queue_depth=self._config.rate_limit_max_queue_depth,
            max_wait_ms=self._config.rate_limit_max_wait_ms,
        )
        self._lock = threading.RLock()
        self._latest_market_events: Dict[Any, MarketEvent] = {}
        self._active_orders: Dict[str, OrderRequest] = {}
        self._order_versions: Dict[str, int] = {}
        self._broker_order_ids: Dict[str, str] = {}
        self._order_status: Dict[str, CanonicalOrderStatus] = {}
        self._order_fills: Dict[str, List[Fill]] = {}
        self._fill_sequence = 0
        self._fill_callbacks: List[Callable[[Fill], None]] = []
        self._update_callbacks: List[Callable[[OrderUpdate], None]] = []

    def register_fill_callback(self, callback: Callable[[Fill], None]) -> None:
        with self._lock:
            self._fill_callbacks.append(callback)

    def register_update_callback(self, callback: Callable[[OrderUpdate], None]) -> None:
        with self._lock:
            self._update_callbacks.append(callback)

    def _get_latest_market_event(self, instrument_id: InstrumentId) -> Optional[MarketEvent]:
        return self._latest_market_events.get(instrument_id) or self._latest_market_events.get(instrument_id.symbol)

    def on_market_event(self, event: MarketEvent) -> None:
        """
        Ingests a Phase 1 MarketEvent tick and checks pending LIMIT orders for matching.
        """
        sym = getattr(event, "symbol", None) or getattr(event, "provider_symbol_id", None)
        inst_id = getattr(event, "instrument_id", None)

        with self._lock:
            if inst_id is not None:
                self._latest_market_events[inst_id] = event
            if sym:
                self._latest_market_events[sym] = event

            # Check open limit orders for this instrument
            for client_order_id, request in list(self._active_orders.items()):
                matches = False
                if inst_id is not None and request.instrument_id == inst_id:
                    matches = True
                elif sym and request.instrument_id.symbol == sym:
                    matches = True

                if not matches:
                    continue

                if self._order_status.get(client_order_id) not in (
                    CanonicalOrderStatus.ACKNOWLEDGED,
                    CanonicalOrderStatus.PARTIALLY_FILLED,
                ):
                    continue

                if request.order_type == OrderType.LIMIT:
                    self._match_limit_order(request, event)

    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        with self._lock:
            # 1. Rate Limiting Check
            if not self._rate_limiter.acquire():
                return SubmissionResult(
                    outcome=SubmissionOutcomeType.RETRYABLE_FAILURE,
                    rejection_reason=ExecutionFailureReason.RATE_LIMIT_EXCEEDED,
                    error_message="Rate limit token exhaustion; bounded wait exceeded.",
                )

            # 2. Assign Simulated Broker Order ID
            seq = len(self._broker_order_ids) + 1
            broker_order_id = f"SIM-ORD-{seq:06d}"
            self._active_orders[request.client_order_id] = request
            self._order_versions[request.client_order_id] = request.order_version
            self._broker_order_ids[request.client_order_id] = broker_order_id
            self._order_status[request.client_order_id] = CanonicalOrderStatus.ACKNOWLEDGED
            self._order_fills[request.client_order_id] = []

            now_utc = datetime.now(timezone.utc)
            monotonic_ns = time.perf_counter_ns()

            ack = OrderAcknowledgement(
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                order_version=request.order_version,
                exchange_timestamp=now_utc,
                local_received_timestamp=now_utc,
                local_receive_monotonic_ns=monotonic_ns,
            )

            # 3. Execution Processing
            if request.order_type == OrderType.MARKET:
                # Immediate matching against latest quote or reference price
                latest_event = self._get_latest_market_event(request.instrument_id)
                self._execute_market_fill(request, latest_event)
            elif request.order_type == OrderType.LIMIT:
                latest_event = self._get_latest_market_event(request.instrument_id)
                if latest_event is not None:
                    self._match_limit_order(request, latest_event)

            return SubmissionResult(
                outcome=SubmissionOutcomeType.ACKNOWLEDGED,
                acknowledgement=ack,
            )

    def cancel_order(self, client_order_id: str, broker_order_id: str) -> bool:
        with self._lock:
            if client_order_id not in self._active_orders:
                return False
            status = self._order_status.get(client_order_id)
            if status in (CanonicalOrderStatus.FILLED, CanonicalOrderStatus.CANCELLED):
                return False

            self._order_status[client_order_id] = CanonicalOrderStatus.CANCELLED
            if client_order_id in self._active_orders:
                del self._active_orders[client_order_id]

            now_utc = datetime.now(timezone.utc)
            update = OrderUpdate(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                status=CanonicalOrderStatus.CANCELLED,
                cumulative_filled_quantity=sum(f.fill_quantity for f in self._order_fills.get(client_order_id, [])),
                remaining_quantity=0,
                average_price=0.0,
                timestamp=now_utc,
                monotonic_ns=time.perf_counter_ns(),
            )
            for cb in self._update_callbacks:
                cb(update)
            return True

    def replace_order(
        self,
        client_order_id: str,
        broker_order_id: str,
        new_price: Optional[float],
        new_quantity: Optional[int],
    ) -> OrderAcknowledgement:
        with self._lock:
            if client_order_id not in self._active_orders:
                raise ValueError(f"Order {client_order_id} not found for replacement.")

            curr_req = self._active_orders[client_order_id]
            new_version = self._order_versions[client_order_id] + 1
            new_broker_order_id = f"SIM-ORD-{len(self._broker_order_ids) + 1:06d}"

            # Create updated OrderRequest
            updated_req = OrderRequest(
                client_order_id=curr_req.client_order_id,
                intent_id=curr_req.intent_id,
                signal_id=curr_req.signal_id,
                strategy_id=curr_req.strategy_id,
                instrument_id=curr_req.instrument_id,
                side=curr_req.side,
                position_effect=curr_req.position_effect,
                order_type=curr_req.order_type,
                quantity=new_quantity if new_quantity is not None else curr_req.quantity,
                price=new_price if new_price is not None else curr_req.price,
                reference_price=curr_req.reference_price,
                time_in_force=curr_req.time_in_force,
                order_purpose=curr_req.order_purpose,
                order_version=new_version,
                creation_timestamp=curr_req.creation_timestamp,
                expiry_timestamp=curr_req.expiry_timestamp,
                idempotency_key=curr_req.idempotency_key,
                metadata=curr_req.metadata,
            )

            self._active_orders[client_order_id] = updated_req
            self._order_versions[client_order_id] = new_version
            self._broker_order_ids[client_order_id] = new_broker_order_id

            now_utc = datetime.now(timezone.utc)
            ack = OrderAcknowledgement(
                client_order_id=client_order_id,
                broker_order_id=new_broker_order_id,
                order_version=new_version,
                exchange_timestamp=now_utc,
                local_received_timestamp=now_utc,
                local_receive_monotonic_ns=time.perf_counter_ns(),
            )
            return ack

    def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str]
    ) -> OrderUpdate:
        with self._lock:
            status = self._order_status.get(client_order_id, CanonicalOrderStatus.UNKNOWN)
            b_id = broker_order_id or self._broker_order_ids.get(client_order_id, "UNKNOWN")
            fills = self._order_fills.get(client_order_id, [])
            cum_qty = sum(f.fill_quantity for f in fills)
            req = self._active_orders.get(client_order_id)
            rem_qty = (req.quantity - cum_qty) if req else 0
            avg_price = (
                sum(f.fill_quantity * f.fill_price for f in fills) / cum_qty
                if cum_qty > 0
                else 0.0
            )

            return OrderUpdate(
                client_order_id=client_order_id,
                broker_order_id=b_id,
                status=status,
                cumulative_filled_quantity=cum_qty,
                remaining_quantity=rem_qty,
                average_price=avg_price,
                timestamp=datetime.now(timezone.utc),
                monotonic_ns=time.perf_counter_ns(),
            )

    def get_open_orders(self) -> List[OrderUpdate]:
        with self._lock:
            return [
                self.get_order_status(cid, self._broker_order_ids.get(cid))
                for cid in self._active_orders.keys()
            ]

    def get_positions(self) -> List[PositionSnapshot]:
        return []

    def health_check(self) -> bool:
        return True

    # -------------------------------------------------------------------------
    # Internal Simulated Matching Helpers
    # -------------------------------------------------------------------------

    def _execute_market_fill(self, request: OrderRequest, event: Optional[MarketEvent]) -> None:
        """Executes full fill for MARKET order applying top-of-book and slippage."""
        base_price = request.reference_price
        if event is not None:
            if getattr(event, "depth", None) is not None:
                if request.side == OrderSide.BUY and event.depth.asks:
                    base_price = event.depth.asks[0].price
                elif request.side == OrderSide.SELL and event.depth.bids:
                    base_price = event.depth.bids[0].price
                else:
                    base_price = getattr(event, "ltp", None) or getattr(event, "price", base_price)
            elif request.side == OrderSide.BUY and getattr(event, "ask", None) is not None:
                base_price = event.ask
            elif request.side == OrderSide.SELL and getattr(event, "bid", None) is not None:
                base_price = event.bid
            else:
                base_price = getattr(event, "ltp", None) or getattr(event, "price", base_price)

        slippage_mult = self._config.paper_base_slippage_bps / 10000.0
        if request.side == OrderSide.BUY:
            fill_price = base_price * (1.0 + slippage_mult)
        else:
            fill_price = base_price * (1.0 - slippage_mult)

        self._fill_sequence += 1
        fill_id = f"FILL-{request.client_order_id}-{self._fill_sequence}"
        now_utc = datetime.now(timezone.utc)
        monotonic_ns = time.perf_counter_ns()

        fill = Fill(
            fill_id=fill_id,
            client_order_id=request.client_order_id,
            broker_order_id=self._broker_order_ids[request.client_order_id],
            instrument_id=request.instrument_id,
            side=request.side,
            fill_quantity=request.quantity,
            fill_price=round(fill_price, 4),
            exchange_timestamp=now_utc,
            local_received_timestamp=now_utc,
            local_receive_monotonic_ns=monotonic_ns,
            exchange_trade_id=None,
            broker_exec_id=None,
            fee_estimate=0.0,
        )

        self._order_fills[request.client_order_id].append(fill)
        self._order_status[request.client_order_id] = CanonicalOrderStatus.FILLED
        del self._active_orders[request.client_order_id]

        for cb in self._fill_callbacks:
            cb(fill)

    def _match_limit_order(self, request: OrderRequest, event: MarketEvent) -> None:
        """Evaluates price boundaries for LIMIT orders."""
        if request.price is None:
            return

        check_price = getattr(event, "ltp", None) or getattr(event, "price", None)
        if check_price is None:
            if request.side == OrderSide.BUY and getattr(event, "ask", None) is not None:
                check_price = event.ask
            elif request.side == OrderSide.SELL and getattr(event, "bid", None) is not None:
                check_price = event.bid
            else:
                return

        should_fill = False
        if request.side == OrderSide.BUY:
            if check_price <= request.price:
                should_fill = True
        elif request.side == OrderSide.SELL:
            if check_price >= request.price:
                should_fill = True

        if should_fill:
            self._fill_sequence += 1
            fill_id = f"FILL-{request.client_order_id}-{self._fill_sequence}"
            now_utc = datetime.now(timezone.utc)
            monotonic_ns = time.perf_counter_ns()

            fill = Fill(
                fill_id=fill_id,
                client_order_id=request.client_order_id,
                broker_order_id=self._broker_order_ids[request.client_order_id],
                instrument_id=request.instrument_id,
                side=request.side,
                fill_quantity=request.quantity,
                fill_price=request.price,
                exchange_timestamp=now_utc,
                local_received_timestamp=now_utc,
                local_receive_monotonic_ns=monotonic_ns,
                exchange_trade_id=None,
                broker_exec_id=None,
                fee_estimate=0.0,
            )

            self._order_fills[request.client_order_id].append(fill)
            self._order_status[request.client_order_id] = CanonicalOrderStatus.FILLED
            if request.client_order_id in self._active_orders:
                del self._active_orders[request.client_order_id]

            for cb in self._fill_callbacks:
                cb(fill)
