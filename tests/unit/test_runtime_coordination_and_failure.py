"""
Unit tests for Tradego Coordination and Failure Semantics (Phase 8).
Verifies Scenarios D, E, F, G, health monitoring error bursts, feed stagnation,
and fail-closed error containment.
"""

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from services.execution.models import CanonicalOrderStatus, OrderType, TimeInForce
from services.execution.state import ExecutionState
from services.execution.planner import ExecutionPlanner
from services.execution.router import ExecutionRouter
from services.market_state.instrument import Exchange, InstrumentId, InstrumentRegistry, InstrumentType
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.limits import RiskLimits
from services.risk.models import ApprovedTradeIntent, RiskDecision, RiskDecisionType, RiskRejection, RiskRejectionReason
from services.signals.models import SignalCandidate, SignalType, TriggerMode
from services.runtime.config import RuntimeConfig
from services.runtime.execution_coordinator import ExecutionCoordinator
from services.runtime.guards import TradingGuard
from services.runtime.health import HealthMonitor
from services.runtime.models import GuardState, GuardTripReason, RuntimeCorrelationRecord
from services.runtime.portfolio import PortfolioRuntimeState
from services.runtime.signal_risk_coordinator import SignalRiskCoordinator
from services.runtime.telemetry import TelemetryCollector


class TestRuntimeCoordinationAndFailure(unittest.TestCase):
    """Verifies failure semantics, risk admission gating, and error boundaries."""

    def setUp(self) -> None:
        self.instrument_id = InstrumentId(
            symbol="INFY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.now = datetime.now(timezone.utc)
        self.registry = InstrumentRegistry()
        self.registry.register(self.instrument_id, lot_size=1, tick_size=0.05)

        self.guard = TradingGuard()
        self.portfolio_state = PortfolioRuntimeState(account_id="TEST_ACCOUNT", initial_cash=1_000_000.0)

    def _create_sample_signal(
        self,
        fingerprint: str = "fp_test_001",
        reaffirmation_key: str = "rk_test_001",
        signal_type: SignalType = SignalType.ENTRY_LONG,
        suggested_entry: float = 1500.0,
    ) -> SignalCandidate:
        return SignalCandidate(
            signal_id="sig-test-001",
            fingerprint=fingerprint,
            reaffirmation_key=reaffirmation_key,
            strategy_id="TEST_STRAT",
            strategy_version="1.0.0",
            config_hash="cfg_hash_001",
            instrument_id=self.instrument_id,
            signal_type=signal_type,
            direction=1,
            trigger_mode=TriggerMode.INTRABAR_PREVIEW,
            confidence_score=0.85,
            suggested_entry_price=suggested_entry,
            suggested_stop_loss=1470.0,
            suggested_take_profit=1560.0,
            risk_reward_ratio=2.0,
            market_timestamp=self.now,
            availability_timestamp=self.now,
            generated_timestamp=self.now,
        )

    def test_scenario_d_intrabar_reaffirmation_deduplication(self) -> None:
        """
        SCENARIO D: Same INTRABAR setup evaluated on multiple ticks.
        First tick produces intent; subsequent tick with identical fingerprint is rejected by Risk.
        """
        admission_state = AdmissionState()
        risk_engine = RiskEngine(admission_state=admission_state)
        coord = SignalRiskCoordinator(risk_engine, self.portfolio_state, self.guard, self.registry)

        signal = self._create_sample_signal(fingerprint="fp_shared_setup", reaffirmation_key="rk_shared_setup")

        # Tick 1: Evaluates and admits
        intent1 = coord.evaluate_signal(signal, current_price=1500.0)
        self.assertIsNotNone(intent1)
        self.assertEqual(intent1.fingerprint, "fp_shared_setup")

        # Tick 2: Re-evaluates identical setup (fingerprint already admitted)
        intent2 = coord.evaluate_signal(signal, current_price=1500.0)
        # Must be rejected by RiskEngine admission state -> Zero intent
        self.assertIsNone(intent2)

    def test_scenario_e_genuinely_changed_intrabar_setup_admitted(self) -> None:
        """
        SCENARIO E: Strategy produces a genuinely changed setup during INTRABAR_PREVIEW.
        New fingerprint is evaluated and admitted as an updated trade intent.
        """
        admission_state = AdmissionState()
        risk_engine = RiskEngine(admission_state=admission_state)
        coord = SignalRiskCoordinator(risk_engine, self.portfolio_state, self.guard, self.registry)

        sig1 = self._create_sample_signal(fingerprint="fp_setup_v1", reaffirmation_key="rk_1")
        intent1 = coord.evaluate_signal(sig1, current_price=1500.0)
        self.assertIsNotNone(intent1)

        # Genuinely changed setup (new price, new fingerprint)
        sig2 = self._create_sample_signal(fingerprint="fp_setup_v2", reaffirmation_key="rk_1", suggested_entry=1510.0)
        intent2 = coord.evaluate_signal(sig2, current_price=1510.0)
        self.assertIsNotNone(intent2)
        self.assertEqual(intent2.fingerprint, "fp_setup_v2")

    def test_scenario_f_risk_engine_unexpected_exception_fails_closed(self) -> None:
        """
        SCENARIO F: Risk engine fails unexpectedly (e.g. unhandled error).
        Coordinator catches exception, logs, and fails closed with ZERO order.
        """
        failing_risk_engine = MagicMock(spec=RiskEngine)
        failing_risk_engine.evaluate.side_effect = RuntimeError("Simulated unhandled risk failure")

        coord = SignalRiskCoordinator(failing_risk_engine, self.portfolio_state, self.guard, self.registry)
        sig = self._create_sample_signal()

        # Fails closed: returns None, does NOT raise or leak unhandled exception
        intent = coord.evaluate_signal(sig, current_price=1500.0)
        self.assertIsNone(intent)

    def test_scenario_g_execution_state_registry_duplicate_intent_prevention(self) -> None:
        """
        SCENARIO G: Execution router receives a duplicate intent.
        ExecutionStateRegistry has_active_intent prevents duplicate submission.
        """
        intent = ApprovedTradeIntent(
            intent_id="intent-test-001",
            intent_generated_timestamp=self.now,
            signal_id="sig-001",
            fingerprint="fp-001",
            reaffirmation_key="rk-001",
            strategy_id="TEST",
            strategy_version="1.0.0",
            risk_config_version="v1",
            risk_config_hash="h1",
            instrument_id=self.instrument_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            permitted_quantity=10,
            approved_entry_price=1500.0,
            approved_stop_loss=1470.0,
            approved_take_profit=1560.0,
            risk_reward_ratio=2.0,
            calculated_monetary_risk=300.0,
            allocated_capital=15000.0,
            binding_constraint="LOT_FLOOR",
            market_timestamp=self.now,
            signal_generated_timestamp=self.now,
        )

        router = MagicMock(spec=ExecutionRouter)
        # Mock router rejecting duplicate
        router.submit.side_effect = ValueError("Intent intent-test-001 already has an active working order")

        coord = ExecutionCoordinator(router, self.portfolio_state, self.guard)
        state = coord.execute_intent(intent)
        self.assertIsNone(state)

    def test_health_monitor_exception_burst_trips_guard(self) -> None:
        """Verifies HealthMonitor detects error bursts and automatically trips to HALTED."""
        monitor = HealthMonitor(self.guard, max_exception_burst_count=3)
        self.assertEqual(self.guard.state, GuardState.NORMAL)

        # 2 errors: still normal
        monitor.record_error("comp_a", ValueError("err 1"))
        monitor.record_error("comp_a", ValueError("err 2"))
        self.assertEqual(self.guard.state, GuardState.NORMAL)

        # 3rd error: trips burst threshold
        monitor.record_error("comp_a", ValueError("err 3"))
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(self.guard.trip_reason, GuardTripReason.EXCEPTION_BURST)

    def test_health_monitor_feed_stagnation_trips_guard(self) -> None:
        """Verifies feed stagnation trips to HALTED."""
        monitor = HealthMonitor(self.guard, feed_stagnation_timeout_ms=1000.0)
        monitor._last_tick_time_ms = 1000.0

        # Current time 2500 ms (> 1000 ms delta)
        tripped = monitor.check_feed_stagnation(current_time_ms=2500.0)
        self.assertTrue(tripped)
        self.assertEqual(self.guard.state, GuardState.HALTED)
        self.assertEqual(self.guard.trip_reason, GuardTripReason.FEED_STAGNATION)

    def test_telemetry_bounded_capacity_drop_counter(self) -> None:
        """Verifies TelemetryCollector bounded ring buffer increments dropped counter on overflow."""
        collector = TelemetryCollector(capacity=3)
        self.assertEqual(collector.capacity, 3)

        for i in range(5):
            rec = RuntimeCorrelationRecord(
                client_order_id=f"TG-TEST-{i}",
                intent_id=f"intent-{i}",
                signal_id=f"sig-{i}",
                signal_fingerprint=f"fp-{i}",
                reaffirmation_key=f"rk-{i}",
                idempotency_key=f"idem-{i}",
                instrument_canonical_id="NSE:INFY",
                strategy_id="TEST",
                t1_market_receive_ns=1000,
                t2_signal_generated_ns=2000,
                t3_risk_evaluated_ns=3000,
                t4_intent_approved_ns=4000,
                t5_order_planned_ns=5000,
                t6_order_submitted_ns=6000,
                t7_wire_dispatched_ns=7000,
                t8_order_acked_ns=8000,
                t9_fill_received_ns=9000,
                t10_position_updated_ns=10000,
            )
            collector.record_lineage(rec)

        # 5 items recorded into capacity 3: 2 items dropped
        self.assertEqual(collector.dropped_telemetry_count, 2)
        self.assertEqual(collector.total_recorded_count, 5)
        records = collector.get_records()
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0].client_order_id, "TG-TEST-2")
        self.assertEqual(records[2].client_order_id, "TG-TEST-4")


if __name__ == "__main__":
    unittest.main()
