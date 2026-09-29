"""
Tradego API & Event Boundary Domain Contracts & DTOs.
Establishes typed, versioned data transfer objects for commands, events, and snapshots.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Generic, Optional, TypeVar
import uuid

T = TypeVar("T")


class CommandType(str, Enum):
    """Supported administrative and control command types."""
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    KILL_SWITCH = "KILL_SWITCH"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    RECOVER_HALTED = "RECOVER_HALTED"


class Capability(str, Enum):
    """Granular RBAC capabilities for boundary operations."""
    CAP_OBSERVE = "CAP_OBSERVE"
    CAP_CONTROL_PAUSE = "CAP_CONTROL_PAUSE"
    CAP_CONTROL_RESUME = "CAP_CONTROL_RESUME"
    CAP_CONTROL_FLATTEN = "CAP_CONTROL_FLATTEN"
    CAP_CONTROL_KILL = "CAP_CONTROL_KILL"
    CAP_CONTROL_RECOVER = "CAP_CONTROL_RECOVER"
    CAP_TRADE_SUBMIT = "CAP_TRADE_SUBMIT"
    CAP_TRADE_APPROVE = "CAP_TRADE_APPROVE"
    CAP_TRADE_CANCEL = "CAP_TRADE_CANCEL"
    CAP_RISK_EVALUATE = "CAP_RISK_EVALUATE"
    CAP_ORDER_INSTRUCT = "CAP_ORDER_INSTRUCT"
    CAP_ORDER_DISPATCH = "CAP_ORDER_DISPATCH"
    CAP_ADMIN = "CAP_ADMIN"


class CommandStatus(str, Enum):
    """Lifecycle status of an ingested command."""
    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TradegoEventEnvelope(Generic[T]):
    """
    Standardized, immutable event envelope pushed across the presentation boundary.
    Enforces monotonic 64-bit sequence ordering, correlation lineage, and typed payload.
    """
    event_id: str
    event_type: str
    sequence: int
    correlation_id: str
    server_timestamp: str
    payload: T
    event_version: str = "1.0.0"

    def to_dict(self) -> Dict[str, Any]:
        """Serializes envelope to dictionary for JSON transmission."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "server_timestamp": self.server_timestamp,
            "payload": self.payload,
            "event_version": self.event_version,
        }


@dataclass(frozen=True)
class CommandRequest:
    """
    Discrete, schema-validated command payload submitted from an authenticated client.
    """
    command_type: CommandType
    operator_id: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parameters: Dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class CommandResult:
    """
    Deterministic result emitted following command authorization and engine dispatch.
    """
    command_id: str
    status: CommandStatus
    guard_state: str
    reason: str = ""
    sequence: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class SnapshotPayload:
    """
    Point-in-time state projection contract anchoring the client sequence baseline (S_snap).
    """
    snapshot_id: str
    snapshot_timestamp: str
    authoritative_sequence: int  # Exact sequence frontier S_snap
    runtime_mode: str
    guard_state: str
    details: Dict[str, Any] = field(default_factory=dict)
