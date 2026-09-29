"""
Tradego Phase 7 Execution Layer — Execution Configuration.

Defines the slotted frozen ExecutionConfig dataclass governing execution parameters,
notional limits, rate limiter buffers, retry horizons, and paper trading slippage.
"""

from dataclasses import dataclass
from services.execution.models import TimeInForce


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Immutable execution layer configuration."""
    config_version: str = "1.2.0"
    execution_mode: str = "PAPER"        # "PAPER" or "LIVE"
    default_time_in_force: TimeInForce = TimeInForce.DAY
    max_order_notional: float = 200000.0 # Configurable notional ceiling per order (₹2 Lakhs default)
    max_submission_retries: int = 2
    retry_base_delay_ms: float = 50.0
    submission_timeout_seconds: float = 5.0
    rate_limit_max_queue_depth: int = 50
    rate_limit_max_wait_ms: float = 500.0
    paper_base_slippage_bps: float = 2.0
    enable_reconciliation_on_start: bool = True
    reconciliation_grace_period_seconds: float = 5.0
