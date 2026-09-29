"""
Tradego Phase 8 — Execution State Synchronization, Fill Reconciliation & Post-Trade Event Stream.

Establishes the authoritative downstream execution lifecycle:
OrderInstruction -> Broker Dispatch -> Broker Acknowledgement -> Execution State
-> Partial Fill / Full Fill / Reject / Cancel -> Fill Reconciliation -> Post-Trade Events.

Enforces:
- Strict provenance retention
- Zero implicit mutation
- Strict duplicate/idempotency protection
- Out-of-order event resilience (never rolls state backwards)
- Exact weighted average fill price calculation
- Comprehensive audit trails with SHA-256 hash chaining
- Integration with existing EventBroadcaster and monotonic sequence numbers
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import uuid

from .broadcaster import EventBroadcaster
from .order_instruction import InstructionState, OrderInstruction
from .recovery import GuardState, TradingGuard
from .security import Tier1AuditLogger


class ExecutionState(str, Enum):
    """Authoritative execution lifecycle states."""
    DISPATCH_PENDING = "DISPATCH_PENDING"
    DISPATCHED = "DISPATCHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, val: Any) -> "ExecutionState":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.UNKNOWN
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.UNKNOWN


class ReconciliationStatus(str, Enum):
    """Authoritative post-trade reconciliation status."""
    RECONCILED = "RECONCILED"
    MISMATCH = "MISMATCH"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, val: Any) -> "ReconciliationStatus":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.UNKNOWN
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.UNKNOWN


# Rank states to prevent out-of-order events from rolling state backwards
_STATE_PRECEDENCE = {
    ExecutionState.UNKNOWN: 0,
    ExecutionState.DISPATCH_PENDING: 1,
    ExecutionState.DISPATCHED: 2,
    ExecutionState.ACKNOWLEDGED: 3,
    ExecutionState.CANCEL_PENDING: 4,
    ExecutionState.PARTIALLY_FILLED: 5,
    ExecutionState.CANCELLED: 6,
    ExecutionState.FILLED: 7,
    ExecutionState.REJECTED: 7,
    ExecutionState.FAILED: 7,
}


@dataclass(frozen=True)
class FillRecord:
    """Immutable authoritative record of an individual venue fill."""
    fill_id: str
    quantity: int
    price: Optional[float]
    timestamp: str
    execution_id: str = ""
    broker_fill_id: Optional[str] = None
    fee: float = 0.0
    raw_payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "broker_fill_id": self.broker_fill_id or self.fill_id,
            "execution_id": self.execution_id,
            "quantity": self.quantity,
            "price": self.price,
            "timestamp": self.timestamp,
            "fee": self.fee,
            "raw_payload": self.raw_payload,
        }


class ExecutionRecord:
    """
    Authoritative point-in-time point of truth for order execution state.
    Maintains provenance, fill ledger, cumulative quantities, and weighted average price.
    """

    def __init__(
        self,
        execution_id: str,
        instruction_id: str,
        intent_id: str,
        symbol: str,
        side: str,
        ordered_quantity: int,
        limit_price: Optional[float] = None,
        correlation_id: str = "",
        gateway_timestamp: str = "",
        broker_order_id: Optional[str] = None,
        broker_timestamp: Optional[str] = None,
        current_state: ExecutionState = ExecutionState.DISPATCH_PENDING,
        reconciliation_status: ReconciliationStatus = ReconciliationStatus.PENDING,
        price: Optional[float] = None,
        fills: Optional[Sequence[FillRecord]] = None,
        filled_quantity: Optional[int] = None,
        remaining_quantity: Optional[int] = None,
        average_fill_price: Optional[float] = None,
    ) -> None:
        self.execution_id = execution_id
        self.instruction_id = instruction_id
        self.intent_id = intent_id
        self.symbol = symbol
        self.side = side
        self.ordered_quantity = ordered_quantity
        self.limit_price = limit_price if limit_price is not None else price
        self.broker_order_id = broker_order_id
        self.broker_timestamp = broker_timestamp
        self.gateway_timestamp = gateway_timestamp or datetime.now(timezone.utc).isoformat()
        self.current_state = current_state
        self.reconciliation_status = reconciliation_status
        self.correlation_id = correlation_id
        self.rejection_reason: Optional[str] = None
        self.failure_reason: Optional[str] = None
        self.cancellation_reason: Optional[str] = None
        self.reconciliation_notes: Optional[str] = None

        # Ledger & quantities
        self.fills: List[FillRecord] = list(fills) if fills else []
        self.filled_quantity = filled_quantity if filled_quantity is not None else sum(f.quantity for f in self.fills)
        if remaining_quantity is not None:
            self.remaining_quantity = remaining_quantity
        elif current_state in [ExecutionState.CANCELLED, ExecutionState.REJECTED, ExecutionState.FAILED]:
            self.remaining_quantity = 0
        else:
            self.remaining_quantity = max(0, self.ordered_quantity - self.filled_quantity)

        self.average_fill_price = average_fill_price
        if self.average_fill_price is None and self.fills:
            self._recalculate_average_fill_price()

        self.last_update_timestamp = self.gateway_timestamp

    @property
    def price(self) -> Optional[float]:
        return self.limit_price

    @property
    def last_error(self) -> Optional[str]:
        return self.failure_reason or self.rejection_reason or self.cancellation_reason or self.reconciliation_notes

    @property
    def reconciliation_details(self) -> Optional[str]:
        return self.reconciliation_notes

    def _recalculate_average_fill_price(self) -> None:
        priced_fills = [f for f in self.fills if f.price is not None and f.quantity > 0]
        if not priced_fills:
            self.average_fill_price = None
            return
        total_qty = sum(f.quantity for f in priced_fills)
        if total_qty == 0:
            self.average_fill_price = None
            return
        total_notional = sum(f.quantity * f.price for f in priced_fills)
        self.average_fill_price = total_notional / total_qty

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "instruction_id": self.instruction_id,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "ordered_quantity": self.ordered_quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "price": self.price,
            "limit_price": self.limit_price,
            "average_fill_price": self.average_fill_price,
            "broker_order_id": self.broker_order_id,
            "broker_timestamp": self.broker_timestamp,
            "gateway_timestamp": self.gateway_timestamp,
            "current_state": self.current_state.value,
            "reconciliation_status": self.reconciliation_status.value,
            "correlation_id": self.correlation_id,
            "rejection_reason": self.rejection_reason,
            "failure_reason": self.failure_reason,
            "cancellation_reason": self.cancellation_reason,
            "last_error": self.last_error,
            "reconciliation_notes": self.reconciliation_notes,
            "reconciliation_details": self.reconciliation_details,
            "fills_count": len(self.fills),
            "fills": [f.to_dict() for f in self.fills],
            "last_update_timestamp": self.last_update_timestamp,
        }


class ExecutionReconciliationEngine:
    """
    Point-in-time auditor evaluating observed broker execution state against
    authoritative OrderInstruction expectations.
    Detects quantity discrepancies, impossible transitions, or missing acknowledgements.
    """

    def reconcile(
        self,
        record: ExecutionRecord,
        instruction: Optional[OrderInstruction] = None,
    ) -> None:
        """
        Evaluates the execution record and updates reconciliation_status and reconciliation_notes.
        """
        # 1. Quantity bounds check
        if record.filled_quantity > record.ordered_quantity:
            record.reconciliation_status = ReconciliationStatus.MISMATCH
            record.reconciliation_notes = (
                f"QUANTITY_OVERFLOW: Cumulative fills {record.filled_quantity} "
                f"exceed ordered {record.ordered_quantity}."
            )
            return

        # 2. Mathematical invariant: filled + remaining == ordered
        # Note: If cancelled, rejected, or failed, remaining is 0 while filled <= ordered.
        if record.current_state in [ExecutionState.CANCELLED, ExecutionState.REJECTED, ExecutionState.FAILED]:
            if record.remaining_quantity != 0:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = (
                    f"QUANTITY_DESYNC: Terminal state {record.current_state.value} "
                    f"has non-zero remaining quantity {record.remaining_quantity}."
                )
                return
        else:
            if record.filled_quantity + record.remaining_quantity != record.ordered_quantity:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = (
                    f"QUANTITY_DESYNC: filled ({record.filled_quantity}) + remaining ({record.remaining_quantity}) "
                    f"!= ordered ({record.ordered_quantity})."
                )
                return

        # 3. Fill ledger sum check
        actual_fill_sum = sum(f.quantity for f in record.fills)
        if actual_fill_sum != record.filled_quantity:
            record.reconciliation_status = ReconciliationStatus.MISMATCH
            record.reconciliation_notes = (
                f"FILL_LEDGER_MISMATCH: Sum of fill records ({actual_fill_sum}) "
                f"does not match filled_quantity ({record.filled_quantity})."
            )
            return

        # 4. State vs fill consistency
        if record.filled_quantity == record.ordered_quantity and record.current_state != ExecutionState.FILLED:
            record.reconciliation_status = ReconciliationStatus.MISMATCH
            record.reconciliation_notes = (
                f"STATE_MISMATCH: Quantity 100% filled but state is {record.current_state.value}."
            )
            return

        if 0 < record.filled_quantity < record.ordered_quantity and record.current_state not in [
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.CANCELLED,
            ExecutionState.CANCEL_PENDING,
        ]:
            record.reconciliation_status = ReconciliationStatus.MISMATCH
            record.reconciliation_notes = (
                f"STATE_MISMATCH: Partially filled quantity ({record.filled_quantity}) "
                f"inconsistent with state {record.current_state.value}."
            )
            return

        # 5. Instruction comparison (if provided)
        if instruction is not None:
            if record.instruction_id != instruction.instruction_id:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = f"INSTRUCTION_MISMATCH: ID {record.instruction_id} != {instruction.instruction_id}"
                return
            if record.symbol != instruction.symbol:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = f"SYMBOL_MISMATCH: Record {record.symbol} != Instruction {instruction.symbol}"
                return
            if record.ordered_quantity != instruction.quantity:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = f"ORDERED_QUANTITY_MISMATCH: Record {record.ordered_quantity} != Instruction {instruction.quantity}"
                return

        # Passed all integrity checks
        record.reconciliation_status = ReconciliationStatus.RECONCILED
        record.reconciliation_notes = "Authoritative fill ledger matches instruction parameters."

    def reconcile_record(
        self,
        record: ExecutionRecord,
        instruction: Optional[OrderInstruction] = None,
    ) -> Tuple[ReconciliationStatus, str]:
        """Convenience method returning (status, details)."""
        self.reconcile(record, instruction)
        return (record.reconciliation_status, record.reconciliation_notes or "")


class ExecutionStateManager:
    """
    Authoritative point of truth for order execution tracking, fill accounting,
    out-of-order handling, duplicate protection, and post-trade event distribution.
    """

    def __init__(
        self,
        trading_guard: Optional[TradingGuard] = None,
        audit_logger: Optional[Tier1AuditLogger] = None,
        broadcaster: Optional[EventBroadcaster] = None,
        reconciliation_engine: Optional[ExecutionReconciliationEngine] = None,
        connectivity_manager: Optional[Any] = None,
        persistence_manager: Optional[Any] = None,
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._broadcaster = broadcaster
        self._reconciler = reconciliation_engine or ExecutionReconciliationEngine()
        self._connectivity_mgr = connectivity_manager
        self._persistence_mgr = persistence_manager
        self._lock = threading.RLock()

        self._executions: Dict[str, ExecutionRecord] = {}
        self._instruction_to_execution: Dict[str, str] = {}
        self._broker_order_to_execution: Dict[str, str] = {}
        self._processed_event_ids: Set[str] = set()

    @property
    def persistence_manager(self) -> Optional[Any]:
        return self._persistence_mgr

    def set_persistence_manager(self, mgr: Any) -> None:
        with self._lock:
            self._persistence_mgr = mgr

    @property
    def connectivity_manager(self) -> Optional[Any]:
        return self._connectivity_mgr

    def set_connectivity_manager(self, mgr: Any) -> None:
        with self._lock:
            self._connectivity_mgr = mgr

    def _log_audit(self, entry: Dict[str, Any]) -> None:
        if self._audit:
            self._audit.log(entry)

    def _publish_event(self, event_type: str, payload: Dict[str, Any], correlation_id: str) -> None:
        if self._broadcaster:
            self._broadcaster.publish(
                event_type=event_type,
                payload=payload,
                correlation_id=correlation_id,
            )

    def _persist_execution(self, record: ExecutionRecord, correlation_id: str = "") -> None:
        if self._persistence_mgr:
            try:
                self._persistence_mgr.append_execution(record, correlation_id=correlation_id or record.correlation_id)
            except Exception:
                pass

    def _persist_fill(self, fill: FillRecord, execution_id: str, correlation_id: str = "") -> None:
        if self._persistence_mgr:
            try:
                self._persistence_mgr.append_fill(fill, execution_id=execution_id, correlation_id=correlation_id)
            except Exception:
                pass

    def create_execution(
        self,
        *args: Any,
        instruction: Optional[OrderInstruction] = None,
        instruction_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        ordered_quantity: Optional[int] = None,
        price: Optional[float] = None,
        limit_price: Optional[float] = None,
        broker_order_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ExecutionRecord:
        """
        Initializes an authoritative ExecutionRecord from an OrderInstruction or canonical parameters.
        """
        with self._lock:
            target_ins = None
            if args and isinstance(args[0], OrderInstruction):
                target_ins = args[0]
                corr_id = args[1] if len(args) > 1 else (correlation_id or target_ins.correlation_id or str(uuid.uuid4()))
            elif isinstance(instruction, OrderInstruction):
                target_ins = instruction
                corr_id = correlation_id or target_ins.correlation_id or str(uuid.uuid4())

            if target_ins is not None:
                ins_id = target_ins.instruction_id
                int_id = target_ins.intent_id
                sym = target_ins.symbol
                s_side = target_ins.side
                qty = target_ins.quantity
                l_price = target_ins.limit_price
                b_order_id = target_ins.broker_order_id
                initial_state = (
                    ExecutionState.ACKNOWLEDGED
                    if target_ins.state == InstructionState.ACKNOWLEDGED
                    else ExecutionState.DISPATCHED
                    if target_ins.state == InstructionState.DISPATCHED
                    else ExecutionState.DISPATCH_PENDING
                )
            elif args and isinstance(args[0], str):
                ins_id = args[0]
                int_id = args[1] if len(args) > 1 else (intent_id or f"int-{uuid.uuid4().hex[:8]}")
                sym = args[2] if len(args) > 2 else (symbol or "UNKNOWN")
                s_side = args[3] if len(args) > 3 else (side or "BUY")
                qty = args[4] if len(args) > 4 else (ordered_quantity if ordered_quantity is not None else 1)
                p = args[5] if len(args) > 5 else (limit_price if limit_price is not None else price)
                l_price = p
                b_order_id = broker_order_id
                corr_id = correlation_id or str(uuid.uuid4())
                initial_state = ExecutionState.DISPATCHED
            else:
                ins_id = instruction_id or f"ins-{uuid.uuid4().hex[:8]}"
                int_id = intent_id or f"int-{uuid.uuid4().hex[:8]}"
                sym = symbol or "UNKNOWN"
                s_side = side or "BUY"
                qty = ordered_quantity if ordered_quantity is not None else 1
                l_price = limit_price if limit_price is not None else price
                b_order_id = broker_order_id
                corr_id = correlation_id or str(uuid.uuid4())
                initial_state = ExecutionState.DISPATCHED

            # Idempotency: Return existing execution if already registered for this instruction
            if ins_id in self._instruction_to_execution:
                existing_id = self._instruction_to_execution[ins_id]
                return self._executions[existing_id]

            now_str = datetime.now(timezone.utc).isoformat()
            execution_id = f"exec-{uuid.uuid4().hex[:12]}"
            record = ExecutionRecord(
                execution_id=execution_id,
                instruction_id=ins_id,
                intent_id=int_id,
                symbol=sym,
                side=s_side,
                ordered_quantity=qty,
                limit_price=l_price,
                correlation_id=corr_id,
                gateway_timestamp=now_str,
                broker_order_id=b_order_id,
                current_state=initial_state,
                reconciliation_status=ReconciliationStatus.RECONCILED if initial_state in [ExecutionState.DISPATCHED, ExecutionState.ACKNOWLEDGED] else ReconciliationStatus.PENDING,
            )

            self._executions[execution_id] = record
            self._instruction_to_execution[ins_id] = execution_id
            if b_order_id:
                self._broker_order_to_execution[b_order_id] = execution_id

            self._log_audit({
                "action": "ORDER_DISPATCHED" if initial_state == ExecutionState.DISPATCHED else "ORDER_ACKNOWLEDGED" if initial_state == ExecutionState.ACKNOWLEDGED else "EXECUTION_CREATED",
                "event_type": "ORDER_DISPATCHED",
                "execution_id": execution_id,
                "instruction_id": ins_id,
                "intent_id": int_id,
                "symbol": record.symbol,
                "ordered_quantity": record.ordered_quantity,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": ins_id,
                    "intent_id": int_id,
                },
            })

            self._publish_event(
                event_type="ORDER_DISPATCHED",
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            self._persist_execution(record, corr_id)

            return record

    def apply_acknowledgement(
        self,
        execution_id: str,
        broker_order_id: str,
        event_id: Optional[str] = None,
        broker_timestamp: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """
        Applies venue order acknowledgement.
        Resilient against duplicates and out-of-order fills.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()
            ack_event_id = event_id or f"ack:{execution_id}:{broker_order_id}"

            # Idempotency Check
            if ack_event_id in self._processed_event_ids:
                self._log_audit({
                    "action": "DUPLICATE_EVENT_IGNORED",
                    "event_type": "DUPLICATE_EVENT_IGNORED",
                    "execution_id": execution_id,
                    "event_id": ack_event_id,
                    "correlation_id": corr_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })
                return record

            self._processed_event_ids.add(ack_event_id)
            record.broker_order_id = broker_order_id
            self._broker_order_to_execution[broker_order_id] = execution_id
            record.broker_timestamp = broker_timestamp or now_str
            record.last_update_timestamp = now_str

            # Out-of-order resilience: Do not regress state if fills have already arrived
            if _STATE_PRECEDENCE[record.current_state] < _STATE_PRECEDENCE[ExecutionState.ACKNOWLEDGED]:
                record.current_state = ExecutionState.ACKNOWLEDGED

            self._log_audit({
                "action": "ORDER_ACKNOWLEDGED",
                "event_type": "ORDER_ACKNOWLEDGED",
                "execution_id": execution_id,
                "instruction_id": record.instruction_id,
                "intent_id": record.intent_id,
                "broker_order_id": broker_order_id,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._publish_event(
                event_type="ORDER_ACKNOWLEDGED",
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            self._reconciler.reconcile(record)
            self._persist_execution(record, corr_id)
            return record

    def apply_fill(
        self,
        execution_id: str,
        fill_id: Optional[Any] = None,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        broker_fill_id: Optional[str] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        fee: float = 0.0,
        raw_payload: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ExecutionRecord:
        """
        Applies a venue fill event (partial or full).
        Calculates exact weighted average price and prevents quantity overflows.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            # Support parameter permutation where quantity is passed positionally as 2nd arg
            if isinstance(fill_id, (int, float)) and quantity is None:
                quantity = int(fill_id)
                fill_id = None

            actual_fill_id = (
                str(fill_id)
                if fill_id is not None
                else broker_fill_id or event_id or f"fill-{uuid.uuid4().hex[:8]}"
            )
            actual_broker_fill_id = broker_fill_id or actual_fill_id
            dedup_key = event_id or actual_broker_fill_id or actual_fill_id

            # Idempotency Check
            if dedup_key in self._processed_event_ids:
                self._log_audit({
                    "action": "DUPLICATE_EVENT_IGNORED",
                    "event_type": "DUPLICATE_EVENT_IGNORED",
                    "execution_id": execution_id,
                    "event_id": dedup_key,
                    "correlation_id": correlation_id or record.correlation_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })
                return record

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()
            fill_time = timestamp or now_str

            # Invariant check: Cannot apply fill to terminal state
            if record.current_state in [ExecutionState.CANCELLED, ExecutionState.REJECTED, ExecutionState.FAILED]:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                if record.current_state == ExecutionState.CANCELLED:
                    record.reconciliation_notes = (
                        f"STALE_FILL_AFTER_CANCEL: ILLEGAL_FILL_AFTER_TERMINAL: Received fill for execution "
                        f"in terminal state {record.current_state.value}."
                    )
                else:
                    record.reconciliation_notes = (
                        f"ILLEGAL_FILL_AFTER_TERMINAL: Received fill for execution "
                        f"in terminal state {record.current_state.value}."
                    )
                self._log_audit({
                    "action": "INVALID_STATE_TRANSITION",
                    "event_type": "INVALID_STATE_TRANSITION",
                    "execution_id": execution_id,
                    "fill_id": actual_fill_id,
                    "reason": record.reconciliation_notes,
                    "correlation_id": corr_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })
                self._publish_event(
                    event_type="EXECUTION_RECONCILED",
                    payload=record.to_dict(),
                    correlation_id=corr_id,
                )
                raise ValueError(record.reconciliation_notes)

            # Quantity validation
            actual_qty = quantity if quantity is not None else 0
            if actual_qty <= 0:
                raise ValueError(f"INVALID_FILL_QUANTITY: Fill quantity must be > 0, got {actual_qty}")

            if record.filled_quantity + actual_qty > record.ordered_quantity:
                record.reconciliation_status = ReconciliationStatus.MISMATCH
                record.reconciliation_notes = (
                    f"QUANTITY_OVERFLOW: Attempted fill {actual_qty} would exceed "
                    f"ordered quantity {record.ordered_quantity} (already filled {record.filled_quantity})."
                )
                self._log_audit({
                    "action": "INVALID_STATE_TRANSITION",
                    "event_type": "INVALID_STATE_TRANSITION",
                    "execution_id": execution_id,
                    "fill_id": actual_fill_id,
                    "reason": record.reconciliation_notes,
                    "correlation_id": corr_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })
                self._publish_event(
                    event_type="EXECUTION_RECONCILED",
                    payload=record.to_dict(),
                    correlation_id=corr_id,
                )
                raise ValueError(record.reconciliation_notes)

            # Record fill
            self._processed_event_ids.add(dedup_key)
            fill_rec = FillRecord(
                fill_id=actual_fill_id,
                execution_id=execution_id,
                quantity=actual_qty,
                price=price,
                timestamp=fill_time,
                broker_fill_id=actual_broker_fill_id,
                fee=fee,
                raw_payload=raw_payload,
            )
            record.fills.append(fill_rec)

            # Update quantities & average fill price
            record.filled_quantity += actual_qty
            record.remaining_quantity = max(0, record.ordered_quantity - record.filled_quantity)
            record._recalculate_average_fill_price()
            record.last_update_timestamp = now_str

            # Update state: FILLED or PARTIALLY_FILLED
            if record.filled_quantity == record.ordered_quantity:
                record.current_state = ExecutionState.FILLED
                event_type = "ORDER_FILLED"
            else:
                record.current_state = ExecutionState.PARTIALLY_FILLED
                event_type = "ORDER_PARTIALLY_FILLED"

            self._log_audit({
                "action": event_type,
                "event_type": event_type,
                "execution_id": execution_id,
                "fill_id": actual_fill_id,
                "filled_quantity": record.filled_quantity,
                "remaining_quantity": record.remaining_quantity,
                "fill_price": price,
                "average_fill_price": record.average_fill_price,
                "resulting_state": record.current_state.value,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._publish_event(
                event_type=event_type,
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            # Live audit event if in live mode
            is_live = False
            if self._connectivity_mgr:
                raw_mode = getattr(self._connectivity_mgr, "execution_mode", None)
                mode_str = raw_mode.value if hasattr(raw_mode, "value") else str(raw_mode)
                is_live = (mode_str == "LIVE")
            if is_live or kwargs.get("is_live"):
                self._log_audit({
                    "action": "LIVE_FILL_RECEIVED",
                    "event_type": "LIVE_FILL_RECEIVED",
                    "execution_id": execution_id,
                    "fill_id": actual_fill_id,
                    "quantity": actual_qty,
                    "price": price,
                    "correlation_id": corr_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })


            # Reconcile after fill
            self._reconciler.reconcile(record)
            if record.reconciliation_status == ReconciliationStatus.RECONCILED:
                self._log_audit({
                    "action": "EXECUTION_RECONCILED",
                    "event_type": "EXECUTION_RECONCILED",
                    "execution_id": execution_id,
                    "correlation_id": corr_id,
                    "details": {
                        "execution_id": execution_id,
                        "instruction_id": record.instruction_id,
                        "intent_id": record.intent_id,
                    },
                })
                self._publish_event(
                    event_type="EXECUTION_RECONCILED",
                    payload=record.to_dict(),
                    correlation_id=corr_id,
                )

            self._persist_fill(fill_rec, execution_id, corr_id)
            self._persist_execution(record, corr_id)

            return record

    def apply_rejection(
        self,
        execution_id: str,
        rejection_reason: Optional[str] = None,
        reason: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """
        Applies venue business rejection.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()
            effective_reason = rejection_reason or reason or "BROKER_REJECTED"

            # If already terminal, ignore
            if record.current_state in [ExecutionState.REJECTED, ExecutionState.FAILED, ExecutionState.FILLED]:
                return record

            record.current_state = ExecutionState.REJECTED
            record.rejection_reason = effective_reason
            if broker_order_id:
                record.broker_order_id = broker_order_id
            record.remaining_quantity = 0
            record.last_update_timestamp = now_str

            self._log_audit({
                "action": "ORDER_REJECTED",
                "event_type": "ORDER_REJECTED",
                "execution_id": execution_id,
                "reason": effective_reason,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._publish_event(
                event_type="ORDER_REJECTED",
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            self._reconciler.reconcile(record)
            self._persist_execution(record, corr_id)
            return record

    def apply_failure(
        self,
        execution_id: str,
        failure_reason: Optional[str] = None,
        reason: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """
        Applies transport or adapter failure.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()
            effective_reason = failure_reason or reason or "TRANSPORT_FAILURE"

            if record.current_state in [ExecutionState.FAILED, ExecutionState.FILLED]:
                return record

            record.current_state = ExecutionState.FAILED
            record.failure_reason = effective_reason
            record.remaining_quantity = 0
            record.last_update_timestamp = now_str

            self._log_audit({
                "action": "ORDER_FAILED",
                "event_type": "ORDER_FAILED",
                "execution_id": execution_id,
                "reason": effective_reason,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._publish_event(
                event_type="ORDER_FAILED",
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            self._reconciler.reconcile(record)
            record.reconciliation_status = ReconciliationStatus.MISMATCH
            self._persist_execution(record, corr_id)
            return record

    def apply_cancellation(
        self,
        execution_id: str,
        reason: str = "ORDER_CANCELLED",
        correlation_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """
        Applies order cancellation acknowledgement.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()

            if record.current_state == ExecutionState.FILLED:
                raise ValueError("CANNOT_CANCEL_FILLED_ORDER: Order is already FILLED.")

            if record.current_state == ExecutionState.CANCELLED:
                return record

            record.current_state = ExecutionState.CANCELLED
            record.cancellation_reason = reason
            record.remaining_quantity = 0
            record.last_update_timestamp = now_str

            self._log_audit({
                "action": "ORDER_CANCELLED",
                "event_type": "ORDER_CANCELLED",
                "execution_id": execution_id,
                "reason": reason,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._publish_event(
                event_type="ORDER_CANCELLED",
                payload=record.to_dict(),
                correlation_id=corr_id,
            )

            self._reconciler.reconcile(record)
            self._persist_execution(record, corr_id)
            return record

    def apply_unknown_state(
        self,
        execution_id: str,
        raw_state: str,
        correlation_id: Optional[str] = None,
    ) -> ExecutionRecord:
        """
        Safely records an unknown/unrecognized broker state without crashing.
        """
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")

            corr_id = correlation_id or record.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()

            record.current_state = ExecutionState.UNKNOWN
            record.reconciliation_status = ReconciliationStatus.UNKNOWN
            record.reconciliation_notes = f"UNKNOWN_BROKER_STATE: {raw_state}"
            record.last_update_timestamp = now_str

            self._log_audit({
                "action": "UNKNOWN_BROKER_STATE",
                "event_type": "UNKNOWN_BROKER_STATE",
                "execution_id": execution_id,
                "raw_state": raw_state,
                "correlation_id": corr_id,
                "details": {
                    "execution_id": execution_id,
                    "instruction_id": record.instruction_id,
                    "intent_id": record.intent_id,
                },
            })

            self._persist_execution(record, corr_id)
            return record

    def reconcile(
        self,
        execution_id: str,
        instruction: Optional[OrderInstruction] = None,
    ) -> ExecutionRecord:
        """Explicit on-demand reconciliation."""
        with self._lock:
            record = self._executions.get(execution_id)
            if not record:
                raise KeyError(f"Execution not found: {execution_id}")
            self._reconciler.reconcile(record, instruction)
            if self._persistence_mgr:
                try:
                    self._persistence_mgr.append_reconciliation(record, correlation_id=record.correlation_id)
                except Exception:
                    pass
            self._persist_execution(record, record.correlation_id)
            return record

    def get_execution(self, execution_id: str) -> Optional[ExecutionRecord]:
        with self._lock:
            return self._executions.get(execution_id)

    def get_by_instruction(self, instruction_id: str) -> Optional[ExecutionRecord]:
        with self._lock:
            exec_id = self._instruction_to_execution.get(instruction_id)
            return self._executions.get(exec_id) if exec_id else None

    def get_by_broker_order(self, broker_order_id: str) -> Optional[ExecutionRecord]:
        with self._lock:
            exec_id = self._broker_order_to_execution.get(broker_order_id)
            return self._executions.get(exec_id) if exec_id else None

    def list_executions(self) -> List[ExecutionRecord]:
        with self._lock:
            return list(self._executions.values())
