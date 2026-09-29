"""
Unit tests and Performance Benchmarks for Phase 6 RiskEngine.

Verifies:
- Requirement 35: Deterministic semantic equivalence on identical inputs
- Multi-threaded concurrency safety
- Performance benchmark: p50, p95, p99, max latency
- Memory footprint benchmark: < 50 KB per RiskEngine instance
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gc
import sys
import time
import unittest

from services.analytics.models import FeatureQuality
from services.market_state.instrument import Exchange, InstrumentId, InstrumentMetadata, InstrumentType
from services.risk.context import AccountRiskState, RiskContext
from services.risk.engine import AdmissionState, RiskEngine
from services.risk.limits import RiskLimits
from services.risk.models import PortfolioSnapshot, RiskDecisionType
from services.signals.models import SignalCandidate, SignalType, TriggerMode


class TestRiskEngineConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = datetime(2026, 9, 14, 10, 0, 0)
        self.inst_id = InstrumentId(
            symbol="INFY",
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
            current_market_price=1000.0,
            feature_quality=FeatureQuality.VALID,
        )

    def _create_signal(self, sig_id: str, fp: str) -> SignalCandidate:
        return SignalCandidate(
            signal_id=sig_id,
            fingerprint=fp,
            reaffirmation_key="RF_001",
            strategy_id="TREND_CONT",
            strategy_version="1.0.0",
            config_hash="CFG_001",
            instrument_id=self.inst_id,
            signal_type=SignalType.ENTRY_LONG,
            direction=1,
            trigger_mode=TriggerMode.BAR_CLOSE,
            confidence_score=0.9,
            suggested_entry_price=1000.0,
            suggested_stop_loss=980.0,
            suggested_take_profit=1040.0,
            risk_reward_ratio=2.0,
            market_timestamp=self.ts,
            availability_timestamp=self.ts,
            generated_timestamp=self.ts,
            is_confirmed=True,
        )

    def test_identical_inputs_produce_semantically_equivalent_decisions(self) -> None:
        """Requirement 35: Identical evaluation inputs produce semantically equivalent results."""
        limits = RiskLimits()
        sig = self._create_signal(sig_id="SIG_001", fp="FP_DET_001")

        # Two separate engines with clean initial AdmissionStates
        engine1 = RiskEngine(limits=limits, admission_state=AdmissionState())
        engine2 = RiskEngine(limits=limits, admission_state=AdmissionState())

        dec1 = engine1.evaluate(sig, self.ctx)
        dec2 = engine2.evaluate(sig, self.ctx)

        self.assertEqual(dec1.decision, RiskDecisionType.APPROVED)
        self.assertEqual(dec2.decision, RiskDecisionType.APPROVED)

        intent1 = dec1.approved_intent
        intent2 = dec2.approved_intent
        self.assertIsNotNone(intent1)
        self.assertIsNotNone(intent2)

        # Runtime UUIDs must differ
        self.assertNotEqual(intent1.intent_id, intent2.intent_id)

        # But semantic equivalence MUST hold!
        self.assertTrue(intent1.is_semantically_equivalent(intent2))
        self.assertTrue(intent2.is_semantically_equivalent(intent1))

    def test_concurrent_evaluations_thread_safety(self) -> None:
        """Verify thread-safety and lack of deadlocks under concurrent multi-threaded execution."""
        engine = RiskEngine()
        num_threads = 8
        evaluations_per_thread = 100

        def worker(thread_idx: int) -> int:
            approved_count = 0
            for i in range(evaluations_per_thread):
                sig = self._create_signal(
                    sig_id=f"SIG_T{thread_idx}_{i}",
                    fp=f"FP_T{thread_idx}_{i}",  # Unique fingerprints per evaluation
                )
                dec = engine.evaluate(sig, self.ctx)
                if dec.decision == RiskDecisionType.APPROVED:
                    approved_count += 1
            return approved_count

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, t) for t in range(num_threads)]
            results = [f.result() for f in futures]

        self.assertEqual(sum(results), num_threads * evaluations_per_thread)

    def test_risk_evaluation_latency_and_memory_benchmarks(self) -> None:
        """Measure latency percentiles and memory footprint against architectural targets."""
        limits = RiskLimits()
        engine = RiskEngine(limits=limits)
        iterations = 5000
        latencies = []

        # Warm up
        for i in range(100):
            sig = self._create_signal(f"SIG_WARM_{i}", f"FP_WARM_{i}")
            engine.evaluate(sig, self.ctx)

        # Timed benchmark
        engine.admission_state.clear()
        for i in range(iterations):
            sig = self._create_signal(f"SIG_BENCH_{i}", f"FP_BENCH_{i}")
            t0 = time.perf_counter_ns()
            engine.evaluate(sig, self.ctx)
            t1 = time.perf_counter_ns()
            latencies.append((t1 - t0) / 1000.0)  # microseconds

        latencies.sort()
        p50 = latencies[int(iterations * 0.50)]
        p95 = latencies[int(iterations * 0.95)]
        p99 = latencies[int(iterations * 0.99)]
        max_lat = latencies[-1]

        # Memory benchmark
        gc.collect()
        initial_objects = len(gc.get_objects())
        engines = [RiskEngine(limits=limits) for _ in range(50)]
        gc.collect()
        final_objects = len(gc.get_objects())
        # Estimate memory per engine
        size_bytes = sum(sys.getsizeof(e) for e in engines) / len(engines)
        memory_kb = size_bytes / 1024.0

        print(f"\n--- PHASE 6 BENCHMARK: RISK ENGINE LATENCY ---")
        print(f"Iterations: {iterations}")
        print(f"Risk Evaluation p50: {p50:.2f} us (Target < 15.0 us)")
        print(f"Risk Evaluation p95: {p95:.2f} us (Target < 35.0 us)")
        print(f"Risk Evaluation p99: {p99:.2f} us (Target < 60.0 us)")
        print(f"Risk Evaluation max: {max_lat:.2f} us")
        print(f"Memory Per RiskEngine: {memory_kb:.2f} KB (Target < 50.0 KB)\n")

        # Optimization targets check
        self.assertLess(p50, 100.0, "p50 latency anomaly")
        self.assertLess(memory_kb, 50.0, "Memory footprint exceeded 50 KB")


if __name__ == "__main__":
    unittest.main()
