"""
Tradego Broker-Independent Position Sizing Engine (Phase 6).

Implements the cash-equity position sizing mathematics:
1. Stop-distance validation
2. Strategy and account-level risk budgeting
3. Capital allocation, cash, and instrument exposure bounds
4. Discrete lot-size floor rounding and freeze quantity clamping
"""

from abc import ABC, abstractmethod
import math
from typing import Tuple

from services.market_state.instrument import InstrumentMetadata
from services.signals.models import SignalCandidate
from .context import RiskContext
from .limits import RiskLimits
from .models import PositionSizingResult


class CapitalRequirementCalculator(ABC):
    """Abstract calculator for position capital or margin requirements."""

    @abstractmethod
    def calculate_required_capital(
        self, instrument_metadata: InstrumentMetadata, entry_price: float, quantity: int
    ) -> float:
        """Computes monetary capital or margin committed for a given quantity."""
        ...


class CashNotionalCapitalCalculator(CapitalRequirementCalculator):
    """
    Cash-equity capital calculator: Capital = quantity * entry_price.
    Applies strictly to unleveraged Indian cash equities (NSE/BSE).
    """

    def calculate_required_capital(
        self, instrument_metadata: InstrumentMetadata, entry_price: float, quantity: int
    ) -> float:
        return float(quantity) * entry_price


class CashEquityPositionSizer:
    """
    Deterministic position sizing calculator for cash equities.
    """

    def __init__(
        self, capital_calculator: CapitalRequirementCalculator = CashNotionalCapitalCalculator()
    ) -> None:
        self.capital_calculator = capital_calculator

    def calculate_entry_size(
        self,
        signal: SignalCandidate,
        context: RiskContext,
        limits: RiskLimits,
    ) -> PositionSizingResult:
        entry_price = signal.suggested_entry_price or context.current_market_price
        stop_price = signal.suggested_stop_loss or entry_price

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0.0 or entry_price <= 0.0:
            return PositionSizingResult(
                permitted_quantity=0,
                calculated_monetary_risk=0.0,
                allocated_capital=0.0,
                unrounded_quantity=0.0,
                lot_size=context.instrument_metadata.lot_size,
                binding_constraint="ZERO_STOP_OR_PRICE",
            )

        equity = context.account_state.total_equity

        # 1. Account Risk Ceiling
        account_risk_ceiling = equity * limits.max_risk_per_trade_pct
        if limits.max_risk_per_trade_absolute is not None:
            account_risk_ceiling = min(account_risk_ceiling, limits.max_risk_per_trade_absolute)

        # 2. Strategy Risk Budget
        strategy_budget_pct = None
        if limits.strategy_budgets_pct is not None:
            strategy_budget_pct = limits.strategy_budgets_pct.get(signal.strategy_id)

        if strategy_budget_pct is not None:
            strat_risk_ceiling = account_risk_ceiling * strategy_budget_pct
            risk_budget = min(account_risk_ceiling, strat_risk_ceiling)
        else:
            risk_budget = account_risk_ceiling

        # 3. Raw Upper Bounds
        q_risk = risk_budget / stop_distance
        q_capital = (equity * limits.max_capital_allocation_per_trade_pct) / entry_price
        q_cash = context.account_state.available_cash / entry_price

        # Current instrument exposure
        pos = context.portfolio_snapshot.get_position(signal.instrument_id)
        current_exp = pos.market_value if pos is not None else 0.0
        max_inst_exp = equity * limits.max_instrument_exposure_pct
        remaining_inst_exp = max(0.0, max_inst_exp - current_exp)
        q_inst = remaining_inst_exp / entry_price

        # 4. Binding Constraint Identification
        constraints: Tuple[Tuple[str, float], ...] = (
            ("RISK_BUDGET", q_risk),
            ("CAPITAL_ALLOCATION", q_capital),
            ("AVAILABLE_CASH", q_cash),
            ("INSTRUMENT_EXPOSURE", q_inst),
        )
        binding_constraint, q_raw = min(constraints, key=lambda c: c[1])

        # 5. Discrete Lot Size Alignment (Strict Floor Rounding)
        lot_size = max(1, context.instrument_metadata.lot_size)
        lots = math.floor(q_raw / lot_size)
        permitted_quantity = lots * lot_size

        # Freeze quantity clamping
        freeze_qty = context.instrument_metadata.freeze_quantity
        if freeze_qty is not None and permitted_quantity > freeze_qty:
            permitted_quantity = freeze_qty
            binding_constraint = "FREEZE_QUANTITY"

        if permitted_quantity <= 0:
            return PositionSizingResult(
                permitted_quantity=0,
                calculated_monetary_risk=0.0,
                allocated_capital=0.0,
                unrounded_quantity=round(q_raw, 4),
                lot_size=lot_size,
                binding_constraint=binding_constraint,
            )

        calculated_monetary_risk = round(permitted_quantity * stop_distance, 4)
        allocated_capital = round(
            self.capital_calculator.calculate_required_capital(
                context.instrument_metadata, entry_price, permitted_quantity
            ),
            4,
        )

        return PositionSizingResult(
            permitted_quantity=permitted_quantity,
            calculated_monetary_risk=calculated_monetary_risk,
            allocated_capital=allocated_capital,
            unrounded_quantity=round(q_raw, 4),
            lot_size=lot_size,
            binding_constraint=binding_constraint,
        )
