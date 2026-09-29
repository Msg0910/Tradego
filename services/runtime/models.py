"""
Tradego Trading Runtime Models (Phase 8).

Defines immutable data contracts, operational modes, lifecycle states,
guard states, and correlation telemetry records for system orchestration.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from services.market_state.instrument import InstrumentId


class RuntimeMode(str, Enum):
    """Execution operating mode for the Tradego runtime."""
    DEVELOPMENT = "DEVELOPMENT"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class RuntimeState(str, Enum):
    """Authoritative system lifecycle state machine states."""
    STARTING = "STARTING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    DEGRADED = "DEGRADED"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class GuardState(str, Enum):
    """Trading guard operational state."""
    NORMAL = "NORMAL"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"


class GuardTripReason(str, Enum):
    """Reason code for tripping the trading guard or kill switch."""
    MANUAL = "MANUAL"
    DAILY_LOSS_EXCEEDED = "DAILY_LOSS_EXCEEDED"
    FEED_STAGNATION = "FEED_STAGNATION"
    EXCEPTION_BURST = "EXCEPTION_BURST"
    QUARANTINE_TRIP = "QUARANTINE_TRIP"
    CRITICAL_EVENT_OVERFLOW = "CRITICAL_EVENT_OVERFLOW"
    STARTUP_FAILURE = "STARTUP_FAILURE"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"


@dataclass(frozen=True, slots=True)
class RuntimeCorrelationRecord:
    """
    End-to-end cryptographic and temporal lineage record spanning T1 through T10.
    Emitted to the non-blocking telemetry buffer upon trade completion.
    """
    client_order_id: str
    intent_id: str
    signal_id: str
    signal_fingerprint: str
    reaffirmation_key: str
    idempotency_key: str
    instrument_canonical_id: str
    strategy_id: str
    t1_market_receive_ns: int
    t2_signal_generated_ns: int
    t3_risk_evaluated_ns: int
    t4_intent_approved_ns: int
    t5_order_planned_ns: int
    t6_order_submitted_ns: int
    t7_wire_dispatched_ns: int
    t8_order_acked_ns: int
    t9_fill_received_ns: int
    t10_position_updated_ns: int


@dataclass(frozen=True, slots=True)
class ComponentHeartbeatRecord:
    """Diagnostic heartbeat emitted by runtime subsystems."""
    component_name: str
    status: str
    timestamp_ns: int
    error_count: int
    metrics: Optional[Mapping[str, Any]] = None
