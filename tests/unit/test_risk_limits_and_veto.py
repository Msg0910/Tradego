"""
Unit tests for Phase 6 Risk Limits, Veto Hierarchy, and Asset Class Gates.

Verifies:
- Futures and Options derivative prohibition (UNSUPPORTED_INSTRUMENT_TYPE)
- NSE Equity and BSE Equity permitted
- Position conflict checks (opposing active position)
- Gross and net leverage limits
- Stop geometry constraints (direction, zero distance, tight, wide, R:R)
- Invalid/NaN/Infinity numerical fail-safe
- Missing portfolio state fail-safe
"""

from datetime import datetime
import math
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType, OptionType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.engine import RiskEngine
from services.risk.limits import RiskLimits
from services.risk.models import (
    PortfolioSnapshot,
    PositionSnapshot,
    RiskDecisionType,
    RiskRejectionReason,
)
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestRiskLimitsAndVeto(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_nse = InstrumentId(
            symbol="SBIN",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.meta_nse = InstrumentMetadata(
            instrument_id=self.inst_nse,
            lot_size=1,
            tick_size=0.05,
            price_precision=2,
        )
        self.account_state = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        self.portfolio_empty = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={})
        self.ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_empty,
            instrument_metadata=self.meta_nse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        self.engine = RiskEngine()

    def _create_signal(
        self,
        instrument_id: InstrumentId,
        signal_type: SignalType = SignalType.ENTRY_LONG,
        direction: int = 1,
        entry_price: float = 600.0,
        stop_loss: float = 585.0,
        take_profit: float = 630.0,
        risk_reward_ratio: float = 2.0,
    ) -> SignalCandidate:
        return SignalCandidate(
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=instrument_id,
            signal_type=signal_type,
            direction=direction,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.85,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=take_profit,
            risk_reward_ratio=risk_reward_ratio,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def test_futures_are_rejected(self) -> None:
        """Requirement 4: Futures are rejected with UNSUPPORTED_INSTRUMENT_TYPE."""
        fut_inst = InstrumentId(
            symbol="NIFTY26SEPFUT",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.FUTURES,
            expiry=datetime(2026, 9, 24).date(),
        )
        sig = self._create_signal(instrument_id=fut_inst, entry_price=24000.0, stop_loss=23800.0)
        dec = self.engine.evaluate(sig, self.ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertIsNotNone(dec.rejection)
        self.assertEqual(
            dec.rejection.reason_code, RiskRejectionReason.UNSUPPORTED_INSTRUMENT_TYPE
        )

    def test_options_are_rejected(self) -> None:
        """Requirement 5: Options are rejected with UNSUPPORTED_INSTRUMENT_TYPE."""
        opt_inst = InstrumentId(
            symbol="NIFTY26SEP24000CE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.OPTIONS,
            expiry=datetime(2026, 9, 24).date(),
            strike=24000.0,
            option_type=OptionType.CE,
        )
        sig = self._create_signal(instrument_id=opt_inst, entry_price=150.0, stop_loss=100.0)
        dec = self.engine.evaluate(sig, self.ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(
            dec.rejection.reason_code, RiskRejectionReason.UNSUPPORTED_INSTRUMENT_TYPE
        )

    def test_nse_equity_can_proceed(self) -> None:
        """Requirement 6: NSE equity can proceed to approval."""
        sig = self._create_signal(instrument_id=self.inst_nse, entry_price=600.0, stop_loss=585.0)
        dec = self.engine.evaluate(sig, self.ctx)

        self.assertEqual(dec.decision, RiskDecisionType.APPROVED)
        self.assertIsNotNone(dec.approved_intent)
        self.assertEqual(dec.approved_intent.instrument_id, self.inst_nse)

    def test_bse_equity_can_proceed(self) -> None:
        """Requirement 7: BSE equity can proceed to approval."""
        bse_inst = InstrumentId(
            symbol="500325",
            exchange=Exchange.BSE,
            instrument_type=InstrumentType.EQUITY,
        )
        meta_bse = InstrumentMetadata(
            instrument_id=bse_inst, lot_size=1, tick_size=0.05, price_precision=2
        )
        ctx_bse = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_empty,
            instrument_metadata=meta_bse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        sig = self._create_signal(instrument_id=bse_inst, entry_price=600.0, stop_loss=585.0)
        dec = self.engine.evaluate(sig, ctx_bse)

        self.assertEqual(dec.decision, RiskDecisionType.APPROVED)
        self.assertEqual(dec.approved_intent.instrument_id, bse_inst)

    def test_position_conflict_rejects(self) -> None:
        """Requirement 23: Position conflict rejects (e.g. entry long when currently short)."""
        short_pos = PositionSnapshot(
            instrument_id=self.inst_nse,
            net_quantity=-100,  # Currently Short
            average_entry_price=600.0,
            current_market_price=600.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={self.inst_nse: short_pos},
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta_nse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        # Attempting ENTRY_LONG (+1) on an active short position
        sig = self._create_signal(
            instrument_id=self.inst_nse,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            entry_price=600.0,
            stop_loss=585.0,
        )
        dec = self.engine.evaluate(sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.CONFLICTING_POSITION)

    def test_gross_leverage_constraint(self) -> None:
        """Requirement 31: Gross leverage constraint works."""
        # Equity = 1,000,000. Max gross leverage = 1.5. Max allowed gross = 1,500,000.
        # Existing gross = 1,400,000. New trade capital = 200,000.
        # Projected gross = 1,600,000 / 1,000,000 = 1.6 > 1.5 => REJECT GROSS_LEVERAGE_EXCEEDED.
        existing_inst = InstrumentId(
            symbol="RELIANCE", exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
        )
        existing_pos = PositionSnapshot(
            instrument_id=existing_inst,
            net_quantity=700,
            average_entry_price=2000.0,
            current_market_price=2000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={existing_inst: existing_pos},  # 1.4M gross
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta_nse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(
            max_gross_leverage=1.5,
            max_risk_per_trade_pct=0.05,  # 50,000 risk budget
        )
        engine = RiskEngine(limits=limits)
        # Sizing will try to size for 50,000 risk / 15 stop distance = 3333 shares @ 600 = 2M capital!
        # Projected gross would massively exceed 1.5
        sig = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=585.0
        )
        dec = engine.evaluate(sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.GROSS_LEVERAGE_EXCEEDED)

    def test_net_leverage_constraint(self) -> None:
        """Requirement 32: Net leverage constraint works."""
        # Equity = 1,000,000. Max net leverage = 1.0 (1,000,000 max directional exposure).
        # Existing long exposure = 900,000.
        # Sizing attempts to add 150,000 long capital.
        # Projected net = 1,050,000 / 1,000,000 = 1.05 > 1.0 => REJECT NET_LEVERAGE_EXCEEDED.
        existing_inst = InstrumentId(
            symbol="INFY", exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
        )
        existing_pos = PositionSnapshot(
            instrument_id=existing_inst,
            net_quantity=900,
            average_entry_price=1000.0,
            current_market_price=1000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={existing_inst: existing_pos},
        )
        ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta_nse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        limits = RiskLimits(
            max_gross_leverage=3.0,  # High gross leverage allowed
            max_net_leverage=1.0,    # Strict net leverage cap
            max_risk_per_trade_pct=0.02, # 20k risk / 15 stop = 1333 shares @ 600 = 800k capital
        )
        engine = RiskEngine(limits=limits)
        sig = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=585.0
        )
        dec = engine.evaluate(sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.NET_LEVERAGE_EXCEEDED)

    def test_invalid_nan_infinity_values_reject(self) -> None:
        """Requirement 33: Invalid/NaN/Infinity values reject."""
        # NaN entry price
        sig_nan = self._create_signal(
            instrument_id=self.inst_nse, entry_price=float("nan"), stop_loss=585.0
        )
        dec1 = self.engine.evaluate(sig_nan, self.ctx)
        self.assertEqual(dec1.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec1.rejection.reason_code, RiskRejectionReason.INVALID_SIGNAL)

        # Infinity stop loss
        sig_inf = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=float("inf")
        )
        dec2 = self.engine.evaluate(sig_inf, self.ctx)
        self.assertEqual(dec2.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec2.rejection.reason_code, RiskRejectionReason.INVALID_SIGNAL)

        # Non-positive equity
        acc_negative = AccountRiskState(
            account_id="ACC_001",
            total_equity=-100.0,
            available_cash=0.0,
            peak_equity=1000.0,
            realized_pnl_today=-1100.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx_neg = RiskContext(
            account_state=acc_negative,
            portfolio_snapshot=self.portfolio_empty,
            instrument_metadata=self.meta_nse,
            evaluation_timestamp=self.ts,
            current_market_price=600.0,
            feature_quality=FeatureQuality.VALID,
        )
        sig_valid = self._create_signal(instrument_id=self.inst_nse, entry_price=600.0, stop_loss=585.0)
        dec3 = self.engine.evaluate(sig_valid, ctx_neg)
        self.assertEqual(dec3.decision, RiskDecisionType.REJECTED)
        self.assertEqual(
            dec3.rejection.reason_code, RiskRejectionReason.ACCOUNT_EQUITY_NON_POSITIVE
        )

    def test_missing_portfolio_snapshot_guardrail(self) -> None:
        """Requirement 34: Missing PortfolioSnapshot cannot produce approval."""
        sig = self._create_signal(instrument_id=self.inst_nse, entry_price=600.0, stop_loss=585.0)
        # Calling evaluate with context=None fails safe
        dec = self.engine.evaluate(sig, None)
        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.MISSING_PORTFOLIO_STATE)

    def test_stop_geometry_veto_rules(self) -> None:
        """Verify inverted stop, tight stop, wide stop, and insufficient R:R vetoes."""
        # 1. Inverted stop (stop >= entry for Long)
        sig_inverted = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=610.0
        )
        dec = self.engine.evaluate(sig_inverted, self.ctx)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.INVALID_STOP_DIRECTION)

        # 2. Stop too tight (< 10 bps)
        # 600 * 0.0005 = 0.30 rs = 5 bps
        sig_tight = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=599.70
        )
        dec = self.engine.evaluate(sig_tight, self.ctx)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.STOP_TOO_TIGHT)

        # 3. Stop too wide (> 500 bps = 5%)
        # 600 * 0.06 = 36 rs = 600 bps => stop = 540.0
        sig_wide = self._create_signal(
            instrument_id=self.inst_nse, entry_price=600.0, stop_loss=540.0
        )
        dec = self.engine.evaluate(sig_wide, self.ctx)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.STOP_TOO_WIDE)

        # 4. Insufficient R:R (< 1.5)
        sig_rr = self._create_signal(
            instrument_id=self.inst_nse,
            entry_price=600.0,
            stop_loss=585.0,
            take_profit=610.0,
            risk_reward_ratio=1.0,  # Below min 1.5
        )
        dec = self.engine.evaluate(sig_rr, self.ctx)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.INSUFFICIENT_RISK_REWARD)


if __name__ == "__main__":
    unittest.main()
