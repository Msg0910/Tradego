"""
Tradego Strategy & Signal Intelligence Models (Phase 5).

Defines immutable data models for signal candidates, strategy decisions,
read-only position views, trigger modes, and quality policies.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from services.market_state.instrument import InstrumentId


class TriggerMode(str, Enum):
    """Execution trigger mode for setups and strategies."""
    BAR_CLOSE = "BAR_CLOSE"              # Evaluated strictly on official closed candles
    INTRABAR_PREVIEW = "INTRABAR_PREVIEW"# Evaluated on active forming candles or hot ticks


class SignalType(str, Enum):
    """Action intent emitted by a strategy."""
    ENTRY_LONG = "ENTRY_LONG"
    ENTRY_SHORT = "ENTRY_SHORT"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"
    SCALE_IN = "SCALE_IN"
    SCALE_OUT = "SCALE_OUT"


class DecisionType(str, Enum):
    """High-level outcome of a strategy evaluation cycle."""
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"


class QualityPolicy(str, Enum):
    """Policy governing handling of degraded feature quality."""
    STRICT = "STRICT"          # Requires FeatureQuality.VALID; rejects DEGRADED
    PERMISSIVE = "PERMISSIVE"  # Allows FeatureQuality.DEGRADED with confidence penalty


@dataclass(frozen=True, slots=True)
class PositionView:
    """
    Immutable, read-only snapshot of an instrument's current open position.
    Supplied by the caller (portfolio/risk engine or backtest driver).
    Phase 5 strategies may read this view but must NEVER modify or manage it.
    """
    instrument_id: InstrumentId
    net_quantity: int                    # Positive = Long, Negative = Short, 0 = Flat
    entry_price: float                   # Average entry fill price
    entry_time: datetime                 # Timestamp when position was opened
    highest_price_since_entry: float     # High water mark (for trailing long reference)
    lowest_price_since_entry: float      # Low water mark (for trailing short reference)
    unrealized_pnl_estimate: Optional[float] = None

    @property
    def is_flat(self) -> bool:
        return self.net_quantity == 0

    @property
    def is_long(self) -> bool:
        return self.net_quantity > 0

    @property
    def is_short(self) -> bool:
        return self.net_quantity < 0


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    """
    Immutable representation of an actionable strategy signal.
    Strictly contains signal intent and geometry; contains ZERO broker,
    order execution, lot sizing, or capital allocation parameters.
    """
    # 1. Identity & Provenance
    signal_id: str                      # Unique instance ID (UUIDv4)
    fingerprint: str                    # Canonical content hash of semantic payload
    reaffirmation_key: str              # Setup continuity key (canonical JSON hash)
    strategy_id: str                    # Canonical strategy identifier
    strategy_version: str               # Semantic version string (e.g. "1.0.0")
    config_hash: str                    # SHA-256 hash of immutable StrategyConfig

    # 2. Instrument & Intent
    instrument_id: InstrumentId         # Canonical InstrumentId
    signal_type: SignalType             # Action intent (ENTRY_LONG, etc.)
    direction: int                      # +1 for Long, -1 for Short
    trigger_mode: TriggerMode           # BAR_CLOSE or INTRABAR_PREVIEW

    # 3. Conviction & Suggested Geometry
    confidence_score: float             # Normalized conviction in [0.0, 1.0]
    suggested_entry_price: Optional[float]
    suggested_stop_loss: Optional[float]
    suggested_take_profit: Optional[float]
    risk_reward_ratio: Optional[float]

    # 4. Timestamps & Horizons
    market_timestamp: datetime          # observation_timestamp of triggering data
    availability_timestamp: datetime    # availability_timestamp of triggering data
    generated_timestamp: datetime       # System evaluation timestamp

    priority: int = 0                   # Priority tier (lower = higher priority)
    expiry_timestamp: Optional[datetime] = None  # Deadline after which signal is stale
    time_in_force_seconds: Optional[float] = None
    is_confirmed: bool = True           # True if triggered on confirmed bar

    # 5. Diagnostic Context
    regime: str = "UNKNOWN"             # Name of active regime
    setup: str = "UNKNOWN"              # Name of active setup
    metadata: Optional[Mapping[str, Any]] = None  # Read-only audit details

    def __post_init__(self) -> None:
        # Enforce Timestamp Invariants
        if self.availability_timestamp < self.market_timestamp:
            raise ValueError(
                f"Temporal Monotonicity violation: availability_timestamp ({self.availability_timestamp}) "
                f"< market_timestamp ({self.market_timestamp})"
            )
        if self.generated_timestamp < self.availability_timestamp:
            raise ValueError(
                f"Execution Causality violation: generated_timestamp ({self.generated_timestamp}) "
                f"< availability_timestamp ({self.availability_timestamp})"
            )
        if self.expiry_timestamp is not None and self.generated_timestamp >= self.expiry_timestamp:
            raise ValueError(
                f"Signal Expiry Monotonicity violation: generated_timestamp ({self.generated_timestamp}) "
                f">= expiry_timestamp ({self.expiry_timestamp})"
            )

        # Enforce immutable metadata mapping if a mutable dict was provided
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(self.metadata))


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """
    Typed, allocation-conscious evaluation result of a strategy cycle.
    """
    decision: DecisionType
    candidate: Optional[SignalCandidate] = None
    reason: Optional[str] = None


# Preallocated NO_TRADE singletons for critical-path allocation avoidance
NO_TRADE_DEFAULT = StrategyDecision(decision=DecisionType.NO_TRADE, candidate=None, reason="NO_ACTION")
NO_TRADE_WARMING_UP = StrategyDecision(decision=DecisionType.NO_TRADE, candidate=None, reason="WARMING_UP")
NO_TRADE_INVALID_QUALITY = StrategyDecision(decision=DecisionType.NO_TRADE, candidate=None, reason="INVALID_QUALITY")
NO_TRADE_REGIME_FILTER = StrategyDecision(decision=DecisionType.NO_TRADE, candidate=None, reason="REGIME_FILTER")
NO_TRADE_NO_SETUP = StrategyDecision(decision=DecisionType.NO_TRADE, candidate=None, reason="NO_SETUP")
