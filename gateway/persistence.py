"""
Tradego Phase 10 — Durable Persistence Layer.

Provides append-only, crash-consistent Write-Ahead Log (WAL) storage:
- DurableEvent: Immutable, versioned, hash-chained record envelope.
- WALWriter: Atomic append with thread-safe os.fsync() guarantees.
- WALReader: Tolerant sequential parser handling restarts and partial trailing lines.
- WALIntegrityResult: Structured verification outcome.
- PersistenceManager: Authoritative coordinator for WAL append, read_all, replay,
  and integrity verification.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

GENESIS_HASH: str = "0000000000000000000000000000000000000000000000000000000000000000"


@dataclass(frozen=True)
class DurableEvent:
    """
    Standardized, immutable, hash-chained entry in the durable execution WAL.
    """
    seq: int
    event_id: str
    timestamp: str
    record_type: str
    record_id: str
    correlation_id: str
    payload: Dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "record_type": self.record_type,
            "record_id": self.record_id,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DurableEvent":
        return cls(
            seq=int(data["seq"]),
            event_id=str(data["event_id"]),
            timestamp=str(data["timestamp"]),
            record_type=str(data["record_type"]),
            record_id=str(data["record_id"]),
            correlation_id=str(data.get("correlation_id", "")),
            payload=dict(data.get("payload", {})),
            prev_hash=str(data.get("prev_hash", GENESIS_HASH)),
            hash=str(data["hash"]),
        )


@dataclass(frozen=True)
class WALIntegrityResult:
    """Outcome of WAL hash chain and sequence verification."""
    valid: bool
    records_checked: int
    last_sequence: int
    last_hash: str
    failure_reason: Optional[str] = None
    first_invalid_sequence: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "records_checked": self.records_checked,
            "last_sequence": self.last_sequence,
            "last_hash": self.last_hash,
            "failure_reason": self.failure_reason,
            "first_invalid_sequence": self.first_invalid_sequence,
        }


class WALReader:
    """
    Parses on-disk WAL records.
    Tolerates normal restarts, repeated recovery, and incomplete trailing lines from abrupt crashes.
    """

    def __init__(self, wal_path: str) -> None:
        self._wal_path = wal_path

    def read_all(
        self,
        tolerate_trailing_partial: bool = True,
        tolerate_corrupted: bool = False,
    ) -> List[DurableEvent]:
        """
        Reads all valid events from the WAL file.
        If tolerate_trailing_partial is True and the last line is truncated/partial,
        it is ignored without failing the clean preceding entries.
        If tolerate_corrupted is True, corrupted lines anywhere in the file are skipped.
        """
        if not os.path.exists(self._wal_path):
            return []

        events: List[DurableEvent] = []
        with open(self._wal_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for idx, line in enumerate(lines):
            raw = line.strip()
            if not raw:
                continue

            is_last_line = (idx == len(lines) - 1)
            try:
                data = json.loads(raw)
                event = DurableEvent.from_dict(data)
                events.append(event)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                if is_last_line and tolerate_trailing_partial:
                    # Trailing incomplete write from sudden crash is safely ignored
                    break
                if tolerate_corrupted:
                    continue
                raise ValueError(f"WAL_PARSE_ERROR at line {idx + 1}: {exc}")

        return events


class WALWriter:
    """
    Appends durable events with sequential monotonic numbering, SHA-256 hash chaining,
    and atomic os.fsync() persistence.
    """

    def __init__(self, wal_path: str, auto_sync: bool = True) -> None:
        self._wal_path = wal_path
        self._auto_sync = auto_sync
        self._lock = threading.RLock()
        self._file = None
        self._last_seq = 0
        self._last_hash = GENESIS_HASH

        dir_name = os.path.dirname(os.path.abspath(self._wal_path))
        os.makedirs(dir_name, exist_ok=True)

        # Initialize sequence and hash from existing file if present
        self._initialize_from_existing()
        self._file = open(self._wal_path, "a", encoding="utf-8")

    def _initialize_from_existing(self) -> None:
        if os.path.exists(self._wal_path) and os.path.getsize(self._wal_path) > 0:
            reader = WALReader(self._wal_path)
            events = reader.read_all(tolerate_trailing_partial=True, tolerate_corrupted=True)
            if events:
                self._last_seq = events[-1].seq
                self._last_hash = events[-1].hash

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._last_seq

    @property
    def last_hash(self) -> str:
        with self._lock:
            return self._last_hash

    def append(
        self,
        record_type: str,
        record_id: str,
        payload: Dict[str, Any],
        correlation_id: str = "",
        event_id: Optional[str] = None,
    ) -> DurableEvent:
        """
        Atomically appends an event to disk and calls fsync().
        """
        import uuid
        with self._lock:
            self._last_seq += 1
            now_str = datetime.now(timezone.utc).isoformat()
            eid = event_id or f"wal-{uuid.uuid4().hex[:12]}"

            entry_dict = {
                "seq": self._last_seq,
                "event_id": eid,
                "timestamp": now_str,
                "record_type": record_type,
                "record_id": record_id,
                "correlation_id": correlation_id,
                "payload": payload,
                "prev_hash": self._last_hash,
            }

            serialized = json.dumps(entry_dict, sort_keys=True)
            curr_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            entry_dict["hash"] = curr_hash
            self._last_hash = curr_hash

            event = DurableEvent.from_dict(entry_dict)

            if self._file and not self._file.closed:
                self._file.write(event.to_json() + "\n")
                if self._auto_sync:
                    self._file.flush()
                    try:
                        os.fsync(self._file.fileno())
                    except OSError:
                        pass

            return event

    def close(self) -> None:
        with self._lock:
            if self._file and not self._file.closed:
                self._file.flush()
                try:
                    os.fsync(self._file.fileno())
                except OSError:
                    pass
                self._file.close()
                self._file = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class PersistenceManager:
    """
    Authoritative coordinator for the durable append-only execution WAL.
    Enforces atomic disk commits, integrity verification, and crash state rehydration.
    """

    def __init__(
        self,
        wal_path: str = "data/execution_wal.jsonl",
        auto_sync: bool = True,
    ) -> None:
        self._wal_path = wal_path
        self._auto_sync = auto_sync
        self._lock = threading.RLock()
        self._writer = WALWriter(wal_path=self._wal_path, auto_sync=self._auto_sync)
        self._reader = WALReader(wal_path=self._wal_path)

    @property
    def wal_path(self) -> str:
        return self._wal_path

    @property
    def last_sequence(self) -> int:
        return self._writer.last_sequence

    @property
    def last_hash(self) -> str:
        return self._writer.last_hash

    def append(
        self,
        record_type: str,
        record_id: str,
        payload: Dict[str, Any],
        correlation_id: str = "",
        event_id: Optional[str] = None,
    ) -> DurableEvent:
        """Appends a durable record to the WAL."""
        with self._lock:
            return self._writer.append(
                record_type=record_type,
                record_id=record_id,
                payload=payload,
                correlation_id=correlation_id,
                event_id=event_id,
            )

    def read_all(
        self,
        tolerate_trailing_partial: bool = True,
        tolerate_corrupted: bool = False,
    ) -> List[DurableEvent]:
        """Reads all events from the WAL."""
        with self._lock:
            return self._reader.read_all(
                tolerate_trailing_partial=tolerate_trailing_partial,
                tolerate_corrupted=tolerate_corrupted,
            )

    def verify_integrity(self) -> WALIntegrityResult:
        """
        Traverses the WAL from beginning to end:
        - Confirms monotonic sequence continuity (seq == prev_seq + 1)
        - Confirms prev_hash matches the previous event's hash
        - Confirms hash matches SHA-256 recalculation of sorted entry contents
        """
        with self._lock:
            if not os.path.exists(self._wal_path):
                return WALIntegrityResult(
                    valid=True,
                    records_checked=0,
                    last_sequence=0,
                    last_hash=GENESIS_HASH,
                )

            events = self._reader.read_all(tolerate_trailing_partial=False)
            expected_prev = GENESIS_HASH
            expected_seq = 1

            for ev in events:
                # 1. Monotonic sequence check
                if ev.seq != expected_seq:
                    return WALIntegrityResult(
                        valid=False,
                        records_checked=expected_seq - 1,
                        last_sequence=ev.seq,
                        last_hash=ev.hash,
                        failure_reason=f"Sequence discontinuity: expected {expected_seq}, got {ev.seq}",
                        first_invalid_sequence=ev.seq,
                    )

                # 2. Previous hash chain check
                if ev.prev_hash != expected_prev:
                    return WALIntegrityResult(
                        valid=False,
                        records_checked=expected_seq - 1,
                        last_sequence=ev.seq,
                        last_hash=ev.hash,
                        failure_reason=f"Hash chain broken at seq {ev.seq}: expected prev_hash {expected_prev[:12]}..., got {ev.prev_hash[:12]}...",
                        first_invalid_sequence=ev.seq,
                    )

                # 3. Hash integrity recalculation
                entry_copy = {
                    "seq": ev.seq,
                    "event_id": ev.event_id,
                    "timestamp": ev.timestamp,
                    "record_type": ev.record_type,
                    "record_id": ev.record_id,
                    "correlation_id": ev.correlation_id,
                    "payload": ev.payload,
                    "prev_hash": ev.prev_hash,
                }
                serialized = json.dumps(entry_copy, sort_keys=True)
                calc_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

                if ev.hash != calc_hash:
                    return WALIntegrityResult(
                        valid=False,
                        records_checked=expected_seq - 1,
                        last_sequence=ev.seq,
                        last_hash=ev.hash,
                        failure_reason=f"Checksum mismatch at seq {ev.seq}: stored {ev.hash[:12]}..., calculated {calc_hash[:12]}...",
                        first_invalid_sequence=ev.seq,
                    )

                expected_prev = ev.hash
                expected_seq += 1

            return WALIntegrityResult(
                valid=True,
                records_checked=len(events),
                last_sequence=events[-1].seq if events else 0,
                last_hash=events[-1].hash if events else GENESIS_HASH,
            )

    def replay(self, tolerate_corrupted: bool = False) -> Dict[str, Any]:
        """
        Reconstructs in-memory domain models from the durable append-only WAL.

        State Reconstruction Model (M-02 Specification):
        - Last-Write-Wins (LWW) for Entity Snapshots:
          For point-in-time domain models ('INTENT', 'INSTRUCTION', 'EXECUTION'),
          each state transition emits a full snapshot record. In accordance with
          sequential WAL log order, later records overwrite earlier records in
          the dictionary representation, ensuring the rehydrated state matches
          the latest authoritative state of each entity prior to shutdown.
        - Event Deduplication for Fills:
          'FILL' records represent atomic financial executions. During replay,
          fills are strictly deduplicated by unique fill_id to prevent double-counting
          in the event of duplicate replay passes or duplicate journal entries.
        - Append-Only History:
          'QUARANTINE', 'RECONCILIATION', and 'RECOVERY_DECISION' records are
          preserved as immutable audit history lists.

        Returns:
            {
                "intents": Dict[str, ExecutionIntent],
                "instructions": Dict[str, OrderInstruction],
                "executions": Dict[str, ExecutionRecord],
                "fills": List[FillRecord],
                "quarantines": List[Dict[str, Any]],
                "reconciliations": List[Dict[str, Any]],
                "recovery_decisions": List[Dict[str, Any]],
            }
        """
        from .execution import ExecutionRecord, ExecutionState, FillRecord, ReconciliationStatus
        from .intent import ExecutionIntent, IntentState
        from .order_instruction import InstructionState, OrderInstruction

        with self._lock:
            events = self.read_all(tolerate_trailing_partial=True, tolerate_corrupted=tolerate_corrupted)

            intents: Dict[str, ExecutionIntent] = {}
            instructions: Dict[str, OrderInstruction] = {}
            executions: Dict[str, ExecutionRecord] = {}
            fills_by_exec: Dict[str, List[FillRecord]] = {}
            seen_fill_ids: Set[str] = set()
            quarantines: List[Dict[str, Any]] = []
            reconciliations: List[Dict[str, Any]] = []
            recovery_decisions: List[Dict[str, Any]] = []

            for ev in events:
                p = ev.payload
                rtype = ev.record_type

                if rtype == "INTENT":
                    iid = ev.record_id
                    raw_state = p.get("state", "DRAFT")
                    try:
                        intent_state = IntentState.from_str(raw_state)
                    except ValueError as exc:
                        intents.pop(iid, None)
                        quarantines.append({
                            "record_type": "INTENT",
                            "record_id": iid,
                            "reason": f"CORRUPTED_INTENT_STATE: {exc}",
                            "raw_state": raw_state,
                            "event_id": ev.event_id,
                            "seq": ev.seq,
                        })
                        continue

                    c_dt = datetime.fromisoformat(p["created_at"]) if p.get("created_at") else datetime.now(timezone.utc)
                    u_dt = datetime.fromisoformat(p["updated_at"]) if p.get("updated_at") else c_dt
                    e_dt = datetime.fromisoformat(p["expires_at"]) if p.get("expires_at") else None

                    intent = ExecutionIntent(
                        intent_id=iid,
                        creator_id=p.get("creator_id", "UNKNOWN"),
                        symbol=p.get("symbol", "UNKNOWN"),
                        side=p.get("side", "BUY"),
                        quantity=p.get("quantity", 1),
                        order_type=p.get("order_type", "LIMIT"),
                        limit_price=p.get("limit_price"),
                        stop_price=p.get("stop_price"),
                        time_in_force=p.get("time_in_force", "DAY"),
                        correlation_id=ev.correlation_id or p.get("correlation_id", ""),
                        created_at=c_dt,
                        updated_at=u_dt,
                        expires_at=e_dt,
                        state=intent_state,
                        approver_id=p.get("approver_id"),
                        approved_at=p.get("approved_at"),
                        rejection_reason=p.get("rejection_reason"),
                        risk_status=p.get("risk_status", "UNAVAILABLE"),
                        risk_evaluator_id=p.get("risk_evaluator_id"),
                        risk_evaluated_at=p.get("risk_evaluated_at"),
                        risk_rejection_reason=p.get("risk_rejection_reason"),
                        risk_details=p.get("risk_details") or {},
                    )
                    intents[iid] = intent

                elif rtype == "INSTRUCTION":
                    ins_id = ev.record_id
                    c_dt = datetime.fromisoformat(p["created_at"]) if p.get("created_at") else datetime.now(timezone.utc)

                    state_val = p.get("state", "CREATED")
                    ins_state = InstructionState.from_str(state_val) if hasattr(InstructionState, "from_str") else InstructionState(state_val)

                    ins = OrderInstruction(
                        instruction_id=ins_id,
                        intent_id=p.get("intent_id", ""),
                        symbol=p.get("symbol", "UNKNOWN"),
                        side=p.get("side", "BUY"),
                        quantity=p.get("quantity", 1),
                        order_type=p.get("order_type", "LIMIT"),
                        risk_evaluation_reference=p.get("risk_evaluation_reference", ""),
                        approval_reference=p.get("approval_reference", ""),
                        correlation_id=ev.correlation_id or p.get("correlation_id", ""),
                        provenance=p.get("provenance") or {},
                        limit_price=p.get("limit_price"),
                        stop_price=p.get("stop_price"),
                        time_in_force=p.get("time_in_force", "DAY"),
                        created_at=c_dt,
                        state=ins_state,
                    )
                    ins.broker_order_id = p.get("broker_order_id")
                    ins.dispatched_at = p.get("dispatched_at")
                    ins.acknowledged_at = p.get("acknowledged_at")
                    ins.rejection_reason = p.get("rejection_reason")
                    ins.failure_reason = p.get("failure_reason")
                    ins.cancellation_reason = p.get("cancellation_reason")
                    instructions[ins_id] = ins

                elif rtype == "EXECUTION":
                    exec_id = ev.record_id
                    rec = ExecutionRecord(
                        execution_id=exec_id,
                        instruction_id=p.get("instruction_id", ""),
                        intent_id=p.get("intent_id", ""),
                        symbol=p.get("symbol", "UNKNOWN"),
                        side=p.get("side", "BUY"),
                        ordered_quantity=p.get("ordered_quantity", p.get("quantity", 1)),
                        limit_price=p.get("limit_price", p.get("price")),
                        correlation_id=ev.correlation_id or p.get("correlation_id", ""),
                        gateway_timestamp=p.get("gateway_timestamp", ev.timestamp),
                        broker_order_id=p.get("broker_order_id"),
                        broker_timestamp=p.get("broker_timestamp"),
                        current_state=ExecutionState.from_str(p.get("current_state", "DISPATCH_PENDING")),
                        reconciliation_status=ReconciliationStatus.from_str(p.get("reconciliation_status", "PENDING")),
                        filled_quantity=p.get("filled_quantity", 0),
                        remaining_quantity=p.get("remaining_quantity"),
                        average_fill_price=p.get("average_fill_price"),
                    )
                    rec.rejection_reason = p.get("rejection_reason")
                    rec.failure_reason = p.get("failure_reason")
                    rec.cancellation_reason = p.get("cancellation_reason")
                    rec.reconciliation_notes = p.get("reconciliation_notes")
                    executions[exec_id] = rec

                elif rtype == "FILL":
                    fill_id = ev.record_id
                    if fill_id in seen_fill_ids:
                        continue
                    seen_fill_ids.add(fill_id)

                    exec_id = p.get("execution_id", "")
                    fill = FillRecord(
                        fill_id=fill_id,
                        quantity=p.get("quantity", 0),
                        price=p.get("price"),
                        timestamp=p.get("timestamp", ev.timestamp),
                        execution_id=exec_id,
                        broker_fill_id=p.get("broker_fill_id"),
                        fee=p.get("fee", 0.0),
                    )
                    if exec_id not in fills_by_exec:
                        fills_by_exec[exec_id] = []
                    fills_by_exec[exec_id].append(fill)

                elif rtype == "QUARANTINE":
                    quarantines.append(p)
                elif rtype == "RECONCILIATION":
                    reconciliations.append(p)
                elif rtype == "RECOVERY_DECISION":
                    recovery_decisions.append(p)

            # Associate fills with executions
            for exec_id, rec in executions.items():
                if exec_id in fills_by_exec:
                    rec.fills = list(fills_by_exec[exec_id])

            return {
                "intents": intents,
                "instructions": instructions,
                "executions": executions,
                "fills": [f for flist in fills_by_exec.values() for f in flist],
                "quarantines": quarantines,
                "reconciliations": reconciliations,
                "recovery_decisions": recovery_decisions,
            }

    # Helper append methods for domain models
    def append_intent(self, intent: Any, correlation_id: str = "") -> DurableEvent:
        payload = {
            "intent_id": intent.intent_id,
            "creator_id": intent.creator_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "limit_price": intent.limit_price,
            "stop_price": intent.stop_price,
            "time_in_force": intent.time_in_force,
            "correlation_id": intent.correlation_id,
            "created_at": intent.created_at.isoformat() if intent.created_at else None,
            "updated_at": intent.updated_at.isoformat() if intent.updated_at else None,
            "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
            "state": intent.state.value,
            "approver_id": intent.approver_id,
            "approved_at": intent.approved_at,
            "rejection_reason": intent.rejection_reason,
            "risk_status": intent.risk_status,
            "risk_evaluator_id": intent.risk_evaluator_id,
            "risk_evaluated_at": intent.risk_evaluated_at,
            "risk_rejection_reason": intent.risk_rejection_reason,
            "risk_details": intent.risk_details,
        }
        return self.append("INTENT", intent.intent_id, payload, correlation_id or intent.correlation_id)

    def append_instruction(self, instruction: Any, correlation_id: str = "") -> DurableEvent:
        if hasattr(instruction, "to_dict"):
            payload = instruction.to_dict()
            ins_id = instruction.instruction_id
            corr = correlation_id or getattr(instruction, "correlation_id", "")
        elif isinstance(instruction, dict):
            payload = dict(instruction)
            ins_id = payload.get("instruction_id", "")
            corr = correlation_id or payload.get("correlation_id", "")
        else:
            ins_id = getattr(instruction, "instruction_id", "")
            state_obj = getattr(instruction, "state", "CREATED")
            state_val = state_obj.value if hasattr(state_obj, "value") else str(state_obj)
            payload = {
                "instruction_id": ins_id,
                "intent_id": getattr(instruction, "intent_id", ""),
                "symbol": getattr(instruction, "symbol", ""),
                "side": getattr(instruction, "side", ""),
                "quantity": getattr(instruction, "quantity", 0),
                "order_type": getattr(instruction, "order_type", ""),
                "limit_price": getattr(instruction, "limit_price", None),
                "stop_price": getattr(instruction, "stop_price", None),
                "time_in_force": getattr(instruction, "time_in_force", "DAY"),
                "state": state_val,
                "broker_order_id": getattr(instruction, "broker_order_id", None),
                "rejection_reason": getattr(instruction, "rejection_reason", None),
                "failure_reason": getattr(instruction, "failure_reason", None),
                "risk_evaluation_reference": getattr(instruction, "risk_evaluation_reference", ""),
                "approval_reference": getattr(instruction, "approval_reference", ""),
                "correlation_id": getattr(instruction, "correlation_id", ""),
                "provenance": getattr(instruction, "provenance", {}),
                "created_at": getattr(instruction, "created_at").isoformat() if getattr(instruction, "created_at", None) else None,
            }
            corr = correlation_id or getattr(instruction, "correlation_id", "")
        return self.append("INSTRUCTION", ins_id, payload, corr)

    def append_execution(self, record: Any, correlation_id: str = "") -> DurableEvent:
        if hasattr(record, "to_dict"):
            payload = record.to_dict()
            exec_id = record.execution_id
            corr = correlation_id or getattr(record, "correlation_id", "")
        elif isinstance(record, dict):
            payload = dict(record)
            exec_id = payload.get("execution_id", "")
            corr = correlation_id or payload.get("correlation_id", "")
        else:
            exec_id = getattr(record, "execution_id", "")
            curr_st = getattr(record, "current_state", "UNKNOWN")
            st_val = curr_st.value if hasattr(curr_st, "value") else str(curr_st)
            rec_st = getattr(record, "reconciliation_status", "UNKNOWN")
            rec_val = rec_st.value if hasattr(rec_st, "value") else str(rec_st)
            payload = {
                "execution_id": exec_id,
                "instruction_id": getattr(record, "instruction_id", ""),
                "intent_id": getattr(record, "intent_id", ""),
                "symbol": getattr(record, "symbol", ""),
                "side": getattr(record, "side", ""),
                "ordered_quantity": getattr(record, "ordered_quantity", 0),
                "limit_price": getattr(record, "limit_price", None),
                "broker_order_id": getattr(record, "broker_order_id", None),
                "broker_timestamp": getattr(record, "broker_timestamp", None),
                "current_state": st_val,
                "filled_quantity": getattr(record, "filled_quantity", 0),
                "remaining_quantity": getattr(record, "remaining_quantity", 0),
                "average_fill_price": getattr(record, "average_fill_price", None),
                "reconciliation_status": rec_val,
                "reconciliation_notes": getattr(record, "reconciliation_notes", None),
                "gateway_timestamp": getattr(record, "gateway_timestamp", None),
            }
            corr = correlation_id or getattr(record, "correlation_id", "")
        return self.append("EXECUTION", exec_id, payload, corr)

    def append_fill(self, fill: Any, execution_id: str = "", correlation_id: str = "") -> DurableEvent:
        if hasattr(fill, "to_dict"):
            payload = fill.to_dict()
            fill_id = payload.get("fill_id", "")
            if execution_id:
                payload["execution_id"] = execution_id
            corr = correlation_id or payload.get("correlation_id", "")
        elif isinstance(fill, dict):
            payload = dict(fill)
            fill_id = payload.get("fill_id", "")
            if execution_id:
                payload["execution_id"] = execution_id
            corr = correlation_id or payload.get("correlation_id", "")
        else:
            fill_id = getattr(fill, "fill_id", "")
            payload = {
                "fill_id": fill_id,
                "execution_id": execution_id or getattr(fill, "execution_id", ""),
                "quantity": getattr(fill, "quantity", 0),
                "price": getattr(fill, "price", None),
                "timestamp": getattr(fill, "timestamp", ""),
                "broker_fill_id": getattr(fill, "broker_fill_id", None),
                "fee": getattr(fill, "fee", 0.0),
            }
            corr = correlation_id
        return self.append("FILL", fill_id, payload, corr)

    def append_quarantine(self, quarantine: Any, correlation_id: str = "") -> DurableEvent:
        payload = quarantine.to_dict() if hasattr(quarantine, "to_dict") else dict(quarantine)
        qid = payload.get("execution_id", payload.get("quarantine_id", str(uuid.uuid4())))
        return self.append("QUARANTINE", qid, payload, correlation_id)

    def append_reconciliation(self, rec: Any, correlation_id: str = "") -> DurableEvent:
        payload = rec.to_dict() if hasattr(rec, "to_dict") else dict(rec)
        rid = payload.get("execution_id", payload.get("reconciliation_id", str(uuid.uuid4())))
        return self.append("RECONCILIATION", rid, payload, correlation_id)

    def append_recovery_decision(self, decision: Any, correlation_id: str = "") -> DurableEvent:
        payload = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        did = payload.get("recovery_id", payload.get("execution_id", str(uuid.uuid4())))
        return self.append("RECOVERY_DECISION", did, payload, correlation_id)

    def close(self) -> None:
        with self._lock:
            self._writer.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
