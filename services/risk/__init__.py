"""
Tradego Risk Management & Portfolio Control (Phase 6).

Provides deterministic, immutable risk evaluation, portfolio limits,
cash-equity position sizing, and handoff contracts (ApprovedTradeIntent, RiskRejection).
"""

from .context import AccountRiskState, RiskContext
from .engine import AdmissionState, RiskEngine
from .limits import RiskLimits, compute_risk_config_hash
from .models import (
    ApprovedTradeIntent,
    PortfolioSnapshot,
    PositionSizingResult,
    PositionSnapshot,
    RiskDecision,
    RiskDecisionType,
    RiskRejection,
    RiskRejectionReason,
)
from .position_sizing import (
    CapitalRequirementCalculator,
    CashEquityPositionSizer,
    CashNotionalCapitalCalculator,
)
from .validators import (
    validate_entry_gates,
    validate_entry_post_sizing,
    validate_exit_gates,
    validate_instrument_eligibility,
    validate_temporal_and_quality,
)

__all__ = [
    # Models
    "RiskDecisionType",
    "RiskRejectionReason",
    "PositionSnapshot",
    "PortfolioSnapshot",
    "PositionSizingResult",
    "ApprovedTradeIntent",
    "RiskRejection",
    "RiskDecision",
    # Context
    "AccountRiskState",
    "RiskContext",
    # Limits
    "RiskLimits",
    "compute_risk_config_hash",
    # Position Sizing
    "CapitalRequirementCalculator",
    "CashNotionalCapitalCalculator",
    "CashEquityPositionSizer",
    # Validators
    "validate_instrument_eligibility",
    "validate_temporal_and_quality",
    "validate_entry_gates",
    "validate_entry_post_sizing",
    "validate_exit_gates",
    # Engine & Admission
    "AdmissionState",
    "RiskEngine",
]
