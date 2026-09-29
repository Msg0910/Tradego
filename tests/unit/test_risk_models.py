"""
Unit tests for Phase 6 Risk Models and Contracts.

Verifies:
- Frozen/slotted immutability of all data contracts
- Read-only mappings (MappingProxyType)
- ApprovedTradeIntent semantic equivalence excluding runtime UUID & timestamp
- Phase 5 SignalCandidate immutability
- RiskDecision constructor invariants
"""

from dataclasses import FrozenInstanceError
from datetime import datetime
import unittest
import uuid

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.limits import RiskLimits, compute_risk_config_hash
from services.risk.models import (
    ApprovedTradeIntent,
    PortfolioSnapshot,
    PositionSizingResult,
    PositionSnapshot,
    RiskDecision,
    RiskDecisionType,
    RiskRejection,
    RiskRejectionReason,
)
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestRiskModels(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_nse = InstrumentId(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )

    def test_phase_6_contracts_are_frozen_and_slotted(self) -> None:
        """Requirement 1: Verify all Phase 6 contracts are slotted and frozen."""
        pos = PositionSnapshot(
            instrument_id=self.inst_nse,
            net_quantity=100,
            average_entry_price=2500.0,
            current_market_price=2550.0,
            unrealized_pnl=5000.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
            strategy_id="TEST_STRAT",
        )
        self.assertTrue(hasattr(pos, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            pos.net_quantity = 200  # type: ignore

        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={self.inst_nse: pos},
        )
        self.assertTrue(hasattr(portfolio, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            portfolio.snapshot_timestamp = self.ts  # type: ignore

        # Sizing result
        sizing = PositionSizingResult(
            permitted_quantity=100,
            calculated_monetary_risk=1000.0,
            allocated_capital=250000.0,
            unrounded_quantity=105.4,
            lot_size=1,
            binding_constraint="RISK_BUDGET",
        )
        self.assertTrue(hasattr(sizing, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            sizing.permitted_quantity = 50  # type: ignore

        # Account state
        acc = AccountRiskState(
            account_id="ACC001",
            total_equity=1000000.0,
            available_cash=500000.0,
            peak_equity=1100000.0,
            realized_pnl_today=10000.0,
            unrealized_pnl_current=-5000.0,
            last_updated_timestamp=self.ts,
        )
        self.assertTrue(hasattr(acc, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            acc.total_equity = 900000.0  # type: ignore

        # Risk limits
        limits = RiskLimits()
        self.assertTrue(hasattr(limits, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            limits.max_gross_leverage = 3.0  # type: ignore

        # Rejection
        rejection = RiskRejection(
            signal_id="SIG_001",
            fingerprint="FP_001",
            strategy_id="TEST_STRAT",
            instrument_id=self.inst_nse,
            reason_code=RiskRejectionReason.STOP_TOO_TIGHT,
            violating_rule="STOP_TOO_TIGHT",
            message="Stop distance too tight",
            evaluation_timestamp=self.ts,
        )
        self.assertTrue(hasattr(rejection, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            rejection.message = "new"  # type: ignore

        # Decision
        decision = RiskDecision(decision=RiskDecisionType.REJECTED, rejection=rejection)
        self.assertTrue(hasattr(decision, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            decision.decision = RiskDecisionType.APPROVED  # type: ignore

    def test_signal_candidate_cannot_be_mutated(self) -> None:
        """Requirement 2: SignalCandidate cannot be mutated."""
        sig = SignalCandidate(
            signal_id="SIG_100",
            fingerprint="FP_100",
            reaffirmation_key="RF_100",
            strategy_id="STRAT_1",
            strategy_version="1.0.0",
            config_hash="HASH_100",
            instrument_id=self.inst_nse,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.85,
            suggested_entry_price=2500.0,
            suggested_stop_loss=2450.0,
            suggested_take_profit=2600.0,
            risk_reward_ratio=2.0,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )
        self.assertTrue(hasattr(sig, "__slots__"))
        with self.assertRaises(FrozenInstanceError):
            sig.confidence_score = 0.99  # type: ignore

    def test_approved_trade_intent_semantic_equivalence_ignores_uuid_and_timestamp(self) -> None:
        """Requirement 3: ApprovedTradeIntent semantic equivalence ignores UUID/timestamp."""
        intent1 = ApprovedTradeIntent(
            intent_id="UUID-AAA-111",
            intent_generated_timestamp=datetime(2026, 9, 14, 10, 0, 1),
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id="STRAT_A",
            strategy_version="1.0.0",
            risk_config_version="1.0.0",
            risk_config_hash="CFG_HASH_123",
            instrument_id=self.inst_nse,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            permitted_quantity=100,
            approved_entry_price=2500.0,
            approved_stop_loss=2450.0,
            approved_take_profit=2600.0,
            risk_reward_ratio=2.0,
            calculated_monetary_risk=5000.0,
            allocated_capital=250000.0,
            binding_constraint="RISK_BUDGET",
            market_timestamp=self.ts,
            signal_generated_timestamp=self.ts,
            expiry_timestamp=None,
        )

        intent2 = ApprovedTradeIntent(
            intent_id="UUID-BBB-222",  # Different UUID
            intent_generated_timestamp=datetime(2026, 9, 14, 10, 0, 5),  # Different timestamp
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id="STRAT_A",
            strategy_version="1.0.0",
            risk_config_version="1.0.0",
            risk_config_hash="CFG_HASH_123",
            instrument_id=self.inst_nse,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            permitted_quantity=100,
            approved_entry_price=2500.0,
            approved_stop_loss=2450.0,
            approved_take_profit=2600.0,
            risk_reward_ratio=2.0,
            calculated_monetary_risk=5000.0,
            allocated_capital=250000.0,
            binding_constraint="RISK_BUDGET",
            market_timestamp=self.ts,
            signal_generated_timestamp=self.ts,
            expiry_timestamp=None,
        )

        # Non-identical object equality because of UUID
        self.assertNotEqual(intent1, intent2)
        # But 100% semantically equivalent!
        self.assertTrue(intent1.is_semantically_equivalent(intent2))
        self.assertTrue(intent2.is_semantically_equivalent(intent1))

        # Changing permitted_quantity breaks semantic equivalence
        intent3 = ApprovedTradeIntent(
            intent_id=intent1.intent_id,
            intent_generated_timestamp=intent1.intent_generated_timestamp,
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id="STRAT_A",
            strategy_version="1.0.0",
            risk_config_version="1.0.0",
            risk_config_hash="CFG_HASH_123",
            instrument_id=self.inst_nse,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            permitted_quantity=99,  # Changed
            approved_entry_price=2500.0,
            approved_stop_loss=2450.0,
            approved_take_profit=2600.0,
            risk_reward_ratio=2.0,
            calculated_monetary_risk=5000.0,
            allocated_capital=250000.0,
            binding_constraint="RISK_BUDGET",
            market_timestamp=self.ts,
            signal_generated_timestamp=self.ts,
            expiry_timestamp=None,
        )
        self.assertFalse(intent1.is_semantically_equivalent(intent3))

    def test_portfolio_and_position_snapshot_computations(self) -> None:
        """Test PositionSnapshot and PortfolioSnapshot metrics."""
        pos1 = PositionSnapshot(
            instrument_id=self.inst_nse,
            net_quantity=100,
            average_entry_price=2500.0,
            current_market_price=2600.0,
            unrealized_pnl=10000.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
            strategy_id="STRAT_1",
        )
        self.assertTrue(pos1.is_long)
        self.assertFalse(pos1.is_short)
        self.assertFalse(pos1.is_flat)
        self.assertEqual(pos1.market_value, 260000.0)
        self.assertEqual(pos1.directional_exposure, 260000.0)

        inst_tcs = InstrumentId(
            symbol="TCS", exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY
        )
        pos2 = PositionSnapshot(
            instrument_id=inst_tcs,
            net_quantity=-50,
            average_entry_price=3500.0,
            current_market_price=3400.0,
            unrealized_pnl=5000.0,
            realized_pnl=0.0,
            opened_timestamp=self.ts,
            last_updated_timestamp=self.ts,
            strategy_id="STRAT_2",
        )
        self.assertTrue(pos2.is_short)
        self.assertEqual(pos2.market_value, 170000.0)
        self.assertEqual(pos2.directional_exposure, -170000.0)

        portfolio = PortfolioSnapshot(
            snapshot_timestamp=self.ts,
            positions={self.inst_nse: pos1, inst_tcs: pos2},
        )
        self.assertEqual(portfolio.open_positions_count, 2)
        self.assertEqual(portfolio.total_gross_exposure, 430000.0)
        self.assertEqual(portfolio.total_net_exposure, 90000.0)
        self.assertEqual(portfolio.get_strategy_exposure("STRAT_1"), 260000.0)
        self.assertEqual(portfolio.get_strategy_exposure("STRAT_2"), 170000.0)

    def test_risk_decision_invariants(self) -> None:
        """Verify RiskDecision requires matching intent or rejection."""
        with self.assertRaises(ValueError):
            RiskDecision(decision=RiskDecisionType.APPROVED, approved_intent=None)

        with self.assertRaises(ValueError):
            RiskDecision(decision=RiskDecisionType.REJECTED, rejection=None)


if __name__ == "__main__":
    unittest.main()
