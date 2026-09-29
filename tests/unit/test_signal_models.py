"""
Unit tests for Phase 5 Signal Models (SignalCandidate, PositionView, StrategyDecision).

Tests:
- Model immutability (frozen dataclasses)
- Enums (TriggerMode, SignalType, DecisionType, QualityPolicy)
- PositionView helper properties
- SignalCandidate timestamp invariants (temporal, causality, expiry)
- Conditional expiry handling (expiry_timestamp=None allowed)
- Metadata mapping immutability
"""

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone

from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.signals.models import (
    DecisionType,
    PositionView,
    QualityPolicy,
    SignalCandidate,
    SignalType,
    StrategyDecision,
    TriggerMode,
)


class TestSignalModels(unittest.TestCase):

    def setUp(self):
        self.iid = InstrumentId(
            "NIFTY26OCTFUT",
            Exchange.NFO,
            InstrumentType.FUTURES,
            expiry=datetime(2026, 10, 29).date(),
        )
        self.market_ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)
        self.avail_ts = self.market_ts
        self.gen_ts = self.avail_ts + timedelta(milliseconds=10)
        self.exp_ts = self.gen_ts + timedelta(minutes=5)

    def test_enums(self):
        self.assertEqual(TriggerMode.BAR_CLOSE.value, "BAR_CLOSE")
        self.assertEqual(TriggerMode.INTRABAR_PREVIEW.value, "INTRABAR_PREVIEW")

        self.assertEqual(SignalType.ENTRY_LONG.value, "ENTRY_LONG")
        self.assertEqual(SignalType.ENTRY_SHORT.value, "ENTRY_SHORT")
        self.assertEqual(SignalType.EXIT_LONG.value, "EXIT_LONG")
        self.assertEqual(SignalType.EXIT_SHORT.value, "EXIT_SHORT")

        self.assertEqual(DecisionType.TRADE.value, "TRADE")
        self.assertEqual(DecisionType.NO_TRADE.value, "NO_TRADE")

        self.assertEqual(QualityPolicy.STRICT.value, "STRICT")
        self.assertEqual(QualityPolicy.PERMISSIVE.value, "PERMISSIVE")

    def test_position_view_immutability_and_properties(self):
        pv = PositionView(
            instrument_id=self.iid,
            net_quantity=50,
            entry_price=25000.0,
            entry_time=self.market_ts,
            highest_price_since_entry=25100.0,
            lowest_price_since_entry=24950.0,
            unrealized_pnl_estimate=5000.0,
        )

        self.assertTrue(pv.is_long)
        self.assertFalse(pv.is_short)
        self.assertFalse(pv.is_flat)
        self.assertEqual(pv.net_quantity, 50)

        # Immutability
        with self.assertRaises(dataclasses.FrozenInstanceError):
            pv.net_quantity = 0

        # Flat position
        pv_flat = PositionView(
            instrument_id=self.iid,
            net_quantity=0,
            entry_price=0.0,
            entry_time=self.market_ts,
            highest_price_since_entry=0.0,
            lowest_price_since_entry=0.0,
        )
        self.assertTrue(pv_flat.is_flat)
        self.assertFalse(pv_flat.is_long)
        self.assertFalse(pv_flat.is_short)

    def test_signal_candidate_creation_and_immutability(self):
        candidate = SignalCandidate(
            signal_id="SIG_001",
            fingerprint="abc123hash",
            reaffirmation_key="reaff123hash",
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="cfg123hash",
            instrument_id=self.iid,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.85,
            suggested_entry_price=25000.0,
            suggested_stop_loss=24900.0,
            suggested_take_profit=25200.0,
            risk_reward_ratio=2.0,
            priority=0,
            market_timestamp=self.market_ts,
            availability_timestamp=self.avail_ts,
            generated_timestamp=self.gen_ts,
            expiry_timestamp=self.exp_ts,
            time_in_force_seconds=300.0,
            is_confirmed=True,
            regime="BULLISH",
            setup="PULLBACK",
            metadata={"source": "unit_test"},
        )

        self.assertEqual(candidate.signal_id, "SIG_001")
        self.assertEqual(candidate.direction, 1)
        self.assertEqual(candidate.confidence_score, 0.85)
        self.assertEqual(candidate.metadata["source"], "unit_test")

        # Immutability check
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.confidence_score = 0.99

        # Metadata immutability check
        with self.assertRaises((TypeError, AttributeError)):
            candidate.metadata["new_key"] = 123

    def test_timestamp_invariants(self):
        # 1. Temporal monotonicity violation: availability < market
        with self.assertRaises(ValueError):
            SignalCandidate(
                signal_id="SIG_BAD_1",
                fingerprint="hash",
                reaffirmation_key="reaff",
                strategy_id="STRAT",
                strategy_version="1.0",
                config_hash="cfg",
                instrument_id=self.iid,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=TriggerMode.BAR_CLOSE,
                confidence_score=0.8,
                suggested_entry_price=100.0,
                suggested_stop_loss=90.0,
                suggested_take_profit=120.0,
                risk_reward_ratio=2.0,
                market_timestamp=self.market_ts,
                availability_timestamp=self.market_ts - timedelta(seconds=1),  # Invalid!
                generated_timestamp=self.gen_ts,
            )

        # 2. Causality violation: generated < availability
        with self.assertRaises(ValueError):
            SignalCandidate(
                signal_id="SIG_BAD_2",
                fingerprint="hash",
                reaffirmation_key="reaff",
                strategy_id="STRAT",
                strategy_version="1.0",
                config_hash="cfg",
                instrument_id=self.iid,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=TriggerMode.BAR_CLOSE,
                confidence_score=0.8,
                suggested_entry_price=100.0,
                suggested_stop_loss=90.0,
                suggested_take_profit=120.0,
                risk_reward_ratio=2.0,
                market_timestamp=self.market_ts,
                availability_timestamp=self.avail_ts,
                generated_timestamp=self.avail_ts - timedelta(seconds=1),  # Invalid!
            )

        # 3. Expiry monotonicity violation: generated >= expiry
        with self.assertRaises(ValueError):
            SignalCandidate(
                signal_id="SIG_BAD_3",
                fingerprint="hash",
                reaffirmation_key="reaff",
                strategy_id="STRAT",
                strategy_version="1.0",
                config_hash="cfg",
                instrument_id=self.iid,
                signal_type=SignalType.ENTRY_LONG,
                direction=1,
                trigger_mode=TriggerMode.BAR_CLOSE,
                confidence_score=0.8,
                suggested_entry_price=100.0,
                suggested_stop_loss=90.0,
                suggested_take_profit=120.0,
                risk_reward_ratio=2.0,
                market_timestamp=self.market_ts,
                availability_timestamp=self.avail_ts,
                generated_timestamp=self.gen_ts,
                expiry_timestamp=self.gen_ts - timedelta(seconds=1),  # Invalid!
            )

    def test_conditional_expiry_none(self):
        # When expiry_timestamp is None, it defines no explicit expiry and must be valid
        candidate = SignalCandidate(
            signal_id="SIG_NO_EXPIRY",
            fingerprint="hash",
            reaffirmation_key="reaff",
            strategy_id="STRAT",
            strategy_version="1.0",
            config_hash="cfg",
            instrument_id=self.iid,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.8,
            suggested_entry_price=100.0,
            suggested_stop_loss=90.0,
            suggested_take_profit=120.0,
            risk_reward_ratio=2.0,
            market_timestamp=self.market_ts,
            availability_timestamp=self.avail_ts,
            generated_timestamp=self.gen_ts,
            expiry_timestamp=None,  # Valid!
        )
        self.assertIsNone(candidate.expiry_timestamp)

    def test_strategy_decision_model(self):
        decision = StrategyDecision(decision=DecisionType.NO_TRADE, reason="WARMING_UP")
        self.assertEqual(decision.decision, DecisionType.NO_TRADE)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, "WARMING_UP")


if __name__ == "__main__":
    unittest.main()
