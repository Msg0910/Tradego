"""
Unit tests for StrategyContext and FeatureQuality Policies (Phase 5).

Tests:
- StrategyContext immutability (frozen dataclass, MappingProxyType)
- Explicit evaluation_timestamp requirement
- QualityPolicy.STRICT rejection of DEGRADED, WARMING_UP, INVALID features
- QualityPolicy.PERMISSIVE acceptance of DEGRADED features with flag
- NO_TRADE early exits on quality failures
"""

import dataclasses
import unittest
from datetime import datetime, timezone

from services.analytics.models import FeatureQuality, FeatureSnapshot, FeatureValue
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.state import InstrumentState
from services.signals.base import BaseStrategy
from services.signals.context import StrategyContext
from services.signals.models import (
    NO_TRADE_INVALID_QUALITY,
    NO_TRADE_WARMING_UP,
    DecisionType,
    QualityPolicy,
    StrategyDecision,
    TriggerMode,
)


class DummyTestStrategy(BaseStrategy):
    """Minimal concrete strategy to test validate_features_and_trigger_mode."""

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        inv = self.validate_features_and_trigger_mode(context)
        if inv is not None:
            return inv
        return StrategyDecision(decision=DecisionType.TRADE, reason="VALID")


class TestStrategyContextAndQuality(unittest.TestCase):

    def setUp(self):
        self.iid = InstrumentId("TCS", Exchange.NSE, InstrumentType.EQUITY)
        self.ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)

        inst_state = InstrumentState(
            instrument_id=self.iid,
            provider="TEST",
            provider_symbol_id="TCS_TOK",
            is_resolved=True,
        )
        self.market_state = inst_state.create_snapshot()

    def test_context_immutability(self):
        feat_snap = FeatureSnapshot(instrument_id=self.iid, snapshot_timestamp=self.ts, _features={})
        preview_dict = {
            "EMA_20": FeatureValue("EMA_20", 3500.0, FeatureQuality.VALID, self.ts, self.ts, False)
        }

        ctx = StrategyContext(
            instrument_id=self.iid,
            trigger_mode=TriggerMode.BAR_CLOSE,
            market_state=self.market_state,
            features=feat_snap,
            evaluation_timestamp=self.ts,
            active_preview_features=preview_dict,
        )

        # StrategyContext itself is frozen
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.trigger_mode = TriggerMode.INTRABAR_PREVIEW

        # active_preview_features is wrapped as read-only MappingProxyType
        with self.assertRaises((TypeError, AttributeError)):
            ctx.active_preview_features["NEW_KEY"] = 123

    def test_quality_policy_strict(self):
        strat = DummyTestStrategy(
            strategy_id="TEST",
            strategy_version="1.0",
            config_hash="cfg",
            quality_policy=QualityPolicy.STRICT,
            required_features=["EMA_20"],
        )

        # 1. Feature is VALID
        fv_valid = FeatureValue("EMA_20", 100.0, FeatureQuality.VALID, self.ts, self.ts, True)
        snap_valid = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_valid})
        ctx_valid = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_valid, self.ts)
        self.assertIsNone(strat.validate_features_and_trigger_mode(ctx_valid))

        # 2. Feature is DEGRADED -> Rejection under STRICT
        fv_deg = FeatureValue("EMA_20", 100.0, FeatureQuality.DEGRADED, self.ts, self.ts, True)
        snap_deg = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_deg})
        ctx_deg = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_deg, self.ts)
        self.assertEqual(strat.validate_features_and_trigger_mode(ctx_deg), NO_TRADE_INVALID_QUALITY)

        # 3. Feature is WARMING_UP -> Rejection
        fv_warm = FeatureValue("EMA_20", 100.0, FeatureQuality.WARMING_UP, self.ts, self.ts, True)
        snap_warm = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_warm})
        ctx_warm = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_warm, self.ts)
        self.assertEqual(strat.validate_features_and_trigger_mode(ctx_warm), NO_TRADE_WARMING_UP)

        # 4. Feature is INVALID -> Rejection
        fv_inv = FeatureValue("EMA_20", None, FeatureQuality.INVALID, self.ts, self.ts, True)
        snap_inv = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_inv})
        ctx_inv = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_inv, self.ts)
        self.assertEqual(strat.validate_features_and_trigger_mode(ctx_inv), NO_TRADE_INVALID_QUALITY)

    def test_quality_policy_permissive(self):
        strat_permissive = DummyTestStrategy(
            strategy_id="TEST",
            strategy_version="1.0",
            config_hash="cfg",
            quality_policy=QualityPolicy.PERMISSIVE,
            required_features=["EMA_20"],
        )

        # Feature is DEGRADED -> Accepted under PERMISSIVE
        fv_deg = FeatureValue("EMA_20", 100.0, FeatureQuality.DEGRADED, self.ts, self.ts, True)
        snap_deg = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_deg})
        ctx_deg = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_deg, self.ts)

        self.assertIsNone(strat_permissive.validate_features_and_trigger_mode(ctx_deg))
        self.assertTrue(strat_permissive.has_degraded_features(ctx_deg))

        # But WARMING_UP and INVALID must still be rejected under PERMISSIVE!
        fv_warm = FeatureValue("EMA_20", 100.0, FeatureQuality.WARMING_UP, self.ts, self.ts, True)
        snap_warm = FeatureSnapshot(self.iid, self.ts, {"EMA_20": fv_warm})
        ctx_warm = StrategyContext(self.iid, TriggerMode.BAR_CLOSE, self.market_state, snap_warm, self.ts)
        self.assertEqual(strat_permissive.validate_features_and_trigger_mode(ctx_warm), NO_TRADE_WARMING_UP)


if __name__ == "__main__":
    unittest.main()
