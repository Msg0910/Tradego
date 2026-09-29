"""
Tradego Risk Validators (Phase 6).

Implements pure functional validation checks for:
- Asset class & eligibility
- Temporal, causality, and feature quality invariants
- Entry path pre-sizing and post-sizing risk gates
- Exit path position existence and direction alignment
"""

import math
from typing import Optional, Tuple

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentType
from services.signals.models import SignalCandidate, SignalType
from .context import RiskContext
from .limits import RiskLimits
from .models import PositionSizingResult, RiskRejectionReason


def validate_instrument_eligibility(signal: SignalCandidate) -> Optional[RiskRejectionReason]:
    """
    Validates asset class eligibility for Phase 6 v1:
    Strictly supports Indian cash equities (Exchange.NSE, Exchange.BSE and InstrumentType.EQUITY).
    Futures, Options, Commodities, and Currencies are rejected as unsupported.
    """
    iid = signal.instrument_id
    if iid.instrument_type != InstrumentType.EQUITY:
        return RiskRejectionReason.UNSUPPORTED_INSTRUMENT_TYPE

    if iid.exchange not in (Exchange.NSE, Exchange.BSE):
        return RiskRejectionReason.UNSUPPORTED_INSTRUMENT_TYPE

    return None


def validate_temporal_and_quality(
    signal: SignalCandidate,
    context: RiskContext,
    limits: RiskLimits,
) -> Optional[RiskRejectionReason]:
    """
    Validates timestamp ordering, causality against evaluation clock, staleness,
    signal expiry, and aggregated feature quality.
    """
    # 1. Numerical sanity check
    entry_p = signal.suggested_entry_price or context.current_market_price
    stop_p = signal.suggested_stop_loss
    if math.isnan(entry_p) or math.isinf(entry_p) or entry_p <= 0.0:
        return RiskRejectionReason.INVALID_SIGNAL
    if stop_p is not None and (math.isnan(stop_p) or math.isinf(stop_p)):
        return RiskRejectionReason.INVALID_SIGNAL

    # 2. Monotonicity checks
    if signal.availability_timestamp < signal.market_timestamp:
        return RiskRejectionReason.INVALID_TIMESTAMPS
    if signal.generated_timestamp < signal.availability_timestamp:
        return RiskRejectionReason.INVALID_TIMESTAMPS

    # 3. Causality check against evaluation clock
    if signal.generated_timestamp > context.evaluation_timestamp:
        return RiskRejectionReason.SIGNAL_FUTURE_DATED

    # 4. Expiry check
    if (
        signal.expiry_timestamp is not None
        and context.evaluation_timestamp >= signal.expiry_timestamp
    ):
        return RiskRejectionReason.SIGNAL_EXPIRED

    # 5. Staleness check
    age_seconds = (context.evaluation_timestamp - signal.availability_timestamp).total_seconds()
    if age_seconds > limits.max_signal_age_seconds:
        return RiskRejectionReason.SIGNAL_STALE

    # 6. Aggregated Feature Quality check
    fq = context.feature_quality
    if fq in (FeatureQuality.WARMING_UP, FeatureQuality.STALE, FeatureQuality.INVALID):
        return RiskRejectionReason.DEGRADED_FEATURE_QUALITY
    if fq == FeatureQuality.DEGRADED and not limits.allow_degraded_features:
        return RiskRejectionReason.DEGRADED_FEATURE_QUALITY

    return None


def validate_entry_gates(
    signal: SignalCandidate,
    context: RiskContext,
    limits: RiskLimits,
) -> Optional[RiskRejectionReason]:
    """
    Evaluates pre-sizing entry gates: account health, position conflicts,
    and stop geometry bounds.
    """
    equity = context.account_state.total_equity
    if equity <= 0.0 or math.isnan(equity) or math.isinf(equity):
        return RiskRejectionReason.ACCOUNT_EQUITY_NON_POSITIVE

    # 1. Macro Account Health
    if context.account_state.total_pnl_today <= -(limits.max_daily_loss_pct * equity):
        return RiskRejectionReason.MAX_DAILY_LOSS_EXCEEDED

    if context.account_state.drawdown_pct >= limits.max_drawdown_pct:
        return RiskRejectionReason.MAX_DRAWDOWN_EXCEEDED

    if context.portfolio_snapshot.open_positions_count >= limits.max_concurrent_positions:
        return RiskRejectionReason.MAX_CONCURRENT_POSITIONS_REACHED

    if context.account_state.available_cash <= 0.0:
        return RiskRejectionReason.INSUFFICIENT_AVAILABLE_CASH

    # 2. Position Conflict Check
    pos = context.portfolio_snapshot.get_position(signal.instrument_id)
    if pos is not None and not pos.is_flat:
        if signal.direction > 0 and pos.is_short:
            return RiskRejectionReason.CONFLICTING_POSITION
        if signal.direction < 0 and pos.is_long:
            return RiskRejectionReason.CONFLICTING_POSITION

    # 3. Stop Geometry Check
    entry_p = signal.suggested_entry_price or context.current_market_price
    stop_p = signal.suggested_stop_loss
    if stop_p is None:
        return RiskRejectionReason.ZERO_STOP_DISTANCE

    if signal.direction > 0 and stop_p >= entry_p:
        return RiskRejectionReason.INVALID_STOP_DIRECTION
    if signal.direction < 0 and stop_p <= entry_p:
        return RiskRejectionReason.INVALID_STOP_DIRECTION

    stop_distance = abs(entry_p - stop_p)
    if stop_distance <= 0.0:
        return RiskRejectionReason.ZERO_STOP_DISTANCE

    stop_bps = (stop_distance / entry_p) * 10000.0
    if stop_bps < limits.min_stop_distance_bps:
        return RiskRejectionReason.STOP_TOO_TIGHT
    if stop_bps > limits.max_stop_distance_bps:
        return RiskRejectionReason.STOP_TOO_WIDE

    if (
        signal.risk_reward_ratio is not None
        and signal.risk_reward_ratio < limits.min_risk_reward_ratio
    ):
        return RiskRejectionReason.INSUFFICIENT_RISK_REWARD

    return None


def validate_entry_post_sizing(
    sizing_result: PositionSizingResult,
    signal: SignalCandidate,
    context: RiskContext,
    limits: RiskLimits,
) -> Optional[RiskRejectionReason]:
    """
    Evaluates post-sizing entry gates: discrete lot compliance,
    gross leverage, net leverage, and single-instrument exposure.
    """
    lot_size = max(1, context.instrument_metadata.lot_size)
    if sizing_result.permitted_quantity < lot_size:
        return RiskRejectionReason.INSUFFICIENT_CAPITAL_FOR_MIN_LOT

    equity = context.account_state.total_equity
    new_capital = sizing_result.allocated_capital

    # Gross Leverage
    projected_gross = context.portfolio_snapshot.total_gross_exposure + new_capital
    if (projected_gross / equity) > limits.max_gross_leverage:
        return RiskRejectionReason.GROSS_LEVERAGE_EXCEEDED

    # Net Leverage
    projected_net = abs(
        context.portfolio_snapshot.total_net_exposure + (signal.direction * new_capital)
    )
    if (projected_net / equity) > limits.max_net_leverage:
        return RiskRejectionReason.NET_LEVERAGE_EXCEEDED

    # Instrument Concentration
    pos = context.portfolio_snapshot.get_position(signal.instrument_id)
    current_inst_exp = pos.market_value if pos is not None else 0.0
    projected_inst_exp = current_inst_exp + new_capital
    if (projected_inst_exp / equity) > limits.max_instrument_exposure_pct:
        return RiskRejectionReason.INSTRUMENT_EXPOSURE_EXCEEDED

    return None


def validate_exit_gates(
    signal: SignalCandidate,
    context: RiskContext,
    limits: RiskLimits,
) -> Tuple[bool, int, Optional[RiskRejectionReason]]:
    """
    Evaluates exit path gates. Risk-reducing exits are NEVER blocked by
    daily loss, drawdown, max positions, leverage, or cash exhaustion.
    Returns (is_valid, permitted_quantity, rejection_reason).
    """
    pos = context.portfolio_snapshot.get_position(signal.instrument_id)
    if pos is None or pos.is_flat:
        return (False, 0, RiskRejectionReason.NO_POSITION_TO_EXIT)

    # Direction Alignment
    if signal.signal_type == SignalType.EXIT_LONG and not pos.is_long:
        return (False, 0, RiskRejectionReason.OPPOSING_EXIT_DIRECTION)
    if signal.signal_type == SignalType.EXIT_SHORT and not pos.is_short:
        return (False, 0, RiskRejectionReason.OPPOSING_EXIT_DIRECTION)

    # Permitted Quantity bounded by held position
    held_qty = abs(pos.net_quantity)
    lot_size = max(1, context.instrument_metadata.lot_size)
    lots = math.floor(held_qty / lot_size)
    permitted_qty = lots * lot_size

    if permitted_qty <= 0:
        return (False, 0, RiskRejectionReason.ZERO_EXIT_QUANTITY)

    return (True, permitted_qty, None)
