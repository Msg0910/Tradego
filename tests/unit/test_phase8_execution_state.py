"""
Unit tests for Tradego Phase 8: Execution State Synchronization, Fill Reconciliation & Post-Trade Event Stream.

Covers all 24 required test specifications:
1. execution created from dispatched instruction
2. dispatch acknowledgement
3. successful fill
4. partial fill
5. multiple partial fills
6. final fill
7. duplicate acknowledgement (idempotency)
8. duplicate fill (idempotency)
9. out-of-order event (ACK after FILL)
10. invalid state transition (stale event after cancel/fill)
11. quantity overflow rejection
12. broker rejection
13. transport failure
14. cancellation
15. cancellation rejection
16. reconciliation success
17. reconciliation mismatch
18. unknown broker state
19. post-trade event publication
20. event sequence integrity
21. audit lineage with SHA-256 hash chaining
22. read-only API authorization (RBAC 401/403)
23. execution API response schema
24. full Phase 5 -> 6 -> 7 -> 8 lifecycle smoke test
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock
import uuid
import warnings

import argon2
from fastapi.testclient import TestClient

from gateway.adapters import MarketStateAdapter, PortfolioRiskAdapter
from gateway.api.app import create_app
from gateway.broadcaster import EventBroadcaster, SequenceManager
from gateway.broker_adapter import BrokerAdapter, BrokerDispatchResult, PaperBrokerAdapter
from gateway.command import CommandGateway
from gateway.contracts import Capability
from gateway.intent import ExecutionIntent, ExecutionIntentManager, IntentState
from gateway.order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from gateway.execution import (
    ExecutionRecord,
    ExecutionReconciliationEngine,
    ExecutionState,
    ExecutionStateManager,
    FillRecord,
    ReconciliationStatus,
)
from gateway.projection import SnapshotGenerator
from gateway.recovery import GuardState, RecoveryManager, TradingGuard
from gateway.risk_gate import PreTradeRiskEvaluator
from gateway.security import InMemorySessionStore, NativeCredentialStore, Tier1AuditLogger
from services.risk.limits import RiskLimits
from services.runtime.portfolio import PortfolioRuntimeState


class TestPhase8ExecutionState(unittest.TestCase):
    """Exhaustive test suite for Phase 8 Execution State Synchronization & Fill Reconciliation."""

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.guard = TradingGuard()
        self.seq_mgr = SequenceManager(initial_sequence=0)
        self.broadcaster = EventBroadcaster(sequence_manager=self.seq_mgr)
        self.session_store = InMemorySessionStore(default_ttl_seconds=3600)
        self.audit_logger = Tier1AuditLogger(log_file_path=None)
        self.test_hasher = argon2.PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
        self.credential_store = NativeCredentialStore(hasher=self.test_hasher, max_failed_attempts=5)
        self.command_gateway = CommandGateway(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            broadcaster=self.broadcaster,
        )
        self.market_adapter = MarketStateAdapter(state_store=None)
        self.snapshot_gen = SnapshotGenerator(
            trading_guard=self.guard,
            sequence_manager=self.seq_mgr,
            market_adapter=self.market_adapter,
        )
        self.recovery_mgr = RecoveryManager(
            trading_guard=self.guard,
            command_gateway=self.command_gateway,
            audit_logger=self.audit_logger,
            challenge_ttl_seconds=60,
        )
        self.portfolio_risk_adapter = PortfolioRiskAdapter(
            trading_guard=self.guard,
        )
        self.intent_mgr = ExecutionIntentManager(
            audit_logger=self.audit_logger,
            default_ttl_seconds=3600,
        )

        # Core risk gate structures
        self.risk_limits = RiskLimits(
            config_version="1.0.0",
            max_gross_leverage=2.0,
            max_net_leverage=1.0,
            max_concurrent_positions=10,
        )
        self.portfolio_state = PortfolioRuntimeState(
            account_id="PAPER_TEST_P8",
            initial_cash=1000000.0,
        )
        self.risk_gate = PreTradeRiskEvaluator(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            portfolio_state=self.portfolio_state,
            risk_limits=self.risk_limits,
            max_order_quantity=1000,
            margin_rate=0.20,
        )

        # Phase 8 Execution State Manager
        self.recon_engine = ExecutionReconciliationEngine()
        self.exec_mgr = ExecutionStateManager(
            audit_logger=self.audit_logger,
            broadcaster=self.broadcaster,
            reconciliation_engine=self.recon_engine,
        )

        # Phase 7 Order Instruction Boundary with Phase 8 execution manager wired
        self.broker_adapter = PaperBrokerAdapter(venue_name="PAPER_TEST_VENUE")
        self.instruction_mgr = OrderInstructionManager(
            trading_guard=self.guard,
            audit_logger=self.audit_logger,
            broker_adapter=self.broker_adapter,
            execution_manager=self.exec_mgr,
        )

        # FastAPI app wiring
        self.app = create_app(
            trading_guard=self.guard,
            session_store=self.session_store,
            audit_logger=self.audit_logger,
            sequence_manager=self.seq_mgr,
            broadcaster=self.broadcaster,
            credential_store=self.credential_store,
            command_gateway=self.command_gateway,
            snapshot_generator=self.snapshot_gen,
            market_adapter=self.market_adapter,
            recovery_manager=self.recovery_mgr,
            portfolio_risk_adapter=self.portfolio_risk_adapter,
            intent_manager=self.intent_mgr,
            risk_gate=self.risk_gate,
            order_instruction_manager=self.instruction_mgr,
            broker_adapter=self.broker_adapter,
            execution_state_manager=self.exec_mgr,
        )
        self.client = TestClient(self.app)

        # Sessions for testing
        self.token_observer = self.session_store.create_session(
            operator_id="observer_user",
            roles=["OBSERVER"],
            capabilities={Capability.CAP_OBSERVE},
        )
        self.token_trader = self.session_store.create_session(
            operator_id="trader_user",
            roles=["TRADER"],
            capabilities={Capability.CAP_OBSERVE, Capability.CAP_TRADE_SUBMIT},
        )
        self.token_admin = self.session_store.create_session(
            operator_id="admin_user",
            roles=["ADMIN"],
            capabilities={
                Capability.CAP_OBSERVE,
                Capability.CAP_ADMIN,
                Capability.CAP_TRADE_SUBMIT,
                Capability.CAP_TRADE_APPROVE,
                Capability.CAP_ORDER_INSTRUCT,
                Capability.CAP_ORDER_DISPATCH,
                Capability.CAP_TRADE_CANCEL,
            },
        )

    def _create_and_risk_approve_intent(
        self,
        symbol: str = "TCS",
        side: str = "BUY",
        quantity: int = 100,
        price: float = 3500.0,
    ) -> ExecutionIntent:
        intent = self.intent_mgr.create_intent(
            creator_id="trader_1",
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type="LIMIT",
            correlation_id="corr-test-intent",
            limit_price=price,
            reason="Phase 8 test execution",
        )
        self.intent_mgr.approve_intent(intent.intent_id, approver_id="approver_1", correlation_id="corr-test-intent")
        self.risk_gate.evaluate_intent(intent, evaluator_id="risk_gate_service", correlation_id="corr-test-intent")
        return intent

    def _create_instruction(self, intent: ExecutionIntent) -> OrderInstruction:
        return self.instruction_mgr.create_from_intent(intent, operator_id="trader_1")

    # -----------------------------------------------------------------------
    # 1. execution created from dispatched instruction
    # -----------------------------------------------------------------------
    def test_01_execution_created_from_dispatched_instruction(self):
        intent = self._create_and_risk_approve_intent(quantity=100, price=3500.0)
        ins = self._create_instruction(intent)

        # Dispatch through instruction_mgr which automatically registers execution
        result = self.instruction_mgr.dispatch_instruction(ins.instruction_id, operator_id="admin_user")
        self.assertEqual(result.state, InstructionState.ACKNOWLEDGED)

        execution = self.exec_mgr.get_by_instruction(ins.instruction_id)
        self.assertIsNotNone(execution)
        self.assertEqual(execution.instruction_id, ins.instruction_id)
        self.assertEqual(execution.intent_id, intent.intent_id)
        self.assertEqual(execution.symbol, "TCS")
        self.assertEqual(execution.side, "BUY")
        self.assertEqual(execution.ordered_quantity, 100)
        self.assertEqual(execution.filled_quantity, 0)
        self.assertEqual(execution.remaining_quantity, 100)
        self.assertIsNone(execution.average_fill_price)
        self.assertEqual(execution.current_state, ExecutionState.ACKNOWLEDGED)
        self.assertEqual(execution.reconciliation_status, ReconciliationStatus.RECONCILED)

    # -----------------------------------------------------------------------
    # 2. dispatch acknowledgement
    # -----------------------------------------------------------------------
    def test_02_dispatch_acknowledgement(self):
        rec = self.exec_mgr.create_execution(
            instruction_id="INS-02",
            intent_id="INT-02",
            symbol="INFY",
            side="BUY",
            ordered_quantity=50,
            price=1500.0,
            correlation_id="corr-02",
        )
        self.assertEqual(rec.current_state, ExecutionState.DISPATCHED)

        updated = self.exec_mgr.apply_acknowledgement(
            execution_id=rec.execution_id,
            broker_order_id="BRK-ACK-002",
            event_id="evt-ack-002",
        )
        self.assertEqual(updated.current_state, ExecutionState.ACKNOWLEDGED)
        self.assertEqual(updated.broker_order_id, "BRK-ACK-002")
        self.assertIsNotNone(updated.broker_timestamp)

    # -----------------------------------------------------------------------
    # 3. successful fill (single full fill)
    # -----------------------------------------------------------------------
    def test_03_successful_fill(self):
        rec = self.exec_mgr.create_execution(
            instruction_id="INS-03",
            intent_id="INT-03",
            symbol="RELIANCE",
            side="BUY",
            ordered_quantity=100,
            price=2500.0,
        )
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-003")

        filled_rec = self.exec_mgr.apply_fill(
            execution_id=rec.execution_id,
            quantity=100,
            price=2500.0,
            broker_fill_id="FILL-003",
            event_id="evt-fill-003",
        )
        self.assertEqual(filled_rec.current_state, ExecutionState.FILLED)
        self.assertEqual(filled_rec.filled_quantity, 100)
        self.assertEqual(filled_rec.remaining_quantity, 0)
        self.assertEqual(filled_rec.average_fill_price, 2500.0)
        self.assertEqual(filled_rec.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertEqual(len(filled_rec.fills), 1)

    # -----------------------------------------------------------------------
    # 4. partial fill
    # -----------------------------------------------------------------------
    def test_04_partial_fill(self):
        rec = self.exec_mgr.create_execution(
            instruction_id="INS-04",
            intent_id="INT-04",
            symbol="HDFC",
            side="BUY",
            ordered_quantity=100,
            price=1600.0,
        )
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-004")

        part_rec = self.exec_mgr.apply_fill(
            execution_id=rec.execution_id,
            quantity=30,
            price=1600.0,
            broker_fill_id="FILL-004",
            event_id="evt-fill-004",
        )
        self.assertEqual(part_rec.current_state, ExecutionState.PARTIALLY_FILLED)
        self.assertEqual(part_rec.filled_quantity, 30)
        self.assertEqual(part_rec.remaining_quantity, 70)
        self.assertEqual(part_rec.average_fill_price, 1600.0)
        self.assertEqual(part_rec.reconciliation_status, ReconciliationStatus.RECONCILED)

    # -----------------------------------------------------------------------
    # 5. multiple partial fills
    # -----------------------------------------------------------------------
    def test_05_multiple_partial_fills(self):
        rec = self.exec_mgr.create_execution(
            instruction_id="INS-05",
            intent_id="INT-05",
            symbol="SBIN",
            side="BUY",
            ordered_quantity=100,
        )
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-005")

        # Fill 1: 30 @ 100.0
        rec1 = self.exec_mgr.apply_fill(rec.execution_id, quantity=30, price=100.0, broker_fill_id="FILL-05-1")
        self.assertEqual(rec1.filled_quantity, 30)
        self.assertEqual(rec1.average_fill_price, 100.0)

        # Fill 2: 20 @ 110.0
        rec2 = self.exec_mgr.apply_fill(rec.execution_id, quantity=20, price=110.0, broker_fill_id="FILL-05-2")
        self.assertEqual(rec2.filled_quantity, 50)
        self.assertEqual(rec2.remaining_quantity, 50)
        self.assertEqual(rec2.current_state, ExecutionState.PARTIALLY_FILLED)
        # Avg price = (30*100 + 20*110) / 50 = (3000 + 2200) / 50 = 104.0
        self.assertAlmostEqual(rec2.average_fill_price, 104.0, places=2)

    # -----------------------------------------------------------------------
    # 6. final fill (closing multiple partial fills)
    # -----------------------------------------------------------------------
    def test_06_final_fill(self):
        rec = self.exec_mgr.create_execution(
            instruction_id="INS-06",
            intent_id="INT-06",
            symbol="WIPRO",
            side="BUY",
            ordered_quantity=100,
        )
        self.exec_mgr.apply_fill(rec.execution_id, quantity=30, price=100.0, broker_fill_id="F1")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=20, price=110.0, broker_fill_id="F2")
        final_rec = self.exec_mgr.apply_fill(rec.execution_id, quantity=50, price=105.0, broker_fill_id="F3")

        self.assertEqual(final_rec.current_state, ExecutionState.FILLED)
        self.assertEqual(final_rec.filled_quantity, 100)
        self.assertEqual(final_rec.remaining_quantity, 0)
        # Avg price: (3000 + 2200 + 5250) / 100 = 10450 / 100 = 104.5
        self.assertAlmostEqual(final_rec.average_fill_price, 104.5, places=2)
        self.assertEqual(final_rec.reconciliation_status, ReconciliationStatus.RECONCILED)

    # -----------------------------------------------------------------------
    # 7. duplicate acknowledgement (idempotency)
    # -----------------------------------------------------------------------
    def test_07_duplicate_acknowledgement(self):
        rec = self.exec_mgr.create_execution("INS-07", "INT-07", "TCS", "BUY", 100)
        ack1 = self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-007", event_id="evt-ack-7")
        self.assertEqual(ack1.current_state, ExecutionState.ACKNOWLEDGED)

        # Duplicate delivery
        ack2 = self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-007", event_id="evt-ack-7")
        self.assertEqual(ack2.current_state, ExecutionState.ACKNOWLEDGED)
        self.assertEqual(ack2.broker_order_id, "BRK-007")

    # -----------------------------------------------------------------------
    # 8. duplicate fill (idempotency)
    # -----------------------------------------------------------------------
    def test_08_duplicate_fill(self):
        rec = self.exec_mgr.create_execution("INS-08", "INT-08", "TCS", "BUY", 100)
        fill1 = self.exec_mgr.apply_fill(
            rec.execution_id,
            quantity=30,
            price=3000.0,
            broker_fill_id="FILL-DUP-1",
            event_id="evt-fill-dup-1",
        )
        self.assertEqual(fill1.filled_quantity, 30)
        self.assertEqual(len(fill1.fills), 1)

        # Re-delivery of identical fill
        fill2 = self.exec_mgr.apply_fill(
            rec.execution_id,
            quantity=30,
            price=3000.0,
            broker_fill_id="FILL-DUP-1",
            event_id="evt-fill-dup-1",
        )
        # Quantity must NOT double
        self.assertEqual(fill2.filled_quantity, 30)
        self.assertEqual(fill2.remaining_quantity, 70)
        self.assertEqual(len(fill2.fills), 1)

    # -----------------------------------------------------------------------
    # 9. out-of-order event (ACK arriving after FILL)
    # -----------------------------------------------------------------------
    def test_09_out_of_order_event(self):
        rec = self.exec_mgr.create_execution("INS-09", "INT-09", "INFY", "BUY", 100)
        
        # Fill arrives first (e.g. fast execution before transport returns ACK)
        filled_rec = self.exec_mgr.apply_fill(rec.execution_id, quantity=100, price=1500.0, broker_fill_id="F-OOO")
        self.assertEqual(filled_rec.current_state, ExecutionState.FILLED)

        # Delayed ACK arrives
        ack_rec = self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-DELAYED-ACK")
        # State must remain FILLED, not regress to ACKNOWLEDGED
        self.assertEqual(ack_rec.current_state, ExecutionState.FILLED)
        self.assertEqual(ack_rec.broker_order_id, "BRK-DELAYED-ACK")

    # -----------------------------------------------------------------------
    # 10. invalid state transition (fill after cancelled)
    # -----------------------------------------------------------------------
    def test_10_invalid_state_transition(self):
        rec = self.exec_mgr.create_execution("INS-10", "INT-10", "TCS", "BUY", 100)
        self.exec_mgr.apply_cancellation(rec.execution_id, reason="User cancelled")
        self.assertEqual(self.exec_mgr.get_execution(rec.execution_id).current_state, ExecutionState.CANCELLED)

        # Late fill arrives after order cancelled
        with self.assertRaises(ValueError) as ctx:
            self.exec_mgr.apply_fill(rec.execution_id, quantity=10, price=3000.0, broker_fill_id="F-LATE")
        self.assertIn("STALE_FILL_AFTER_CANCEL", str(ctx.exception))

        # Check execution state remained CANCELLED, with reconciliation status MISMATCH
        current = self.exec_mgr.get_execution(rec.execution_id)
        self.assertEqual(current.current_state, ExecutionState.CANCELLED)
        self.assertEqual(current.reconciliation_status, ReconciliationStatus.MISMATCH)

    # -----------------------------------------------------------------------
    # 11. quantity overflow rejection
    # -----------------------------------------------------------------------
    def test_11_quantity_overflow(self):
        rec = self.exec_mgr.create_execution("INS-11", "INT-11", "TCS", "BUY", 50)
        # Attempt to fill 60 on a 50 order
        with self.assertRaises(ValueError) as ctx:
            self.exec_mgr.apply_fill(rec.execution_id, quantity=60, price=3000.0, broker_fill_id="F-OVERFLOW")
        self.assertIn("QUANTITY_OVERFLOW", str(ctx.exception))

        current = self.exec_mgr.get_execution(rec.execution_id)
        self.assertEqual(current.filled_quantity, 0)
        self.assertEqual(current.reconciliation_status, ReconciliationStatus.MISMATCH)

    # -----------------------------------------------------------------------
    # 12. broker rejection
    # -----------------------------------------------------------------------
    def test_12_broker_rejection(self):
        rec = self.exec_mgr.create_execution("INS-12", "INT-12", "TCS", "BUY", 100)
        rejected = self.exec_mgr.apply_rejection(
            rec.execution_id,
            rejection_reason="INSUFFICIENT_MARGIN_AT_BROKER",
            broker_order_id="BRK-REJ-012",
        )
        self.assertEqual(rejected.current_state, ExecutionState.REJECTED)
        self.assertEqual(rejected.last_error, "INSUFFICIENT_MARGIN_AT_BROKER")
        self.assertEqual(rejected.remaining_quantity, 0)
        self.assertEqual(rejected.reconciliation_status, ReconciliationStatus.RECONCILED)

    # -----------------------------------------------------------------------
    # 13. transport failure
    # -----------------------------------------------------------------------
    def test_13_transport_failure(self):
        rec = self.exec_mgr.create_execution("INS-13", "INT-13", "TCS", "BUY", 100)
        failed = self.exec_mgr.apply_failure(
            rec.execution_id,
            failure_reason="GATEWAY_TIMEOUT_CONNECTING_TO_EXCHANGE",
        )
        self.assertEqual(failed.current_state, ExecutionState.FAILED)
        self.assertEqual(failed.last_error, "GATEWAY_TIMEOUT_CONNECTING_TO_EXCHANGE")
        self.assertEqual(failed.reconciliation_status, ReconciliationStatus.MISMATCH)

    # -----------------------------------------------------------------------
    # 14. cancellation
    # -----------------------------------------------------------------------
    def test_14_cancellation(self):
        rec = self.exec_mgr.create_execution("INS-14", "INT-14", "TCS", "BUY", 100)
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-014")

        # Fill 30 first
        self.exec_mgr.apply_fill(rec.execution_id, quantity=30, price=3000.0, broker_fill_id="F-14")

        # Cancel remainder
        cancelled = self.exec_mgr.apply_cancellation(rec.execution_id, reason="Cancel remaining 70")
        self.assertEqual(cancelled.current_state, ExecutionState.CANCELLED)
        self.assertEqual(cancelled.filled_quantity, 30)
        self.assertEqual(cancelled.remaining_quantity, 0)
        self.assertEqual(cancelled.reconciliation_status, ReconciliationStatus.RECONCILED)

    # -----------------------------------------------------------------------
    # 15. cancellation rejection
    # -----------------------------------------------------------------------
    def test_15_cancellation_rejection(self):
        rec = self.exec_mgr.create_execution("INS-15", "INT-15", "TCS", "BUY", 100)
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-015")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=100, price=3000.0, broker_fill_id="F-15")

        # Attempt to cancel an order that is already FILLED
        with self.assertRaises(ValueError) as ctx:
            self.exec_mgr.apply_cancellation(rec.execution_id, reason="Cancel filled order")
        self.assertIn("CANNOT_CANCEL_FILLED_ORDER", str(ctx.exception))

        current = self.exec_mgr.get_execution(rec.execution_id)
        self.assertEqual(current.current_state, ExecutionState.FILLED)

    # -----------------------------------------------------------------------
    # 16. reconciliation success
    # -----------------------------------------------------------------------
    def test_16_reconciliation_success(self):
        rec = self.exec_mgr.create_execution("INS-16", "INT-16", "TCS", "BUY", 100)
        self.exec_mgr.apply_fill(rec.execution_id, quantity=40, price=3000.0, broker_fill_id="F16-1")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=60, price=3005.0, broker_fill_id="F16-2")

        status, details = self.recon_engine.reconcile_record(self.exec_mgr.get_execution(rec.execution_id))
        self.assertEqual(status, ReconciliationStatus.RECONCILED)
        self.assertIn("Authoritative fill ledger matches", details)

    # -----------------------------------------------------------------------
    # 17. reconciliation mismatch
    # -----------------------------------------------------------------------
    def test_17_reconciliation_mismatch(self):
        # Create a corrupted execution record directly to test reconciliation engine detection
        corrupted_rec = ExecutionRecord(
            execution_id="EXEC-CORRUPT",
            instruction_id="INS-CORRUPT",
            intent_id="INT-CORRUPT",
            correlation_id="corr-corrupt",
            symbol="TCS",
            side="BUY",
            ordered_quantity=100,
            filled_quantity=80,  # Reported 80
            remaining_quantity=20,
            current_state=ExecutionState.PARTIALLY_FILLED,
            fills=(FillRecord(fill_id="f1", broker_fill_id="bf1", quantity=50, price=3000.0, timestamp="now"),),  # Ledger only has 50!
        )
        status, details = self.recon_engine.reconcile_record(corrupted_rec)
        self.assertEqual(status, ReconciliationStatus.MISMATCH)
        self.assertIn("FILL_LEDGER_MISMATCH", details)

    # -----------------------------------------------------------------------
    # 18. unknown broker state
    # -----------------------------------------------------------------------
    def test_18_unknown_broker_state(self):
        # ExecutionState parser handles unknown states gracefully
        parsed = ExecutionState.from_str("WEIRD_BROKER_STATE_XYZ")
        self.assertEqual(parsed, ExecutionState.UNKNOWN)

        # Applying an unknown state transition records UNKNOWN and updates details
        rec = self.exec_mgr.create_execution("INS-18", "INT-18", "TCS", "BUY", 100)
        updated = self.exec_mgr.apply_unknown_state(rec.execution_id, raw_state="WEIRD_BROKER_STATE_XYZ")
        self.assertEqual(updated.current_state, ExecutionState.UNKNOWN)
        self.assertEqual(updated.reconciliation_status, ReconciliationStatus.UNKNOWN)

    # -----------------------------------------------------------------------
    # 19. post-trade event publication
    # -----------------------------------------------------------------------
    def test_19_post_trade_event_publication(self):
        initial_seq = self.seq_mgr.current_sequence()
        rec = self.exec_mgr.create_execution("INS-19", "INT-19", "TCS", "BUY", 100)
        # Event for ORDER_DISPATCHED
        self.assertGreater(self.seq_mgr.current_sequence(), initial_seq)

        seq_before_ack = self.seq_mgr.current_sequence()
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-019")
        # Event for ORDER_ACKNOWLEDGED
        self.assertGreater(self.seq_mgr.current_sequence(), seq_before_ack)

        seq_before_fill = self.seq_mgr.current_sequence()
        self.exec_mgr.apply_fill(rec.execution_id, quantity=50, price=3000.0, broker_fill_id="F-19")
        # Event for ORDER_PARTIALLY_FILLED
        self.assertGreater(self.seq_mgr.current_sequence(), seq_before_fill)

    # -----------------------------------------------------------------------
    # 20. event sequence integrity
    # -----------------------------------------------------------------------
    def test_20_event_sequence_integrity(self):
        start_seq = self.seq_mgr.current_sequence()
        rec = self.exec_mgr.create_execution("INS-20", "INT-20", "TCS", "BUY", 100)
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-020")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=100, price=3000.0, broker_fill_id="F-20")

        end_seq = self.seq_mgr.current_sequence()
        # 4 events published (DISPATCHED, ACKNOWLEDGED, FILLED, and EXECUTION_RECONCILED), strictly monotonic
        self.assertEqual(end_seq, start_seq + 4)

    # -----------------------------------------------------------------------
    # 21. audit lineage with SHA-256 hash chaining
    # -----------------------------------------------------------------------
    def test_21_audit_lineage(self):
        rec = self.exec_mgr.create_execution("INS-21", "INT-21", "TCS", "BUY", 100)
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-021")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=100, price=3000.0, broker_fill_id="F-21")

        self.audit_logger.flush()
        entries = self.audit_logger.in_memory_records
        exec_entries = [e for e in entries if e.get("event_type", "").startswith("ORDER_") or e.get("event_type", "").startswith("EXECUTION_")]
        self.assertGreaterEqual(len(exec_entries), 3)

        # Check SHA-256 hash chaining integrity
        prev_hash = Tier1AuditLogger.GENESIS_HASH
        for rec_entry in entries:
            self.assertEqual(rec_entry["prev_hash"], prev_hash)
            self.assertTrue("hash" in rec_entry)
            prev_hash = rec_entry["hash"]

        # Check provenance fields in audit entries
        for entry in exec_entries:
            details = entry.get("details", {})
            self.assertEqual(details.get("execution_id"), rec.execution_id)
            self.assertEqual(details.get("instruction_id"), "INS-21")
            self.assertEqual(details.get("intent_id"), "INT-21")

    # -----------------------------------------------------------------------
    # 22. read-only API authorization (RBAC 401/403)
    # -----------------------------------------------------------------------
    def test_22_read_only_api_authorization(self):
        # 1. Unauthenticated request -> 401
        resp = self.client.get("/api/v1/executions")
        self.assertEqual(resp.status_code, 401)

        # 2. Authenticated observer -> 200
        resp = self.client.get(
            "/api/v1/executions",
            headers={"Authorization": f"Bearer {self.token_observer}"},
        )
        self.assertEqual(resp.status_code, 200)

        # 3. Observer attempting administrative reconcile -> 403
        rec = self.exec_mgr.create_execution("INS-22", "INT-22", "TCS", "BUY", 100)
        resp = self.client.post(
            f"/api/v1/executions/{rec.execution_id}/reconcile",
            headers={"Authorization": f"Bearer {self.token_observer}"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("FORBIDDEN", str(resp.json()))

        # 4. Admin attempting reconcile -> 200
        resp = self.client.post(
            f"/api/v1/executions/{rec.execution_id}/reconcile",
            headers={"Authorization": f"Bearer {self.token_admin}"},
        )
        self.assertEqual(resp.status_code, 200)

    # -----------------------------------------------------------------------
    # 23. execution API response schema
    # -----------------------------------------------------------------------
    def test_23_execution_api_response(self):
        rec = self.exec_mgr.create_execution("INS-23", "INT-23", "TCS", "BUY", 100, price=3000.0)
        self.exec_mgr.apply_acknowledgement(rec.execution_id, broker_order_id="BRK-023")
        self.exec_mgr.apply_fill(rec.execution_id, quantity=40, price=2990.0, broker_fill_id="BF-23-1")

        resp = self.client.get(
            f"/api/v1/executions/{rec.execution_id}",
            headers={"Authorization": f"Bearer {self.token_observer}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["execution_id"], rec.execution_id)
        self.assertEqual(data["instruction_id"], "INS-23")
        self.assertEqual(data["intent_id"], "INT-23")
        self.assertEqual(data["symbol"], "TCS")
        self.assertEqual(data["side"], "BUY")
        self.assertEqual(data["ordered_quantity"], 100)
        self.assertEqual(data["filled_quantity"], 40)
        self.assertEqual(data["remaining_quantity"], 60)
        self.assertEqual(data["average_fill_price"], 2990.0)
        self.assertEqual(data["broker_order_id"], "BRK-023")
        self.assertEqual(data["current_state"], "PARTIALLY_FILLED")
        self.assertEqual(data["reconciliation_status"], "RECONCILED")
        self.assertEqual(len(data["fills"]), 1)
        self.assertEqual(data["fills"][0]["broker_fill_id"], "BF-23-1")

        # Query by instruction
        resp_by_ins = self.client.get(
            f"/api/v1/order-instructions/INS-23/execution",
            headers={"Authorization": f"Bearer {self.token_observer}"},
        )
        self.assertEqual(resp_by_ins.status_code, 200)
        self.assertEqual(resp_by_ins.json()["execution_id"], rec.execution_id)

    # -----------------------------------------------------------------------
    # 24. full Phase 5 -> 6 -> 7 -> 8 lifecycle smoke test
    # -----------------------------------------------------------------------
    def test_24_full_phase5_to_8_lifecycle_smoke_test(self):
        # 1. Phase 5: Create Execution Intent
        intent = self.intent_mgr.create_intent(
            creator_id="trader_smoke",
            symbol="INFY",
            side="BUY",
            quantity=100,
            order_type="LIMIT",
            correlation_id="smoke-corr-1",
            limit_price=1450.0,
            reason="Phase 5 to 8 E2E smoke test",
        )
        self.assertEqual(intent.state, IntentState.PENDING_APPROVAL)

        # 2. Phase 5: Operational Approval
        self.intent_mgr.approve_intent(intent.intent_id, approver_id="approver_smoke", correlation_id="smoke-corr-1")
        self.assertEqual(intent.state, IntentState.APPROVED)

        # 3. Phase 6: Pre-Trade Risk Gate Evaluation
        risk_result = self.risk_gate.evaluate_intent(intent, evaluator_id="risk_gate_smoke", correlation_id="smoke-corr-1")
        self.assertEqual(risk_result.resulting_state, "RISK_APPROVED")
        self.assertEqual(intent.state, IntentState.RISK_APPROVED)

        # 4. Phase 7: Create Canonical Order Instruction
        instruction = self.instruction_mgr.create_from_intent(intent, operator_id="trader_smoke")
        self.assertEqual(instruction.state, InstructionState.VALIDATED)

        # 5. Phase 7 & 8: Dispatch Instruction to Broker
        dispatch_res = self.instruction_mgr.dispatch_instruction(instruction.instruction_id, operator_id="admin_user")
        self.assertEqual(dispatch_res.state, InstructionState.ACKNOWLEDGED)
        self.assertEqual(instruction.state, InstructionState.ACKNOWLEDGED)

        # 6. Phase 8: Execution State Synchronization & Fills
        execution = self.exec_mgr.get_by_instruction(instruction.instruction_id)
        self.assertIsNotNone(execution)
        self.assertEqual(execution.current_state, ExecutionState.ACKNOWLEDGED)

        # Apply Partial Fill 1: 40 @ 1445.0
        self.exec_mgr.apply_fill(execution.execution_id, quantity=40, price=1445.0, broker_fill_id="SMOKE-F1")
        rec = self.exec_mgr.get_execution(execution.execution_id)
        self.assertEqual(rec.current_state, ExecutionState.PARTIALLY_FILLED)
        self.assertEqual(rec.filled_quantity, 40)
        self.assertEqual(rec.remaining_quantity, 60)

        # Apply Final Fill 2: 60 @ 1450.0
        self.exec_mgr.apply_fill(execution.execution_id, quantity=60, price=1450.0, broker_fill_id="SMOKE-F2")
        final_rec = self.exec_mgr.get_execution(execution.execution_id)
        self.assertEqual(final_rec.current_state, ExecutionState.FILLED)
        self.assertEqual(final_rec.filled_quantity, 100)
        self.assertEqual(final_rec.remaining_quantity, 0)
        # Avg price = (40*1445 + 60*1450)/100 = (57800 + 87000)/100 = 1448.0
        self.assertEqual(final_rec.average_fill_price, 1448.0)

        # 7. Phase 8: Authoritative Fill Reconciliation
        status, details = self.recon_engine.reconcile_record(final_rec)
        self.assertEqual(status, ReconciliationStatus.RECONCILED)

        # 8. Verify Audit Chain
        self.audit_logger.flush()
        prev_hash = Tier1AuditLogger.GENESIS_HASH
        for rec_entry in self.audit_logger.in_memory_records:
            self.assertEqual(rec_entry["prev_hash"], prev_hash)
            self.assertTrue("hash" in rec_entry)
            prev_hash = rec_entry["hash"]


if __name__ == "__main__":
    unittest.main()
