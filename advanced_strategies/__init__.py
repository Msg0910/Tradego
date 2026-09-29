"""
TradeGo Phase 11 — Advanced Strategies Package.

Foundation models and algorithmic execution schedules.
"""

from advanced_strategies.models import (
    ExecutionSchedule,
    ExecutionSlice,
    ScheduleType,
    TimeDecayCheckpoint,
    TimeDecayExitConfig,
    TimeDecaySchedule,
    TWAPScheduleConfig,
)
from advanced_strategies.regimes import (
    CompositeRegimeResult,
    MarketRegimeState,
    MultiTimeframeCompositeRegimeClassifier,
    MultiTimeframeRegimeConfig,
)
from advanced_strategies.mean_reversion import (
    StatisticalMeanReversionConfig,
    StatisticalMeanReversionStrategy,
)
from advanced_strategies.multi_timeframe_trend import (
    MultiTimeframeTrendConfig,
    MultiTimeframeTrendContinuationStrategy,
)
from advanced_strategies.range_breakout import (
    RangeBreakoutConfig,
    RangeBreakoutStrategy,
)
from advanced_strategies.backtest import (
    create_indicator_from_feature_id,
    register_strategy_indicators,
    run_advanced_strategy_backtest,
)
from advanced_strategies.schedules.algorithmic import (
    generate_time_decay_schedule,
    generate_twap_schedule,
)

__all__ = [
    "ExecutionSchedule",
    "ExecutionSlice",
    "ScheduleType",
    "TimeDecayCheckpoint",
    "TimeDecayExitConfig",
    "TimeDecaySchedule",
    "TWAPScheduleConfig",
    "generate_twap_schedule",
    "generate_time_decay_schedule",
    "CompositeRegimeResult",
    "MarketRegimeState",
    "MultiTimeframeCompositeRegimeClassifier",
    "MultiTimeframeRegimeConfig",
    "MultiTimeframeTrendConfig",
    "MultiTimeframeTrendContinuationStrategy",
    "RangeBreakoutConfig",
    "RangeBreakoutStrategy",
    "StatisticalMeanReversionConfig",
    "StatisticalMeanReversionStrategy",
    "create_indicator_from_feature_id",
    "register_strategy_indicators",
    "run_advanced_strategy_backtest",
]
