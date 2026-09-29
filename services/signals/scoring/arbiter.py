"""
Deterministic Signal Arbiter for Tradego Signal Intelligence (Phase 5).

Ranks, filters, and resolves candidate signals across strategies and instruments.
Guarantees 100% deterministic ordering using an explicit lexicographical ranking key.
Zero reliance on dictionary iteration, hash randomization, or memory addresses.
"""

from typing import List, Optional, Tuple

from ..models import SignalCandidate


class DeterministicSignalArbiter:
    """
    Arbitrates across multi-strategy and multi-instrument SignalCandidates.
    Sorts strictly via the canonical tuple:
    (-confidence_score, priority, strategy_id, instrument_id.symbol, signal_type.value)
    """

    def __init__(self, min_confidence: float = 0.5, max_candidates: Optional[int] = None) -> None:
        self.min_confidence = min_confidence
        self.max_candidates = max_candidates

    @staticmethod
    def canonical_ranking_key(candidate: SignalCandidate) -> Tuple[float, int, str, str, str]:
        """
        Deterministic tie-breaking key:
        1. -confidence_score (highest confidence first)
        2. priority (lower numeric tier = higher priority)
        3. strategy_id (alphabetical)
        4. symbol (alphabetical)
        5. signal_type (alphabetical)
        """
        return (
            -candidate.confidence_score,
            candidate.priority,
            candidate.strategy_id,
            candidate.instrument_id.symbol,
            candidate.signal_type.value,
        )

    def arbitrate(self, candidates: List[SignalCandidate]) -> List[SignalCandidate]:
        """
        Filters by min_confidence and returns deterministically sorted SignalCandidates.
        """
        if not candidates:
            return []

        # Filter
        qualified = [c for c in candidates if c.confidence_score >= self.min_confidence]

        # Deterministic sort
        sorted_candidates = sorted(qualified, key=self.canonical_ranking_key)

        if self.max_candidates is not None and self.max_candidates > 0:
            return sorted_candidates[: self.max_candidates]

        return sorted_candidates
