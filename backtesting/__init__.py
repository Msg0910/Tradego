"""
Tradego Backtesting & Historical Replay Subsystem.

Provides deterministic historical event ingestion, simulation clock,
replay controller, isolated backtest engine execution with PaperExecutionAdapter,
performance analytics, parameter sweep exploration, and structured reporting.
"""

from .clock import SimulationClock
from .dataset import HistoricalDataset
from .engine import BacktestEngine
from .metrics import PerformanceMetrics
from .models import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    PerformanceMetricsResult,
    ReplayState,
    SweepConfig,
    SweepResult,
    SweepRunResult,
    TradeRecord,
)
from .replay_controller import ReplayController
from .report import ReportGenerator
from .sweep import ParameterSweepRunner

__all__ = [
    "SimulationClock",
    "HistoricalDataset",
    "BacktestEngine",
    "PerformanceMetrics",
    "BacktestConfig",
    "BacktestResult",
    "EquityPoint",
    "PerformanceMetricsResult",
    "ReplayState",
    "SweepConfig",
    "SweepResult",
    "SweepRunResult",
    "TradeRecord",
    "ReplayController",
    "ReportGenerator",
    "ParameterSweepRunner",
]
