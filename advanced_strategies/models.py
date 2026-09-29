"""
TradeGo Phase 11 — Advanced Strategies: Foundation Models & Schedule Descriptors.

Defines immutable, strongly-typed data contracts for:
- Algorithmic execution schedules (TWAP slice parameters, distributions)
- Time-decay exit horizons and urgency dynamics
- Execution slices and deterministic schedule state

Enforces zero wall-clock dependency, zero randomness, and strict input validation.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


class ScheduleType(str, Enum):
    """Supported algorithmic execution schedule paradigms."""
    TWAP = "TWAP"
    TIME_DECAY = "TIME_DECAY"
    STAGED = "STAGED"


@dataclass(frozen=True, slots=True)
class ExecutionSlice:
    """
    Immutable representation of an individual execution slice within an algorithmic schedule.
    """
    slice_id: str
    slice_index: int
    total_slices: int
    scheduled_timestamp: datetime
    target_quantity: int
    time_in_force_seconds: float
    target_notional: Optional[float] = None
    is_final_slice: bool = False
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.slice_index < 0:
            raise ValueError(f"slice_index must be >= 0, got {self.slice_index}")
        if self.total_slices <= 0:
            raise ValueError(f"total_slices must be >= 1, got {self.total_slices}")
        if self.slice_index >= self.total_slices:
            raise ValueError(
                f"slice_index ({self.slice_index}) must be < total_slices ({self.total_slices})"
            )
        if self.target_quantity <= 0:
            raise ValueError(f"target_quantity must be > 0, got {self.target_quantity}")
        if self.time_in_force_seconds < 0.0:
            raise ValueError(
                f"time_in_force_seconds must be >= 0.0, got {self.time_in_force_seconds}"
            )
        if self.target_notional is not None and self.target_notional <= 0.0:
            raise ValueError(f"target_notional must be > 0.0, got {self.target_notional}")

        # Enforce immutable metadata mapping
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class TWAPScheduleConfig:
    """
    Configuration parameters governing deterministic TWAP order slicing.
    """
    duration_seconds: float
    num_slices: int
    max_participation_rate: float = 0.20
    min_slice_quantity: int = 1

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0.0:
            raise ValueError(f"duration_seconds must be > 0.0, got {self.duration_seconds}")
        if self.num_slices <= 0:
            raise ValueError(f"num_slices must be >= 1, got {self.num_slices}")
        if not (0.0 < self.max_participation_rate <= 1.0):
            raise ValueError(
                f"max_participation_rate must be in (0.0, 1.0], got {self.max_participation_rate}"
            )
        if self.min_slice_quantity <= 0:
            raise ValueError(
                f"min_slice_quantity must be >= 1, got {self.min_slice_quantity}"
            )

    @property
    def slice_interval_seconds(self) -> float:
        """Deterministic duration of each slice interval."""
        return self.duration_seconds / float(self.num_slices)


@dataclass(frozen=True, slots=True)
class TimeDecayExitConfig:
    """
    Configuration parameters governing time-decay holding horizons and urgency escalation.
    """
    max_holding_seconds: float
    half_life_seconds: float
    urgency_multiplier_start: float = 1.0
    urgency_multiplier_end: float = 3.0
    num_eval_checkpoints: int = 5

    def __post_init__(self) -> None:
        if self.max_holding_seconds <= 0.0:
            raise ValueError(
                f"max_holding_seconds must be > 0.0, got {self.max_holding_seconds}"
            )
        if self.half_life_seconds <= 0.0:
            raise ValueError(f"half_life_seconds must be > 0.0, got {self.half_life_seconds}")
        if self.half_life_seconds > self.max_holding_seconds:
            raise ValueError(
                f"half_life_seconds ({self.half_life_seconds}) cannot exceed "
                f"max_holding_seconds ({self.max_holding_seconds})"
            )
        if self.urgency_multiplier_start <= 0.0:
            raise ValueError(
                f"urgency_multiplier_start must be > 0.0, got {self.urgency_multiplier_start}"
            )
        if self.urgency_multiplier_end < self.urgency_multiplier_start:
            raise ValueError(
                f"urgency_multiplier_end ({self.urgency_multiplier_end}) cannot be less than "
                f"urgency_multiplier_start ({self.urgency_multiplier_start})"
            )
        if self.num_eval_checkpoints <= 0:
            raise ValueError(
                f"num_eval_checkpoints must be >= 1, got {self.num_eval_checkpoints}"
            )


@dataclass(frozen=True, slots=True)
class ExecutionSchedule:
    """
    Immutable deterministic execution schedule encompassing a sequence of planned execution slices.
    """
    schedule_id: str
    schedule_type: ScheduleType
    instrument_symbol: str
    total_quantity: int
    start_timestamp: datetime
    end_timestamp: datetime
    slices: Tuple[ExecutionSlice, ...]
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.total_quantity <= 0:
            raise ValueError(f"total_quantity must be > 0, got {self.total_quantity}")
        if self.end_timestamp < self.start_timestamp:
            raise ValueError(
                f"end_timestamp ({self.end_timestamp}) cannot precede "
                f"start_timestamp ({self.start_timestamp})"
            )
        if not self.slices:
            raise ValueError("slices tuple cannot be empty")

        # Invariant: sum of slice quantities must equal total_quantity
        sum_qty = sum(s.target_quantity for s in self.slices)
        if sum_qty != self.total_quantity:
            raise ValueError(
                f"Total slice quantity sum ({sum_qty}) does not match "
                f"schedule total_quantity ({self.total_quantity})"
            )

        # Enforce immutable metadata mapping
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def slice_count(self) -> int:
        return len(self.slices)

    def get_slice(self, index: int) -> Optional[ExecutionSlice]:
        if 0 <= index < len(self.slices):
            return self.slices[index]
        return None

    def get_due_slices(self, current_timestamp: datetime) -> Tuple[ExecutionSlice, ...]:
        """Returns all slices whose scheduled_timestamp <= current_timestamp."""
        return tuple(s for s in self.slices if s.scheduled_timestamp <= current_timestamp)


@dataclass(frozen=True, slots=True)
class TimeDecayCheckpoint:
    """
    Point-in-time holding decay evaluation milestone.
    """
    checkpoint_index: int
    elapsed_seconds: float
    decay_ratio: float
    scheduled_timestamp: datetime
    urgency_multiplier: float
    suggested_action: str
    remaining_quantity_pct: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.decay_ratio <= 1.0):
            raise ValueError(f"decay_ratio must be in [0.0, 1.0], got {self.decay_ratio}")
        if self.urgency_multiplier <= 0.0:
            raise ValueError(
                f"urgency_multiplier must be > 0.0, got {self.urgency_multiplier}"
            )
        if not (0.0 <= self.remaining_quantity_pct <= 100.0):
            raise ValueError(
                f"remaining_quantity_pct must be in [0.0, 100.0], got {self.remaining_quantity_pct}"
            )


@dataclass(frozen=True, slots=True)
class TimeDecaySchedule:
    """
    Immutable representation of an active time-decay exit schedule.
    """
    schedule_id: str
    start_timestamp: datetime
    expiry_timestamp: datetime
    checkpoints: Tuple[TimeDecayCheckpoint, ...]
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.expiry_timestamp < self.start_timestamp:
            raise ValueError(
                f"expiry_timestamp ({self.expiry_timestamp}) cannot precede "
                f"start_timestamp ({self.start_timestamp})"
            )
        if not self.checkpoints:
            raise ValueError("checkpoints tuple cannot be empty")

        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def evaluate_at(self, current_timestamp: datetime) -> TimeDecayCheckpoint:
        """
        Retrieves the latest applicable decay checkpoint for the given simulation timestamp.
        """
        applicable = [cp for cp in self.checkpoints if cp.scheduled_timestamp <= current_timestamp]
        if applicable:
            return applicable[-1]
        return self.checkpoints[0]
