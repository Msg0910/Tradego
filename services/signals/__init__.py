"""
Tradego Strategy & Signal Intelligence Layer (Phase 5).

Public Package Exports:
- Models:
    TriggerMode, SignalType, DecisionType, QualityPolicy,
    PositionView, SignalCandidate, StrategyDecision,
    NO_TRADE_DEFAULT, NO_TRADE_WARMING_UP, NO_TRADE_INVALID_QUALITY,
    NO_TRADE_REGIME_FILTER, NO_TRADE_NO_SETUP
- Context:
    StrategyContext
- Base Contracts:
    BaseRegimeClassifier, BaseSetupDetector, SetupResult, BaseScorer, BaseStrategy
- Fingerprinting:
    compute_signal_fingerprint, compute_reaffirmation_key, compute_config_hash, serialize_instrument_id
- Regimes:
    EMABasedTrendRegime, ATRVolatilityRegime
- Setups:
    EMAVWAPPullbackSetup, RangeBreakoutSetup
- Scoring & Arbiter:
    DeterministicSignalArbiter, RegimeAlignmentScorer, RiskRewardScorer
- Engine:
    SignalEngine, SignalDispatcher, SyncSignalDispatcher
"""

from .base import (
    BaseRegimeClassifier,
    BaseScorer,
    BaseSetupDetector,
    BaseStrategy,
    SetupResult,
)
from .context import StrategyContext
from .engine import SignalDispatcher, SignalEngine, SyncSignalDispatcher
from .fingerprint import (
    compute_config_hash,
    compute_reaffirmation_key,
    compute_signal_fingerprint,
    serialize_instrument_id,
)
from .models import (
    NO_TRADE_DEFAULT,
    NO_TRADE_INVALID_QUALITY,
    NO_TRADE_NO_SETUP,
    NO_TRADE_REGIME_FILTER,
    NO_TRADE_WARMING_UP,
    DecisionType,
    PositionView,
    QualityPolicy,
    SignalCandidate,
    SignalType,
    StrategyDecision,
    TriggerMode,
)
from .regimes import ATRVolatilityRegime, EMABasedTrendRegime
from .scoring import (
    DeterministicSignalArbiter,
    RegimeAlignmentScorer,
    RiskRewardScorer,
)
from .setups import EMAVWAPPullbackSetup, RangeBreakoutSetup

__all__ = [
    # Models
    "TriggerMode",
    "SignalType",
    "DecisionType",
    "QualityPolicy",
    "PositionView",
    "SignalCandidate",
    "StrategyDecision",
    "NO_TRADE_DEFAULT",
    "NO_TRADE_WARMING_UP",
    "NO_TRADE_INVALID_QUALITY",
    "NO_TRADE_REGIME_FILTER",
    "NO_TRADE_NO_SETUP",
    # Context
    "StrategyContext",
    # Base Contracts
    "BaseRegimeClassifier",
    "BaseSetupDetector",
    "SetupResult",
    "BaseScorer",
    "BaseStrategy",
    # Fingerprinting
    "compute_signal_fingerprint",
    "compute_reaffirmation_key",
    "compute_config_hash",
    "serialize_instrument_id",
    # Regimes
    "EMABasedTrendRegime",
    "ATRVolatilityRegime",
    # Setups
    "EMAVWAPPullbackSetup",
    "RangeBreakoutSetup",
    # Scoring & Arbiter
    "DeterministicSignalArbiter",
    "RegimeAlignmentScorer",
    "RiskRewardScorer",
    # Engine
    "SignalEngine",
    "SignalDispatcher",
    "SyncSignalDispatcher",
]
