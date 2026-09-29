"""
Tradego Trading Strategies (Phase 5).

Exports the reference strategy:
- EMA_VWAP_TrendContinuationStrategy, TrendContinuationConfig
"""

from .trend_continuation import (
    EMA_VWAP_TrendContinuationStrategy,
    TrendContinuationConfig,
)

__all__ = [
    "EMA_VWAP_TrendContinuationStrategy",
    "TrendContinuationConfig",
]
