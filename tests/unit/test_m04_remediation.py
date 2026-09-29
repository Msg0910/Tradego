"""
Regression tests for M-04 IntentState.from_str fail-closed and WAL replay quarantine:
- Valid states including RISK_REJECTED parse correctly
- Unknown strings raise ValueError
- Empty strings raise ValueError
- Corrupted intent state during WAL replay is quarantined and not reinterpreted as PENDING_APPROVAL
"""

import os
import shutil
import tempfile
import unittest

from gateway.intent import IntentState
from gateway.persistence import PersistenceManager


class TestM04IntentStateFailClosed(unittest.TestCase):
    """M-04 IntentState.from_str and WAL replay quarantine regression suite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.temp_dir, "test_wal_m04.jsonl")
        self.pm = PersistenceManager(wal_path=self.wal_path, auto_sync=True)

    def tearDown(self) -> None:
        try:
            self.pm.close()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_intent_state_from_str_risk_rejected_preserved(self) -> None:
        """Valid states including RISK_REJECTED parse cleanly and preserve exact enum identity."""
        self.assertEqual(IntentState.from_str("RISK_REJECTED"), IntentState.RISK_REJECTED)
        self.assertEqual(IntentState.from_str("risk_rejected"), IntentState.RISK_REJECTED)
        self.assertEqual(IntentState.from_str("  RISK_REJECTED  "), IntentState.RISK_REJECTED)

        # Check other canonical states
        for state in IntentState:
            self.assertEqual(IntentState.from_str(state.value), state)
            self.assertEqual(IntentState.from_str(state.value.lower()), state)

    def test_intent_state_from_str_unknown_raises(self) -> None:
        """Unknown or unrecognized strings fail closed with ValueError."""
        unknown_states = [
            "UNKNOWN_STATE",
            "GARBAGE_STATE_XYZ",
            "PENDING_SOMETHING_ELSE",
            "APPROVE",  # not APPROVED
            "REJECT",   # not REJECTED
            "DRAFTING", # not DRAFT
        ]
        for bad_state in unknown_states:
            with self.assertRaises(ValueError, msg=f"Should raise ValueError for {bad_state}"):
                IntentState.from_str(bad_state)

    def test_intent_state_from_str_empty_raises(self) -> None:
        """Empty, whitespace-only, or None values fail closed with ValueError."""
        empty_inputs = ["", "   ", "\t", "\n", None]
        for empty_val in empty_inputs:
            with self.assertRaises(ValueError, msg=f"Should raise ValueError for {empty_val!r}"):
                IntentState.from_str(empty_val)

    def test_wal_replay_corrupt_state_quarantined(self) -> None:
        """Corrupted intent state in WAL is quarantined and does NOT silently become PENDING_APPROVAL."""
        valid_intent_id = "INT-M04-VALID-01"
        corrupt_intent_id = "INT-M04-CORRUPT-02"

        # Valid intent record
        self.pm.append(
            record_type="INTENT",
            record_id=valid_intent_id,
            payload={
                "intent_id": valid_intent_id,
                "symbol": "NSE:INFY",
                "side": "BUY",
                "quantity": 10,
                "order_type": "LIMIT",
                "limit_price": 1500.0,
                "state": "APPROVED",
                "creator_id": "operator_1",
            },
        )

        # Corrupted intent record with unrecognized state
        self.pm.append(
            record_type="INTENT",
            record_id=corrupt_intent_id,
            payload={
                "intent_id": corrupt_intent_id,
                "symbol": "NSE:TCS",
                "side": "BUY",
                "quantity": 20,
                "order_type": "LIMIT",
                "limit_price": 3500.0,
                "state": "TOTALLY_CORRUPT_UNKNOWN_STATE",
                "creator_id": "operator_2",
            },
        )

        replayed = self.pm.replay()

        # Valid intent is preserved in active intents
        self.assertIn(valid_intent_id, replayed["intents"])
        self.assertEqual(replayed["intents"][valid_intent_id].state, IntentState.APPROVED)

        # Corrupted intent is NOT present in active intents and is NOT converted to PENDING_APPROVAL
        self.assertNotIn(corrupt_intent_id, replayed["intents"])

        # Corrupted intent is recorded in quarantines
        quarantined_ids = [q.get("record_id") for q in replayed["quarantines"]]
        self.assertIn(corrupt_intent_id, quarantined_ids)

        # Verify quarantine record details
        corrupted_q = next(q for q in replayed["quarantines"] if q.get("record_id") == corrupt_intent_id)
        self.assertEqual(corrupted_q.get("record_type"), "INTENT")
        self.assertEqual(corrupted_q.get("raw_state"), "TOTALLY_CORRUPT_UNKNOWN_STATE")
        self.assertIn("CORRUPTED_INTENT_STATE", corrupted_q.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
