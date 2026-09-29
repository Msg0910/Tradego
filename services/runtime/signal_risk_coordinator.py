"""
Tradego Signal-to-Risk Coordinator (Phase 8).

Coordinates the handoff from Phase 5 SignalCandidate to Phase 6 RiskEngine.
Assembles the complete immutable RiskContext, applies TradingGuard checks,
and evaluates trade admission. Fails closed on any exception.
"""

from datetime import datetime, timezone
import logging
from typing import Optional

from services.analytics.models import FeatureQuality
from services.market_state.instrument import InstrumentMetadata, InstrumentRegistry
from services.risk.context import RiskContext
from services.risk.engine import RiskEngine
from services.risk.models import ApprovedTradeIntent, RiskDecisionType
from services.signals.models import SignalCandidate, SignalType
from .guards import TradingGuard
from .portfolio import PortfolioRuntimeState

logger = logging.getLogger("runtime.signal_risk")


class SignalRiskCoordinator:
    """
    Coordinates signal admission with the frozen Phase 6 RiskEngine.
    Enforces TradingGuard entry blocks while permitting risk-reducing exits.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        portfolio_state: PortfolioRuntimeState,
        guard: TradingGuard,
        registry: InstrumentRegistry,
    ) -> None:
        self._risk_engine = risk_engine
        self._portfolio_state = portfolio_state
        self._guard = guard
        self._registry = registry

    def evaluate_signal(
        self,
        signal: SignalCandidate,
        current_price: float,
        eval_time: Optional[datetime] = None,
        feature_quality: FeatureQuality = FeatureQuality.VALID,
    ) -> Optional[ApprovedTradeIntent]:
        """
        Evaluates a Phase 5 SignalCandidate against Phase 6 RiskEngine.
        Returns ApprovedTradeIntent if approved; returns None if rejected, blocked, or on error.
        Fails closed on any exception.
        """
        # 1. TradingGuard Check: Bifurcated entry vs exit
        is_entry = signal.signal_type in (
            SignalType.ENTRY_LONG,
            SignalType.ENTRY_SHORT,
            SignalType.SCALE_IN,
        )

        if is_entry:
            if not self._guard.can_submit_speculative_entry():
                logger.warning(
                    f"[SignalRiskCoordinator] Speculative entry blocked by TradingGuard ({self._guard.state.value}) "
                    f"for signal {signal.signal_id} on {signal.instrument_id.canonical_id}."
                )
                return None
        else:
            if not self._guard.can_submit_exit():
                logger.warning(
                    f"[SignalRiskCoordinator] Exit blocked by TradingGuard for signal {signal.signal_id}."
                )
                return None

        # 2. Assemble Immutable RiskContext
        now = eval_time or signal.generated_timestamp
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        metadata = self._registry.get_metadata(signal.instrument_id)
        if metadata is None:
            # Fallback metadata for unregistered test instruments
            metadata = InstrumentMetadata(
                instrument_id=signal.instrument_id,
                lot_size=1,
                tick_size=0.05,
                price_precision=2,
            )

        portfolio_snapshot = self._portfolio_state.get_portfolio_snapshot(timestamp=now)
        account_state = self._portfolio_state.get_account_state(timestamp=now)

        context = RiskContext(
            account_state=account_state,
            portfolio_snapshot=portfolio_snapshot,
            instrument_metadata=metadata,
            evaluation_timestamp=now,
            current_market_price=current_price,
            feature_quality=feature_quality,
        )

        # 3. Synchronous Risk Evaluation (Fail-Closed Boundary)
        try:
            decision = self._risk_engine.evaluate(signal, context)
        except Exception as e:
            logger.error(
                f"[SignalRiskCoordinator] Unhandled exception inside RiskEngine.evaluate for signal "
                f"{signal.signal_id}: {e}",
                exc_info=True,
            )
            # Fail closed: zero order
            return None

        if decision.decision == RiskDecisionType.APPROVED and decision.approved_intent is not None:
            return decision.approved_intent

        # Rejected (logged via RiskRejection)
        return None
