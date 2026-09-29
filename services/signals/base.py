"""
Tradego Strategy & Signal Intelligence Base Contracts (Phase 5).

Defines abstract base classes for regime classifiers, setup detectors,
scorers, and strategies enforcing the Phase 5 architecture.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, List, Mapping, Optional

from services.analytics.models import FeatureQuality
from .context import StrategyContext
from .models import (
    NO_TRADE_DEFAULT,
    NO_TRADE_INVALID_QUALITY,
    NO_TRADE_WARMING_UP,
    QualityPolicy,
    SignalCandidate,
    StrategyDecision,
    TriggerMode,
)


@dataclass(frozen=True, slots=True)
class SetupResult:
    """
    Immutable result emitted by a setup detector.
    setup_anchor_timestamp anchors the setup's continuity in time for reaffirmation hashing.
    """
    is_active: bool
    direction: int                      # +1 for Long, -1 for Short, 0 for None
    setup_anchor_timestamp: datetime
    confidence: float = 1.0             # Base setup conviction in [0.0, 1.0]
    suggested_entry_price: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(self.metadata))


class BaseRegimeClassifier(ABC):
    """Abstract evaluator for macro/instrument market regimes."""

    def __init__(self, name: str) -> None:
        self.regime_name = name

    @abstractmethod
    def classify(self, context: StrategyContext) -> Optional[str]:
        """
        Evaluates current market regime (e.g. "BULLISH", "BEARISH", "RANGING").
        Returns None if data is insufficient or regime is undefined.
        """
        ...


class BaseSetupDetector(ABC):
    """Abstract detector for structural trade patterns/setups."""

    def __init__(self, name: str) -> None:
        self.setup_name = name

    @abstractmethod
    def evaluate(self, context: StrategyContext, regime: Optional[str] = None) -> SetupResult:
        """
        Evaluates whether the setup is active given market context and active regime.
        Must deterministically provide setup_anchor_timestamp when active.
        """
        ...


class BaseScorer(ABC):
    """Abstract conviction and priority scorer."""

    def __init__(self, name: str) -> None:
        self.scorer_name = name

    @abstractmethod
    def score(self, candidate: SignalCandidate, context: StrategyContext) -> float:
        """Computes a normalized conviction score in [0.0, 1.0]."""
        ...


class BaseStrategy(ABC):
    """
    Abstract trading strategy contract.
    Coordinates required feature checks, quality policy enforcement,
    regime classification, setup detection, and signal candidate generation.
    """

    def __init__(
        self,
        strategy_id: str,
        strategy_version: str,
        config_hash: str,
        trigger_mode: TriggerMode = TriggerMode.BAR_CLOSE,
        quality_policy: QualityPolicy = QualityPolicy.STRICT,
        required_features: Optional[List[str]] = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self.config_hash = config_hash
        self.trigger_mode = trigger_mode
        self.quality_policy = quality_policy
        self.required_features = required_features or []

    def validate_features_and_trigger_mode(
        self, context: StrategyContext
    ) -> Optional[StrategyDecision]:
        """
        Validates TriggerMode alignment and FeatureQuality invariants.
        Returns a NO_TRADE StrategyDecision if validation fails, or None if evaluation can proceed.
        """
        # 1. TriggerMode alignment
        if self.trigger_mode == TriggerMode.BAR_CLOSE:
            # Active candle must be closed if available
            if context.active_candle is not None and not context.active_candle.is_closed:
                return NO_TRADE_DEFAULT

        # 2. Required feature checks
        for fid in self.required_features:
            fv = context.features.get(fid)
            if fv is None:
                return NO_TRADE_INVALID_QUALITY

            if self.trigger_mode == TriggerMode.BAR_CLOSE and not fv.is_confirmed:
                return NO_TRADE_DEFAULT

            if fv.value is None or fv.quality in (
                FeatureQuality.INVALID,
                FeatureQuality.STALE,
            ):
                return NO_TRADE_INVALID_QUALITY

            if fv.quality == FeatureQuality.WARMING_UP:
                return NO_TRADE_WARMING_UP

            if fv.quality == FeatureQuality.DEGRADED:
                if self.quality_policy == QualityPolicy.STRICT:
                    return NO_TRADE_INVALID_QUALITY

        return None

    def has_degraded_features(self, context: StrategyContext) -> bool:
        """Returns True if any required feature is DEGRADED."""
        for fid in self.required_features:
            fv = context.features.get(fid)
            if fv is not None and fv.quality == FeatureQuality.DEGRADED:
                return True
        return False

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        """
        Evaluates strategy logic on StrategyContext.
        Returns typed StrategyDecision (TRADE with candidate, or NO_TRADE).
        """
        ...
