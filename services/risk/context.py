"""
Tradego Risk Context and Account State Models (Phase 6).

Defines immutable data contracts for environmental evaluation context,
account equity, and portfolio exposure snapshots.
"""

from dataclasses import dataclass
from datetime import datetime

from services.analytics.models import FeatureQuality
from services.market_state.instrument import InstrumentMetadata
from .models import PortfolioSnapshot


@dataclass(frozen=True, slots=True)
class AccountRiskState:
    """
    Immutable point-in-time view of account equity, cash, and PnL metrics.
    """
    account_id: str
    total_equity: float                  # NAV: cash + unrealized PnL of open positions
    available_cash: float                # Free uncommitted cash balance
    peak_equity: float                   # High water mark (for drawdown evaluation)
    realized_pnl_today: float            # Closed PnL accumulated today
    unrealized_pnl_current: float        # Mark-to-market PnL of all open positions
    last_updated_timestamp: datetime
    currency: str = "INR"                # Base account currency

    @property
    def total_pnl_today(self) -> float:
        """Total closed + open PnL accumulated today."""
        return self.realized_pnl_today + self.unrealized_pnl_current

    @property
    def drawdown_pct(self) -> float:
        """Peak-to-trough equity drawdown percentage."""
        if self.peak_equity <= 0.0:
            return 0.0
        return max(0.0, (self.peak_equity - self.total_equity) / self.peak_equity)


@dataclass(frozen=True, slots=True)
class RiskContext:
    """
    Immutable point-in-time contextual view supplied to the RiskEngine.
    evaluation_timestamp MUST be explicitly supplied by the caller:
    - Live trading: Event/clock timestamp.
    - Backtesting: Historical simulation clock.
    """
    account_state: AccountRiskState
    portfolio_snapshot: PortfolioSnapshot
    instrument_metadata: InstrumentMetadata
    evaluation_timestamp: datetime
    current_market_price: float
    feature_quality: FeatureQuality = FeatureQuality.VALID

    def __post_init__(self) -> None:
        if self.portfolio_snapshot is None:
            raise ValueError("RiskContext requires a valid, non-null PortfolioSnapshot.")
        if self.account_state is None:
            raise ValueError("RiskContext requires a valid, non-null AccountRiskState.")
        if self.instrument_metadata is None:
            raise ValueError("RiskContext requires a valid, non-null InstrumentMetadata.")
