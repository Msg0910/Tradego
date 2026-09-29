"""
TradeGo Phase 11 — Advanced Strategies: Algorithmic Execution Schedules.

Pure deterministic execution schedule generation for:
1. TWAP (Time-Weighted Average Price) order slicing.
2. Time-decay holding horizon evaluation schedules.

Guarantees:
- Zero wall-clock dependency (all timestamps passed explicitly by caller).
- Zero randomness / no random numbers.
- Zero network / zero broker connectivity.
- Zero execution side effects.
- Purely deterministic output: identical inputs produce identical schedules.
"""

from datetime import datetime, timedelta
import math
from typing import Any, Mapping, Optional, Tuple

from advanced_strategies.models import (
    ExecutionSchedule,
    ExecutionSlice,
    ScheduleType,
    TimeDecayCheckpoint,
    TimeDecayExitConfig,
    TimeDecaySchedule,
    TWAPScheduleConfig,
)


def generate_twap_schedule(
    config: TWAPScheduleConfig,
    total_quantity: int,
    start_time: datetime,
    instrument_symbol: str = "",
    target_notional: Optional[float] = None,
    schedule_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> ExecutionSchedule:
    """
    Deterministically slices an aggregate target quantity into a TWAP execution schedule.

    Slice quantities are integer distributed such that:
    - sum(slice.target_quantity) == total_quantity
    - Any remainder is distributed one-by-one to the earliest slices
    - Every slice has target_quantity >= config.min_slice_quantity

    Parameters
    ----------
    config : TWAPScheduleConfig
        TWAP configuration specifying duration, slice count, and participation cap.
    total_quantity : int
        Total quantity to slice. Must be >= config.num_slices * config.min_slice_quantity.
    start_time : datetime
        Deterministic simulation/event timestamp for the initial slice.
    instrument_symbol : str, optional
        Symbol of the target instrument.
    target_notional : Optional[float], optional
        Total target notional to be proportionally partitioned across slices.
    schedule_id : Optional[str], optional
        Explicit schedule ID. If None, generated deterministically from inputs.
    metadata : Optional[Mapping[str, Any]], optional
        Optional caller-supplied metadata.

    Returns
    -------
    ExecutionSchedule
        Immutable schedule containing the deterministic tuple of execution slices.
    """
    if not isinstance(config, TWAPScheduleConfig):
        raise TypeError(f"config must be a TWAPScheduleConfig, got {type(config).__name__}")
    if not isinstance(start_time, datetime):
        raise TypeError(f"start_time must be a datetime, got {type(start_time).__name__}")
    if total_quantity <= 0:
        raise ValueError(f"total_quantity must be > 0, got {total_quantity}")

    min_required_qty = config.num_slices * config.min_slice_quantity
    if total_quantity < min_required_qty:
        raise ValueError(
            f"total_quantity ({total_quantity}) is insufficient for {config.num_slices} slices "
            f"with min_slice_quantity={config.min_slice_quantity} (requires at least {min_required_qty})"
        )

    if target_notional is not None and target_notional <= 0.0:
        raise ValueError(f"target_notional must be > 0.0, got {target_notional}")

    # Deterministic schedule ID if not provided
    if schedule_id is None:
        sym = instrument_symbol.strip() if instrument_symbol else "GENERIC"
        ts_str = start_time.strftime("%Y%m%d_%H%M%S")
        schedule_id = f"twap_{sym}_{ts_str}_q{total_quantity}_n{config.num_slices}"

    # Integer division and remainder distribution
    base_qty = total_quantity // config.num_slices
    remainder = total_quantity % config.num_slices

    interval_sec = config.slice_interval_seconds
    slices_list = []

    for i in range(config.num_slices):
        slice_qty = base_qty + (1 if i < remainder else 0)
        slice_time = start_time + timedelta(seconds=i * interval_sec)
        is_final = (i == config.num_slices - 1)
        slice_id = f"{schedule_id}_s{i:03d}"

        slice_notional: Optional[float] = None
        if target_notional is not None:
            slice_notional = round((target_notional * slice_qty) / float(total_quantity), 4)

        slice_meta: dict[str, Any] = {
            "participation_cap": config.max_participation_rate,
            "min_slice_quantity": config.min_slice_quantity,
        }

        exec_slice = ExecutionSlice(
            slice_id=slice_id,
            slice_index=i,
            total_slices=config.num_slices,
            scheduled_timestamp=slice_time,
            target_quantity=slice_qty,
            time_in_force_seconds=interval_sec,
            target_notional=slice_notional,
            is_final_slice=is_final,
            metadata=slice_meta,
        )
        slices_list.append(exec_slice)

    end_timestamp = start_time + timedelta(seconds=config.duration_seconds)

    schedule_meta: dict[str, Any] = {
        "max_participation_rate": config.max_participation_rate,
        "slice_interval_seconds": interval_sec,
    }
    if metadata:
        schedule_meta.update(metadata)

    return ExecutionSchedule(
        schedule_id=schedule_id,
        schedule_type=ScheduleType.TWAP,
        instrument_symbol=instrument_symbol,
        total_quantity=total_quantity,
        start_timestamp=start_time,
        end_timestamp=end_timestamp,
        slices=tuple(slices_list),
        metadata=schedule_meta,
    )


def generate_time_decay_schedule(
    config: TimeDecayExitConfig,
    total_quantity: int,
    entry_time: datetime,
    schedule_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> TimeDecaySchedule:
    """
    Deterministically computes holding decay checkpoints and urgency escalation for a position.

    Parameters
    ----------
    config : TimeDecayExitConfig
        Time decay configuration specifying max holding horizon, half-life, and urgency.
    total_quantity : int
        Initial position size being monitored.
    entry_time : datetime
        Deterministic simulation/event timestamp when the position was entered.
    schedule_id : Optional[str], optional
        Explicit schedule ID. If None, generated deterministically from inputs.
    metadata : Optional[Mapping[str, Any]], optional
        Optional caller-supplied metadata.

    Returns
    -------
    TimeDecaySchedule
        Immutable schedule containing deterministic evaluation checkpoints.
    """
    if not isinstance(config, TimeDecayExitConfig):
        raise TypeError(f"config must be a TimeDecayExitConfig, got {type(config).__name__}")
    if not isinstance(entry_time, datetime):
        raise TypeError(f"entry_time must be a datetime, got {type(entry_time).__name__}")
    if total_quantity <= 0:
        raise ValueError(f"total_quantity must be > 0, got {total_quantity}")

    # Deterministic schedule ID if not provided
    if schedule_id is None:
        ts_str = entry_time.strftime("%Y%m%d_%H%M%S")
        schedule_id = f"tdecay_{ts_str}_q{total_quantity}_max{int(config.max_holding_seconds)}"

    checkpoints_list = []
    n = config.num_eval_checkpoints

    for i in range(n):
        if n == 1:
            fraction = 1.0
        else:
            fraction = float(i) / float(n - 1)

        elapsed = fraction * config.max_holding_seconds
        decay_ratio = min(1.0, max(0.0, fraction))
        cp_time = entry_time + timedelta(seconds=elapsed)

        urgency = config.urgency_multiplier_start + (
            (config.urgency_multiplier_end - config.urgency_multiplier_start) * decay_ratio
        )
        urgency = round(urgency, 4)

        if fraction >= 1.0:
            remaining_pct = 0.0
            action = "FLATTEN"
        else:
            remaining_pct = round(
                100.0 * math.pow(0.5, elapsed / config.half_life_seconds), 2
            )
            remaining_pct = max(0.0, min(100.0, remaining_pct))
            if decay_ratio >= 0.8:
                action = "EXPEDITE_EXIT"
            elif decay_ratio >= 0.5:
                action = "PARTIAL_REDUCE"
            else:
                action = "HOLD"

        checkpoint = TimeDecayCheckpoint(
            checkpoint_index=i,
            elapsed_seconds=elapsed,
            decay_ratio=decay_ratio,
            scheduled_timestamp=cp_time,
            urgency_multiplier=urgency,
            suggested_action=action,
            remaining_quantity_pct=remaining_pct,
        )
        checkpoints_list.append(checkpoint)

    expiry_timestamp = entry_time + timedelta(seconds=config.max_holding_seconds)

    schedule_meta: dict[str, Any] = {
        "max_holding_seconds": config.max_holding_seconds,
        "half_life_seconds": config.half_life_seconds,
        "initial_quantity": total_quantity,
    }
    if metadata:
        schedule_meta.update(metadata)

    return TimeDecaySchedule(
        schedule_id=schedule_id,
        start_timestamp=entry_time,
        expiry_timestamp=expiry_timestamp,
        checkpoints=tuple(checkpoints_list),
        metadata=schedule_meta,
    )
