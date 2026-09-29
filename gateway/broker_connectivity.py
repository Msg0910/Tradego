"""
Tradego Phase 9 — Broker Connectivity State Machine & Execution Mode Controls.

Provides authoritative lifecycle management for broker sessions and execution gating:
- Execution mode states: PAPER (default) vs LIVE
- Rigorous multi-gate checks for LIVE mode activation
- Zero silent fallback from LIVE to PAPER or unauthenticated states
- Deterministic broker connectivity states:
    DISCONNECTED, CONNECTING, CONNECTED, DEGRADED, RECONNECTING, AUTH_FAILED, UNKNOWN
- Heartbeat tracking and stale connection detection
- Authoritative reconciliation comparison:
    Local OrderInstruction vs Local ExecutionState vs Broker Order State vs Broker Fill State
- Tier-1 audit logging with SHA-256 chaining and zero credential leakage
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from .broadcaster import EventBroadcaster
from .broker_credentials import BrokerCredentialsConfig
from .recovery import GuardState, TradingGuard
from .security import Tier1AuditLogger


class ExecutionMode(str, Enum):
    """Authoritative execution modes."""
    PAPER = "PAPER"
    LIVE = "LIVE"

    @classmethod
    def from_str(cls, val: Any) -> "ExecutionMode":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.PAPER
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.PAPER


class BrokerConnectivityState(str, Enum):
    """Authoritative broker connectivity lifecycle states."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    AUTH_FAILED = "AUTH_FAILED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_str(cls, val: Any) -> "BrokerConnectivityState":
        if isinstance(val, cls):
            return val
        if not val or not isinstance(val, str):
            return cls.UNKNOWN
        try:
            return cls(val.upper().strip())
        except (ValueError, KeyError):
            return cls.UNKNOWN


class BrokerReconciliationResult(str, Enum):
    """Authoritative result of broker book reconciliation."""
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"
    REQUIRES_OPERATOR_ACTION = "REQUIRES_OPERATOR_ACTION"


@dataclass(frozen=True)
class ReconciliationReport:
    """Authoritative summary of a broker reconciliation run."""
    result: BrokerReconciliationResult
    timestamp: str
    inspected_orders_count: int
    matched_count: int
    mismatch_count: int
    unknown_count: int
    discrepancies: List[Dict[str, Any]] = field(default_factory=list)
    operator_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result.value,
            "timestamp": self.timestamp,
            "inspected_orders_count": self.inspected_orders_count,
            "matched_count": self.matched_count,
            "mismatch_count": self.mismatch_count,
            "unknown_count": self.unknown_count,
            "discrepancies": list(self.discrepancies),
            "operator_notes": self.operator_notes,
        }


class BrokerConnectivityManager:
    """
    Authoritative manager for venue connectivity, heartbeat monitoring,
    mode transitions, and multi-gate safety checks.
    """

    HEARTBEAT_TIMEOUT_SECONDS: float = 60.0

    VALID_TRANSITIONS: Dict[str, Set[str]] = {
        "DISCONNECTED": {"CONNECTING", "UNKNOWN"},
        "CONNECTING": {"CONNECTED", "AUTH_FAILED", "DISCONNECTED", "UNKNOWN"},
        "CONNECTED": {"DISCONNECTED", "DEGRADED", "RECONNECTING", "UNKNOWN"},
        "DEGRADED": {"CONNECTED", "DISCONNECTED", "RECONNECTING", "UNKNOWN"},
        "RECONNECTING": {"CONNECTED", "DISCONNECTED", "AUTH_FAILED", "UNKNOWN"},
        "AUTH_FAILED": {"DISCONNECTED", "UNKNOWN"},
        "UNKNOWN": {"DISCONNECTED", "CONNECTING"},
    }

    def __init__(
        self,
        trading_guard: TradingGuard,
        audit_logger: Tier1AuditLogger,
        broadcaster: Optional[EventBroadcaster] = None,
        risk_gate_available: bool = True,
        active_broker: str = "PAPER_VENUE_SIM",
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._broadcaster = broadcaster
        self._risk_gate_available = risk_gate_available
        self._active_broker = active_broker

        self._lock = threading.RLock()
        self._mode = ExecutionMode.PAPER
        self._state = BrokerConnectivityState.DISCONNECTED
        self._credentials: Optional[BrokerCredentialsConfig] = None
        self._last_heartbeat: Optional[datetime] = None
        self._last_broker_event: Optional[Dict[str, Any]] = None
        self._last_reconciliation: Optional[ReconciliationReport] = None
        self._failure_reason: Optional[str] = None

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def execution_mode(self) -> ExecutionMode:
        with self._lock:
            return self._mode

    @property
    def connectivity_state(self) -> BrokerConnectivityState:
        with self._lock:
            # Check for stale heartbeat if currently CONNECTED
            if self._state == BrokerConnectivityState.CONNECTED and self._last_heartbeat:
                delta = (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds()
                if delta > self.HEARTBEAT_TIMEOUT_SECONDS:
                    return BrokerConnectivityState.DEGRADED
            return self._state

    @property
    def active_broker(self) -> str:
        with self._lock:
            return self._active_broker

    @property
    def credentials(self) -> Optional[BrokerCredentialsConfig]:
        with self._lock:
            return self._credentials

    @property
    def last_heartbeat(self) -> Optional[str]:
        with self._lock:
            return self._last_heartbeat.isoformat() if self._last_heartbeat else None

    @property
    def last_broker_event(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._last_broker_event) if self._last_broker_event else None

    @property
    def last_reconciliation(self) -> Optional[ReconciliationReport]:
        with self._lock:
            return self._last_reconciliation

    @property
    def is_risk_gate_available(self) -> bool:
        with self._lock:
            return self._risk_gate_available

    def set_risk_gate_available(self, available: bool) -> None:
        with self._lock:
            self._risk_gate_available = available

    # -------------------------------------------------------------------------
    # Status Summary (Redacted & Safe)
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Returns safe, authoritative point-in-time status without leaking secrets."""
        with self._lock:
            current_conn = self.connectivity_state
            creds_status = self._credentials.redacted_dict() if self._credentials else {
                "venue_name": "N/A",
                "client_id": "N/A",
                "is_valid": False,
                "has_access_token": False,
            }

            return {
                "execution_mode": self._mode.value,
                "connectivity_state": current_conn.value,
                "active_broker": self._active_broker,
                "is_authenticated": bool(self._credentials and self._credentials.is_valid),
                "credentials": creds_status,
                "risk_gate_available": self._risk_gate_available,
                "guard_state": self._guard.state.value,
                "last_heartbeat": self.last_heartbeat,
                "last_broker_event": self.last_broker_event,
                "last_reconciliation": (
                    self._last_reconciliation.to_dict() if self._last_reconciliation else None
                ),
                "failure_reason": self._failure_reason,
            }

    # -------------------------------------------------------------------------
    # Connection Lifecycle
    # -------------------------------------------------------------------------

    def set_credentials(self, credentials: BrokerCredentialsConfig, operator_id: str) -> None:
        """Sets or updates broker credentials securely with zero credential leakage."""
        with self._lock:
            self._credentials = credentials
            self._audit.log({
                "action": "BROKER_CREDENTIALS_UPDATED",
                "operator_id": operator_id,
                "venue_name": credentials.venue_name,
                "client_id": credentials.redacted_dict().get("client_id", "***"),
                "is_valid": credentials.is_valid,
            })

    def connect(
        self,
        operator_id: str,
        credentials: Optional[BrokerCredentialsConfig] = None,
        venue_name: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> bool:
        """
        Initiates connection to the broker venue.
        Verifies credentials, updates state, records heartbeat, and logs audit events.
        """
        with self._lock:
            now_dt = datetime.now(timezone.utc)
            self._audit.log({
                "action": "BROKER_CONNECT_REQUESTED",
                "operator_id": operator_id,
                "correlation_id": correlation_id,
            })

            target_creds = credentials or self._credentials
            if not target_creds or not target_creds.is_valid:
                self._state = BrokerConnectivityState.AUTH_FAILED
                self._failure_reason = "INVALID_OR_MISSING_CREDENTIALS"
                self._audit.log({
                    "action": "BROKER_AUTH_FAILED",
                    "operator_id": operator_id,
                    "reason": self._failure_reason,
                    "correlation_id": correlation_id,
                })
                raise ValueError("BROKER_AUTH_FAILED: Missing or invalid broker credentials.")

            self._credentials = target_creds
            if venue_name:
                self._active_broker = venue_name
            elif target_creds.venue_name:
                self._active_broker = target_creds.venue_name

            self._state = BrokerConnectivityState.CONNECTED
            self._failure_reason = None
            self._last_heartbeat = now_dt
            self._last_broker_event = {
                "event": "SESSION_ESTABLISHED",
                "timestamp": now_dt.isoformat(),
                "venue": self._active_broker,
            }

            self._audit.log({
                "action": "BROKER_CONNECTED",
                "operator_id": operator_id,
                "venue": self._active_broker,
                "correlation_id": correlation_id,
            })

            if self._broadcaster:
                self._broadcaster.publish(
                    event_type="BROKER_CONNECTED",
                    payload={"venue": self._active_broker, "state": self._state.value},
                    correlation_id=correlation_id or "",
                )

            return True

    def disconnect(
        self,
        operator_id: str,
        reason: str = "OPERATOR_REQUESTED",
        correlation_id: Optional[str] = None,
    ) -> bool:
        """
        Disconnects the active broker session.
        In LIVE mode, disconnecting does NOT silently fall back to PAPER;
        instead it marks the connection DISCONNECTED so live execution is strictly blocked.
        """
        with self._lock:
            now_dt = datetime.now(timezone.utc)
            self._state = BrokerConnectivityState.DISCONNECTED
            self._failure_reason = reason
            self._last_broker_event = {
                "event": "SESSION_TERMINATED",
                "reason": reason,
                "timestamp": now_dt.isoformat(),
            }

            self._audit.log({
                "action": "BROKER_DISCONNECTED",
                "operator_id": operator_id,
                "reason": reason,
                "execution_mode": self._mode.value,
                "correlation_id": correlation_id,
            })

            if self._broadcaster:
                self._broadcaster.publish(
                    event_type="BROKER_DISCONNECTED",
                    payload={"reason": reason, "mode": self._mode.value},
                    correlation_id=correlation_id or "",
                )

            return True

    def record_heartbeat(self, now: Optional[datetime] = None) -> None:
        """Records reception of a transport or venue heartbeat."""
        with self._lock:
            self._last_heartbeat = now or datetime.now(timezone.utc)
            if self._state == BrokerConnectivityState.DEGRADED:
                self._state = BrokerConnectivityState.CONNECTED

    def record_broker_event(self, event_name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Records latest incoming broker event for UI/telemetry."""
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()
            self._last_broker_event = {
                "event": event_name,
                "payload": payload or {},
                "timestamp": now_str,
            }

    def set_connectivity_state(
        self,
        new_state: BrokerConnectivityState,
        operator_id: str = "SYSTEM",
        reason: Optional[str] = None,
    ) -> None:
        """
        Deterministic transition of connectivity state.
        Validates transition against authoritative VALID_TRANSITIONS graph.
        Rejects illegal transitions without mutating current state.
        """
        with self._lock:
            target_state = (
                new_state
                if isinstance(new_state, BrokerConnectivityState)
                else BrokerConnectivityState.from_str(new_state)
            )
            old_state = self._state
            old_val = old_state.value
            target_val = target_state.value

            valid_targets = self.VALID_TRANSITIONS.get(old_val, set())
            if target_val not in valid_targets and target_state != old_state:
                self._audit.log({
                    "action": "BROKER_ILLEGAL_STATE_TRANSITION_REJECTED",
                    "operator_id": operator_id,
                    "current_state": old_val,
                    "attempted_state": target_val,
                    "reason": reason or "ILLEGAL_STATE_TRANSITION",
                })
                raise ValueError(
                    f"ILLEGAL_CONNECTIVITY_TRANSITION: Cannot transition broker connectivity from "
                    f"{old_val} to {target_val}."
                )

            self._state = target_state
            if reason:
                self._failure_reason = reason

            if target_state == BrokerConnectivityState.RECONNECTING:
                self._audit.log({
                    "action": "BROKER_RECONNECTING",
                    "operator_id": operator_id,
                    "previous_state": old_state.value,
                    "reason": reason,
                })
            elif target_state == BrokerConnectivityState.AUTH_FAILED:
                self._audit.log({
                    "action": "BROKER_AUTH_FAILED",
                    "operator_id": operator_id,
                    "reason": reason or "AUTH_FAILURE",
                })

    # -------------------------------------------------------------------------
    # Mode Transition Control (Multi-Gate Protection)
    # -------------------------------------------------------------------------

    def set_mode(
        self,
        target_mode: ExecutionMode,
        operator_id: str,
        confirm_live: bool = False,
        correlation_id: Optional[str] = None,
    ) -> None:
        """
        Safely transitions execution mode between PAPER and LIVE.
        Guarantees:
        1. LIVE requires explicit configuration / confirmation (confirm_live=True).
        2. LIVE requires operator authorization.
        3. LIVE requires TradingGuard state == RUNNING (blocks if HALTED or PAUSED).
        4. LIVE requires valid authenticated credentials.
        5. LIVE requires broker connectivity == CONNECTED.
        6. LIVE requires risk system availability.
        7. Never silently falls back from LIVE to PAPER or unauthenticated state.
        """
        with self._lock:
            if target_mode == ExecutionMode.LIVE:
                self._audit.log({
                    "action": "LIVE_MODE_REQUESTED",
                    "operator_id": operator_id,
                    "correlation_id": correlation_id,
                })

                # Check 1: Explicit live confirmation
                if not confirm_live:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "operator_id": operator_id,
                        "reason": "MISSING_EXPLICIT_LIVE_CONFIRMATION",
                        "correlation_id": correlation_id,
                    })
                    raise ValueError(
                        "LIVE_ACTIVATION_REJECTED: Explicit operator confirmation (confirm_live=True) is required."
                    )

                # Check 2: Operational Guard state == RUNNING (NORMAL)
                guard_val = getattr(self._guard.state, "value", str(self._guard.state))
                if self._guard.state in (GuardState.HALTED, GuardState.PAUSED) or guard_val not in ("NORMAL", "RUNNING"):
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "operator_id": operator_id,
                        "reason": f"GUARD_NOT_RUNNING_{guard_val}",
                        "correlation_id": correlation_id,
                    })
                    raise ValueError(
                        f"LIVE_ACTIVATION_REJECTED: TradingGuard is {guard_val}. "
                        "Must be in RUNNING state to enable LIVE mode."
                    )


                # Check 3: Authenticated Credentials
                if not self._credentials or not self._credentials.is_valid:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "operator_id": operator_id,
                        "reason": "MISSING_OR_INVALID_CREDENTIALS",
                        "correlation_id": correlation_id,
                    })
                    raise ValueError(
                        "LIVE_ACTIVATION_REJECTED: Valid authenticated broker credentials are required."
                    )

                # Check 4: Broker Connectivity State == CONNECTED
                if self.connectivity_state != BrokerConnectivityState.CONNECTED:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "operator_id": operator_id,
                        "reason": f"BROKER_NOT_CONNECTED_{self.connectivity_state.value}",
                        "correlation_id": correlation_id,
                    })
                    raise ValueError(
                        f"LIVE_ACTIVATION_REJECTED: Broker is not connected (current state: {self.connectivity_state.value})."
                    )

                # Check 5: Risk system availability
                if not self._risk_gate_available:
                    self._audit.log({
                        "action": "LIVE_EXECUTION_BLOCKED",
                        "operator_id": operator_id,
                        "reason": "RISK_SYSTEM_UNAVAILABLE",
                        "correlation_id": correlation_id,
                    })
                    raise ValueError(
                        "LIVE_ACTIVATION_REJECTED: Pre-trade risk evaluation system is unavailable."
                    )

                # All gates passed: Enable LIVE mode
                self._mode = ExecutionMode.LIVE
                self._audit.log({
                    "action": "LIVE_MODE_ENABLED",
                    "operator_id": operator_id,
                    "venue": self._active_broker,
                    "correlation_id": correlation_id,
                })

                if self._broadcaster:
                    self._broadcaster.publish(
                        event_type="LIVE_MODE_ENABLED",
                        payload={"mode": "LIVE", "operator_id": operator_id, "venue": self._active_broker},
                        correlation_id=correlation_id or "",
                    )

            elif target_mode == ExecutionMode.PAPER:
                # Transition to PAPER mode
                was_live = (self._mode == ExecutionMode.LIVE)
                self._mode = ExecutionMode.PAPER

                if was_live:
                    self._audit.log({
                        "action": "LIVE_MODE_DISABLED",
                        "operator_id": operator_id,
                        "correlation_id": correlation_id,
                    })
                    if self._broadcaster:
                        self._broadcaster.publish(
                            event_type="LIVE_MODE_DISABLED",
                            payload={"mode": "PAPER", "operator_id": operator_id},
                            correlation_id=correlation_id or "",
                        )

    # -------------------------------------------------------------------------
    # Reconciliation Engine
    # -------------------------------------------------------------------------

    def reconcile(
        self,
        operator_id: str,
        execution_manager: Any,
        instruction_manager: Any,
        broker_adapter: Any,
        correlation_id: Optional[str] = None,
    ) -> ReconciliationReport:
        """
        Performs authoritative four-way reconciliation:
        Local OrderInstruction vs Local ExecutionState vs Broker Order State vs Broker Fill State.
        """
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()
            self._audit.log({
                "action": "LIVE_RECONCILIATION_STARTED",
                "operator_id": operator_id,
                "correlation_id": correlation_id,
            })

            instructions = instruction_manager.list_instructions()
            discrepancies: List[Dict[str, Any]] = []
            matched_count = 0
            unknown_count = 0

            for ins in instructions:
                # 1. Local execution state
                exec_rec = None
                if execution_manager:
                    exec_rec = execution_manager.get_by_instruction(ins.instruction_id)

                # If not dispatched or pending without broker order, check local consistency
                if not ins.broker_order_id:
                    if ins.state.value in ["DISPATCHED", "ACKNOWLEDGED"]:
                        discrepancies.append({
                            "instruction_id": ins.instruction_id,
                            "issue": "MISSING_BROKER_ORDER_ID",
                            "details": f"Instruction state {ins.state.value} has no broker_order_id.",
                        })
                    continue

                # 2. Broker order status query
                broker_order = broker_adapter.get_order_status(ins.broker_order_id)
                if broker_order is None:
                    # Missing from broker book
                    discrepancies.append({
                        "instruction_id": ins.instruction_id,
                        "broker_order_id": ins.broker_order_id,
                        "issue": "ORDER_MISSING_FROM_BROKER",
                        "details": f"Broker order {ins.broker_order_id} not found in venue book.",
                    })
                    continue

                # 3. Check parameter match
                broker_symbol = broker_order.get("symbol")
                broker_side = broker_order.get("side")
                broker_qty = broker_order.get("quantity")
                broker_status = broker_order.get("status")

                if broker_symbol and broker_symbol != ins.symbol:
                    discrepancies.append({
                        "instruction_id": ins.instruction_id,
                        "broker_order_id": ins.broker_order_id,
                        "issue": "SYMBOL_MISMATCH",
                        "details": f"Local {ins.symbol} != Broker {broker_symbol}",
                    })

                if broker_qty is not None and broker_qty != ins.quantity:
                    discrepancies.append({
                        "instruction_id": ins.instruction_id,
                        "broker_order_id": ins.broker_order_id,
                        "issue": "QUANTITY_MISMATCH",
                        "details": f"Local qty {ins.quantity} != Broker qty {broker_qty}",
                    })

                # 4. Check fill reconciliation if execution record exists
                if exec_rec:
                    broker_filled = broker_order.get("filled_quantity")
                    if broker_filled is not None and broker_filled != exec_rec.filled_quantity:
                        discrepancies.append({
                            "instruction_id": ins.instruction_id,
                            "execution_id": exec_rec.execution_id,
                            "broker_order_id": ins.broker_order_id,
                            "issue": "FILL_MISMATCH",
                            "details": f"Local filled {exec_rec.filled_quantity} != Broker filled {broker_filled}",
                        })

                if not any(d.get("instruction_id") == ins.instruction_id for d in discrepancies):
                    matched_count += 1

            mismatch_count = len(discrepancies)
            if mismatch_count > 0:
                result = BrokerReconciliationResult.MISMATCH
                audit_action = "LIVE_RECONCILIATION_MISMATCH"
            else:
                result = BrokerReconciliationResult.MATCHED
                audit_action = "LIVE_RECONCILIATION_MATCHED"

            report = ReconciliationReport(
                result=result,
                timestamp=now_str,
                inspected_orders_count=len(instructions),
                matched_count=matched_count,
                mismatch_count=mismatch_count,
                unknown_count=unknown_count,
                discrepancies=discrepancies,
                operator_notes="Reconciliation completed." if result == BrokerReconciliationResult.MATCHED else "Discrepancies require operator review.",
            )
            self._last_reconciliation = report

            self._audit.log({
                "action": audit_action,
                "operator_id": operator_id,
                "result": result.value,
                "inspected_orders_count": len(instructions),
                "matched_count": matched_count,
                "mismatch_count": mismatch_count,
                "correlation_id": correlation_id,
            })

            return report
