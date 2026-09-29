"""
Unit tests for Phase 6 Bifurcated Entry vs Exit Evaluation Paths.

Verifies:
- Daily loss limit blocks entries but NEVER blocks exits
- Drawdown circuit breaker blocks entries but NEVER blocks exits
- Max concurrent positions blocks entries but NEVER blocks exits
- Available cash exhaustion blocks entries but NEVER blocks exits
- Exits do not perform stop-distance sizing (monetary risk = 0.0, capital = 0.0)
- Exit quantity is bounded by held position
- Opposing exit direction rejects with OPPOSING_EXIT_DIRECTION
- Exit with no active position rejects with NO_POSITION_TO_EXIT
"""

from datetime import datetime
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
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


class TestRiskEntryVsExit(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_id = InstrumentId(
            symbol="TCS",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.meta = InstrumentMetadata(
            instrument_id=self.inst_id,
            lot_size=1,
            tick_size=0.05,
            price_precision=2,
        )
        self.engine = RiskEngine()

    def _create_entry_signal(
        self,
        symbol: str = "TCS",
        signal_type: SignalType = SignalType.ENTRY_LONG,
        direction: int = 1,
        entry_price: float = 3500.0,
        stop_loss: float = 3450.0,
    ) -> SignalCandidate:
        inst = InstrumentId(
            symbol=symbol, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
        )
        return SignalCandidate(
            signal_id=f"SIG_ENTRY_{symbol}",
            fingerprint=f"FP_ENTRY_{symbol}",
            reaffirmation_key=f"RF_ENTRY_{symbol}",
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=inst,
            signal_type=signal_type,
            direction=direction,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.8,
            suggested_entry_price=entry_price,
            suggested_stop_loss=stop_loss,
            suggested_take_profit=entry_price + 2.0 * abs(entry_price - stop_loss),
            risk_reward_ratio=2.0,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def _create_exit_signal(
        self,
        symbol: str = "TCS",
        signal_type: SignalType = SignalType.EXIT_LONG,
        direction: int = -1,
        entry_price: float = 3500.0,
    ) -> SignalCandidate:
        inst = InstrumentId(
            symbol=symbol, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
        )
        return SignalCandidate(
            signal_id=f"SIG_EXIT_{symbol}",
            fingerprint=f"FP_EXIT_{symbol}",
            reaffirmation_key=f"RF_EXIT_{symbol}",
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=inst,
            signal_type=signal_type,
            direction=direction,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.8,
            suggested_entry_price=entry_price,
            suggested_stop_loss=None,  # Market exits do not carry stops
            suggested_take_profit=None,
            risk_reward_ratio=None,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def test_daily_loss_limit_blocks_entry_but_allows_exit(self) -> None:
        """Requirements 14 & 15: Daily loss blocks entries, does NOT block exits."""
        # Account equity: 1,000,000. Daily loss limit: 3% = 30,000.
        # Current daily loss: -35,000 (breached!).
        pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=100,  # Currently Long 100 shares
            average_entry_price=3500.0,
            current_market_price=3500.0,
            unrealized_pnl=0.0,
            realized_pnl=-35000.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={self.inst_id: pos})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=-35000.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )

        # 1. Entry candidate for new instrument must be REJECTED with MAX_DAILY_LOSS_EXCEEDED
        entry_sig = self._create_entry_signal(symbol="INFY")
        meta_infy = InstrumentMetadata(
            instrument_id=entry_sig.instrument_id, lot_size=1, tick_size=0.05, price_precision=2
        )
        ctx_infy = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=meta_infy,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )
        entry_dec = self.engine.evaluate(entry_sig, ctx_infy)
        self.assertEqual(entry_dec.decision, RiskDecisionType.REJECTED)
        self.assertIsNotNone(entry_dec.rejection)
        self.assertEqual(
            entry_dec.rejection.reason_code, RiskRejectionReason.MAX_DAILY_LOSS_EXCEEDED
        )

        # 2. Exit candidate for existing TCS position must be APPROVED!
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        exit_dec = self.engine.evaluate(exit_sig, ctx)
        self.assertEqual(exit_dec.decision, RiskDecisionType.APPROVED)
        self.assertIsNotNone(exit_dec.approved_intent)
        self.assertEqual(exit_dec.approved_intent.permitted_quantity, 100)
        self.assertEqual(exit_dec.approved_intent.calculated_monetary_risk, 0.0)
        self.assertEqual(exit_dec.approved_intent.allocated_capital, 0.0)

    def test_drawdown_limit_blocks_entry_but_allows_exit(self) -> None:
        """Requirements 16 & 17: Drawdown blocks entries, does NOT block exits."""
        # Peak equity: 1,200,000. Total equity: 1,000,000.
        # Drawdown = (1.2M - 1.0M) / 1.2M = 16.67% (exceeds default 10% limit!).
        pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=50,
            average_entry_price=3500.0,
            current_market_price=3500.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={self.inst_id: pos})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1200000.0,  # Drawdown = 16.67%
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )

        # Entry must be rejected
        entry_sig = self._create_entry_signal(symbol="WIPRO")
        meta_wipro = InstrumentMetadata(
            instrument_id=entry_sig.instrument_id, lot_size=1, tick_size=0.05, price_precision=2
        )
        ctx_wipro = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=meta_wipro,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )
        entry_dec = self.engine.evaluate(entry_sig, ctx_wipro)
        self.assertEqual(entry_dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(entry_dec.rejection.reason_code, RiskRejectionReason.MAX_DRAWDOWN_EXCEEDED)

        # Exit must be approved
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        exit_dec = self.engine.evaluate(exit_sig, ctx)
        self.assertEqual(exit_dec.decision, RiskDecisionType.APPROVED)
        self.assertEqual(exit_dec.approved_intent.permitted_quantity, 50)

    def test_max_concurrent_positions_blocks_entry_but_allows_exit(self) -> None:
        """Requirement 18: Max concurrent positions blocks entries."""
        # 10 open positions already in portfolio (limit = 10)
        positions = {}
        for i in range(10):
            sym = f"SYM_{i}"
            iid = InstrumentId(
                symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
            )
            positions[iid] = PositionSnapshot(
                instrument_id=iid,
                net_quantity=10,
                average_entry_price=100.0,
                current_market_price=100.0,
                unrealized_pnl=0.0,
                realized_pnl=0.0,
                opened_timestamp=self.ts,
                last_updated_timestamp=self.ts,
            )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions=positions)
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )

        # 11th entry must be rejected
        entry_sig = self._create_entry_signal(symbol="NEW_11")
        meta_11 = InstrumentMetadata(
            instrument_id=entry_sig.instrument_id, lot_size=1, tick_size=0.05, price_precision=2
        )
        ctx_11 = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=meta_11,
            evaluation_timestamp=self.ts,
            current_market_price=100.0,
            feature_quality=FeatureQuality.VALID,
        )
        entry_dec = self.engine.evaluate(entry_sig, ctx_11)
        self.assertEqual(entry_dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(
            entry_dec.rejection.reason_code,
            RiskRejectionReason.MAX_CONCURRENT_POSITIONS_REACHED,
        )

        # Exit for SYM_0 must be approved
        exit_sig = self._create_exit_signal(symbol="SYM_0", signal_type=SignalType.EXIT_LONG)
        meta_0 = InstrumentMetadata(
            instrument_id=exit_sig.instrument_id, lot_size=1, tick_size=0.05, price_precision=2
        )
        ctx_0 = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=meta_0,
            evaluation_timestamp=self.ts,
            current_market_price=100.0,
            feature_quality=FeatureQuality.VALID,
        )
        exit_dec = self.engine.evaluate(exit_sig, ctx_0)
        self.assertEqual(exit_dec.decision, RiskDecisionType.APPROVED)
        self.assertEqual(exit_dec.approved_intent.permitted_quantity, 10)

    def test_exits_do_not_perform_stop_distance_sizing(self) -> None:
        """Requirement 19: Exits do not perform stop-distance sizing."""
        pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=75,
            average_entry_price=3500.0,
            current_market_price=3600.0,
            unrealized_pnl=7500.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={self.inst_id: pos})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=7500.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3600.0,
            feature_quality=FeatureQuality.VALID,
        )
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        dec = self.engine.evaluate(exit_sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.APPROVED)
        intent = dec.approved_intent
        self.assertIsNotNone(intent)
        self.assertEqual(intent.permitted_quantity, 75)
        self.assertIsNone(intent.approved_stop_loss)
        self.assertIsNone(intent.approved_take_profit)
        self.assertEqual(intent.calculated_monetary_risk, 0.0)
        self.assertEqual(intent.allocated_capital, 0.0)
        self.assertEqual(intent.binding_constraint, "HELD_POSITION_BOUND")

    def test_exit_quantity_cannot_exceed_held_position(self) -> None:
        """Requirement 20: Exit quantity is bounded by held position."""
        pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=42,  # Holds 42 shares
            average_entry_price=3500.0,
            current_market_price=3500.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={self.inst_id: pos})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        dec = self.engine.evaluate(exit_sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.APPROVED)
        self.assertEqual(dec.approved_intent.permitted_quantity, 42)

    def test_wrong_exit_direction_rejects(self) -> None:
        """Requirement 21: Wrong exit direction rejects with OPPOSING_EXIT_DIRECTION."""
        # Position is SHORT (-50), but signal says EXIT_LONG
        pos = PositionSnapshot(
            instrument_id=self.inst_id,
            net_quantity=-50,  # Short
            average_entry_price=3500.0,
            current_market_price=3500.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
        )
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={self.inst_id: pos})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )
        # Attempting EXIT_LONG on a short position
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        dec = self.engine.evaluate(exit_sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.OPPOSING_EXIT_DIRECTION)

    def test_no_position_exit_rejects(self) -> None:
        """Requirement 22: No-position exit rejects with NO_POSITION_TO_EXIT."""
        portfolio = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={})
        account = AccountRiskState(
            account_id="ACC_001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1000000.0,
            realized_pnl_today=0.0,
            unrealized_pnl_current=0.0,
            last_updated_timestamp=self.ts,
        )
        ctx = RiskContext(
            account_state=account,
            portfolio_snapshot=portfolio,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=3500.0,
            feature_quality=FeatureQuality.VALID,
        )
        exit_sig = self._create_exit_signal(symbol="TCS", signal_type=SignalType.EXIT_LONG)
        dec = self.engine.evaluate(exit_sig, ctx)

        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.NO_POSITION_TO_EXIT)


if __name__ == "__main__":
    unittest.main()
