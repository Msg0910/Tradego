"""
Regression tests for M-02 WAL Replay remediation:
- Explicit Last-Write-Wins (LWW) entity snapshot replay
- Fill deduplication preventing double-counting
- Corrupted line tolerance option
- Truncated trailing line tolerance
"""

import json
import os
import shutil
import tempfile
import unittest

from gateway.intent import IntentState
from gateway.persistence import PersistenceManager, WALWriter


class TestM02WALReplay(unittest.TestCase):
    """M-02 WAL replay regression suite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.temp_dir, "test_wal_m02.jsonl")
        self.pm = PersistenceManager(wal_path=self.wal_path, auto_sync=True)

    def tearDown(self) -> None:
        try:
            self.pm.close()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_wal_duplicate_intent_record_last_wins(self) -> None:
        """Sequential records for the same intent follow Last-Write-Wins (LWW) snapshot semantics."""
        intent_id = "INT-M02-001"
        self.pm.append(
            record_type="INTENT",
            record_id=intent_id,
            payload={
                "intent_id": intent_id,
                "symbol": "NSE:INFY",
                "side": "BUY",
                "quantity": 25,
                "order_type": "LIMIT",
                "limit_price": 1600.0,
                "state": "PENDING_APPROVAL",
                "creator_id": "operator_1",
            },
        )

        # Later event: intent approved
        self.pm.append(
            record_type="INTENT",
            record_id=intent_id,
            payload={
                "intent_id": intent_id,
                "symbol": "NSE:INFY",
                "side": "BUY",
                "quantity": 25,
                "order_type": "LIMIT",
                "limit_price": 1600.0,
                "state": "APPROVED",
                "creator_id": "operator_1",
                "approver_id": "approver_super",
                "approved_at": "2026-09-23T12:00:00+00:00",
            },
        )

        replayed = self.pm.replay()
        self.assertIn(intent_id, replayed["intents"])
        replayed_intent = replayed["intents"][intent_id]
        self.assertEqual(replayed_intent.state, IntentState.APPROVED)
        self.assertEqual(replayed_intent.approver_id, "approver_super")
        self.assertIsNotNone(replayed_intent.approved_at)

    def test_wal_duplicate_fill_does_not_double_count(self) -> None:
        """Duplicate fill records with identical fill_id are deduplicated and do not double-count."""
        exec_id = "EXEC-M02-100"
        fill_id = "FILL-M02-999"

        # Record execution
        self.pm.append(
            record_type="EXECUTION",
            record_id=exec_id,
            payload={
                "execution_id": exec_id,
                "symbol": "NSE:TCS",
                "side": "BUY",
                "quantity": 10,
                "current_state": "FILLED",
            },
        )

        # Append original fill
        self.pm.append(
            record_type="FILL",
            record_id=fill_id,
            payload={
                "execution_id": exec_id,
                "quantity": 10,
                "price": 3500.0,
                "broker_fill_id": "BF-001",
            },
        )

        # Append duplicate fill record with identical fill_id
        self.pm.append(
            record_type="FILL",
            record_id=fill_id,
            payload={
                "execution_id": exec_id,
                "quantity": 10,
                "price": 3500.0,
                "broker_fill_id": "BF-001",
            },
        )

        replayed = self.pm.replay()
        self.assertIn(exec_id, replayed["executions"])
        # Exactly 1 fill in global fills list
        self.assertEqual(len(replayed["fills"]), 1)
        self.assertEqual(replayed["fills"][0].fill_id, fill_id)

        # Execution record also only contains 1 fill
        exec_record = replayed["executions"][exec_id]
        self.assertEqual(len(exec_record.fills), 1)
        self.assertEqual(exec_record.fills[0].fill_id, fill_id)
        self.assertEqual(exec_record.fills[0].quantity, 10)

    def test_wal_corrupted_line_tolerated(self) -> None:
        """Corrupted lines in the WAL are tolerated when tolerate_corrupted=True."""
        intent_1 = "INT-CORRUPT-1"
        intent_2 = "INT-CORRUPT-2"

        self.pm.append(
            record_type="INTENT",
            record_id=intent_1,
            payload={"intent_id": intent_1, "symbol": "NSE:SBIN", "state": "DRAFT"},
        )
        self.pm.close()

        # Inject corrupted line in the middle
        with open(self.wal_path, "a", encoding="utf-8") as f:
            f.write("<<<CORRUPTED_NON_JSON_CORRUPTED_LINE>>>\n")

        # Open writer again and append second valid event
        writer = WALWriter(self.wal_path, auto_sync=True)
        writer.append(
            record_type="INTENT",
            record_id=intent_2,
            payload={"intent_id": intent_2, "symbol": "NSE:WIPRO", "state": "DRAFT"},
        )
        writer.close()

        pm_reader = PersistenceManager(wal_path=self.wal_path)

        # Default replay must fail on corrupted mid-log lines
        with self.assertRaises(ValueError) as ctx:
            pm_reader.replay(tolerate_corrupted=False)
        self.assertIn("WAL_PARSE_ERROR", str(ctx.exception))

        # Replay with tolerate_corrupted=True skips corrupted line and rehydrates clean entities
        replayed = pm_reader.replay(tolerate_corrupted=True)
        self.assertIn(intent_1, replayed["intents"])
        self.assertIn(intent_2, replayed["intents"])
        pm_reader.close()

    def test_wal_truncated_replayed_correctly(self) -> None:
        """Trailing incomplete/truncated write from an abrupt crash is safely ignored during replay."""
        intent_id = "INT-TRUNC-1"

        self.pm.append(
            record_type="INTENT",
            record_id=intent_id,
            payload={"intent_id": intent_id, "symbol": "NSE:HDFCBANK", "state": "DRAFT"},
        )
        self.pm.close()

        # Simulate trailing partial line from sudden power loss or process kill
        with open(self.wal_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "event_id": "wal-crash", "record_type": "INTENT", "paylo')

        pm_reader = PersistenceManager(wal_path=self.wal_path)
        # Default replay tolerates trailing partial line
        replayed = pm_reader.replay()
        self.assertIn(intent_id, replayed["intents"])
        self.assertEqual(replayed["intents"][intent_id].symbol, "NSE:HDFCBANK")
        pm_reader.close()


if __name__ == "__main__":
    unittest.main()
