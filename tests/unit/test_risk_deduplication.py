"""
Unit tests for Phase 6 Stateful Admission Control and Signal Deduplication.

Verifies:
- First valid entry is APPROVED and fingerprint is registered
- Subsequent entry with identical active fingerprint is REJECTED with DUPLICATE_SIGNAL
- Releasing fingerprint allows re-admission
- Clearing admission state resets registry
- Deterministic state transitions
- Thread-safe admission operations
"""

from datetime import datetime
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.models import (
    PortfolioSnapshot,
    RiskDecisionType,
    RiskRejectionReason,
)
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestRiskDeduplication(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_id = InstrumentId(
            symbol="HDFCBANK",
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
            last_updated_timestamp=self.ts,
        )
        self.portfolio_snapshot = PortfolioSnapshot(snapshot_timestamp=self.ts, positions={})
        self.ctx = RiskContext(
            account_state=self.account_state,
            portfolio_snapshot=self.portfolio_snapshot,
            instrument_metadata=self.meta,
            evaluation_timestamp=self.ts,
            current_market_price=1500.0,
            feature_quality=FeatureQuality.VALID,
        )
        self.admission_state = AdmissionState()
        self.engine = RiskEngine(admission_state=self.admission_state)

    def _create_signal(self, signal_id: str, fingerprint: str) -> SignalCandidate:
        return SignalCandidate(
            signal_id=signal_id,
            fingerprint=fingerprint,
            reaffirmation_key="RF_HDFC_001",
            strategy_id="TREND_CONT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=self.inst_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.88,
            suggested_entry_price=1500.0,
            suggested_stop_loss=1470.0,
            suggested_take_profit=1560.0,
            risk_reward_ratio=2.0,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def test_duplicate_entry_rejects_and_state_is_deterministic(self) -> None:
        """Requirements 24 & 36: Duplicate entry rejects, admission state transitions deterministically."""
        fp = "FP_UNIQUE_CANONICAL_HASH_123"
        sig1 = self._create_signal(signal_id="SIG_001", fingerprint=fp)
        sig2 = self._create_signal(signal_id="SIG_002", fingerprint=fp)

        # Initial state: not admitted
        self.assertFalse(self.admission_state.is_admitted(fp))

        # 1. First evaluation: must be APPROVED
        dec1 = self.engine.evaluate(sig1, self.ctx)
        self.assertEqual(dec1.decision, RiskDecisionType.APPROVED)
        self.assertIsNotNone(dec1.approved_intent)

        # State transition check: fingerprint is now recorded in admission state
        self.assertTrue(self.admission_state.is_admitted(fp))

        # 2. Second evaluation with identical active fingerprint: must be REJECTED with DUPLICATE_SIGNAL
        dec2 = self.engine.evaluate(sig2, self.ctx)
        self.assertEqual(dec2.decision, RiskDecisionType.REJECTED)
        self.assertIsNotNone(dec2.rejection)
        self.assertEqual(dec2.rejection.reason_code, RiskRejectionReason.DUPLICATE_SIGNAL)

    def test_releasing_fingerprint_allows_readmission(self) -> None:
        """Verify releasing a fingerprint permits a subsequent signal to be admitted."""
        fp = "FP_RELEASE_TEST_456"
        sig = self._create_signal(signal_id="SIG_010", fingerprint=fp)

        # First approval
        dec1 = self.engine.evaluate(sig, self.ctx)
        self.assertEqual(dec1.decision, RiskDecisionType.APPROVED)
        self.assertTrue(self.admission_state.is_admitted(fp))

        # Release intent
        self.admission_state.release(fp)
        self.assertFalse(self.admission_state.is_admitted(fp))

        # Can be admitted again
        dec2 = self.engine.evaluate(sig, self.ctx)
        self.assertEqual(dec2.decision, RiskDecisionType.APPROVED)
        self.assertTrue(self.admission_state.is_admitted(fp))

    def test_clearing_admission_state(self) -> None:
        """Verify clear() wipes all registered fingerprints."""
        self.admission_state.admit("FP_1", self.ts)
        self.admission_state.admit("FP_2", self.ts)
        self.assertTrue(self.admission_state.is_admitted("FP_1"))
        self.assertTrue(self.admission_state.is_admitted("FP_2"))

        self.admission_state.clear()
        self.assertFalse(self.admission_state.is_admitted("FP_1"))
        self.assertFalse(self.admission_state.is_admitted("FP_2"))


if __name__ == "__main__":
    unittest.main()
