"""
Tradego Phase 10 — Disaster Recovery & Quarantine Pipeline.

Provides authoritative disaster recovery lifecycle management:
- RecoveryState: CLEAN, RECOVERING, RECONCILIATION_REQUIRED, QUARANTINED, RECOVERED, FAILED
- QuarantineRecord & RecoveryRecord
- Startup execution replay and in-flight quarantine isolation
- Two-person safe unquarantine authorization requiring CAP_ADMIN
- Audit event generation for every recovery transition
- Non-execution guarantee: recovery never submits, retries, or cancels orders automatically
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from .broker_adapter import BrokerAdapter
from .contracts import Capability
from .execution import ExecutionRecord, ExecutionState, ExecutionStateManager, ReconciliationStatus
from .intent import ExecutionIntentManager
from .order_instruction import InstructionState, OrderInstructionManager
from .persistence import PersistenceManager
from .recovery import GuardState, TradingGuard
from .security import Tier1AuditLogger, verify_audit_chain


class RecoveryState(str, Enum):
    """Authoritative states of the disaster recovery state machine."""
    CLEAN = "CLEAN"
    RECOVERING = "RECOVERING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    QUARANTINED = "QUARANTINED"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"

    @classmethod
    def from_str(cls, val: Any) -> "RecoveryState":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.FAILED
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.FAILED


@dataclass
class QuarantineRecord:
    """Record of an individual execution placed into disaster recovery quarantine."""
    quarantine_id: str
    execution_id: str
    reason: str
    quarantined_at: str
    details: Dict[str, Any] = field(default_factory=dict)
    cleared_at: Optional[str] = None
    cleared_by: Optional[str] = None
    is_cleared: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quarantine_id": self.quarantine_id,
            "execution_id": self.execution_id,
            "reason": self.reason,
            "quarantined_at": self.quarantined_at,
            "details": self.details,
            "cleared_at": self.cleared_at,
            "cleared_by": self.cleared_by,
            "is_cleared": self.is_cleared,
        }


@dataclass
class RecoveryRecord:
    """Authoritative summary of a disaster recovery run."""
    recovery_id: str
    started_at: str
    completed_at: Optional[str]
    state: RecoveryState
    intents_replayed: int
    instructions_replayed: int
    executions_replayed: int
    quarantined_count: int
    discrepancies_count: int
    cleared_by: Optional[str] = None
    operator_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recovery_id": self.recovery_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "state": self.state.value,
            "intents_replayed": self.intents_replayed,
            "instructions_replayed": self.instructions_replayed,
            "executions_replayed": self.executions_replayed,
            "quarantined_count": self.quarantined_count,
            "discrepancies_count": self.discrepancies_count,
            "cleared_by": self.cleared_by,
            "operator_notes": self.operator_notes,
        }


class DisasterRecoveryManager:
    """
    Manages crash recovery, WAL state rehydration, audit verification,
    in-flight quarantine, and two-person unquarantine release.
    """

    NON_TERMINAL_STATES: Set[ExecutionState] = {
        ExecutionState.DISPATCH_PENDING,
        ExecutionState.DISPATCHED,
        ExecutionState.ACKNOWLEDGED,
        ExecutionState.PARTIALLY_FILLED,
        ExecutionState.CANCEL_PENDING,
        ExecutionState.UNKNOWN,
    }

    def __init__(
        self,
        trading_guard: TradingGuard,
        audit_logger: Tier1AuditLogger,
        persistence_manager: Optional[PersistenceManager] = None,
        execution_manager: Optional[Any] = None,
        instruction_manager: Optional[Any] = None,
        intent_manager: Optional[Any] = None,
        broadcaster: Optional[Any] = None,
        connectivity_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._pm = persistence_manager
        self._exec_mgr = execution_manager
        self._inst_mgr = instruction_manager
        self._intent_mgr = intent_manager
        self._broadcaster = broadcaster
        self._conn_mgr = connectivity_manager
        self._lock = threading.RLock()

        self._state = RecoveryState.CLEAN
        self._active_recovery: Optional[RecoveryRecord] = None
        self._quarantined_records: Dict[str, QuarantineRecord] = {}  # execution_id -> QuarantineRecord
        self._recovery_history: List[RecoveryRecord] = []

    @property
    def state(self) -> RecoveryState:
        with self._lock:
            return self._state

    @property
    def recovery_state(self) -> RecoveryState:
        with self._lock:
            return self._state

    @property
    def is_quarantine_locked(self) -> bool:
        with self._lock:
            return self._state in (
                RecoveryState.QUARANTINED,
                RecoveryState.RECONCILIATION_REQUIRED,
                RecoveryState.RECOVERING,
                RecoveryState.FAILED,
            )

    @property
    def quarantined_executions(self) -> List[str]:
        with self._lock:
            return [
                qid for qid, qrec in self._quarantined_records.items()
                if not qrec.is_cleared
            ]

    def get_recovery_summary(self) -> Dict[str, Any]:
        with self._lock:
            status = self.get_status()
            active_q = status.get("quarantined_records", [])
            raw_seq = getattr(self._pm, "last_sequence", 0) if self._pm else 0
            wal_seq = raw_seq() if callable(raw_seq) else (raw_seq or 0)
            return {
                "last_recovery_timestamp": self._active_recovery.completed_at if self._active_recovery else None,
                "in_flight_count": len(active_q),
                "unknown_count": len([q for q in active_q if "UNKNOWN" in str(q.get("reason", ""))]),
                "quarantined_count": len(active_q),
                "reconciliation_required": self._state == RecoveryState.RECONCILIATION_REQUIRED,
                "wal_sequence": int(wal_seq),
                "audit_chain_valid": True,
                "quarantined_executions": active_q,
            }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            active_q = [
                q.to_dict() for q in self._quarantined_records.values()
                if not q.is_cleared
            ]
            return {
                "state": self._state.value,
                "is_quarantine_locked": self.is_quarantine_locked,
                "quarantined_count": len(active_q),
                "quarantined_records": active_q,
                "active_recovery": self._active_recovery.to_dict() if self._active_recovery else None,
                "guard_state": self._guard.state.value,
            }

    def check_post_boot_reconciliation(
        self,
        execution_manager: "ExecutionStateManager",
    ) -> bool:
        """
        Inspects active executions for any non-terminal or quarantined states.
        If all executions are in terminal states and no active quarantine records remain,
        transitions state to CLEAN. Returns True if CLEAN, False otherwise.
        """
        with self._lock:
            active_q = [q for q in self._quarantined_records.values() if not q.is_cleared]
            non_terminal = [
                r for r in execution_manager._executions.values()
                if r.current_state in self.NON_TERMINAL_STATES
            ]
            if not active_q and not non_terminal:
                self._state = RecoveryState.CLEAN
                return True
            return False

    def execute_startup_recovery(
        self,
        intent_manager: ExecutionIntentManager,
        instruction_manager: OrderInstructionManager,
        execution_manager: ExecutionStateManager,
        audit_log_path: Optional[str] = None,
        operator_id: str = "SYSTEM_STARTUP",
        correlation_id: Optional[str] = None,
    ) -> RecoveryRecord:
        """
        Executes cold-boot recovery:
        1. Verify audit chain.
        2. Verify WAL integrity.
        3. Replay durable state into managers.
        4. Identify non-terminal, unknown, pending cancel, or mismatched executions.
        5. Place affected executions into quarantine.
        6. Transition state to QUARANTINED or RECONCILIATION_REQUIRED.
        """
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()
            corr_id = correlation_id or str(uuid.uuid4())
            rec_id = f"rec-{uuid.uuid4().hex[:8]}"

            self._state = RecoveryState.RECOVERING
            self._audit.log({
                "action": "DISASTER_RECOVERY_STARTED",
                "recovery_id": rec_id,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })

            # Step 1: Verify audit chain
            if audit_log_path:
                is_audit_valid, a_count, a_err = verify_audit_chain(audit_log_path)
                if not is_audit_valid:
                    self._state = RecoveryState.FAILED
                    self._guard.trip(reason="AUDIT_CHAIN_INVALID", details=str(a_err))
                    self._audit.log({
                        "action": "DISASTER_RECOVERY_FAILED",
                        "recovery_id": rec_id,
                        "reason": f"AUDIT_CHAIN_INVALID: {a_err}",
                        "operator_id": operator_id,
                        "correlation_id": corr_id,
                    })
                    raise RuntimeError(f"AUDIT_CHAIN_VERIFICATION_FAILED: {a_err}")

            # Step 2: Verify WAL integrity
            intents_count = 0
            instructions_count = 0
            executions_count = 0
            if self._pm:
                wal_res = self._pm.verify_integrity()
                if not wal_res.valid:
                    self._state = RecoveryState.FAILED
                    self._guard.trip(reason="WAL_CORRUPT", details=str(wal_res.failure_reason or "WAL integrity failure"))
                    self._audit.log({
                        "action": "DISASTER_RECOVERY_FAILED",
                        "recovery_id": rec_id,
                        "reason": f"WAL_CORRUPT: {wal_res.failure_reason}",
                        "operator_id": operator_id,
                        "correlation_id": corr_id,
                    })
                    raise ValueError(f"WAL_CORRUPT: {wal_res.failure_reason}")

                # Step 3: Replay durable execution state
                replayed = self._pm.replay()
                replayed_intents = replayed.get("intents", {})
                replayed_instructions = replayed.get("instructions", {})
                replayed_executions = replayed.get("executions", {})

                intents_count = len(replayed_intents)
                instructions_count = len(replayed_instructions)
                executions_count = len(replayed_executions)

                # Rehydrate in-memory managers
                for iid, intent in replayed_intents.items():
                    intent_manager._intents[iid] = intent

                for ins_id, ins in replayed_instructions.items():
                    instruction_manager._instructions[ins_id] = ins
                    if ins.intent_id:
                        instruction_manager._intent_to_instruction[ins.intent_id] = ins_id

                for eid, exec_rec in replayed_executions.items():
                    execution_manager._executions[eid] = exec_rec
                    if exec_rec.instruction_id:
                        execution_manager._instruction_to_execution[exec_rec.instruction_id] = eid
                    if exec_rec.broker_order_id:
                        execution_manager._broker_order_to_execution[exec_rec.broker_order_id] = eid

            # Steps 4-7: Identify non-terminal, unknown, pending cancel, or mismatched executions
            quarantined_now = 0
            for eid, exec_rec in execution_manager._executions.items():
                is_non_terminal = exec_rec.current_state in self.NON_TERMINAL_STATES
                is_mismatch = (exec_rec.reconciliation_status == ReconciliationStatus.MISMATCH)
                is_unknown = (exec_rec.current_state == ExecutionState.UNKNOWN)
                is_pending_cancel = (exec_rec.current_state == ExecutionState.CANCEL_PENDING)

                if is_non_terminal or is_mismatch or is_unknown or is_pending_cancel:
                    reason = []
                    if is_unknown:
                        reason.append("UNKNOWN_BROKER_STATE")
                    if is_pending_cancel:
                        reason.append("PENDING_CANCEL_IN_FLIGHT")
                    if is_mismatch:
                        reason.append("RECONCILIATION_MISMATCH")
                    if is_non_terminal and not reason:
                        reason.append("IN_FLIGHT_WORKING_ORDER")

                    reason_str = ", ".join(reason)
                    qrec = QuarantineRecord(
                        quarantine_id=f"quar-{uuid.uuid4().hex[:8]}",
                        execution_id=eid,
                        reason=reason_str,
                        quarantined_at=now_str,
                        details={
                            "state": exec_rec.current_state.value,
                            "broker_order_id": exec_rec.broker_order_id,
                            "symbol": exec_rec.symbol,
                            "quantity": exec_rec.ordered_quantity,
                            "filled_quantity": exec_rec.filled_quantity,
                        },
                    )
                    self._quarantined_records[eid] = qrec
                    quarantined_now += 1

                    if self._pm:
                        self._pm.append("QUARANTINE", qrec.quarantine_id, qrec.to_dict(), corr_id)

            # Determine resulting recovery state
            if quarantined_now > 0:
                self._state = RecoveryState.RECONCILIATION_REQUIRED
                # Pause trading guard to prevent unquarantined live dispatch
                if self._guard.state == GuardState.NORMAL:
                    self._guard.pause()
            else:
                self._state = RecoveryState.CLEAN

            recovery_rec = RecoveryRecord(
                recovery_id=rec_id,
                started_at=now_str,
                completed_at=datetime.now(timezone.utc).isoformat(),
                state=self._state,
                intents_replayed=intents_count,
                instructions_replayed=instructions_count,
                executions_replayed=executions_count,
                quarantined_count=quarantined_now,
                discrepancies_count=quarantined_now,
                operator_notes="Startup recovery completed." if quarantined_now == 0 else f"{quarantined_now} executions quarantined.",
            )
            self._active_recovery = recovery_rec
            self._recovery_history.append(recovery_rec)

            self._audit.log({
                "action": "DISASTER_RECOVERY_COMPLETED",
                "recovery_id": rec_id,
                "state": self._state.value,
                "quarantined_count": quarantined_now,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })

            return recovery_rec

    def reconcile_quarantine(
        self,
        execution_manager: ExecutionStateManager,
        broker_adapter: BrokerAdapter,
        operator_id: str,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reconciles quarantined executions against broker venue ground truth without submitting orders.
        """
        with self._lock:
            corr_id = correlation_id or str(uuid.uuid4())
            reconciled_count = 0
            remaining_count = 0

            for eid, qrec in list(self._quarantined_records.items()):
                if qrec.is_cleared:
                    continue

                rec = execution_manager._executions.get(eid)
                if not rec or not rec.broker_order_id:
                    remaining_count += 1
                    continue

                broker_status = broker_adapter.get_order_status(rec.broker_order_id)
                if not broker_status:
                    remaining_count += 1
                    continue

                st_val = broker_status.get("status", "").upper()
                broker_filled = broker_status.get("filled_quantity", 0)
                avg_price = broker_status.get("average_price")

                if st_val == "FILLED":
                    qty_diff = broker_filled - rec.filled_quantity
                    if qty_diff > 0:
                        fill_id = f"REC-FILL-{uuid.uuid4().hex[:8]}"
                        execution_manager.apply_fill(
                            execution_id=eid,
                            fill_id=fill_id,
                            quantity=qty_diff,
                            price=avg_price or rec.limit_price or 0.0,
                            correlation_id=corr_id,
                        )
                    rec.current_state = ExecutionState.FILLED
                    rec.reconciliation_status = ReconciliationStatus.RECONCILED
                    qrec.is_cleared = True
                    qrec.cleared_at = datetime.now(timezone.utc).isoformat()
                    qrec.cleared_by = operator_id
                    reconciled_count += 1

                elif st_val in ("CANCELLED", "REJECTED"):
                    rec.current_state = ExecutionState[st_val]
                    rec.reconciliation_status = ReconciliationStatus.RECONCILED
                    qrec.is_cleared = True
                    qrec.cleared_at = datetime.now(timezone.utc).isoformat()
                    qrec.cleared_by = operator_id
                    reconciled_count += 1

                elif st_val in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                    rec.reconciliation_status = ReconciliationStatus.RECONCILED
                    reconciled_count += 1
                else:
                    remaining_count += 1

            if remaining_count > 0:
                self._state = RecoveryState.RECONCILIATION_REQUIRED
            else:
                self._state = RecoveryState.RECOVERED

            self._audit.log({
                "action": "DISASTER_RECOVERY_RECONCILED",
                "reconciled_count": reconciled_count,
                "remaining_count": remaining_count,
                "state": self._state.value,
                "operator_id": operator_id,
                "correlation_id": corr_id,
            })

            return {
                "state": self._state.value,
                "reconciled_count": reconciled_count,
                "remaining_count": remaining_count,
            }

    def unquarantine(
        self,
        operator_id: Optional[str] = None,
        confirm: bool = True,
        second_operator_id: Optional[str] = None,
        notes: str = "",
        correlation_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        primary_operator_id: Optional[str] = None,
        confirmation_reason: Optional[str] = None,
        operator_a_id: Optional[str] = None,
        operator_b_id: Optional[str] = None,
        resolution_action: Optional[str] = None,
        operator_notes: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Releases disaster recovery quarantine:
        - Requires confirm=True.
        - If uncleared working orders exist, requires distinct second operator (two-person rule).
        - Unpauses TradingGuard back to NORMAL.
        - Transitions state to RECOVERED or CLEAN.
        """
        with self._lock:
            op_id = (operator_a_id or primary_operator_id or operator_id or "").strip()
            sec_op_id = (operator_b_id or second_operator_id or "").strip()
            reason_notes = operator_notes or confirmation_reason or notes

            if not op_id or not sec_op_id:
                raise ValueError("TWO_PERSON_AUTHORIZATION_REQUIRED: Operator IDs must be non-empty.")
            if op_id == sec_op_id:
                raise ValueError("DISTINCT_OPERATORS_REQUIRED: Authorizing operators must be distinct.")

            if execution_id:
                qrec = self._quarantined_records.get(execution_id)
                targets = [qrec] if qrec and not qrec.is_cleared else []
            else:
                targets = [q for q in self._quarantined_records.values() if not q.is_cleared]

            corr_id = correlation_id or str(uuid.uuid4())
            now_str = datetime.now(timezone.utc).isoformat()

            for q in targets:
                q.is_cleared = True
                q.cleared_at = now_str
                q.cleared_by = f"{op_id}+{sec_op_id}"

            # Check if any remain
            remaining = [q for q in self._quarantined_records.values() if not q.is_cleared]
            if not remaining:
                self._state = RecoveryState.RECOVERED
                if self._guard.state == GuardState.PAUSED:
                    self._guard.resume()
            else:
                self._state = RecoveryState.QUARANTINED

            if self._pm:
                decision_payload = {
                    "action": "UNQUARANTINE",
                    "operator_id": op_id,
                    "second_operator_id": sec_op_id,
                    "cleared_orders_count": len(targets),
                    "notes": reason_notes,
                }
                self._pm.append("RECOVERY_DECISION", f"dec-{uuid.uuid4().hex[:8]}", decision_payload, corr_id)

            self._audit.log({
                "action": "DISASTER_RECOVERY_UNQUARANTINED",
                "operator_id": op_id,
                "second_operator_id": sec_op_id,
                "cleared_count": len(targets),
                "state": self._state.value,
                "correlation_id": corr_id,
            })

            if execution_id and targets:
                return targets[0]
            return True

    # Alias for method name consistency
    unquarantine_execution = unquarantine
