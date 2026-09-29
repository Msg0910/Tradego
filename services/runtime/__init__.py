"""
Tradego Trading Runtime Package (Phase 8).

Public package exports for Tradego runtime orchestration, state machine,
guard/kill-switch, portfolio tracking, and telemetry components.
"""

from .config import RuntimeConfig
from .coordinator import PipelineCoordinator
from .execution_coordinator import ExecutionCoordinator
from .guards import TradingGuard
from .health import HealthMonitor
from .lifecycle import RuntimeLifecycleManager
from .models import (
    ComponentHeartbeatRecord,
    GuardState,
    GuardTripReason,
    RuntimeCorrelationRecord,
    RuntimeMode,
    RuntimeState,
)
from .portfolio import PortfolioRuntimeState
from .scheduler import StrategyScheduler
from .signal_risk_coordinator import SignalRiskCoordinator
from .telemetry import TelemetryCollector

__all__ = [
    "ComponentHeartbeatRecord",
    "ExecutionCoordinator",
    "GuardState",
    "GuardTripReason",
    "HealthMonitor",
    "PipelineCoordinator",
    "PortfolioRuntimeState",
    "RuntimeConfig",
    "RuntimeCorrelationRecord",
    "RuntimeLifecycleManager",
    "RuntimeMode",
    "RuntimeState",
    "SignalRiskCoordinator",
    "StrategyScheduler",
    "TelemetryCollector",
    "TradingGuard",
]
