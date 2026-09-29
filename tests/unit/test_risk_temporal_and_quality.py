"""
Unit tests for Phase 6 Temporal Safety, Causality, and Feature Quality Gates.

Verifies:
- Monotonic timestamp invariants (market <= availability <= generated)
- Future-dated signals relative to evaluation clock reject (SIGNAL_FUTURE_DATED)
- Expired signals reject (SIGNAL_EXPIRED)
- Stale signals exceeding max_signal_age_seconds reject (SIGNAL_STALE)
- FeatureQuality WARMING_UP, STALE, INVALID reject (DEGRADED_FEATURE_QUALITY)
- FeatureQuality DEGRADED behavior strictly obeys RiskLimits.allow_degraded_features
"""

from datetime import datetime, timedelta
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.engine import RiskEngine
from services.risk.limits import RiskLimits
from services.risk.models import (
    PortfolioSnapshot,
    RiskDecisionType,
    RiskRejectionReason,
)
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestRiskTemporalAndQuality(unittest.TestCase):
    def setUp(self) -> None:
        self.t_eval = datetime(2026, 9, 14, 10, 0, 30)
        self.inst_id = InstrumentId(
            symbol="AXISBANK",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.meta = InstrumentMetadata(
            instrument_id=self.inst_id,
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
            last_updated_timestamp=self.t_eval,
        )
        self.portfolio_snapshot = PortfolioSnapshot(snapshot_timestamp=self.t_eval, positions={})
        self.engine = RiskEngine()

    def _create_context(
        self,
        feature_quality: FeatureQuality = FeatureQuality.VALID,
        evaluation_timestamp: datetime = None,
    ) -> RiskContext:
        eval_ts = evaluation_timestamp or self.t_eval
        return RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=self.meta,
            evaluation_timestamp=eval_ts,
            current_market_price=1200.0,
            feature_quality=feature_quality,
        )

    def _create_signal(
        self,
        market_ts: datetime,
        avail_ts: datetime,
        gen_ts: datetime,
        expiry_ts: datetime = None,
    ) -> SignalCandidate:
        return SignalCandidate(
            signal_id="SIG_001",
            fingerprint="FP_001",
            reaffirmation_key="RF_001",
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=self.inst_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.9,
            suggested_entry_price=1200.0,
            suggested_stop_loss=1176.0,
            suggested_take_profit=1248.0,
            risk_reward_ratio=2.0,
            market_timestamp=market_ts,
            availability_timestamp=avail_ts,
            generated_timestamp=gen_ts,
            expiry_timestamp=expiry_ts,
            is_confirmed=True,
        )

    def test_invalid_timestamps_monotonicity_rejection(self) -> None:
        """Requirement 8: Non-monotonic timestamps reject with INVALID_TIMESTAMPS."""
        from unittest.mock import MagicMock

        ctx = self._create_context()

        # availability_timestamp < market_timestamp (defense in depth)
        sig_bad1 = MagicMock()
        sig_bad1.suggested_entry_price = 1200.0
        sig_bad1.suggested_stop_loss = 1176.0
        sig_bad1.market_timestamp = datetime(2026, 9, 14, 10, 0, 10)
        sig_bad1.availability_timestamp = datetime(2026, 9, 14, 10, 0, 5)
        sig_bad1.generated_timestamp = datetime(2026, 9, 14, 10, 0, 15)
        sig_bad1.instrument_id = self.inst_id
        sig_bad1.signal_id = "SIG_BAD1"
        sig_bad1.fingerprint = "FP_BAD1"
        sig_bad1.strategy_id = "TEST"
        sig_bad1.trigger_mode = TriggerMode.BAR_CLOSE
        sig_bad1.direction = 1

        dec1 = self.engine.evaluate(sig_bad1, ctx)
        self.assertEqual(dec1.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec1.rejection.reason_code, RiskRejectionReason.INVALID_TIMESTAMPS)

        # generated_timestamp < availability_timestamp (defense in depth)
        sig_bad2 = MagicMock()
        sig_bad2.suggested_entry_price = 1200.0
        sig_bad2.suggested_stop_loss = 1176.0
        sig_bad2.market_timestamp = datetime(2026, 9, 14, 10, 0, 5)
        sig_bad2.availability_timestamp = datetime(2026, 9, 14, 10, 0, 15)
        sig_bad2.generated_timestamp = datetime(2026, 9, 14, 10, 0, 10)
        sig_bad2.instrument_id = self.inst_id
        sig_bad2.signal_id = "SIG_BAD2"
        sig_bad2.fingerprint = "FP_BAD2"
        sig_bad2.strategy_id = "TEST"
        sig_bad2.trigger_mode = TriggerMode.BAR_CLOSE
        sig_bad2.direction = 1

        dec2 = self.engine.evaluate(sig_bad2, ctx)
        self.assertEqual(dec2.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec2.rejection.reason_code, RiskRejectionReason.INVALID_TIMESTAMPS)

    def test_future_dated_signal_rejection(self) -> None:
        """Requirement 9: Future-dated signal relative to evaluation clock rejects."""
        ctx = self._create_context(evaluation_timestamp=datetime(2026, 9, 14, 10, 0, 20))

        # Signal generated at 10:00:25, but evaluation clock is 10:00:20
        sig_future = self._create_signal(
            market_ts=datetime(2026, 9, 14, 10, 0, 15),
            avail_ts=datetime(2026, 9, 14, 10, 0, 20),
            gen_ts=datetime(2026, 9, 14, 10, 0, 25),  # Future dated!
        )
        dec = self.engine.evaluate(sig_future, ctx)
        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.SIGNAL_FUTURE_DATED)

    def test_expired_signal_rejection(self) -> None:
        """Requirement 10: Expired signal rejects with SIGNAL_EXPIRED."""
        # Evaluation clock is 10:00:30, signal expired at 10:00:25
        ctx = self._create_context(evaluation_timestamp=datetime(2026, 9, 14, 10, 0, 30))
        sig_expired = self._create_signal(
            market_ts=datetime(2026, 9, 14, 10, 0, 0),
            avail_ts=datetime(2026, 9, 14, 10, 0, 10),
            gen_ts=datetime(2026, 9, 14, 10, 0, 15),
            expiry_ts=datetime(2026, 9, 14, 10, 0, 25),  # Expired!
        )
        dec = self.engine.evaluate(sig_expired, ctx)
        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.SIGNAL_EXPIRED)

    def test_stale_signal_rejection(self) -> None:
        """Requirement 11: Stale signal exceeding max_signal_age_seconds rejects with SIGNAL_STALE."""
        # Max signal age = 60s. Availability = 10:00:00. Evaluation = 10:01:05 (65s age).
        ctx = self._create_context(evaluation_timestamp=datetime(2026, 9, 14, 10, 1, 5))
        sig_stale = self._create_signal(
            market_ts=datetime(2026, 9, 14, 10, 0, 0),
            avail_ts=datetime(2026, 9, 14, 10, 0, 0),
            gen_ts=datetime(2026, 9, 14, 10, 0, 5),
        )
        dec = self.engine.evaluate(sig_stale, ctx)
        self.assertEqual(dec.decision, RiskDecisionType.REJECTED)
        self.assertEqual(dec.rejection.reason_code, RiskRejectionReason.SIGNAL_STALE)

    def test_invalid_feature_quality_rejects(self) -> None:
        """Requirement 12: WARMING_UP, STALE, and INVALID feature quality reject."""
        sig = self._create_signal(
            market_ts=self.t_eval - timedelta(seconds=10),
            avail_ts=self.t_eval - timedelta(seconds=5),
            gen_ts=self.t_eval - timedelta(seconds=2),
        )

        for bad_quality in (FeatureQuality.WARMING_UP, FeatureQuality.STALE, FeatureQuality.INVALID):
            ctx = self._create_context(feature_quality=bad_quality)
            dec = self.engine.evaluate(sig, ctx)
            self.assertEqual(
                dec.decision,
                RiskDecisionType.REJECTED,
                f"Expected rejection for {bad_quality}",
            )
            self.assertEqual(
                dec.rejection.reason_code,
                RiskRejectionReason.DEGRADED_FEATURE_QUALITY,
                f"Expected DEGRADED_FEATURE_QUALITY for {bad_quality}",
            )

    def test_degraded_feature_behavior_follows_risk_limits(self) -> None:
        """Requirement 13: Degraded feature behavior follows RiskLimits.allow_degraded_features."""
        sig = self._create_signal(
            market_ts=self.t_eval - timedelta(seconds=10),
            avail_ts=self.t_eval - timedelta(seconds=5),
            gen_ts=self.t_eval - timedelta(seconds=2),
        )
        ctx = self._create_context(feature_quality=FeatureQuality.DEGRADED)

        # 1. Default: allow_degraded_features is False => REJECT
        engine_strict = RiskEngine(limits=RiskLimits(allow_degraded_features=False))
        dec_strict = engine_strict.evaluate(sig, ctx)
        self.assertEqual(dec_strict.decision, RiskDecisionType.REJECTED)
        self.assertEqual(
            dec_strict.rejection.reason_code, RiskRejectionReason.DEGRADED_FEATURE_QUALITY
        )

        # 2. Permissive: allow_degraded_features is True => APPROVE
        engine_permissive = RiskEngine(limits=RiskLimits(allow_degraded_features=True))
        dec_permissive = engine_permissive.evaluate(sig, ctx)
        self.assertEqual(dec_permissive.decision, RiskDecisionType.APPROVED)
        self.assertIsNotNone(dec_permissive.approved_intent)


if __name__ == "__main__":
    unittest.main()
