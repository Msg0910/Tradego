"""
Tradego Boundary Execution Intent Gating & Lifecycle Management.
Establishes the authenticated boundary for creating, validating, reviewing,
approving, rejecting, expiring, and cancelling execution intents.

INVARIANT:
An Execution Intent is NOT an executed order.
This boundary strictly decouples intent creation/approval from broker submission.
No broker or execution router calls occur within Phase 5.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional
import uuid

from .security import Tier1AuditLogger


class IntentState(str, Enum):
    """Execution Intent formal lifecycle states."""
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @classmethod
    def from_str(cls, val: str) -> "IntentState":
        """
        Parses a string into an authoritative IntentState.
        Fails closed with ValueError on unknown, malformed, or empty values (M-04).
        """
        if not val or not str(val).strip():
            raise ValueError("IntentState string cannot be empty or null")
        val_clean = str(val).upper().strip()
        for member in cls:
            if member.value == val_clean or member.name == val_clean:
                return member
        raise ValueError(f"Unknown or invalid IntentState: {val!r}")


@dataclass
class ExecutionIntent:
    """
    Typed, schema-validated execution intent submitted at the Zone 3 boundary.
    Represents an authorized proposal to execute, never an executed or broker-submitted order.
    """
    intent_id: str
    correlation_id: str
    creator_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    state: IntentState = IntentState.PENDING_APPROVAL
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    reason: str = ""
    approver_id: Optional[str] = None
    approved_at: Optional[str] = None
    rejection_reason: Optional[str] = None
    cancellation_reason: Optional[str] = None
    status_category: str = "ACCEPTED_AS_INTENT"
    is_executed: bool = False
    risk_status: Optional[str] = None
    risk_evaluator_id: Optional[str] = None
    risk_rejection_reason: Optional[str] = None
    risk_evaluated_at: Optional[str] = None
    risk_details: Optional[Dict[str, Any]] = None

    @property
    def is_expired(self) -> bool:
        """Determines if the intent has exceeded its time-to-live."""
        return datetime.now(timezone.utc) >= self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Serializes intent to JSON-safe dictionary representation."""
        return {
            "intent_id": self.intent_id,
            "correlation_id": self.correlation_id,
            "creator_id": self.creator_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "time_in_force": self.time_in_force,
            "reason": self.reason,
            "state": self.state.value,
            "approver_id": self.approver_id,
            "rejection_reason": self.rejection_reason,
            "cancellation_reason": self.cancellation_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status_category": self.status_category,
            "is_executed": self.is_executed,
            "risk_status": self.risk_status,
            "risk_evaluator_id": self.risk_evaluator_id,
            "risk_rejection_reason": self.risk_rejection_reason,
            "risk_evaluated_at": self.risk_evaluated_at,
            "risk_details": self.risk_details,
        }


class ExecutionIntentManager:
    """
    Thread-safe operational manager enforcing two-person gating and lifecycle
    transitions for execution intents.
    """

    VALID_SIDES = {"BUY", "SELL"}
    VALID_ORDER_TYPES = {"LIMIT", "MARKET", "STOP_LOSS", "STOP_LIMIT"}
    VALID_TIF = {"DAY", "IOC", "GTC"}

    def __init__(
        self,
        audit_logger: Tier1AuditLogger,
        default_ttl_seconds: int = 3600,
        persistence_store: Optional[Any] = None,
    ) -> None:
        self._audit = audit_logger
        self._default_ttl = default_ttl_seconds
        self._persistence_store = persistence_store
        self._lock = threading.RLock()
        self._intents: Dict[str, ExecutionIntent] = {}

    def _persist(self, intent: ExecutionIntent) -> None:
        if self._persistence_store:
            try:
                self._persistence_store.append_intent(intent)
            except Exception:
                pass

    def _check_and_expire_unlocked(self, intent: ExecutionIntent, correlation_id: str) -> bool:
        """Evaluates expiry and transitions if expired. Must be called under lock."""
        if intent.state in [IntentState.DRAFT, IntentState.SUBMITTED, IntentState.PENDING_APPROVAL]:
            if intent.is_expired:
                intent.state = IntentState.EXPIRED
                intent.updated_at = datetime.now(timezone.utc)
                self._persist(intent)
                self._audit.log({
                    "action": "INTENT_EXPIRED",
                    "intent_id": intent.intent_id,
                    "operator_id": intent.creator_id,
                    "resulting_state": intent.state.value,
                    "correlation_id": correlation_id,
                })
                return True
        return False

    def create_intent(
        self,
        creator_id: str,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        correlation_id: str,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "DAY",
        reason: str = "",
        ttl_seconds: Optional[int] = None,
        initial_state: IntentState = IntentState.PENDING_APPROVAL,
    ) -> ExecutionIntent:
        """
        Creates and strictly validates a new execution intent.
        Defaults to PENDING_APPROVAL for immediate operational review.
        """
        # Strict input validation
        if not symbol or not symbol.strip():
            raise ValueError("MALFORMED_INTENT: Symbol must be a non-empty string")

        side_clean = side.strip().upper()
        if side_clean not in self.VALID_SIDES:
            raise ValueError(f"MALFORMED_INTENT: Side must be one of {self.VALID_SIDES}")

        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("MALFORMED_INTENT: Quantity must be a strictly positive integer")

        type_clean = order_type.strip().upper()
        if type_clean not in self.VALID_ORDER_TYPES:
            raise ValueError(f"MALFORMED_INTENT: Order type must be one of {self.VALID_ORDER_TYPES}")

        if type_clean in {"LIMIT", "STOP_LIMIT"}:
            if limit_price is None or limit_price <= 0.0:
                raise ValueError("MALFORMED_INTENT: Limit price must be strictly positive for limit orders")

        tif_clean = time_in_force.strip().upper()
        if tif_clean not in self.VALID_TIF:
            raise ValueError(f"MALFORMED_INTENT: Time in force must be one of {self.VALID_TIF}")

        if initial_state not in [IntentState.DRAFT, IntentState.SUBMITTED, IntentState.PENDING_APPROVAL]:
            raise ValueError(f"INVALID_STATE_TRANSITION: Cannot initialize intent in {initial_state.value}")

        now = datetime.now(timezone.utc)
        ttl = ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else self._default_ttl
        expires_at = now + timedelta(seconds=ttl)
        intent_id = f"intent-{uuid.uuid4().hex[:12]}"

        intent = ExecutionIntent(
            intent_id=intent_id,
            correlation_id=correlation_id,
            creator_id=creator_id,
            symbol=symbol.strip().upper(),
            side=side_clean,
            quantity=quantity,
            order_type=type_clean,
            limit_price=limit_price,
            stop_price=stop_price,
            time_in_force=tif_clean,
            reason=reason.strip(),
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            state=initial_state,
            status_category="ACCEPTED_AS_INTENT",
            is_executed=False,
        )

        with self._lock:
            self._intents[intent_id] = intent

            self._audit.log({
                "action": "INTENT_CREATED",
                "intent_id": intent.intent_id,
                "operator_id": creator_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "order_type": intent.order_type,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            if initial_state == IntentState.PENDING_APPROVAL:
                self._audit.log({
                    "action": "INTENT_APPROVAL_REQUESTED",
                    "intent_id": intent.intent_id,
                    "operator_id": creator_id,
                    "resulting_state": intent.state.value,
                    "correlation_id": correlation_id,
                })

            self._persist(intent)

        return intent

    def submit_intent(
        self,
        intent_id: str,
        operator_id: str,
        correlation_id: str,
    ) -> ExecutionIntent:
        """
        Transitions a DRAFT intent to SUBMITTED / PENDING_APPROVAL.
        """
        with self._lock:
            intent = self._intents.get(intent_id)
            if not intent:
                raise ValueError("INTENT_NOT_FOUND: Execution intent does not exist")

            self._check_and_expire_unlocked(intent, correlation_id)

            if intent.state != IntentState.DRAFT:
                self._audit.log({
                    "action": "INTENT_INVALID_TRANSITION",
                    "intent_id": intent_id,
                    "operator_id": operator_id,
                    "current_state": intent.state.value,
                    "target_state": IntentState.PENDING_APPROVAL.value,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    f"INVALID_STATE_TRANSITION: Cannot submit intent in state {intent.state.value}"
                )

            intent.state = IntentState.PENDING_APPROVAL
            intent.updated_at = datetime.now(timezone.utc)

            self._audit.log({
                "action": "INTENT_SUBMITTED",
                "intent_id": intent.intent_id,
                "operator_id": operator_id,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })
            self._audit.log({
                "action": "INTENT_APPROVAL_REQUESTED",
                "intent_id": intent.intent_id,
                "operator_id": operator_id,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            self._persist(intent)
            return intent

    def approve_intent(
        self,
        intent_id: str,
        approver_id: str,
        correlation_id: str,
    ) -> ExecutionIntent:
        """
        Two-Person Operational Approval: Operator B approves a PENDING_APPROVAL intent.
        Enforces distinct identity (approver_id != creator_id), single-use approval,
        and unexpired state.
        """
        with self._lock:
            intent = self._intents.get(intent_id)
            if not intent:
                raise ValueError("INTENT_NOT_FOUND: Execution intent does not exist")

            self._check_and_expire_unlocked(intent, correlation_id)

            if intent.state == IntentState.EXPIRED:
                raise ValueError("INTENT_EXPIRED: Cannot approve an expired execution intent")

            if intent.state == IntentState.CANCELLED:
                raise ValueError("INTENT_CANCELLED: Cannot approve a cancelled execution intent")

            if intent.state == IntentState.APPROVED:
                raise ValueError("INTENT_ALREADY_TERMINAL: Intent is already approved (single-use constraint)")

            if intent.state == IntentState.REJECTED:
                raise ValueError("INTENT_ALREADY_TERMINAL: Cannot approve an already rejected intent")

            if intent.state != IntentState.PENDING_APPROVAL:
                self._audit.log({
                    "action": "INTENT_INVALID_TRANSITION",
                    "intent_id": intent_id,
                    "operator_id": approver_id,
                    "current_state": intent.state.value,
                    "target_state": IntentState.APPROVED.value,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    f"INVALID_STATE_TRANSITION: Cannot approve intent in state {intent.state.value}"
                )

            # Two-person separation constraint
            if approver_id == intent.creator_id:
                self._audit.log({
                    "action": "INTENT_SAME_OPERATOR_REJECTED",
                    "intent_id": intent_id,
                    "operator_id": approver_id,
                    "creator_id": intent.creator_id,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    "SAME_OPERATOR_APPROVAL_REJECTED: Approver must be distinct from Intent Creator"
                )

            now = datetime.now(timezone.utc)
            intent.state = IntentState.APPROVED
            intent.approver_id = approver_id
            intent.updated_at = now
            intent.status_category = "ACCEPTED_AS_INTENT"
            intent.is_executed = False  # Explicit Phase 5 non-execution invariant

            self._persist(intent)
            self._audit.log({
                "action": "INTENT_APPROVED",
                "intent_id": intent.intent_id,
                "operator_id": approver_id,
                "creator_id": intent.creator_id,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            return intent

    def reject_intent(
        self,
        intent_id: str,
        rejector_id: str,
        reason: str,
        correlation_id: str,
    ) -> ExecutionIntent:
        """
        Two-Person Operational Review: Operator B rejects a PENDING_APPROVAL intent.
        Enforces distinct identity (rejector_id != creator_id) and single-use rejection.
        """
        with self._lock:
            intent = self._intents.get(intent_id)
            if not intent:
                raise ValueError("INTENT_NOT_FOUND: Execution intent does not exist")

            self._check_and_expire_unlocked(intent, correlation_id)

            if intent.state == IntentState.EXPIRED:
                raise ValueError("INTENT_EXPIRED: Cannot reject an expired execution intent")

            if intent.state == IntentState.CANCELLED:
                raise ValueError("INTENT_CANCELLED: Cannot reject a cancelled execution intent")

            if intent.state == IntentState.REJECTED:
                raise ValueError("INTENT_ALREADY_TERMINAL: Intent is already rejected (single-use constraint)")

            if intent.state == IntentState.APPROVED:
                raise ValueError("INTENT_ALREADY_TERMINAL: Cannot reject an already approved intent")

            if intent.state != IntentState.PENDING_APPROVAL:
                self._audit.log({
                    "action": "INTENT_INVALID_TRANSITION",
                    "intent_id": intent_id,
                    "operator_id": rejector_id,
                    "current_state": intent.state.value,
                    "target_state": IntentState.REJECTED.value,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    f"INVALID_STATE_TRANSITION: Cannot reject intent in state {intent.state.value}"
                )

            # Two-person separation constraint
            if rejector_id == intent.creator_id:
                self._audit.log({
                    "action": "INTENT_SAME_OPERATOR_REJECTED",
                    "intent_id": intent_id,
                    "operator_id": rejector_id,
                    "creator_id": intent.creator_id,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    "SAME_OPERATOR_APPROVAL_REJECTED: Rejector must be distinct from Intent Creator"
                )

            now = datetime.now(timezone.utc)
            intent.state = IntentState.REJECTED
            intent.approver_id = rejector_id
            intent.rejection_reason = reason.strip()
            intent.updated_at = now

            self._audit.log({
                "action": "INTENT_REJECTED",
                "intent_id": intent.intent_id,
                "operator_id": rejector_id,
                "creator_id": intent.creator_id,
                "rejection_reason": intent.rejection_reason,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            return intent

    def cancel_intent(
        self,
        intent_id: str,
        canceller_id: str,
        reason: str,
        correlation_id: str,
    ) -> ExecutionIntent:
        """
        Cancels an intent prior to approval/rejection.
        Can be performed by the creator or an authorized operator.
        """
        with self._lock:
            intent = self._intents.get(intent_id)
            if not intent:
                raise ValueError("INTENT_NOT_FOUND: Execution intent does not exist")

            self._check_and_expire_unlocked(intent, correlation_id)

            if intent.state in [IntentState.APPROVED, IntentState.REJECTED, IntentState.EXPIRED, IntentState.CANCELLED]:
                self._audit.log({
                    "action": "INTENT_INVALID_TRANSITION",
                    "intent_id": intent_id,
                    "operator_id": canceller_id,
                    "current_state": intent.state.value,
                    "target_state": IntentState.CANCELLED.value,
                    "correlation_id": correlation_id,
                })
                raise ValueError(
                    f"INVALID_STATE_TRANSITION: Cannot cancel intent in terminal state {intent.state.value}"
                )

            now = datetime.now(timezone.utc)
            intent.state = IntentState.CANCELLED
            intent.cancellation_reason = reason.strip()
            intent.updated_at = now

            self._audit.log({
                "action": "INTENT_CANCELLED",
                "intent_id": intent.intent_id,
                "operator_id": canceller_id,
                "cancellation_reason": intent.cancellation_reason,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            return intent

    def get_intent(self, intent_id: str, correlation_id: str = "") -> Optional[ExecutionIntent]:
        """Retrieves intent by ID, evaluating expiry if applicable."""
        with self._lock:
            intent = self._intents.get(intent_id)
            if not intent:
                return None
            self._check_and_expire_unlocked(intent, correlation_id)
            return intent

    def list_intents(
        self,
        state_filter: Optional[IntentState] = None,
        correlation_id: str = "",
    ) -> List[ExecutionIntent]:
        """Lists all registered intents, maintaining expiry state."""
        with self._lock:
            results = []
            for intent in self._intents.values():
                self._check_and_expire_unlocked(intent, correlation_id)
                if state_filter is None or intent.state == state_filter:
                    results.append(intent)
            return sorted(results, key=lambda x: x.created_at, reverse=True)
