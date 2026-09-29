"""
Unit tests for DeterministicSignalArbiter (Phase 5).

Tests:
- Deterministic multi-strategy ranking
- Strict tie-breaking sequence: (-confidence, priority, strategy_id, symbol, signal_type)
- Minimum confidence filtering
- Maximum candidates truncation
- Reversible input invariance (different initial list orders produce identical sorted ranking)
"""

import unittest
from datetime import datetime, timezone

from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.signals.models import SignalCandidate, SignalType, TriggerMode
from services.signals.scoring.arbiter import DeterministicSignalArbiter


def make_candidate(
    sig_id: str,
    strategy_id: str,
    symbol: str,
    confidence: float,
    priority: int = 0,
    signal_type: SignalType = SignalType.ENTRY_LONG,
) -> SignalCandidate:
    iid = InstrumentId(symbol, Exchange.NSE, InstrumentType.EQUITY)
    ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)
    return SignalCandidate(
        signal_id=sig_id,
        fingerprint=f"fp_{sig_id}",
        reaffirmation_key=f"reaff_{sig_id}",
        strategy_id=strategy_id,
        strategy_version="1.0",
        config_hash="cfg",
        instrument_id=iid,
        signal_type=signal_type,
        direction=1,
        trigger_mode=TriggerMode.BAR_CLOSE,
        confidence_score=confidence,
        suggested_entry_price=100.0,
        suggested_stop_loss=95.0,
        suggested_take_profit=110.0,
        risk_reward_ratio=2.0,
        priority=priority,
        market_timestamp=ts,
        availability_timestamp=ts,
        generated_timestamp=ts,
    )


class TestDeterministicArbiter(unittest.TestCase):

    def test_ranking_by_confidence(self):
        arbiter = DeterministicSignalArbiter(min_confidence=0.5)

        c1 = make_candidate("1", "STRAT_A", "INFY", confidence=0.7)
        c2 = make_candidate("2", "STRAT_B", "RELIANCE", confidence=0.9)
        c3 = make_candidate("3", "STRAT_C", "TCS", confidence=0.8)

        # Ingest in random order
        ranked = arbiter.arbitrate([c1, c2, c3])
        self.assertEqual([c.signal_id for c in ranked], ["2", "3", "1"])

        # Reverse initial order -> ranking must be 100% identical
        ranked_rev = arbiter.arbitrate([c3, c2, c1])
        self.assertEqual([c.signal_id for c in ranked_rev], ["2", "3", "1"])

    def test_tie_breaking_hierarchy(self):
        arbiter = DeterministicSignalArbiter(min_confidence=0.5)

        # Identical confidence (0.80)
        # c1 has priority 0 (higher priority) vs c2 priority 1
        c1 = make_candidate("P0", "STRAT_B", "TCS", confidence=0.8, priority=0)
        c2 = make_candidate("P1", "STRAT_A", "INFY", confidence=0.8, priority=1)

        ranked = arbiter.arbitrate([c2, c1])
        self.assertEqual(ranked[0].signal_id, "P0")

        # Same confidence and same priority -> strategy_id alphabetical tie-breaker
        c_strat_a = make_candidate("SA", "STRAT_A", "TCS", confidence=0.8, priority=0)
        c_strat_b = make_candidate("SB", "STRAT_B", "INFY", confidence=0.8, priority=0)

        ranked_strat = arbiter.arbitrate([c_strat_b, c_strat_a])
        self.assertEqual(ranked_strat[0].signal_id, "SA")

        # Same confidence, priority, strategy_id -> symbol alphabetical tie-breaker
        c_infy = make_candidate("INFY", "STRAT_A", "INFY", confidence=0.8, priority=0)
        c_tcs = make_candidate("TCS", "STRAT_A", "TCS", confidence=0.8, priority=0)

        ranked_sym = arbiter.arbitrate([c_tcs, c_infy])
        self.assertEqual(ranked_sym[0].signal_id, "INFY")

    def test_filtering_and_max_candidates(self):
        arbiter = DeterministicSignalArbiter(min_confidence=0.7, max_candidates=2)

        c1 = make_candidate("1", "S", "SYM1", confidence=0.9)
        c2 = make_candidate("2", "S", "SYM2", confidence=0.85)
        c3 = make_candidate("3", "S", "SYM3", confidence=0.75)
        c4 = make_candidate("4", "S", "SYM4", confidence=0.6)  # Below 0.7 threshold

        ranked = arbiter.arbitrate([c1, c2, c3, c4])
        # c4 filtered out, max_candidates limits to top 2: c1 and c2
        self.assertEqual(len(ranked), 2)
        self.assertEqual([c.signal_id for c in ranked], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
