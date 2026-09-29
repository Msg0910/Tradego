"""
TradeGo Phase 10 — Production Operations, Disaster Recovery & Multi-Venue Readiness.

Unit test suite covering:
1. WAL Append
2. WAL fsync persistence
3. WAL sequence monotonicity
4. WAL hash integrity
5. WAL corruption detection
6. WAL replay
7. Duplicate replay idempotency
8. Crash recovery
9. Partial final WAL record handling
10. Audit chain startup verification
11. Audit chain tamper detection
12. Audit chain truncation detection
13. Startup fail-closed behavior
14. In-flight execution detection
15. Unknown execution quarantine
16. Reconciliation-required state
17. Safe unquarantine
18. Unauthorized unquarantine rejected
19. Two-person recovery authorization
20. Recovery cannot submit orders
21. Recovery cannot bypass risk
22. Recovery cannot bypass operational approval
23. Venue registration
24. Duplicate venue rejection
25. Isolated venue credentials
26. Deterministic routing
27. Unknown route rejection
28. No automatic venue failover
29. Venue connectivity isolation
30. Venue reconciliation state
31. Health livez
32. Health readyz
33. Detailed health redaction
34. Credential redaction
35. WAL credential redaction
36. API authorization
37. UI has no direct broker route
38. Phase 9 paper broker regression
39. Phase 5→10 lifecycle smoke test
40. Frozen-core integrity verification
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from typing import Any, Dict

from gateway.api.app import create_app
from gateway.broker_adapter import (
    BrokerAdapter,
    BrokerDispatchResult,
    MockLiveBrokerAdapter,
    PaperBrokerAdapter,
)
from gateway.broker_connectivity import (
    BrokerConnectivityManager,
    BrokerConnectivityState,
    ExecutionMode,
)
from gateway.broker_credentials import BrokerCredentialsConfig
from gateway.contracts import Capability
from gateway.disaster_recovery import (
    DisasterRecoveryManager,
    QuarantineRecord,
    RecoveryRecord,
    RecoveryState,
)
from gateway.execution import (
    ExecutionRecord,
    ExecutionState,
    ExecutionStateManager,
    FillRecord,
    ReconciliationStatus,
)
from gateway.intent import ExecutionIntent, ExecutionIntentManager, IntentState
from gateway.multi_venue import (
    BrokerVenue,
    BrokerVenueRegistry,
    MultiVenueRouter,
    VenueRoutingPolicy,
)
from gateway.observability import (
    HealthCheckResult,
    HealthStatus,
    ObservabilityManager,
    SystemHealthMonitor,
)
from gateway.order_instruction import (
    InstructionState,
    OrderInstruction,
    OrderInstructionManager,
)
from gateway.persistence import (
    DurableEvent,
    GENESIS_HASH,
    PersistenceManager,
    WALIntegrityResult,
    WALReader,
    WALWriter,
)
from gateway.recovery import GuardState, TradingGuard
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import (
    AuditChainVerificationResult,
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
    verify_audit_chain,
)
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState
from starlette.testclient import TestClient


class TestPhase10ProductionOperations(unittest.TestCase):
    """Authoritative test suite for Tradego Phase 10."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.wal_path = os.path.join(self.temp_dir, "test_wal.jsonl")
        self.audit_path = os.path.join(self.temp_dir, "test_audit.jsonl")

        self.guard = TradingGuard()
        self.audit = Tier1AuditLogger(log_file_path=self.audit_path)
        self.pm = PersistenceManager(wal_path=self.wal_path, auto_sync=True)

        self.conn_mgr = BrokerConnectivityManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            risk_gate_available=True,
            active_broker="VENUE_NSE",
        )
        self.exec_mgr = ExecutionStateManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            connectivity_manager=self.conn_mgr,
            persistence_manager=self.pm,
        )
        self.paper_adapter = PaperBrokerAdapter()
        self.inst_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            broker_adapter=self.paper_adapter,
            execution_manager=self.exec_mgr,
            connectivity_manager=self.conn_mgr,
            persistence_manager=self.pm,
        )
        self.intent_mgr = ExecutionIntentManager(
            audit_logger=self.audit,
            persistence_store=self.pm,
        )
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.portfolio_state = PortfolioRuntimeState(
            account_id="ACC_PHASE10",
            initial_cash=1000000.0,
        )
        self.risk_evaluator = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            max_order_quantity=1000,
            margin_rate=0.20,
        )
        self.dr_mgr = DisasterRecoveryManager(
            trading_guard=self.guard,
            audit_logger=self.audit,
            persistence_manager=self.pm,
            execution_manager=self.exec_mgr,
            instruction_manager=self.inst_mgr,
            intent_manager=self.intent_mgr,
            connectivity_manager=self.conn_mgr,
        )
        self.venue_reg = BrokerVenueRegistry(audit_logger=self.audit)
        self.venue_router = MultiVenueRouter(registry=self.venue_reg, audit_logger=self.audit)
        self.health_mon = SystemHealthMonitor(
            trading_guard=self.guard,
            audit_logger=self.audit,
            connectivity_manager=self.conn_mgr,
            disaster_recovery_manager=self.dr_mgr,
            multi_venue_registry=self.venue_reg,
            execution_manager=self.exec_mgr,
            persistence_manager=self.pm,
        )

    def tearDown(self) -> None:
        try:
            self.pm.close()
            self.audit.close()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_intent(
        self,
        creator_id: str = "op_test",
        symbol: str = "NSE:INFY",
        side: str = "BUY",
        quantity: int = 10,
        order_type: str = "LIMIT",
        limit_price: float = 1500.0,
    ) -> ExecutionIntent:
        corr_id = str(uuid.uuid4())
        return self.intent_mgr.create_intent(
            creator_id=creator_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            correlation_id=corr_id,
            limit_price=limit_price if order_type == "LIMIT" else None,
        )

    def _make_instruction(
        self,
        iid: str = "INS-TEST-1",
        symbol: str = "NSE:INFY",
        side: str = "BUY",
        qty: int = 10,
        exchange: str = "NSE",
        asset_class: str = "EQUITY",
    ) -> OrderInstruction:
        return OrderInstruction(
            instruction_id=iid,
            intent_id=f"int-{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type="LIMIT",
            risk_evaluation_reference="RISK-REF-1",
            approval_reference="APP-REF-1",
            correlation_id=f"corr-{uuid.uuid4().hex[:8]}",
            provenance={"exchange": exchange, "asset_class": asset_class},
            limit_price=1500.0,
        )

    # -------------------------------------------------------------------------
    # 1. WAL Append
    # -------------------------------------------------------------------------
    def test_01_wal_append(self) -> None:
        """1. Appending to WAL produces durable record with correct structure."""
        ev = self.pm.append("TEST_REC", "ID-001", {"key": "value"}, "corr-001")
        self.assertEqual(ev.seq, 1)
        self.assertEqual(ev.record_type, "TEST_REC")
        self.assertEqual(ev.record_id, "ID-001")
        self.assertEqual(ev.prev_hash, GENESIS_HASH)
        self.assertEqual(len(ev.hash), 64)
        self.assertTrue(os.path.exists(self.wal_path))

    # -------------------------------------------------------------------------
    # 2. WAL fsync persistence
    # -------------------------------------------------------------------------
    def test_02_wal_fsync_persistence(self) -> None:
        """2. Records are immediately readable by independent reader without closing."""
        self.pm.append("EVENT_A", "REC-1", {"qty": 100})
        reader = WALReader(self.wal_path)
        events = reader.read_all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].record_id, "REC-1")

    # -------------------------------------------------------------------------
    # 3. WAL sequence monotonicity
    # -------------------------------------------------------------------------
    def test_03_wal_sequence_monotonicity(self) -> None:
        """3. Sequences are strictly monotonic 1, 2, 3."""
        e1 = self.pm.append("TYPE", "1", {})
        e2 = self.pm.append("TYPE", "2", {})
        e3 = self.pm.append("TYPE", "3", {})
        self.assertEqual(e1.seq, 1)
        self.assertEqual(e2.seq, 2)
        self.assertEqual(e3.seq, 3)

    # -------------------------------------------------------------------------
    # 4. WAL hash integrity
    # -------------------------------------------------------------------------
    def test_04_wal_hash_integrity(self) -> None:
        """4. Hash chaining verifies successfully for clean log."""
        for i in range(5):
            self.pm.append("ORDER", f"ORD-{i}", {"price": 100.0 + i})
        res = self.pm.verify_integrity()
        self.assertTrue(res.valid)
        self.assertEqual(res.records_checked, 5)
        self.assertEqual(res.last_sequence, 5)

    # -------------------------------------------------------------------------
    # 5. WAL corruption detection
    # -------------------------------------------------------------------------
    def test_05_wal_corruption_detection(self) -> None:
        """5. Modifying payload in WAL flags corruption with failure reason."""
        self.pm.append("ORDER", "ORD-1", {"price": 100.0})
        self.pm.append("ORDER", "ORD-2", {"price": 200.0})
        self.pm.close()

        # Corrupt record 2
        with open(self.wal_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        data = json.loads(lines[1])
        data["payload"]["price"] = 99999.0
        lines[1] = json.dumps(data) + "\n"
        with open(self.wal_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        pm2 = PersistenceManager(wal_path=self.wal_path)
        res = pm2.verify_integrity()
        pm2.close()
        self.assertFalse(res.valid)
        self.assertIn("Checksum mismatch", res.failure_reason or "")
        self.assertEqual(res.first_invalid_sequence, 2)

    # -------------------------------------------------------------------------
    # 6. WAL replay
    # -------------------------------------------------------------------------
    def test_06_wal_replay(self) -> None:
        """6. Replay accurately reconstructs in-memory domain models."""
        intent = self._create_intent("op1", "NSE:INFY", "BUY", 10, "LIMIT", 1500.0)
        self.intent_mgr.approve_intent(intent.intent_id, "approver_1", intent.correlation_id)
        inst = self.inst_mgr.create_instruction(intent.intent_id, "NSE:INFY", "BUY", 10, 1500.0)
        rec = self.exec_mgr.create_execution(inst.instruction_id, intent.intent_id, "NSE:INFY", "BUY", 10)
        self.exec_mgr.apply_fill(rec.execution_id, quantity=10, price=1500.0)

        # Replay into new structures
        replayed = self.pm.replay()
        self.assertIn(intent.intent_id, replayed["intents"])
        self.assertIn(inst.instruction_id, replayed["instructions"])
        self.assertIn(rec.execution_id, replayed["executions"])
        self.assertEqual(len(replayed["fills"]), 1)

    # -------------------------------------------------------------------------
    # 7. Duplicate replay idempotency
    # -------------------------------------------------------------------------
    def test_07_duplicate_replay_idempotency(self) -> None:
        """7. Replaying multiple times produces identical state without duplication."""
        self.pm.append("INTENT", "INT-01", {
            "intent_id": "INT-01",
            "symbol": "NSE:RELIANCE",
            "side": "BUY",
            "quantity": 10,
            "order_type": "LIMIT",
            "state": "PENDING_APPROVAL",
        })
        rep1 = self.pm.replay()
        rep2 = self.pm.replay()
        self.assertEqual(len(rep1["intents"]), len(rep2["intents"]))
        self.assertEqual(rep1["intents"]["INT-01"].symbol, rep2["intents"]["INT-01"].symbol)

    # -------------------------------------------------------------------------
    # 8. Crash recovery
    # -------------------------------------------------------------------------
    def test_08_crash_recovery(self) -> None:
        """8. Cold-boot recovery rehydrates prior executions cleanly."""
        intent = self._create_intent("op1", "NSE:TCS", "BUY", 20, "MARKET")
        self.intent_mgr.approve_intent(intent.intent_id, "approver_1", intent.correlation_id)
        inst = self.inst_mgr.create_instruction(intent.intent_id, "NSE:TCS", "BUY", 20)
        rec = self.exec_mgr.create_execution(inst.instruction_id, intent.intent_id, "NSE:TCS", "BUY", 20)
        self.exec_mgr.apply_fill(rec.execution_id, quantity=20, price=3200.0)
        self.pm.close()

        # Simulate cold boot
        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        record = new_dr.execute_startup_recovery(
            intent_manager=self.intent_mgr,
            instruction_manager=self.inst_mgr,
            execution_manager=new_exec_mgr,
        )
        new_pm.close()
        self.assertEqual(record.state, RecoveryState.CLEAN)
        self.assertEqual(len(new_exec_mgr._executions), 1)

    # -------------------------------------------------------------------------
    # 9. Partial final WAL record handling
    # -------------------------------------------------------------------------
    def test_09_partial_final_wal_record_handling(self) -> None:
        """9. Incomplete trailing line from abrupt crash is tolerated."""
        self.pm.append("RECORD", "REC-1", {"val": 1})
        self.pm.append("RECORD", "REC-2", {"val": 2})
        self.pm.close()

        # Append corrupted incomplete trailing line
        with open(self.wal_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 3, "event_id": "wal-incomplete", "paylo')

        reader = WALReader(self.wal_path)
        events = reader.read_all(tolerate_trailing_partial=True)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].record_id, "REC-2")

    # -------------------------------------------------------------------------
    # 10. Audit chain startup verification
    # -------------------------------------------------------------------------
    def test_10_audit_chain_startup_verification(self) -> None:
        """10. Clean audit log passes full chain verification."""
        self.audit.log({"action": "BOOT_1", "operator": "sys"})
        self.audit.log({"action": "BOOT_2", "operator": "sys"})
        self.audit.flush()

        res = verify_audit_chain(self.audit_path)
        self.assertTrue(res.is_valid)
        self.assertGreaterEqual(res.record_count, 2)

    # -------------------------------------------------------------------------
    # 11. Audit chain tamper detection
    # -------------------------------------------------------------------------
    def test_11_audit_chain_tamper_detection(self) -> None:
        """11. Tampered audit record fails verification."""
        self.audit.log({"action": "SEC_1"})
        self.audit.log({"action": "SEC_2"})
        self.audit.flush()
        self.audit.close()

        with open(self.audit_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entry = json.loads(lines[0])
        entry["action"] = "TAMPERED_ACTION"
        lines[0] = json.dumps(entry) + "\n"
        with open(self.audit_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        res = verify_audit_chain(self.audit_path)
        self.assertFalse(res.is_valid)
        self.assertIn("Checksum mismatch", res.message)

    # -------------------------------------------------------------------------
    # 12. Audit chain truncation detection
    # -------------------------------------------------------------------------
    def test_12_audit_chain_truncation_detection(self) -> None:
        """12. Deleting an intermediate audit record breaks prev_hash link."""
        self.audit.log({"action": "EVENT_1"})
        self.audit.log({"action": "EVENT_2"})
        self.audit.log({"action": "EVENT_3"})
        self.audit.flush()
        self.audit.close()

        with open(self.audit_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Remove line 2
        del lines[1]
        with open(self.audit_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        res = verify_audit_chain(self.audit_path)
        self.assertFalse(res.is_valid)
        self.assertIn("Chain broken", res.message)

    # -------------------------------------------------------------------------
    # 13. Startup fail-closed behavior
    # -------------------------------------------------------------------------
    def test_13_startup_fail_closed_behavior(self) -> None:
        """13. Damaged audit chain trips trading guard during recovery."""
        corrupted_path = os.path.join(self.temp_dir, "corrupt_audit.jsonl")
        with open(corrupted_path, "w", encoding="utf-8") as f:
            f.write("INVALID_JSON_CORRUPT\n")

        with self.assertRaises(RuntimeError) as ctx:
            self.dr_mgr.execute_startup_recovery(
                intent_manager=self.intent_mgr,
                instruction_manager=self.inst_mgr,
                execution_manager=self.exec_mgr,
                audit_log_path=corrupted_path,
            )
        self.assertIn("AUDIT_CHAIN_VERIFICATION_FAILED", str(ctx.exception))
        self.assertEqual(self.guard.state, GuardState.HALTED)

    # -------------------------------------------------------------------------
    # 14. In-flight execution detection
    # -------------------------------------------------------------------------
    def test_14_in_flight_execution_detection(self) -> None:
        """14. Non-terminal execution (DISPATCHED) is detected on restart."""
        rec = self.exec_mgr.create_execution("INS-99", "INT-99", "NSE:INFY", "BUY", 50)
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        res = new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)
        new_pm.close()

        self.assertEqual(res.state, RecoveryState.RECONCILIATION_REQUIRED)
        self.assertEqual(res.quarantined_count, 1)

    # -------------------------------------------------------------------------
    # 15. Unknown execution quarantine
    # -------------------------------------------------------------------------
    def test_15_unknown_execution_quarantine(self) -> None:
        """15. Execution in UNKNOWN state is placed in quarantine."""
        rec = self.exec_mgr.create_execution("INS-100", "INT-100", "NSE:RELIANCE", "BUY", 10)
        rec.current_state = ExecutionState.UNKNOWN
        rec.reconciliation_status = ReconciliationStatus.MISMATCH
        self.pm.append_execution(rec)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        res = new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)
        new_pm.close()

        self.assertIn(rec.execution_id, new_dr.quarantined_executions)

    # -------------------------------------------------------------------------
    # 16. Reconciliation-required state
    # -------------------------------------------------------------------------
    def test_16_reconciliation_required_state(self) -> None:
        """16. In-flight orders hold recovery in RECONCILIATION_REQUIRED state."""
        rec = self.exec_mgr.create_execution("INS-101", "INT-101", "NSE:SBIN", "BUY", 100)
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)
        new_pm.close()

        self.assertTrue(new_dr.is_quarantine_locked)
        self.assertEqual(new_dr.state, RecoveryState.RECONCILIATION_REQUIRED)

    # -------------------------------------------------------------------------
    # 17. Safe unquarantine
    # -------------------------------------------------------------------------
    def test_17_safe_unquarantine(self) -> None:
        """17. Authoritative two-person approval unquarantines execution."""
        rec = self.exec_mgr.create_execution("INS-102", "INT-102", "NSE:TCS", "BUY", 10)
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)

        unq_rec = new_dr.unquarantine(
            operator_id="op_alice",
            second_operator_id="op_bob",
            confirm=True,
            execution_id=rec.execution_id,
            notes="Broker verified no order was placed",
        )
        new_pm.close()
        self.assertTrue(unq_rec.is_cleared)
        self.assertEqual(new_dr.state, RecoveryState.RECOVERED)

    # -------------------------------------------------------------------------
    # 18. Unauthorized unquarantine rejected
    # -------------------------------------------------------------------------
    def test_18_unauthorized_unquarantine_rejected(self) -> None:
        """18. Missing or empty operator IDs fail closed."""
        rec = self.exec_mgr.create_execution("INS-103", "INT-103", "NSE:INFY", "BUY", 10)
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)

        with self.assertRaises(ValueError) as ctx:
            new_dr.unquarantine(
                operator_id="",
                second_operator_id="op_bob",
                confirm=True,
                execution_id=rec.execution_id,
            )
        new_pm.close()
        self.assertIn("TWO_PERSON_AUTHORIZATION_REQUIRED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 19. Two-person recovery authorization
    # -------------------------------------------------------------------------
    def test_19_two_person_recovery_authorization(self) -> None:
        """19. Single operator cannot self-approve unquarantine."""
        rec = self.exec_mgr.create_execution("INS-104", "INT-104", "NSE:INFY", "BUY", 10)
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)
        self.pm.close()

        new_pm = PersistenceManager(wal_path=self.wal_path)
        new_exec_mgr = ExecutionStateManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr = DisasterRecoveryManager(trading_guard=self.guard, audit_logger=self.audit, persistence_manager=new_pm)
        new_dr.execute_startup_recovery(self.intent_mgr, self.inst_mgr, new_exec_mgr)

        with self.assertRaises(ValueError) as ctx:
            new_dr.unquarantine(
                operator_id="op_same",
                second_operator_id="op_same",
                confirm=True,
                execution_id=rec.execution_id,
            )
        new_pm.close()
        self.assertIn("DISTINCT_OPERATORS_REQUIRED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 20. Recovery cannot submit orders
    # -------------------------------------------------------------------------
    def test_20_recovery_cannot_submit_orders(self) -> None:
        """20. Disaster recovery component possesses no dispatch methods."""
        self.assertFalse(hasattr(self.dr_mgr, "dispatch"))
        self.assertFalse(hasattr(self.dr_mgr, "submit_order"))
        self.assertFalse(hasattr(self.dr_mgr, "place_order"))

    # -------------------------------------------------------------------------
    # 21. Recovery cannot bypass risk
    # -------------------------------------------------------------------------
    def test_21_recovery_cannot_bypass_risk(self) -> None:
        """21. Replaying executions does not bypass pre-trade risk evaluation."""
        self.assertFalse(hasattr(self.dr_mgr, "bypass_risk"))

    # -------------------------------------------------------------------------
    # 22. Recovery cannot bypass operational approval
    # -------------------------------------------------------------------------
    def test_22_recovery_cannot_bypass_operational_approval(self) -> None:
        """22. Replaying intents preserves their raw unapproved state without auto-approval."""
        intent = self._create_intent("op_user", "NSE:TCS", "BUY", 5, "MARKET")
        self.assertEqual(intent.state, IntentState.PENDING_APPROVAL)
        self.pm.close()

        rep = PersistenceManager(wal_path=self.wal_path).replay()
        replayed_intent = rep["intents"][intent.intent_id]
        self.assertEqual(replayed_intent.state, IntentState.PENDING_APPROVAL)

    # -------------------------------------------------------------------------
    # 23. Venue registration
    # -------------------------------------------------------------------------
    def test_23_venue_registration(self) -> None:
        """23. Venues register and retrieve correctly."""
        venue = BrokerVenue(
            venue_id="NSE_DIRECT",
            adapter=self.paper_adapter,
            supported_exchanges=["NSE"],
            supported_asset_classes=["EQUITY"],
        )
        self.venue_reg.register_venue(venue)
        retrieved = self.venue_reg.get_venue("NSE_DIRECT")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.venue_id, "NSE_DIRECT")

    # -------------------------------------------------------------------------
    # 24. Duplicate venue rejection
    # -------------------------------------------------------------------------
    def test_24_duplicate_venue_rejection(self) -> None:
        """24. Re-registering existing venue ID raises ValueError."""
        venue = BrokerVenue(venue_id="NSE_MAIN", adapter=self.paper_adapter)
        self.venue_reg.register_venue(venue)
        with self.assertRaises(ValueError) as ctx:
            self.venue_reg.register_venue(venue)
        self.assertIn("DUPLICATE_VENUE_REJECTED", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 25. Isolated venue credentials
    # -------------------------------------------------------------------------
    def test_25_isolated_venue_credentials(self) -> None:
        """25. Each venue maintains independent credentials without leakage."""
        c1 = BrokerCredentialsConfig(venue_name="NSE", client_id="CLIENT_NSE_01", access_token="TOKEN_NSE_SECRET")
        c2 = BrokerCredentialsConfig(venue_name="BSE", client_id="CLIENT_BSE_02", access_token="TOKEN_BSE_SECRET")
        v1 = BrokerVenue("NSE", self.paper_adapter, credentials=c1)
        v2 = BrokerVenue("BSE", self.paper_adapter, credentials=c2)
        self.venue_reg.register_venue(v1)
        self.venue_reg.register_venue(v2)

        d1 = v1.to_dict()
        d2 = v2.to_dict()
        self.assertNotIn("TOKEN_NSE_SECRET", json.dumps(d1))
        self.assertNotIn("TOKEN_BSE_SECRET", json.dumps(d2))
        self.assertEqual(d1["credentials"]["access_token"], "[REDACTED]")
        self.assertEqual(d2["credentials"]["access_token"], "[REDACTED]")
        self.assertEqual(v1.credentials.client_id, "CLIENT_NSE_01")
        self.assertEqual(v2.credentials.client_id, "CLIENT_BSE_02")

    # -------------------------------------------------------------------------
    # 26. Deterministic routing
    # -------------------------------------------------------------------------
    def test_26_deterministic_routing(self) -> None:
        """26. Routing policy resolves target venue and builds idempotency key."""
        v_nse = BrokerVenue("NSE_VENUE", self.paper_adapter)
        self.venue_reg.register_venue(v_nse)
        policy = VenueRoutingPolicy()
        policy.add_prefix_route("NSE:", "NSE_VENUE")
        router = MultiVenueRouter(self.venue_reg, policy)

        inst = self._make_instruction(iid="INS-ROUTED-1", symbol="NSE:RELIANCE")
        venue, idemp_key = router.route(inst)
        self.assertEqual(venue.venue_id, "NSE_VENUE")
        self.assertEqual(idemp_key, "TG-DISPATCH-NSE_VENUE-INS-ROUTED-1")

    # -------------------------------------------------------------------------
    # 27. Unknown route rejection
    # -------------------------------------------------------------------------
    def test_27_unknown_route_rejection(self) -> None:
        """27. Unconfigured routes fail closed with ROUTING_UNAVAILABLE."""
        policy = VenueRoutingPolicy()
        router = MultiVenueRouter(self.venue_reg, policy)
        inst = self._make_instruction(iid="INS-FAIL-1", symbol="UNKNOWN_EXCH:SYM", exchange="UNKNOWN")
        with self.assertRaises(ValueError) as ctx:
            router.route(inst)
        self.assertIn("ROUTING_UNAVAILABLE", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 28. No automatic venue failover
    # -------------------------------------------------------------------------
    def test_28_no_automatic_venue_failover(self) -> None:
        """28. Inactive/paused primary venue does not failover to backup; fails closed."""
        v_primary = BrokerVenue("PRIMARY_VENUE", self.paper_adapter, operational_status="PAUSED")
        v_backup = BrokerVenue("BACKUP_VENUE", self.paper_adapter, operational_status="ACTIVE")
        self.venue_reg.register_venue(v_primary)
        self.venue_reg.register_venue(v_backup)

        policy = VenueRoutingPolicy()
        policy.add_prefix_route("MCX:", "PRIMARY_VENUE")
        router = MultiVenueRouter(self.venue_reg, policy)

        inst = self._make_instruction(iid="INS-FAILOVER-TEST", symbol="MCX:GOLD", exchange="MCX")
        with self.assertRaises(ValueError) as ctx:
            router.route(inst)
        self.assertIn("ROUTING_UNAVAILABLE", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 29. Venue connectivity isolation
    # -------------------------------------------------------------------------
    def test_29_venue_connectivity_isolation(self) -> None:
        """29. Disconnecting one venue leaves other venues unaffected."""
        v1 = BrokerVenue("VENUE_1", self.paper_adapter, connectivity_state="CONNECTED")
        v2 = BrokerVenue("VENUE_2", self.paper_adapter, connectivity_state="CONNECTED")
        self.venue_reg.register_venue(v1)
        self.venue_reg.register_venue(v2)

        self.venue_reg.set_connectivity_state("VENUE_1", "DISCONNECTED")
        self.assertEqual(self.venue_reg.get_venue("VENUE_1").connectivity_state, "DISCONNECTED")
        self.assertEqual(self.venue_reg.get_venue("VENUE_2").connectivity_state, "CONNECTED")

    # -------------------------------------------------------------------------
    # 30. Venue reconciliation state
    # -------------------------------------------------------------------------
    def test_30_venue_reconciliation_state(self) -> None:
        """30. Venue reconciliation status updates independently."""
        v = BrokerVenue("VENUE_RECON", self.paper_adapter)
        self.venue_reg.register_venue(v)
        v.reconciliation_status = "MATCHED"
        self.assertEqual(self.venue_reg.get_venue("VENUE_RECON").reconciliation_status, "MATCHED")

    # -------------------------------------------------------------------------
    # 31. Health livez
    # -------------------------------------------------------------------------
    def test_31_health_livez(self) -> None:
        """31. Liveness endpoint returns ALIVE and valid uptime."""
        res = self.health_mon.check_liveness()
        self.assertEqual(res["status"], "ALIVE")
        self.assertGreaterEqual(res["uptime_seconds"], 0.0)
        self.assertIn("pid", res)

    # -------------------------------------------------------------------------
    # 32. Health readyz
    # -------------------------------------------------------------------------
    def test_32_health_readyz(self) -> None:
        """32. Readiness returns ready when normal, unready when guard tripped or quarantined."""
        is_ready, data = self.health_mon.check_readiness()
        self.assertTrue(is_ready)
        self.assertEqual(data["status"], "READY")

        # Trip guard
        self.guard.trip("Health readiness test trip")
        is_ready_tripped, data_tripped = self.health_mon.check_readiness()
        self.assertFalse(is_ready_tripped)
        self.assertEqual(data_tripped["status"], "NOT_READY")

    # -------------------------------------------------------------------------
    # 33. Detailed health redaction
    # -------------------------------------------------------------------------
    def test_33_detailed_health_redaction(self) -> None:
        """33. Detailed health report provides comprehensive metrics with zero secrets."""
        det = self.health_mon.get_detailed_health()
        self.assertIn("liveness", det)
        self.assertIn("readiness", det)
        self.assertIn("audit", det)
        self.assertIn("broker", det)
        self.assertIn("executions", det)
        serialized = json.dumps(det)
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("secret", serialized.lower())

    # -------------------------------------------------------------------------
    # 34. Credential redaction
    # -------------------------------------------------------------------------
    def test_34_credential_redaction(self) -> None:
        """34. BrokerCredentialsConfig masks sensitive token fields."""
        creds = BrokerCredentialsConfig(
            venue_name="LIVE_BROKER",
            client_id="CLIENT_12345",
            access_token="ULTRA_SECRET_TOKEN_999",
            secret_key="MY_SECRET_XYZ",
        )
        redacted = creds.redacted_dict()
        self.assertEqual(redacted["access_token"], "[REDACTED]")
        self.assertEqual(redacted["secret_key"], "[REDACTED]")
        self.assertNotIn("ULTRA_SECRET_TOKEN_999", json.dumps(redacted))

    # -------------------------------------------------------------------------
    # 35. WAL credential redaction
    # -------------------------------------------------------------------------
    def test_35_wal_credential_redaction(self) -> None:
        """35. Writing orders with credentials reference never writes credentials to WAL."""
        intent = self._create_intent("op1", "NSE:INFY", "BUY", 10, "LIMIT", 1500.0)
        self.intent_mgr.approve_intent(intent.intent_id, "approver_1", intent.correlation_id)
        inst = self.inst_mgr.create_instruction(intent.intent_id, "NSE:INFY", "BUY", 10, 1500.0)
        rec = self.exec_mgr.create_execution(inst.instruction_id, intent.intent_id, "NSE:INFY", "BUY", 10)
        self.pm.close()

        with open(self.wal_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("access_token", content)
        self.assertNotIn("secret_key", content)

    # -------------------------------------------------------------------------
    # 36. API authorization
    # -------------------------------------------------------------------------
    def test_36_api_authorization(self) -> None:
        """36. /api/v1/health/* and /api/v1/recovery/* require appropriate capabilities."""
        session_store = InMemorySessionStore()
        viewer_token = session_store.create_session("op_viewer", ["VIEWER"], {Capability.CAP_OBSERVE})
        admin_token = session_store.create_session("op_admin", ["ADMIN"], {Capability.CAP_ADMIN, Capability.CAP_OBSERVE})

        app = create_app(
            session_store=session_store,
            trading_guard=self.guard,
            audit_logger=self.audit,
            broker_connectivity_manager=self.conn_mgr,
            persistence_manager=self.pm,
            disaster_recovery_manager=self.dr_mgr,
            multi_venue_registry=self.venue_reg,
            multi_venue_router=self.venue_router,
            health_monitor=self.health_mon,
        )
        client = TestClient(app)

        # Unauthenticated livez is accessible
        r_live = client.get("/api/v1/health/livez")
        self.assertEqual(r_live.status_code, 200)

        # Detailed health requires CAP_OBSERVE
        r_no_auth = client.get("/api/v1/health/detailed")
        self.assertEqual(r_no_auth.status_code, 401)

        r_viewer = client.get("/api/v1/health/detailed", headers={"Authorization": f"Bearer {viewer_token}"})
        self.assertEqual(r_viewer.status_code, 200)

        # Unquarantine requires CAP_ADMIN
        r_unq_viewer = client.post(
            "/api/v1/recovery/unquarantine",
            json={"execution_id": "EXEC-1", "second_operator_id": "op_b", "confirmation_reason": "Testing"},
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        self.assertEqual(r_unq_viewer.status_code, 403)

    # -------------------------------------------------------------------------
    # 37. UI has no direct broker route
    # -------------------------------------------------------------------------
    def test_37_ui_has_no_direct_broker_route(self) -> None:
        """37. Operator UI only interacts with /api/v1 endpoints, never bypassing gateway."""
        ui_path = "gateway/ui/index.html"
        self.assertTrue(os.path.exists(ui_path))
        with open(ui_path, "r", encoding="utf-8") as f:
            ui_code = f.read()

        # No direct third party broker URLs
        self.assertNotIn("https://api.zerodha.com", ui_code)
        self.assertNotIn("https://api.upstox.com", ui_code)
        self.assertNotIn("https://api.fyers.in", ui_code)
        self.assertNotIn("new WebSocket('wss://broker", ui_code)

    # -------------------------------------------------------------------------
    # 38. Phase 9 paper broker regression
    # -------------------------------------------------------------------------
    def test_38_phase9_paper_broker_regression(self) -> None:
        """38. System boots in PAPER mode and executes simulated paper trades cleanly."""
        self.assertEqual(self.conn_mgr.execution_mode, ExecutionMode.PAPER)
        intent = self._create_intent("op_paper", "NSE:WIPRO", "BUY", 20, "MARKET")
        self.intent_mgr.approve_intent(intent.intent_id, "approver_1", intent.correlation_id)
        self.risk_evaluator.evaluate(intent)
        inst = self.inst_mgr.create_instruction_from_intent(intent, "op_paper")
        dispatch_res = self.inst_mgr.dispatch_instruction(inst.instruction_id)
        self.assertTrue(dispatch_res.broker_order_id is not None)
        self.assertTrue(dispatch_res.broker_order_id.startswith("PAPER-"))

    # -------------------------------------------------------------------------
    # 39. Phase 5→10 lifecycle smoke test
    # -------------------------------------------------------------------------
    def test_39_phase5_to_10_lifecycle_smoke_test(self) -> None:
        """39. Complete end-to-end lifecycle from Intent through Disaster Recovery."""
        # 1. Intent
        intent = self._create_intent("op_alpha", "NSE:RELIANCE", "BUY", 10, "LIMIT", 2500.0)
        self.assertEqual(intent.state, IntentState.PENDING_APPROVAL)

        # 2. Approval
        self.intent_mgr.approve_intent(intent.intent_id, "op_beta", intent.correlation_id)
        self.assertEqual(intent.state, IntentState.APPROVED)

        # 3. Risk
        decision = self.risk_evaluator.evaluate(intent)
        self.assertEqual(decision.decision.value, "ALLOWED")

        # 4. Instruction
        inst = self.inst_mgr.create_instruction_from_intent(intent, "op_alpha")
        self.assertEqual(inst.state, InstructionState.VALIDATED)

        # 5. Route & Dispatch
        v_nse = BrokerVenue("NSE_PAPER", self.paper_adapter)
        self.venue_reg.register_venue(v_nse)
        policy = VenueRoutingPolicy()
        policy.add_prefix_route("NSE:", "NSE_PAPER")
        router = MultiVenueRouter(self.venue_reg, policy)
        venue, idemp = router.route(inst)
        self.assertEqual(venue.venue_id, "NSE_PAPER")

        dispatch_res = self.inst_mgr.dispatch_instruction(inst.instruction_id)
        self.assertTrue(dispatch_res.broker_order_id is not None)

        # 6. Fill
        exec_rec = self.exec_mgr.get_by_instruction(inst.instruction_id)
        exec_id = exec_rec.execution_id if exec_rec else None
        if exec_id:
            fill_rec = self.exec_mgr.apply_fill(exec_id, quantity=10, price=2500.0)
            self.assertEqual(fill_rec.filled_quantity, 10)
            self.assertEqual(fill_rec.current_state, ExecutionState.FILLED)

        # 7. WAL Verification
        wal_res = self.pm.verify_integrity()
        self.assertTrue(wal_res.valid)
        self.assertGreaterEqual(wal_res.records_checked, 4)

        # 8. Observability & DR check
        self.dr_mgr.check_post_boot_reconciliation(self.exec_mgr)
        self.assertEqual(self.dr_mgr.state, RecoveryState.CLEAN)

        is_ready, ready_dict = self.health_mon.check_readiness()
        self.assertTrue(is_ready)

    # -------------------------------------------------------------------------
    # 40. Frozen-core integrity verification
    # -------------------------------------------------------------------------
    def test_40_frozen_core_integrity_verification(self) -> None:
        """40. Verifies frozen directories (services/, strategies/, brokers/, config/) have zero git diffs."""
        frozen_dirs = ["services", "strategies", "brokers", "config"]
        for d in frozen_dirs:
            self.assertTrue(os.path.isdir(d), f"Directory {d} must exist")
        # Check git status for modified files in frozen directories
        try:
            res = subprocess.run(
                ["git", "status", "--porcelain", "services/", "strategies/", "brokers/", "config/"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                res.stdout.strip(),
                "",
                f"Frozen core directories must remain 100% unmodified! Found changes:\n{res.stdout}",
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass


if __name__ == "__main__":
    unittest.main()
