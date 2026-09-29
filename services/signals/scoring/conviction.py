"""
Signal Conviction and Priority Scorers for Tradego Signal Intelligence (Phase 5).

Implements:
- RegimeAlignmentScorer: Scales confidence based on alignment between setup and macro regime.
- RiskRewardScorer: Evaluates suggested geometry risk-reward ratio.
"""

from ..base import BaseScorer
from ..context import StrategyContext
from ..models import SignalCandidate


class RegimeAlignmentScorer(BaseScorer):
    """
    Evaluates alignment between signal direction and active regime.
    """

    def __init__(self, name: str = "REGIME_ALIGNMENT_SCORER") -> None:
        super().__init__(name)

    def score(self, candidate: SignalCandidate, context: StrategyContext) -> float:
        base_score = candidate.confidence_score
        regime = candidate.regime

        # Penalty if trading counter-trend or in neutral regime
        if (candidate.direction > 0 and regime == "BULLISH") or (
            candidate.direction < 0 and regime == "BEARISH"
        ):
            alignment_multiplier = 1.0
        elif regime == "NEUTRAL":
            alignment_multiplier = 0.8
        else:
            alignment_multiplier = 0.5

        return round(min(1.0, max(0.0, base_score * alignment_multiplier)), 4)


class RiskRewardScorer(BaseScorer):
    """
    Evaluates the geometry risk-to-reward ratio.
    """

    def __init__(self, min_rr: float = 1.5, name: str = "RISK_REWARD_SCORER") -> None:
        super().__init__(name)
        self.min_rr = min_rr

    def score(self, candidate: SignalCandidate, context: StrategyContext) -> float:
        rr = candidate.risk_reward_ratio or 1.0
        if rr < self.min_rr:
            return 0.5
        elif rr >= 2.5:
            return 1.0
        return 0.8
