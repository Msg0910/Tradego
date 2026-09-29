"""
Tradego Risk Management & Portfolio Control Models (Phase 6).

Defines immutable data contracts for risk decisions, approved trade intents,
risk rejections, position snapshots, portfolio snapshots, and rejection reasons.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from services.market_state.instrument import InstrumentId
from services.signals.models import SignalType


class RiskDecisionType(str, Enum):
    """Authoritative outcome of a Phase 6 risk evaluation."""
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RiskRejectionReason(str, Enum):
    """Exhaustive, deterministic taxonomy of risk rejection reasons."""
    # Asset Class & Eligibility (Common)
    UNSUPPORTED_INSTRUMENT_TYPE = "UNSUPPORTED_INSTRUMENT_TYPE"

    # Temporal & Signal Validity (Common)
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_STALE = "SIGNAL_STALE"
    SIGNAL_FUTURE_DATED = "SIGNAL_FUTURE_DATED"
    INVALID_TIMESTAMPS = "INVALID_TIMESTAMPS"
    DEGRADED_FEATURE_QUALITY = "DEGRADED_FEATURE_QUALITY"
    INVALID_SIGNAL = "INVALID_SIGNAL"

    # Geometry & Stop Loss (Entry Path Only)
    INVALID_STOP_DIRECTION = "INVALID_STOP_DIRECTION"
    ZERO_STOP_DISTANCE = "ZERO_STOP_DISTANCE"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    INSUFFICIENT_RISK_REWARD = "INSUFFICIENT_RISK_REWARD"

    # Account & Portfolio Constraints (Entry Path Only)
    MISSING_PORTFOLIO_STATE = "MISSING_PORTFOLIO_STATE"
    ACCOUNT_EQUITY_NON_POSITIVE = "ACCOUNT_EQUITY_NON_POSITIVE"
    MAX_DAILY_LOSS_EXCEEDED = "MAX_DAILY_LOSS_EXCEEDED"
    MAX_DRAWDOWN_EXCEEDED = "MAX_DRAWDOWN_EXCEEDED"
    MAX_CONCURRENT_POSITIONS_REACHED = "MAX_CONCURRENT_POSITIONS_REACHED"
    GROSS_LEVERAGE_EXCEEDED = "GROSS_LEVERAGE_EXCEEDED"
    NET_LEVERAGE_EXCEEDED = "NET_LEVERAGE_EXCEEDED"
    INSTRUMENT_EXPOSURE_EXCEEDED = "INSTRUMENT_EXPOSURE_EXCEEDED"
    STRATEGY_RISK_BUDGET_EXCEEDED = "STRATEGY_RISK_BUDGET_EXCEEDED"

    # Position Conflict & Deduplication (Entry Path Only)
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    CONFLICTING_POSITION = "CONFLICTING_POSITION"
    MAX_PYRAMIDING_EXCEEDED = "MAX_PYRAMIDING_EXCEEDED"

    # Sizing & Capital (Entry Path Only)
    INSUFFICIENT_CAPITAL_FOR_MIN_LOT = "INSUFFICIENT_CAPITAL_FOR_MIN_LOT"
    INSUFFICIENT_AVAILABLE_CASH = "INSUFFICIENT_AVAILABLE_CASH"
    ZERO_PERMITTED_QUANTITY = "ZERO_PERMITTED_QUANTITY"

    # Exit Path Specific
    NO_POSITION_TO_EXIT = "NO_POSITION_TO_EXIT"
    OPPOSING_EXIT_DIRECTION = "OPPOSING_EXIT_DIRECTION"
    ZERO_EXIT_QUANTITY = "ZERO_EXIT_QUANTITY"


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """
    Immutable, point-in-time view of an instrument's open position.
    Supplied by the portfolio accounting authority.
    """
    instrument_id: InstrumentId
    net_quantity: int                    # Positive = Long, Negative = Short, 0 = Flat
    average_entry_price: float           # Average entry fill price
    current_market_price: float          # Mark price (LTP or midpoint)
    unrealized_pnl: float                # (current_price - entry_price) * net_qty
    realized_pnl: float                  # Cumulative closed PnL for this position
    opened_timestamp: datetime           # Timestamp when position was opened
    last_updated_timestamp: datetime    # Timestamp of latest fill or mark update
    strategy_id: Optional[str] = None    # Strategy attribution

    @property
    def is_flat(self) -> bool:
        return self.net_quantity == 0

    @property
    def is_long(self) -> bool:
        return self.net_quantity > 0

    @property
    def is_short(self) -> bool:
        return self.net_quantity < 0

    @property
    def market_value(self) -> float:
        """Gross monetary cash-equity value of position."""
        return abs(self.net_quantity) * self.current_market_price

    @property
    def directional_exposure(self) -> float:
        """Signed monetary cash-equity exposure (+ for long, - for short)."""
        return self.net_quantity * self.current_market_price


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """
    Immutable, point-in-time collection of all open positions in the portfolio.
    """
    snapshot_timestamp: datetime
    positions: Mapping[InstrumentId, PositionSnapshot]

    def __post_init__(self) -> None:
        if isinstance(self.positions, dict):
            object.__setattr__(self, "positions", MappingProxyType(self.positions))

    @property
    def open_positions_count(self) -> int:
        return sum(1 for pos in self.positions.values() if not pos.is_flat)

    @property
    def total_gross_exposure(self) -> float:
        return sum(pos.market_value for pos in self.positions.values())

    @property
    def total_net_exposure(self) -> float:
        return sum(pos.directional_exposure for pos in self.positions.values())

    def get_position(self, instrument_id: InstrumentId) -> Optional[PositionSnapshot]:
        return self.positions.get(instrument_id)

    def get_strategy_exposure(self, strategy_id: str) -> float:
        return sum(
            pos.market_value
            for pos in self.positions.values()
            if pos.strategy_id == strategy_id and not pos.is_flat
        )


@dataclass(frozen=True, slots=True)
class PositionSizingResult:
    """
    Result emitted by the deterministic position sizer.
    """
    permitted_quantity: int              # Integer multiple of lot_size (> 0)
    calculated_monetary_risk: float      # Actual currency at risk: Q * |entry - stop|
    allocated_capital: float             # Actual capital committed: Q * entry
    unrounded_quantity: float            # Raw theoretical quantity before lot floor
    lot_size: int                        # Instrument lot size applied
    binding_constraint: str              # Constraint that dictated final size


@dataclass(frozen=True, slots=True)
class ApprovedTradeIntent:
    """
    Authoritative, immutable trade intent emitted by Phase 6 to Phase 7 Execution.
    Specifies exact permitted quantity and geometry; contains ZERO broker/order IDs.
    """
    # 1. Non-Semantic Runtime Identifiers (Excluded from Deterministic Equivalence)
    intent_id: str                       # UUIDv4 runtime instance token
    intent_generated_timestamp: datetime # System timestamp when intent was instantiated

    # 2. Semantic Provenance & Identification
    signal_id: str                       # Upstream SignalCandidate.signal_id
    fingerprint: str                     # SignalCandidate.fingerprint
    reaffirmation_key: str               # SignalCandidate.reaffirmation_key
    strategy_id: str
    strategy_version: str
    risk_config_version: str
    risk_config_hash: str
    instrument_id: InstrumentId

    # 3. Approved Trading Intent & Geometry
    signal_type: SignalType
    direction: int                       # +1 Long, -1 Short
    permitted_quantity: int              # Strictly > 0, multiple of lot_size
    approved_entry_price: float
    approved_stop_loss: Optional[float]  # None for unconstrained market exits
    approved_take_profit: Optional[float]
    risk_reward_ratio: Optional[float]
    calculated_monetary_risk: float      # 0.0 for risk-reducing exits
    allocated_capital: float             # 0.0 for exits
    binding_constraint: str              # Rule that bound position size

    # 4. Temporal Horizons & Diagnostics
    market_timestamp: datetime           # From upstream signal
    signal_generated_timestamp: datetime # From upstream signal
    expiry_timestamp: Optional[datetime] = None  # Intent execution deadline
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.permitted_quantity <= 0:
            raise ValueError(
                f"ApprovedTradeIntent permitted_quantity must be positive, got {self.permitted_quantity}"
            )
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(self.metadata))

    def is_semantically_equivalent(self, other: Any) -> bool:
        """
        Evaluates deterministic mathematical equivalence.
        Explicitly excludes runtime instance fields (intent_id, intent_generated_timestamp).
        """
        if not isinstance(other, ApprovedTradeIntent):
            return False
        return (
            self.signal_id == other.signal_id
            and self.fingerprint == other.fingerprint
            and self.reaffirmation_key == other.reaffirmation_key
            and self.strategy_id == other.strategy_id
            and self.strategy_version == other.strategy_version
            and self.risk_config_version == other.risk_config_version
            and self.risk_config_hash == other.risk_config_hash
            and self.instrument_id == other.instrument_id
            and self.signal_type == other.signal_type
            and self.direction == other.direction
            and self.permitted_quantity == other.permitted_quantity
            and self.approved_entry_price == other.approved_entry_price
            and self.approved_stop_loss == other.approved_stop_loss
            and self.approved_take_profit == other.approved_take_profit
            and self.risk_reward_ratio == other.risk_reward_ratio
            and self.calculated_monetary_risk == other.calculated_monetary_risk
            and self.allocated_capital == other.allocated_capital
            and self.binding_constraint == other.binding_constraint
            and self.market_timestamp == other.market_timestamp
            and self.signal_generated_timestamp == other.signal_generated_timestamp
            and self.expiry_timestamp == other.expiry_timestamp
        )


@dataclass(frozen=True, slots=True)
class RiskRejection:
    """
    Immutable audit record emitted when a candidate signal is vetoed.
    """
    signal_id: str
    fingerprint: str
    strategy_id: str
    instrument_id: InstrumentId
    reason_code: RiskRejectionReason
    violating_rule: str
    message: str
    evaluation_timestamp: datetime
    diagnostic_data: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.diagnostic_data is not None and isinstance(self.diagnostic_data, dict):
            object.__setattr__(self, "diagnostic_data", MappingProxyType(self.diagnostic_data))


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """
    Authoritative decision envelope returned by RiskEngine.evaluate().
    """
    decision: RiskDecisionType
    approved_intent: Optional[ApprovedTradeIntent] = None
    rejection: Optional[RiskRejection] = None

    def __post_init__(self) -> None:
        if self.decision == RiskDecisionType.APPROVED and self.approved_intent is None:
            raise ValueError("RiskDecision.APPROVED must include an approved_intent.")
        if self.decision == RiskDecisionType.REJECTED and self.rejection is None:
            raise ValueError("RiskDecision.REJECTED must include a rejection.")
