"""
Data models and contracts for the Tradego Backtesting & Historical Replay Subsystem.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Type

from services.execution.models import Fill, OrderSide
from services.market_state.instrument import InstrumentId
from services.signals.base import BaseStrategy


class ReplayState(str, Enum):
    """Lifecycle states of the Historical Replay Controller."""
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STEPPING = "STEPPING"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class BacktestConfig:
    """Configuration for an isolated backtest execution run."""
    symbol: str
    strategy_class: Type[BaseStrategy]
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    initial_cash: float = 1_000_000.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    slippage_bps: float = 2.0
    lot_size: int = 1
    tick_size: float = 0.05
    max_daily_loss: float = 100_000.0
    run_id_prefix: str = "BT"


@dataclass(frozen=True)
class EquityPoint:
    """Point-in-time portfolio equity snapshot."""
    timestamp: datetime
    cash: float
    unrealized_pnl: float
    realized_pnl: float
    total_equity: float
    drawdown_pct: float


@dataclass(frozen=True)
class TradeRecord:
    """Completed round-trip or position-reducing trade execution record."""
    trade_id: str
    instrument_id: InstrumentId
    side: OrderSide
    quantity: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    return_pct: float
    holding_duration_seconds: float
    strategy_id: str


@dataclass(frozen=True)
class PerformanceMetricsResult:
    """Calculated statistical and risk-adjusted performance metrics."""
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    max_drawdown_duration_seconds: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    win_rate: float
    profit_factor: float
    payoff_ratio: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    initial_cash: float
    final_equity: float
    total_realized_pnl: float


@dataclass
class BacktestResult:
    """Output artifact of an executed backtest run."""
    run_id: str
    config: BacktestConfig
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    metrics: PerformanceMetricsResult
    equity_curve: List[EquityPoint]
    trades: List[TradeRecord]
    fills: List[Fill]
    execution_duration_seconds: float = 0.0


@dataclass(frozen=True)
class SweepConfig:
    """Specification for a multi-run parameter grid sweep."""
    base_config: BacktestConfig
    param_grid: Dict[str, List[Any]]
    objective_metric: str = "sharpe_ratio"  # e.g., "sharpe_ratio", "total_return_pct", "profit_factor"


@dataclass
class SweepRunResult:
    """Individual run result within a parameter sweep."""
    params: Dict[str, Any]
    result: BacktestResult
    objective_value: float


@dataclass
class SweepResult:
    """Comprehensive outcome of a parameter optimization sweep."""
    sweep_id: str
    best_params: Dict[str, Any]
    best_result: BacktestResult
    all_runs: List[SweepRunResult]
