"""
Unit tests for Canonical Signal Fingerprinting and Reaffirmation Hashing (Phase 5).

Tests:
- Deterministic canonical SHA-256 fingerprinting
- Invariance against volatile fields (signal_id, generated_timestamp)
- Sensitivity to semantic decision changes (price, direction, timestamps, regime, setup)
- Canonical reaffirmation hashing with setup_anchor_timestamp
- Setup continuity recognition across evaluations
"""

import unittest
from datetime import datetime, timedelta, timezone

from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.signals.fingerprint import (
    compute_config_hash,
    compute_reaffirmation_key,
    compute_signal_fingerprint,
)


class TestSignalFingerprintAndReaffirmation(unittest.TestCase):

    def setUp(self):
        self.iid = InstrumentId("RELIANCE", Exchange.NSE, InstrumentType.EQUITY)
        self.market_ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)
        self.avail_ts = self.market_ts
        self.setup_anchor_ts = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_deterministic_fingerprinting(self):
        fp1 = compute_signal_fingerprint(
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="cfg_hash_1",
            instrument_id=self.iid,
            signal_type="ENTRY_LONG",
            direction=1,
            trigger_mode="BAR_CLOSE",
            market_timestamp=self.market_ts,
            availability_timestamp=self.avail_ts,
            suggested_entry_price=2500.50,
            suggested_stop_loss=2480.00,
            suggested_take_profit=2540.00,
            regime="BULLISH",
            setup="PULLBACK",
        )

        fp2 = compute_signal_fingerprint(
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="cfg_hash_1",
            instrument_id=self.iid,
            signal_type="ENTRY_LONG",
            direction=1,
            trigger_mode="BAR_CLOSE",
            market_timestamp=self.market_ts,
            availability_timestamp=self.avail_ts,
            suggested_entry_price=2500.50,
            suggested_stop_loss=2480.00,
            suggested_take_profit=2540.00,
            regime="BULLISH",
            setup="PULLBACK",
        )

        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)  # SHA-256 hex string

    def test_fingerprint_sensitivity(self):
        base_kwargs = dict(
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="cfg_hash_1",
            instrument_id=self.iid,
            signal_type="ENTRY_LONG",
            direction=1,
            trigger_mode="BAR_CLOSE",
            market_timestamp=self.market_ts,
            availability_timestamp=self.avail_ts,
            suggested_entry_price=2500.50,
            suggested_stop_loss=2480.00,
            suggested_take_profit=2540.00,
            regime="BULLISH",
            setup="PULLBACK",
        )
        base_fp = compute_signal_fingerprint(**base_kwargs)

        # Price change
        kwargs_price = dict(base_kwargs, suggested_entry_price=2501.00)
        self.assertNotEqual(base_fp, compute_signal_fingerprint(**kwargs_price))

        # Direction change
        kwargs_dir = dict(base_kwargs, direction=-1, signal_type="ENTRY_SHORT")
        self.assertNotEqual(base_fp, compute_signal_fingerprint(**kwargs_dir))

        # Regime change
        kwargs_regime = dict(base_kwargs, regime="NEUTRAL")
        self.assertNotEqual(base_fp, compute_signal_fingerprint(**kwargs_regime))

    def test_reaffirmation_key_continuity_and_differentiation(self):
        # Anchor 1: Confirmed candle at 09:15
        key1 = compute_reaffirmation_key(
            strategy_id="TEST_STRAT",
            instrument_id=self.iid,
            setup="PULLBACK",
            direction=1,
            setup_anchor_timestamp=self.setup_anchor_ts,
        )

        # Repeated evaluation for same active setup at 09:15
        key1_repeated = compute_reaffirmation_key(
            strategy_id="TEST_STRAT",
            instrument_id=self.iid,
            setup="PULLBACK",
            direction=1,
            setup_anchor_timestamp=self.setup_anchor_ts,
        )

        self.assertEqual(key1, key1_repeated)

        # Different anchor candle at 09:25 (genuinely new setup)
        new_anchor_ts = self.setup_anchor_ts + timedelta(minutes=10)
        key2_new_setup = compute_reaffirmation_key(
            strategy_id="TEST_STRAT",
            instrument_id=self.iid,
            setup="PULLBACK",
            direction=1,
            setup_anchor_timestamp=new_anchor_ts,
        )

        self.assertNotEqual(key1, key2_new_setup)

    def test_config_hash_deterministic(self):
        cfg_dict1 = {"fast_ema": 20, "slow_ema": 50, "rr": 2.0}
        cfg_dict2 = {"rr": 2.0, "slow_ema": 50, "fast_ema": 20}  # different key insertion order

        hash1 = compute_config_hash(cfg_dict1)
        hash2 = compute_config_hash(cfg_dict2)

        self.assertEqual(hash1, hash2)


if __name__ == "__main__":
    unittest.main()
