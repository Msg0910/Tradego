"""
Tradego Trading Runtime Configuration (Phase 8).

Defines immutable runtime parameters, mode validation, buffer capacities,
and safety thresholds. Structurally prohibits LIVE mode in v1.
"""

from dataclasses import dataclass
from typing import Tuple

from .models import RuntimeMode


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """
    Authoritative configuration for the Phase 8 trading runtime.
    """
    runtime_mode: RuntimeMode = RuntimeMode.PAPER
    active_symbols: Tuple[str, ...] = ()
    max_daily_loss: float = 50000.0
    feed_stagnation_timeout_ms: float = 5000.0
    telemetry_ring_buffer_size: int = 65536
    shutdown_drain_timeout_ms: float = 2000.0
    min_intrabar_eval_interval_ms: float = 100.0
    max_exception_burst_count: int = 5
    recovery_token: str = "ADMIN_CONFIRM_RECOVERY"

    def __post_init__(self) -> None:
        if self.runtime_mode == RuntimeMode.LIVE:
            raise RuntimeError(
                "SECURITY LOCKOUT: LIVE trading mode is structurally disabled in Phase 8 v1. "
                "Only DEVELOPMENT, PAPER, and SHADOW modes are permitted."
            )
        if self.telemetry_ring_buffer_size <= 0:
            raise ValueError("telemetry_ring_buffer_size must be positive.")
        if self.max_daily_loss <= 0.0:
            raise ValueError("max_daily_loss must be positive.")
        if self.min_intrabar_eval_interval_ms < 0.0:
            raise ValueError("min_intrabar_eval_interval_ms cannot be negative.")
