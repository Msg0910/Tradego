"""
Tradego Phase 7 Execution Layer — Execution Router.

Acts as the central broker-independent dispatch gateway.
Enforces exchange-local timezone normalization via ExchangeCalendar,
executes canonical reference-price notional safety guards,
coordinates intent deduplication, and handles typed submission outcomes.
"""

from datetime import datetime, timezone
import threading
import time
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
    OrderPurpose,
    OrderRequest,
    OrderType,
    SubmissionOutcomeType,
    SubmissionResult,
)
from services.execution.state import ExecutionState, ExecutionStateRegistry


class ExecutionRouter:
    """
    Broker-independent routing coordinator. Dispatches canonical OrderRequests
    to the active BrokerExecutionAdapter and coordinates lifecycle state tracking.
    """

    def __init__(
        self,
        adapter: BrokerExecutionAdapter,
        registry: ExecutionStateRegistry,
        calendar: Optional[Any] = None,   # Phase 2 / Phase 3 ExchangeCalendar
        config: Optional[ExecutionConfig] = None,
        audit_queue: Optional[Any] = None, # Bounded, non-blocking audit logging queue
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._calendar = calendar
        self._config = config or ExecutionConfig()
        self._audit_queue = audit_queue
        self._lock = threading.RLock()
        self._is_accepting_orders = False

    @property
    def is_accepting_orders(self) -> bool:
        with self._lock:
            return self._is_accepting_orders

    @is_accepting_orders.setter
    def is_accepting_orders(self, value: bool) -> None:
        with self._lock:
            self._is_accepting_orders = value

    def submit(self, request: OrderRequest) -> ExecutionState:
        """
        Validates calendar session, enforces notional safety, registers initial state,
        and dispatches order to the active broker adapter with typed outcome handling.
        """
        with self._lock:
            if not self._is_accepting_orders:
                raise RuntimeError("ExecutionRouter is not accepting orders (system offline or startup incomplete).")

            # 1. Quarantined Instrument Guard: Block new entries if symbol is under UNKNOWN reconciliation
            if self._registry.is_quarantined(request.instrument_id):
                if request.order_purpose != OrderPurpose.EXIT:
                    raise ValueError(
                        f"Instrument {request.instrument_id} is quarantined under UNKNOWN order reconciliation; "
                        f"new entry orders are blocked."
                    )

            # 2. Trading Session Guard with Explicit Exchange-Local Timezone Conversion
            if self._calendar is not None:
                exchange_tz = getattr(self._calendar, "timezone", None)
                if exchange_tz is None:
                    try:
                        exchange_tz = ZoneInfo("Asia/Kolkata")
                    except Exception:
                        from datetime import timezone as dt_tz, timedelta
                        exchange_tz = dt_tz(timedelta(hours=5, minutes=30))

                ts = request.creation_timestamp
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                exchange_local_dt = ts.astimezone(exchange_tz)

                session = self._calendar.resolve_session(request.instrument_id, exchange_local_dt)
                if not session.is_regular_trading(exchange_local_dt):
                    raise ValueError(
                        f"Exchange {request.instrument_id.exchange} is not in regular trading session "
                        f"at exchange-local time {exchange_local_dt}."
                    )

            # 3. Canonical Reference-Price Policy for Notional Safety Guard
            # price=None for MARKET orders must NEVER evaluate to zero for safety calculations
            if request.order_type == OrderType.LIMIT:
                notional_price = request.price
            elif request.order_type == OrderType.MARKET:
                notional_price = request.reference_price
            else:
                raise ValueError(f"Unsupported OrderType: {request.order_type}")

            if notional_price is None or notional_price <= 0.0:
                raise ValueError(f"Invalid reference/limit price for notional calculation: {notional_price}")

            estimated_notional = request.quantity * notional_price
            if estimated_notional > self._config.max_order_notional:
                raise ValueError(
                    f"Order estimated notional ₹{estimated_notional:.2f} exceeds "
                    f"max limit ₹{self._config.max_order_notional:.2f}"
                )

            # 4. Enforce Intent Idempotency
            if self._registry.has_active_intent(request.intent_id):
                raise ValueError(f"Intent {request.intent_id} already has an active working order.")

            # 5. Register Initial State
            state = self._registry.create_state(request)
            state.transition_to(CanonicalOrderStatus.VALIDATED)
            state.transition_to(CanonicalOrderStatus.SUBMITTING)

            # 6. Dispatch to Broker Adapter with Typed Outcome Handling
            attempt = 0
            while True:
                try:
                    result: SubmissionResult = self._adapter.submit_order(request)
                except Exception as e:
                    # Unexpected transport exception during wire dispatch -> UNKNOWN
                    state.record_failure(f"Transport exception during dispatch: {e}")
                    state.transition_to(CanonicalOrderStatus.UNKNOWN)
                    self._registry.quarantine_instrument(request.instrument_id)
                    break

                match result.outcome:
                    case SubmissionOutcomeType.ACKNOWLEDGED:
                        if result.acknowledgement is None:
                            state.record_failure("Missing acknowledgement payload on ACKNOWLEDGED outcome.")
                            state.transition_to(CanonicalOrderStatus.UNKNOWN)
                            self._registry.quarantine_instrument(request.instrument_id)
                        else:
                            state.record_acknowledgement(result.acknowledgement)
                            self._registry.register_broker_order_id(
                                request.client_order_id, result.acknowledgement.broker_order_id
                            )
                            state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
                        break

                    case SubmissionOutcomeType.REJECTED:
                        reason = result.rejection_reason or ExecutionFailureReason.BROKER_REJECTED
                        state.record_rejection(reason, result.error_message or "Broker pre-trade rejection")
                        state.transition_to(CanonicalOrderStatus.REJECTED)
                        self._registry.release_intent(request.intent_id)
                        break

                    case SubmissionOutcomeType.RETRYABLE_FAILURE:
                        attempt += 1
                        if attempt <= self._config.max_submission_retries:
                            backoff_ms = min(self._config.retry_base_delay_ms * (2 ** attempt), 200.0)
                            time.sleep(backoff_ms / 1000.0)
                            continue  # Retry with identical request (stable client_order_id)
                        else:
                            state.record_failure(
                                f"Submission retries exhausted ({attempt - 1} retries): {result.error_message}"
                            )
                            state.transition_to(CanonicalOrderStatus.FAILED)
                            self._registry.release_intent(request.intent_id)
                            break

                    case SubmissionOutcomeType.AMBIGUOUS_UNKNOWN:
                        state.record_failure(f"Ambiguous submission status: {result.error_message}")
                        state.transition_to(CanonicalOrderStatus.UNKNOWN)
                        self._registry.quarantine_instrument(request.instrument_id)
                        # Quarantined in UNKNOWN; triggers reconciliation without blind retry
                        break

            # 7. Asynchronous Bounded Non-Blocking Audit Emission
            if self._audit_queue is not None:
                try:
                    self._audit_queue.put_nowait(
                        {
                            "event": "ORDER_SUBMITTED",
                            "client_order_id": request.client_order_id,
                            "intent_id": request.intent_id,
                            "status": state.status.value,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                except Exception:
                    # Persistence queue full or unavailable; never block execution hot path
                    pass

            return state

    def cancel(self, client_order_id: str) -> bool:
        """
        Requests cancellation of an active working order.
        """
        with self._lock:
            state = self._registry.get_state(client_order_id)
            if state is None:
                return False

            if state.status not in (
                CanonicalOrderStatus.ACKNOWLEDGED,
                CanonicalOrderStatus.PARTIALLY_FILLED,
            ):
                return False

            state.transition_to(CanonicalOrderStatus.CANCEL_PENDING)
            broker_id = state.broker_order_id or "UNKNOWN"
            success = self._adapter.cancel_order(client_order_id, broker_id)
            if success:
                state.transition_to(CanonicalOrderStatus.CANCELLED)
                self._registry.release_intent(state.intent_id)
            return success

    def replace(
        self,
        client_order_id: str,
        new_price: Optional[float] = None,
        new_quantity: Optional[int] = None,
    ) -> bool:
        """
        Coordinates price/size modification, preserving client_order_id and versioning child orders.
        """
        with self._lock:
            state = self._registry.get_state(client_order_id)
            if state is None:
                return False

            if state.status not in (
                CanonicalOrderStatus.ACKNOWLEDGED,
                CanonicalOrderStatus.PARTIALLY_FILLED,
            ):
                return False

            state.initiate_replace()
            broker_id = state.broker_order_id or "UNKNOWN"
            try:
                ack = self._adapter.replace_order(client_order_id, broker_id, new_price, new_quantity)
                state.confirm_replace(ack.broker_order_id)
                self._registry.register_broker_order_id(client_order_id, ack.broker_order_id)
                return True
            except Exception:
                state.reject_replace()
                return False

    def reconcile_order(self, client_order_id: str) -> ExecutionState:
        """
        Reconciles an individual order against the broker order book.
        Resolves UNKNOWN states and releases quarantine if appropriate.
        """
        with self._lock:
            state = self._registry.get_state(client_order_id)
            if state is None:
                raise ValueError(f"Order {client_order_id} not found for reconciliation.")

            update = self._adapter.get_order_status(client_order_id, state.broker_order_id)
            if update.status != CanonicalOrderStatus.UNKNOWN:
                if state.status == CanonicalOrderStatus.UNKNOWN:
                    state.transition_to(update.status)
                    # Check if instrument can be unquarantined
                    instrument_has_unknown = any(
                        s.instrument_id == state.instrument_id and s.status == CanonicalOrderStatus.UNKNOWN
                        for s in self._registry.get_all_states()
                    )
                    if not instrument_has_unknown:
                        self._registry.unquarantine_instrument(state.instrument_id)

            return state
