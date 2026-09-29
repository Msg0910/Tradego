"""
Tradego Phase 7 — Canonical Order Instruction Domain Model & Manager.

Establishes the authoritative, broker-independent boundary representation
for orders created from risk-approved execution intents.
Enforces strict provenance, field immutability, execution chain precedence,
guard-state protection, and idempotency.

INVARIANT:
An OrderInstruction is NOT an executed order.
Phase 7 establishes execution readiness without enabling uncontrolled live trading.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional
import uuid

from .broker_adapter import BrokerAdapter, BrokerDispatchResult, PaperBrokerAdapter
from .intent import ExecutionIntent, IntentState
from .recovery import GuardState, TradingGuard
from .security import Tier1AuditLogger


class InstructionState(str, Enum):
    """Authoritative OrderInstruction lifecycle states."""
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    DISPATCHED = "DISPATCHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def from_str(cls, val: Any) -> "InstructionState":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.CREATED
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.CREATED


_IMMUTABLE_FIELDS = {
    "instruction_id",
    "intent_id",
    "symbol",
    "side",
    "quantity",
    "order_type",
    "limit_price",
    "stop_price",
    "time_in_force",
    "risk_evaluation_reference",
    "approval_reference",
    "correlation_id",
    "created_at",
    "provenance",
}


class OrderInstruction:
    """
    Authoritative canonical order instruction domain model.
    Enforces immutable core attributes after initialization to prevent silent tampering.
    """

    def __init__(
        self,
        instruction_id: str,
        intent_id: str,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str,
        risk_evaluation_reference: Optional[str] = None,
        approval_reference: Optional[str] = None,
        correlation_id: str = "",
        provenance: Optional[Dict[str, Any]] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "DAY",
        created_at: Optional[datetime] = None,
        state: InstructionState = InstructionState.CREATED,
    ) -> None:
        self.__dict__["_initialized"] = False
        self.__dict__["instruction_id"] = instruction_id
        self.__dict__["intent_id"] = intent_id
        self.__dict__["symbol"] = symbol
        self.__dict__["side"] = side
        self.__dict__["quantity"] = quantity
        self.__dict__["order_type"] = order_type
        self.__dict__["limit_price"] = limit_price
        self.__dict__["stop_price"] = stop_price
        self.__dict__["time_in_force"] = time_in_force
        self.__dict__["risk_evaluation_reference"] = risk_evaluation_reference
        self.__dict__["approval_reference"] = approval_reference
        self.__dict__["correlation_id"] = correlation_id
        self.__dict__["provenance"] = dict(provenance or {})
        self.__dict__["created_at"] = created_at or datetime.now(timezone.utc)
        self.__dict__["state"] = state
        self.__dict__["broker_order_id"] = None
        self.__dict__["dispatched_at"] = None
        self.__dict__["acknowledged_at"] = None
        self.__dict__["rejection_reason"] = None
        self.__dict__["failure_reason"] = None
        self.__dict__["cancellation_reason"] = None
        self.__dict__["status_category"] = "ORDER_INSTRUCTION"
        self.__dict__["is_executed"] = False  # INVARIANT: Never claimed as executed
        self.__dict__["_initialized"] = True

    def __setattr__(self, name: str, value: Any) -> None:
        if self.__dict__.get("_initialized", False) and name in _IMMUTABLE_FIELDS:
            raise AttributeError(
                f"IMMUTABLE_FIELD_BREACH: Field '{name}' is immutable on OrderInstruction."
            )
        super().__setattr__(name, value)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes instruction to authoritative dictionary representation."""
        return {
            "instruction_id": self.instruction_id,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "time_in_force": self.time_in_force,
            "risk_evaluation_reference": self.risk_evaluation_reference,
            "approval_reference": self.approval_reference,
            "correlation_id": self.correlation_id,
            "provenance": dict(self.provenance),
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "broker_order_id": self.broker_order_id,
            "dispatched_at": self.dispatched_at,
            "acknowledged_at": self.acknowledged_at,
            "rejection_reason": self.rejection_reason,
            "failure_reason": self.failure_reason,
            "cancellation_reason": self.cancellation_reason,
            "status_category": self.status_category,
            "is_executed": self.is_executed,
        }


class OrderInstructionManager:
    """
    Authoritative manager for canonical OrderInstruction lifecycle and dispatch.
    Enforces execution chain, idempotency, guard state, and audit integrity.
    """

    VALID_SIDES = {"BUY", "SELL"}
    VALID_ORDER_TYPES = {"LIMIT", "MARKET", "STOP_LOSS", "STOP_LIMIT"}

    def __init__(
        self,
        trading_guard: TradingGuard,
        audit_logger: Tier1AuditLogger,
        broker_adapter: Optional[BrokerAdapter] = None,
        execution_manager: Optional[Any] = None,
        connectivity_manager: Optional[Any] = None,
        persistence_manager: Optional[Any] = None,
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._broker = broker_adapter or PaperBrokerAdapter()
        self._execution_mgr = execution_manager
        self._connectivity_mgr = connectivity_manager
        self._persistence_mgr = persistence_manager
        self._lock = threading.RLock()
        self._instructions: Dict[str, OrderInstruction] = {}
        self._intent_to_instruction: Dict[str, str] = {}  # intent_id -> instruction_id

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

    @property
    def execution_manager(self) -> Optional[Any]:
        return self._execution_mgr

    def set_execution_manager(self, mgr: Any) -> None:
        with self._lock:
            self._execution_mgr = mgr

    @property
    def broker_adapter(self) -> BrokerAdapter:
        return self._broker

    def set_broker_adapter(self, adapter: BrokerAdapter) -> None:
        with self._lock:
            self._broker = adapter

    def create_instruction_from_intent(
        self,
        intent: ExecutionIntent,
        operator_id: str,
        correlation_id: Optional[str] = None,
    ) -> OrderInstruction:
        """
        Transforms a RISK_APPROVED ExecutionIntent into an authoritative OrderInstruction.
        Enforces:
        - TradingGuard not HALTED
        - Execution chain: Intent must be RISK_APPROVED and risk_status == ALLOWED
        - Operational approval prerequisite: Approver must be distinct and recorded
        - Intent must not be expired
        - Idempotency: Duplicate instruction creation for same intent is strictly blocked
        - Parameter validity: quantity > 0, valid side, valid order_type, positive price for LIMIT
        """
        with self._lock:
            corr_id = correlation_id or intent.correlation_id or str(uuid.uuid4())
            now_dt = datetime.now(timezone.utc)
            now_str = now_dt.isoformat()

            # 1. Guard-state protection
            if self._guard.state == GuardState.HALTED:
                raise ValueError(
                    "GUARD_HALTED: TradingGuard is HALTED. Cannot create OrderInstruction."
                )

            # 2. Execution Chain: Operational & Risk Approval
            if intent.state != IntentState.RISK_APPROVED or intent.risk_status != "ALLOWED":
                raise ValueError(
                    f"EXECUTION_CHAIN_BREACH: ExecutionIntent {intent.intent_id} has not passed "
                    f"authoritative risk approval (current state: {intent.state.value}, risk: {intent.risk_status})."
                )

            if not intent.approver_id:
                raise ValueError(
                    f"EXECUTION_CHAIN_BREACH: Intent {intent.intent_id} lacks operational approval reference."
                )

            # 3. Expiration Check
            if intent.is_expired:
                raise ValueError(
                    f"INTENT_EXPIRED: Cannot create OrderInstruction for expired intent {intent.intent_id}."
                )

            # 4. Idempotency Check
            if intent.intent_id in self._intent_to_instruction:
                existing_id = self._intent_to_instruction[intent.intent_id]
                raise ValueError(
                    f"DUPLICATE_INSTRUCTION_PREVENTED: OrderInstruction {existing_id} already exists "
                    f"for ExecutionIntent {intent.intent_id}."
                )

            # 5. Order Parameter Validation
            clean_side = intent.side.strip().upper()
            if clean_side not in self.VALID_SIDES:
                raise ValueError(f"INVALID_ORDER_SIDE: Unsupported order side '{intent.side}'.")

            if intent.quantity <= 0:
                raise ValueError(f"INVALID_ORDER_QUANTITY: Quantity must be positive, got {intent.quantity}.")

            clean_type = intent.order_type.strip().upper()
            if clean_type not in self.VALID_ORDER_TYPES:
                raise ValueError(f"UNSUPPORTED_ORDER_TYPE: Unsupported order type '{intent.order_type}'.")

            if clean_type == "LIMIT" and (intent.limit_price is None or intent.limit_price <= 0):
                raise ValueError("INVALID_ORDER_PRICE: LIMIT orders require a positive limit_price.")

            # 6. Immutable Provenance Metadata
            provenance = {
                "intent_id": intent.intent_id,
                "creator_id": intent.creator_id,
                "approver_id": intent.approver_id,
                "risk_evaluator_id": intent.risk_evaluator_id or "RISK_GATE",
                "risk_evaluated_at": intent.risk_evaluated_at or now_str,
                "risk_decision": intent.risk_status,
                "risk_details": intent.risk_details or {},
                "instruction_creator_id": operator_id,
                "correlation_id": corr_id,
            }

            instruction_id = f"ins-{uuid.uuid4().hex[:12]}"
            instruction = OrderInstruction(
                instruction_id=instruction_id,
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                side=clean_side,
                quantity=intent.quantity,
                order_type=clean_type,
                limit_price=intent.limit_price,
                stop_price=intent.stop_price,
                time_in_force=intent.time_in_force or "DAY",
                risk_evaluation_reference=intent.risk_evaluated_at or f"RISK_EVAL_{intent.intent_id}",
                approval_reference=intent.approver_id,
                correlation_id=corr_id,
                provenance=provenance,
                created_at=now_dt,
                state=InstructionState.CREATED,
            )

            # Audit: ORDER_INSTRUCTION_CREATED
            self._audit.log({
                "action": "ORDER_INSTRUCTION_CREATED",
                "instruction_id": instruction_id,
                "intent_id": intent.intent_id,
                "operator_id": operator_id,
                "symbol": instruction.symbol,
                "side": instruction.side,
                "quantity": instruction.quantity,
                "correlation_id": corr_id,
            })

            # Transition to VALIDATED
            instruction.state = InstructionState.VALIDATED
            self._audit.log({
                "action": "ORDER_INSTRUCTION_VALIDATED",
                "instruction_id": instruction_id,
                "intent_id": intent.intent_id,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })

            self._instructions[instruction_id] = instruction
            self._intent_to_instruction[intent.intent_id] = instruction_id

            if self._persistence_mgr:
                try:
                    self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                except Exception:
                    pass

            return instruction

    # Convenience alias for test parity
    create_from_intent = create_instruction_from_intent

    def create_instruction(
        self,
        intent_id: str,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: Optional[float] = None,
        order_type: str = "LIMIT",
        correlation_id: Optional[str] = None,
        operator_id: str = "SYSTEM",
    ) -> "OrderInstruction":
        """
        Convenience builder to create an unvalidated draft OrderInstruction directly from raw parameters.
        Does NOT bypass the canonical chain: produces an instruction in CREATED state without risk approval.
        NON-DISPATCHABLE until advanced through the canonical execution path.
        """
        import uuid as _uuid
        from datetime import datetime, timezone
        corr_id = correlation_id or str(_uuid.uuid4())
        now_dt = datetime.now(timezone.utc)
        clean_side = side.strip().upper()
        clean_type = order_type.strip().upper()
        instruction_id = f"ins-{_uuid.uuid4().hex[:12]}"
        instruction = OrderInstruction(
            instruction_id=instruction_id,
            intent_id=intent_id,
            symbol=symbol,
            side=clean_side,
            quantity=quantity,
            order_type=clean_type,
            limit_price=limit_price,
            correlation_id=corr_id,
            risk_evaluation_reference=None,
            approval_reference=None,
            provenance={
                "intent_id": intent_id,
                "operator_id": operator_id,
                "correlation_id": corr_id,
                "canonical_chain": False,
                "risk_decision": None,
            },
            created_at=now_dt,
            state=InstructionState.CREATED,
        )
        self._instructions[instruction_id] = instruction
        self._intent_to_instruction[intent_id] = instruction_id
        if self._persistence_mgr:
            try:
                self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
            except Exception:
                pass
        return instruction


    def dispatch_instruction(
        self,
        instruction_id: str,
        operator_id: str = "SYSTEM",
        correlation_id: Optional[str] = None,
    ) -> OrderInstruction:
        """
        Dispatches a VALIDATED OrderInstruction through the configured BrokerAdapter.
        Enforces:
        - TradingGuard state: HALTED blocks dispatch (records failure), PAUSED blocks dispatch
        - Instruction state: must be in CREATED or VALIDATED
        - Dispatches without modifying frozen core or placing live broker orders
        """
        with self._lock:
            instruction = self._instructions.get(instruction_id)
            if not instruction:
                raise KeyError(f"OrderInstruction not found: {instruction_id}")

            corr_id = correlation_id or instruction.correlation_id
            now_str = datetime.now(timezone.utc).isoformat()

            # Mode detection
            is_live = False
            if self._connectivity_mgr:
                raw_mode = getattr(self._connectivity_mgr, "execution_mode", None)
                mode_str = raw_mode.value if hasattr(raw_mode, "value") else str(raw_mode)
                is_live = (mode_str == "LIVE")

            # Guard-state check
            if self._guard.state == GuardState.HALTED:
                instruction.state = InstructionState.FAILED
                instruction.failure_reason = "GUARD_HALTED: Dispatch blocked by TradingGuard"
                if self._persistence_mgr:
                    try:
                        self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                    except Exception:
                        pass
                self._audit.log({
                    "action": "ORDER_FAILED",
                    "instruction_id": instruction_id,
                    "reason": "GUARD_HALTED",
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })
                if is_live:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "instruction_id": instruction_id,
                        "operator_id": operator_id,
                        "reason": "GUARD_HALTED",
                        "correlation_id": corr_id,
                    })
                raise ValueError("GUARD_HALTED: TradingGuard is HALTED. Cannot dispatch instruction.")

            if self._guard.state == GuardState.PAUSED:
                if is_live:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "instruction_id": instruction_id,
                        "operator_id": operator_id,
                        "reason": "GUARD_PAUSED",
                        "correlation_id": corr_id,
                    })
                raise ValueError("GUARD_PAUSED: TradingGuard is PAUSED. Cannot dispatch instruction.")

            # In LIVE mode, enforce strict execution gates
            if is_live:
                conn_state = getattr(self._connectivity_mgr, "connectivity_state", None)
                conn_str = conn_state.value if hasattr(conn_state, "value") else str(conn_state)
                if conn_str != "CONNECTED":
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "instruction_id": instruction_id,
                        "operator_id": operator_id,
                        "reason": f"BROKER_NOT_CONNECTED_{conn_str}",
                        "correlation_id": corr_id,
                    })
                    raise ValueError(f"LIVE_DISPATCH_BLOCKED: Broker connectivity is {conn_str}. Must be CONNECTED.")

                risk_avail = getattr(self._connectivity_mgr, "is_risk_gate_available", True)
                if not risk_avail:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "instruction_id": instruction_id,
                        "operator_id": operator_id,
                        "reason": "RISK_SYSTEM_UNAVAILABLE",
                        "correlation_id": corr_id,
                    })
                    raise ValueError("LIVE_DISPATCH_BLOCKED: Risk evaluation system is unavailable.")

                if instruction.provenance.get("risk_decision") != "ALLOWED":
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "instruction_id": instruction_id,
                        "operator_id": operator_id,
                        "reason": "INSTRUCTION_NOT_RISK_APPROVED",
                        "correlation_id": corr_id,
                    })
                    raise ValueError("LIVE_DISPATCH_BLOCKED: Instruction has not received risk approval.")

                self._audit.log({
                    "action": "LIVE_ORDER_SUBMISSION_REQUESTED",
                    "instruction_id": instruction_id,
                    "operator_id": operator_id,
                    "symbol": instruction.symbol,
                    "quantity": instruction.quantity,
                    "correlation_id": corr_id,
                })

            # Instruction state check: only VALIDATED instructions produced through canonical execution chain can be dispatched
            if instruction.state != InstructionState.VALIDATED:
                raise ValueError(
                    f"INVALID_INSTRUCTION_STATE: Cannot dispatch instruction in state {instruction.state.value}. "
                    "Only VALIDATED instructions produced via canonical execution chain may be dispatched."
                )

            # Canonical risk evaluation reference verification: must be present and not fake
            if (
                not instruction.risk_evaluation_reference
                or instruction.risk_evaluation_reference == "RISK_APPROVED"
                or str(instruction.risk_evaluation_reference).strip() == ""
            ):
                raise ValueError(
                    "CANONICAL_CHAIN_BREACH: Instruction lacks authoritative risk evaluation reference or contains fake reference. "
                    "Dispatch rejected."
                )

            # Canonical operational approval reference verification
            if not instruction.approval_reference or str(instruction.approval_reference).strip() == "":
                raise ValueError(
                    "CANONICAL_CHAIN_BREACH: Instruction lacks operational approval reference. "
                    "Dispatch rejected."
                )

            # Audit: ORDER_DISPATCH_REQUESTED
            self._audit.log({
                "action": "ORDER_DISPATCH_REQUESTED",
                "instruction_id": instruction_id,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })

            instruction.state = InstructionState.DISPATCH_PENDING
            if self._persistence_mgr:
                try:
                    self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                except Exception:
                    pass

            exec_rec = None
            if self._execution_mgr:
                exec_rec = self._execution_mgr.create_execution(instruction, corr_id)

            # BrokerAdapter execution call
            result: BrokerDispatchResult = self._broker.dispatch(instruction)

            instruction.dispatched_at = now_str
            self._audit.log({
                "action": "ORDER_DISPATCHED",
                "instruction_id": instruction_id,
                "operator_id": operator_id,
                "symbol": instruction.symbol,
                "correlation_id": corr_id,
            })
            if is_live:
                self._audit.log({
                    "action": "LIVE_ORDER_SUBMITTED",
                    "instruction_id": instruction_id,
                    "operator_id": operator_id,
                    "symbol": instruction.symbol,
                    "correlation_id": corr_id,
                })

            if result.outcome == "ACKNOWLEDGED":
                instruction.state = InstructionState.ACKNOWLEDGED
                instruction.broker_order_id = result.broker_order_id
                instruction.acknowledged_at = result.timestamp
                if self._persistence_mgr:
                    try:
                        self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                    except Exception:
                        pass
                self._audit.log({
                    "action": "ORDER_ACKNOWLEDGED",
                    "instruction_id": instruction_id,
                    "broker_order_id": result.broker_order_id,
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })
                if is_live:
                    self._audit.log({
                        "action": "LIVE_ORDER_ACKNOWLEDGED",
                        "instruction_id": instruction_id,
                        "broker_order_id": result.broker_order_id,
                        "operator_id": operator_id,
                        "correlation_id": corr_id,
                    })
                if self._execution_mgr and exec_rec:
                    self._execution_mgr.apply_acknowledgement(
                        exec_rec.execution_id,
                        result.broker_order_id,
                        result.timestamp,
                        corr_id,
                    )
            elif result.outcome == "REJECTED":
                instruction.state = InstructionState.REJECTED
                instruction.rejection_reason = result.rejection_reason
                if self._persistence_mgr:
                    try:
                        self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                    except Exception:
                        pass
                self._audit.log({
                    "action": "ORDER_REJECTED",
                    "instruction_id": instruction_id,
                    "reason": result.rejection_reason,
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })
                if is_live:
                    self._audit.log({
                        "action": "LIVE_ORDER_REJECTED",
                        "instruction_id": instruction_id,
                        "reason": result.rejection_reason,
                        "operator_id": operator_id,
                        "correlation_id": corr_id,
                    })
                if self._execution_mgr and exec_rec:
                    self._execution_mgr.apply_rejection(
                        exec_rec.execution_id,
                        result.rejection_reason or "BROKER_REJECTED",
                        corr_id,
                    )
            else:  # FAILED
                instruction.state = InstructionState.FAILED
                instruction.failure_reason = result.failure_reason
                if self._persistence_mgr:
                    try:
                        self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                    except Exception:
                        pass
                self._audit.log({
                    "action": "ORDER_FAILED",
                    "instruction_id": instruction_id,
                    "reason": result.failure_reason,
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })
                if is_live:
                    self._audit.log({
                        "action": "LIVE_ORDER_FAILED",
                        "instruction_id": instruction_id,
                        "reason": result.failure_reason,
                        "operator_id": operator_id,
                        "correlation_id": corr_id,
                    })
                if self._execution_mgr and exec_rec:
                    self._execution_mgr.apply_failure(
                        exec_rec.execution_id,
                        result.failure_reason or "DISPATCH_FAILED",
                        corr_id,
                    )

            return instruction

    def cancel_instruction(
        self,
        instruction_id: str,
        operator_id: str,
        reason: str = "OPERATOR_CANCELLED",
        correlation_id: Optional[str] = None,
    ) -> OrderInstruction:
        """
        Requests cancellation of an OrderInstruction and informs BrokerAdapter if acknowledged.
        """
        with self._lock:
            instruction = self._instructions.get(instruction_id)
            if not instruction:
                raise KeyError(f"OrderInstruction not found: {instruction_id}")

            corr_id = correlation_id or instruction.correlation_id

            if instruction.state in [InstructionState.CANCELLED, InstructionState.REJECTED, InstructionState.FAILED]:
                raise ValueError(
                    f"TERMINAL_STATE: Cannot cancel instruction in terminal state {instruction.state.value}."
                )

            is_live = False
            if self._connectivity_mgr:
                raw_mode = getattr(self._connectivity_mgr, "execution_mode", None)
                mode_str = raw_mode.value if hasattr(raw_mode, "value") else str(raw_mode)
                is_live = (mode_str == "LIVE")

            if is_live:
                self._audit.log({
                    "action": "LIVE_ORDER_CANCEL_REQUESTED",
                    "instruction_id": instruction_id,
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })

            if instruction.state == InstructionState.ACKNOWLEDGED:
                self._broker.cancel(instruction, reason)

            instruction.state = InstructionState.CANCELLED
            instruction.cancellation_reason = reason
            if self._persistence_mgr:
                try:
                    self._persistence_mgr.append_instruction(instruction, correlation_id=corr_id)
                except Exception:
                    pass

            self._audit.log({
                "action": "ORDER_CANCELLED",
                "instruction_id": instruction_id,
                "reason": reason,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })
            if is_live:
                self._audit.log({
                    "action": "LIVE_ORDER_CANCELLED",
                    "instruction_id": instruction_id,
                    "reason": reason,
                    "operator_id": operator_id,
                    "correlation_id": corr_id,
                })


            if self._execution_mgr:
                exec_rec = self._execution_mgr.get_by_instruction(instruction_id)
                if exec_rec:
                    self._execution_mgr.apply_cancellation(exec_rec.execution_id, reason, corr_id)

            return instruction

    def get_instruction(self, instruction_id: str) -> Optional[OrderInstruction]:
        with self._lock:
            return self._instructions.get(instruction_id)

    def get_by_intent(self, intent_id: str) -> Optional[OrderInstruction]:
        with self._lock:
            ins_id = self._intent_to_instruction.get(intent_id)
            return self._instructions.get(ins_id) if ins_id else None

    def list_instructions(self) -> List[OrderInstruction]:
        with self._lock:
            return list(self._instructions.values())
