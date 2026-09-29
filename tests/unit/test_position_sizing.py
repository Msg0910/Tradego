"""
Unit tests for Phase 6 Position Sizing Engine.

Verifies:
- Account-level risk ceiling (percentage & absolute cap)
- Strategy-specific risk budget scaling
- Raw constraint bounds: Q_risk, Q_capital, Q_cash, Q_inst
- Discrete lot-size floor rounding
- Rejection on less than 1 lot
- Freeze quantity clamping
- Recalculation of actual committed capital and monetary risk
"""

from datetime import datetime
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.limits import RiskLimits
from services.risk.models import PortfolioSnapshot, PositionSnapshot
from services.risk.position_sizing import CashEquityPositionSizer, CashNotionalCapitalCalculator
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestPositionSizing(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_id = InstrumentId(
            symbol="INFY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.meta = InstrumentMetadata(
            instrument_id=self.inst_id,
            lot_size=1,
            tick_size=0.05,
            price_precision=2,
            freeze_quantity=None,
        )
        self.account_state = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,  # 10 Lakhs
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        self.portfolio_snapshot = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={},
        )
        self.context = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        self.sizer = CashEquityPositionSizer(CashNotionalCapitalCalculator())

    def _create_signal(
        self,
        strategy_id: str = "TREND_CONT",
        entry_price: float = 1000.0,
        stop_loss: float = 950.0,
        take_profit: float = 1100.0,
    ) -> SignalCandidate:
        return SignalCandidate(
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=self.inst_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.9,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            risk_reward_ratio=2.0,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def test_account_and_strategy_risk_budget_mathematics(self) -> None:
        """Requirement 25: Strategy risk budget mathematics is correct."""
        # Equity = 1,000,000. 1% max_risk => R_account = 10,000
        # Strategy budget = 40% => R_strat = 10,000 * 0.40 = 4,000
        # Stop distance = 50 => Q_risk = 4,000 / 50 = 80 shares
        limits = RiskLimits(
            max_risk_per_trade_pct=0.01,
            strategy_budgets_pct={"TREND_CONT": 0.40},
        )
        signal = self._create_signal(entry_price=1000.0, stop_loss=950.0)
        res = self.sizer.calculate_entry_size(signal, self.context, limits)

        self.assertEqual(res.binding_constraint, "RISK_BUDGET")
        self.assertEqual(res.permitted_quantity, 80)
        self.assertEqual(res.calculated_monetary_risk, 80 * 50.0)  # 4000.0
        self.assertEqual(res.allocated_capital, 80 * 1000.0)  # 80,000.0

    def test_absolute_risk_cap_applied(self) -> None:
        """Verify max_risk_per_trade_absolute caps account risk ceiling."""
        # Equity = 1,000,000. 1% would be 10,000, but absolute cap = 2,500.
        # Stop distance = 50 => Q_risk = 2,500 / 50 = 50 shares.
        limits = RiskLimits(
            max_risk_per_trade_pct=0.01,
            max_risk_per_trade_absolute=2500.0,
        )
        signal = self._create_signal(entry_price=1000.0, stop_loss=950.0)
        res = self.sizer.calculate_entry_size(signal, self.context, limits)

        self.assertEqual(res.binding_constraint, "RISK_BUDGET")
        self.assertEqual(res.permitted_quantity, 50)
        self.assertEqual(res.calculated_monetary_risk, 2500.0)

    def test_discrete_lot_size_floor_rounding(self) -> None:
        """Requirement 26: Discrete lot-size floor rounding is correct."""
        # lot_size = 25.
        # R_budget = 10,000. Stop distance = 65 => Q_risk = 10,000 / 65 = 153.846
        # Lots = floor(153.846 / 25) = 6 => Q_permitted = 6 * 25 = 150.
        meta_lot25 = InstrumentMetadata(
            instrument_id=self.inst_id,
            lot_size=25,
            tick_size=0.05,
            price_precision=2,
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=meta_lot25,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(max_risk_per_trade_pct=0.01)
        signal = self._create_signal(entry_price=1000.0, stop_loss=935.0)  # stop dist = 65
        res = self.sizer.calculate_entry_size(signal, ctx, limits)

        self.assertEqual(res.lot_size, 25)
        self.assertEqual(res.permitted_quantity, 150)
        self.assertAlmostEqual(res.unrounded_quantity, 153.8462, places=3)
        self.assertEqual(res.calculated_monetary_risk, 150 * 65.0)  # 9750.0
        self.assertEqual(res.allocated_capital, 150 * 1000.0)  # 150,000.0

    def test_less_than_one_lot_yields_zero(self) -> None:
        """Requirement 27: Less-than-one-lot sizing returns 0 permitted quantity."""
        # lot_size = 100.
        # R_budget = 10,000. Stop distance = 150 => Q_risk = 10,000 / 150 = 66.67
        # Lots = floor(66.67 / 100) = 0 => Q_permitted = 0
        meta_lot100 = InstrumentMetadata(
            instrument_id=self.inst_id,
            lot_size=100,
            tick_size=0.05,
            price_precision=2,
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=meta_lot100,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(max_risk_per_trade_pct=0.01)
        signal = self._create_signal(entry_price=1000.0, stop_loss=850.0)  # stop dist = 150
        res = self.sizer.calculate_entry_size(signal, ctx, limits)

        self.assertEqual(res.permitted_quantity, 0)
        self.assertEqual(res.calculated_monetary_risk, 0.0)
        self.assertEqual(res.allocated_capital, 0.0)

    def test_capital_allocation_constraint(self) -> None:
        """Requirement 28: Capital allocation constraint works."""
        # Equity = 1,000,000. Max capital allocation per trade = 5% = 50,000.
        # Entry price = 1000 => Q_capital = 50,000 / 1000 = 50.
        # Stop distance = 5 => Q_risk = 10,000 / 5 = 2000.
        # Capital allocation binds!
        limits = RiskLimits(
            max_risk_per_trade_pct=0.01,
            max_capital_allocation_per_trade_pct=0.05,
        )
        signal = self._create_signal(entry_price=1000.0, stop_loss=995.0)
        res = self.sizer.calculate_entry_size(signal, self.context, limits)

        self.assertEqual(res.binding_constraint, "CAPITAL_ALLOCATION")
        self.assertEqual(res.permitted_quantity, 50)
        self.assertEqual(res.allocated_capital, 50000.0)

    def test_available_cash_constraint(self) -> None:
        """Requirement 29: Available cash constraint works."""
        # Available cash = 30,000. Entry price = 1000 => Q_cash = 30.
        # Equity = 1,000,000. Max capital allocation = 20% = 200,000.
        # Stop distance = 5 => Q_risk = 2000.
        # Available cash binds!
        low_cash_account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=30000.0,  # Only 30k free cash
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=low_cash_account,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(max_risk_per_trade_pct=0.01)
        signal = self._create_signal(entry_price=1000.0, stop_loss=995.0)
        res = self.sizer.calculate_entry_size(signal, ctx, limits)

        self.assertEqual(res.binding_constraint, "AVAILABLE_CASH")
        self.assertEqual(res.permitted_quantity, 30)

    def test_instrument_exposure_constraint(self) -> None:
        """Requirement 30: Instrument exposure constraint works."""
        # Equity = 1,000,000. Max instrument exposure = 25% = 250,000.
        # Already holding 200 shares @ 1000 = 200,000 exposure.
        # Remaining exposure = 50,000. Entry price = 1000 => Q_inst = 50.
        # Instrument exposure binds!
        existing_pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=200,
            average_entry_price=1000.0,
            current_market_price=1000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={self.inst_id: existing_pos},
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(
            max_risk_per_trade_pct=0.05,  # 50,000 risk budget
            max_instrument_exposure_pct=0.25,
        )
        signal = self._create_signal(entry_price=1000.0, stop_loss=995.0)
        res = self.sizer.calculate_entry_size(signal, ctx, limits)

        self.assertEqual(res.binding_constraint, "INSTRUMENT_EXPOSURE")
        self.assertEqual(res.permitted_quantity, 50)

    def test_freeze_quantity_clamping(self) -> None:
        """Verify freeze quantity clamps permitted quantity."""
        meta_freeze = InstrumentMetadata(
            instrument_id=self.inst_id,
            lot_size=1,
            tick_size=0.05,
            price_precision=2,
            freeze_quantity=20,  # Clamped to 20
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=meta_freeze,
            evaluation_timestamp=self.ts,
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(max_risk_per_trade_pct=0.01)
        signal = self._create_signal(entry_price=1000.0, stop_loss=950.0)  # Q_raw = 200
        res = self.sizer.calculate_entry_size(signal, ctx, limits)

        self.assertEqual(res.binding_constraint, "FREEZE_QUANTITY")
        self.assertEqual(res.permitted_quantity, 20)


if __name__ == "__main__":
    unittest.main()
