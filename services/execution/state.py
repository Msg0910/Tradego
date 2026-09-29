"""
Tradego Phase 7 Execution Layer — Execution State Machine & Registry.

Implements the deterministic 14-state lifecycle state machine, legal transition matrix,
order actor with reentrant locking, multi-version replacement tracking, partial fill
accounting invariants, cumulative fill tracking, and volume-weighted average price math.
"""

from collections import defaultdict
from dataclasses import dataclass, field
import threading
from typing import Dict, List, Optional, Set

from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
    Fill,
    OrderAcknowledgement,
    OrderRequest,
)
from services.market_state.instrument import InstrumentId

# Exhaustive Legal Transition Matrix (Docs Section 9.1)
LEGAL_TRANSITIONS: Dict[CanonicalOrderStatus, Set[CanonicalOrderStatus]] = {
    CanonicalOrderStatus.CREATED: {
        CanonicalOrderStatus.VALIDATED,
        CanonicalOrderStatus.REJECTED,
    },
    CanonicalOrderStatus.VALIDATED: {
        CanonicalOrderStatus.SUBMITTING,
        CanonicalOrderStatus.FAILED,
    },
    CanonicalOrderStatus.SUBMITTING: {
        CanonicalOrderStatus.ACKNOWLEDGED,
        CanonicalOrderStatus.REJECTED,
        CanonicalOrderStatus.UNKNOWN,
        CanonicalOrderStatus.FAILED,
    },
    CanonicalOrderStatus.ACKNOWLEDGED: {
        CanonicalOrderStatus.PARTIALLY_FILLED,
        CanonicalOrderStatus.FILLED,
        CanonicalOrderStatus.CANCEL_PENDING,
        CanonicalOrderStatus.REPLACE_PENDING,
        CanonicalOrderStatus.EXPIRED,
        CanonicalOrderStatus.REJECTED,
    },
    CanonicalOrderStatus.PARTIALLY_FILLED: {
        CanonicalOrderStatus.PARTIALLY_FILLED,
        CanonicalOrderStatus.FILLED,
        CanonicalOrderStatus.CANCEL_PENDING,
        CanonicalOrderStatus.EXPIRED,
    },
    CanonicalOrderStatus.CANCEL_PENDING: {
        CanonicalOrderStatus.CANCELLED,
        CanonicalOrderStatus.PARTIALLY_FILLED,
        CanonicalOrderStatus.FILLED,
    },
    CanonicalOrderStatus.REPLACE_PENDING: {
        CanonicalOrderStatus.REPLACED,
        CanonicalOrderStatus.ACKNOWLEDGED,
        CanonicalOrderStatus.PARTIALLY_FILLED,
        CanonicalOrderStatus.FILLED,
    },
    CanonicalOrderStatus.REPLACED: {
        CanonicalOrderStatus.ACKNOWLEDGED,
    },
    CanonicalOrderStatus.UNKNOWN: {
        CanonicalOrderStatus.ACKNOWLEDGED,
        CanonicalOrderStatus.PARTIALLY_FILLED,
        CanonicalOrderStatus.FILLED,
        CanonicalOrderStatus.CANCELLED,
        CanonicalOrderStatus.REJECTED,
        CanonicalOrderStatus.FAILED,
    },
    # Terminal States: Immutable, zero legal outgoing transitions
    CanonicalOrderStatus.FILLED: set(),
    CanonicalOrderStatus.CANCELLED: set(),
    CanonicalOrderStatus.REJECTED: set(),
    CanonicalOrderStatus.EXPIRED: set(),
    CanonicalOrderStatus.FAILED: set(),
}

TERMINAL_STATES: Set[CanonicalOrderStatus] = {
    CanonicalOrderStatus.FILLED,
    CanonicalOrderStatus.CANCELLED,
    CanonicalOrderStatus.REJECTED,
    CanonicalOrderStatus.EXPIRED,
    CanonicalOrderStatus.FAILED,
}


class ExecutionState:
    """
    Thread-safe order actor encapsulating the lifecycle state machine, order versioning,
    broker order ID mappings, and mathematically closed partial-fill accounting.
    """

    def __init__(self, request: OrderRequest) -> None:
        self.request = request
        self.status = CanonicalOrderStatus.CREATED
        self.order_version = request.order_version
        self.broker_order_id: Optional[str] = None
        self.broker_id_history: Dict[str, int] = {}  # broker_order_id -> order_version
        self.fills: List[Fill] = []
        self.seen_fill_ids: Set[str] = set()
        self.cumulative_filled_quantity: int = 0
        self.remaining_quantity: int = request.quantity
        self.average_execution_price: float = 0.0
        self.failure_reason: Optional[ExecutionFailureReason] = None
        self.error_message: Optional[str] = None
        self.rejection_reason: Optional[ExecutionFailureReason] = None
        self._lock = threading.RLock()

    @property
    def client_order_id(self) -> str:
        return self.request.client_order_id

    @property
    def intent_id(self) -> str:
        return self.request.intent_id

    @property
    def instrument_id(self) -> InstrumentId:
        return self.request.instrument_id

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in TERMINAL_STATES

    def transition_to(self, target_status: CanonicalOrderStatus) -> None:
        """
        Executes a deterministic state transition, strictly enforcing legal matrix rules.
        Fails closed on any illegal or terminal mutation attempt.
        """
        with self._lock:
            if self.status in TERMINAL_STATES:
                raise ValueError(
                    f"Illegal transition: order {self.client_order_id} is in terminal state "
                    f"{self.status} and cannot transition to {target_status}."
                )

            allowed = LEGAL_TRANSITIONS.get(self.status, set())
            if target_status not in allowed:
                raise ValueError(
                    f"Illegal state transition for order {self.client_order_id}: "
                    f"{self.status} -> {target_status}. Allowed targets: {allowed}"
                )

            self.status = target_status

    def record_acknowledgement(self, ack: OrderAcknowledgement) -> None:
        """
        Attaches broker-assigned order ID and records order version mapping.
        """
        with self._lock:
            if ack.broker_order_id:
                self.broker_order_id = ack.broker_order_id
                self.broker_id_history[ack.broker_order_id] = self.order_version

    def apply_fill(self, fill: Fill) -> bool:
        """
        Applies an execution fill to order accounting.
        Enforces idempotency against duplicate fill_id and quantity conservation.
        Returns True if fill was newly applied, False if duplicate.
        """
        with self._lock:
            # 1. Duplicate Fill Suppression (Idempotency)
            if fill.fill_id in self.seen_fill_ids:
                return False

            if fill.client_order_id != self.client_order_id:
                raise ValueError(
                    f"Fill client_order_id mismatch: expected {self.client_order_id}, got {fill.client_order_id}"
                )

            # 2. Fill Conservation Check
            new_cumulative_qty = self.cumulative_filled_quantity + fill.fill_quantity
            if new_cumulative_qty > self.request.quantity:
                # Overfill anomaly detected! Short circuit to UNKNOWN
                self.record_failure(
                    f"Overfill anomaly: fill qty {fill.fill_quantity} causes cumulative "
                    f"{new_cumulative_qty} > requested {self.request.quantity}."
                )
                self.status = CanonicalOrderStatus.UNKNOWN
                return False

            # 3. Volume-Weighted Average Execution Price Calculation
            current_notional = self.average_execution_price * self.cumulative_filled_quantity
            fill_notional = fill.fill_price * fill.fill_quantity
            self.cumulative_filled_quantity = new_cumulative_qty
            self.average_execution_price = (current_notional + fill_notional) / self.cumulative_filled_quantity
            self.remaining_quantity = self.request.quantity - self.cumulative_filled_quantity

            self.fills.append(fill)
            self.seen_fill_ids.add(fill.fill_id)

            # 4. State Progression
            if self.remaining_quantity == 0:
                if self.status != CanonicalOrderStatus.FILLED:
                    self.transition_to(CanonicalOrderStatus.FILLED)
            else:
                if self.status not in (
                    CanonicalOrderStatus.PARTIALLY_FILLED,
                    CanonicalOrderStatus.REPLACE_PENDING,
                ):
                    self.transition_to(CanonicalOrderStatus.PARTIALLY_FILLED)

            return True

    def record_rejection(self, reason: ExecutionFailureReason, message: str) -> None:
        """Records terminal pre-trade or broker business rejection."""
        with self._lock:
            self.rejection_reason = reason
            self.failure_reason = reason
            self.error_message = message

    def record_failure(self, message: str) -> None:
        """Records transport failure or error message."""
        with self._lock:
            self.failure_reason = ExecutionFailureReason.CONNECTION_LOST
            self.error_message = message

    def initiate_replace(self) -> None:
        """Transitions state to REPLACE_PENDING when price/size modification is dispatched."""
        with self._lock:
            self.transition_to(CanonicalOrderStatus.REPLACE_PENDING)

    def confirm_replace(self, new_broker_order_id: str) -> None:
        """
        Applies confirmed replacement, increments order_version, attaches new broker ID,
        and cycles through REPLACED -> ACKNOWLEDGED.
        """
        with self._lock:
            self.order_version += 1
            self.broker_order_id = new_broker_order_id
            self.broker_id_history[new_broker_order_id] = self.order_version
            self.transition_to(CanonicalOrderStatus.REPLACED)
            self.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)

    def reject_replace(self) -> None:
        """
        Reverts modification attempt back to previous working state without terminating order.
        """
        with self._lock:
            target = (
                CanonicalOrderStatus.PARTIALLY_FILLED
                if self.cumulative_filled_quantity > 0
                else CanonicalOrderStatus.ACKNOWLEDGED
            )
            self.status = target


class ExecutionStateRegistry:
    """
    Thread-safe in-memory registry indexing active and historical ExecutionStates.
    Enforces single-active-order-per-intent invariant, manages multi-version broker order ID
    lookup, and maintains quarantined instruments under UNKNOWN states.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states_by_client_id: Dict[str, ExecutionState] = {}
        self._intent_to_client_id: Dict[str, str] = {}
        self._broker_id_to_client_id: Dict[str, str] = {}
        self._quarantined_instruments: Set[InstrumentId] = set()

    def create_state(self, request: OrderRequest) -> ExecutionState:
        """
        Creates and registers a new ExecutionState for an OrderRequest.
        Fails if intent already has an active working order or client_order_id is duplicate.
        """
        with self._lock:
            if request.client_order_id in self._states_by_client_id:
                raise ValueError(f"Duplicate client_order_id: {request.client_order_id} is already registered.")

            if self.has_active_intent(request.intent_id):
                raise ValueError(
                    f"Intent {request.intent_id} already has an active working order "
                    f"({self._intent_to_client_id[request.intent_id]})."
                )

            state = ExecutionState(request)
            self._states_by_client_id[request.client_order_id] = state
            self._intent_to_client_id[request.intent_id] = request.client_order_id
            return state

    def get_state(self, client_order_id: str) -> Optional[ExecutionState]:
        with self._lock:
            return self._states_by_client_id.get(client_order_id)

    def get_by_broker_id(self, broker_order_id: str) -> Optional[ExecutionState]:
        with self._lock:
            client_id = self._broker_id_to_client_id.get(broker_order_id)
            if client_id is not None:
                return self._states_by_client_id.get(client_id)
            return None

    def register_broker_order_id(self, client_order_id: str, broker_order_id: str) -> None:
        with self._lock:
            self._broker_id_to_client_id[broker_order_id] = client_order_id

    def has_active_intent(self, intent_id: str) -> bool:
        with self._lock:
            client_id = self._intent_to_client_id.get(intent_id)
            if client_id is None:
                return False
            state = self._states_by_client_id.get(client_id)
            if state is None:
                return False
            return not state.is_terminal

    def release_intent(self, intent_id: str) -> None:
        with self._lock:
            if intent_id in self._intent_to_client_id:
                del self._intent_to_client_id[intent_id]

    def quarantine_instrument(self, instrument_id: InstrumentId) -> None:
        with self._lock:
            self._quarantined_instruments.add(instrument_id)

    def unquarantine_instrument(self, instrument_id: InstrumentId) -> None:
        with self._lock:
            self._quarantined_instruments.discard(instrument_id)

    def is_quarantined(self, instrument_id: InstrumentId) -> bool:
        with self._lock:
            return instrument_id in self._quarantined_instruments

    def get_all_states(self) -> List[ExecutionState]:
        with self._lock:
            return list(self._states_by_client_id.values())
