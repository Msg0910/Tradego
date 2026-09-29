"""
Tradego Central Risk Engine (Phase 6).

Coordinates asset eligibility, causality validation, stateful admission control,
bifurcated entry/exit evaluation, position sizing, and intent emission.
"""

from datetime import datetime
import threading
from typing import Dict, Optional, Set
import uuid

from services.signals.models import SignalCandidate, SignalType
from .context import RiskContext
from .limits import RiskLimits
from .models import (
    ApprovedTradeIntent,
    RiskDecision,
    RiskDecisionType,
    RiskRejection,
    RiskRejectionReason,
)
from .position_sizing import CashEquityPositionSizer
from .validators import (
    validate_entry_gates,
    validate_entry_post_sizing,
    validate_exit_gates,
    validate_instrument_eligibility,
    validate_temporal_and_quality,
)


class AdmissionState:
    """
    Thread-safe in-memory admission registry tracking active approved trade intents
    to prevent duplicate execution intent within an active window.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active_fingerprints: Dict[str, datetime] = {}

    def is_admitted(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._active_fingerprints

    def admit(self, fingerprint: str, timestamp: datetime) -> None:
        with self._lock:
            self._active_fingerprints[fingerprint] = timestamp

    def release(self, fingerprint: str) -> None:
        with self._lock:
            self._active_fingerprints.pop(fingerprint, None)

    def clear(self) -> None:
        with self._lock:
            self._active_fingerprints.clear()


class RiskEngine:
    """
    Central gatekeeper orchestrating risk and portfolio control.
    """

    def __init__(
        self,
        limits: Optional[RiskLimits] = None,
        sizer: Optional[CashEquityPositionSizer] = None,
        admission_state: Optional[AdmissionState] = None,
    ) -> None:
        self.limits = limits or RiskLimits()
        self.sizer = sizer or CashEquityPositionSizer()
        self.admission_state = admission_state or AdmissionState()
        self._lock = threading.RLock()

    def _reject(
        self,
        signal: SignalCandidate,
        context: Optional[RiskContext],
        reason: RiskRejectionReason,
        message: str,
    ) -> RiskDecision:
        eval_ts = context.evaluation_timestamp if context is not None else signal.generated_timestamp
        rejection = RiskRejection(
            signal_id=signal.signal_id,
            fingerprint=signal.fingerprint,
            strategy_id=signal.strategy_id,
            instrument_id=signal.instrument_id,
            reason_code=reason,
            violating_rule=reason.value,
            message=message,
            evaluation_timestamp=eval_ts,
            diagnostic_data={
                "trigger_mode": signal.trigger_mode.value,
                "direction": signal.direction,
            },
        )
        return RiskDecision(decision=RiskDecisionType.REJECTED, rejection=rejection)

    def evaluate(
        self,
        signal: SignalCandidate,
        context: Optional[RiskContext],
    ) -> RiskDecision:
        """
        Evaluates a Phase 5 SignalCandidate against RiskContext and RiskLimits.
        Returns typed RiskDecision (APPROVED with ApprovedTradeIntent or REJECTED with RiskRejection).
        """
        # Guardrail: Fail-safe rejection if context is missing
        if context is None or context.portfolio_snapshot is None or context.account_state is None:
            return self._reject(
                signal,
                context,
                RiskRejectionReason.MISSING_PORTFOLIO_STATE,
                "RiskContext or PortfolioSnapshot is missing or uninitialized",
            )

        # Step 0: Asset Class Eligibility (Cash Equities only in Phase 6 v1)
        elig_reason = validate_instrument_eligibility(signal)
        if elig_reason is not None:
            return self._reject(
                signal,
                context,
                elig_reason,
                f"Instrument {signal.instrument_id.canonical_id} unsupported in Phase 6 v1",
            )

        # Step 1: Common Temporal & Feature Quality Validation
        tq_reason = validate_temporal_and_quality(signal, context, self.limits)
        if tq_reason is not None:
            return self._reject(
                signal,
                context,
                tq_reason,
                f"Temporal, causality, or feature quality failure: {tq_reason.value}",
            )

        # Step 2: Path Classification (Bifurcated Entry vs Exit)
        is_exit = signal.signal_type in (
            SignalType.EXIT_LONG,
            SignalType.EXIT_SHORT,
            SignalType.SCALE_OUT,
        )

        if is_exit:
            # =================================================================
            # EXIT / RISK-REDUCING PATH
            # =================================================================
            valid, exit_qty, exit_reason = validate_exit_gates(signal, context, self.limits)
            if not valid:
                assert exit_reason is not None
                return self._reject(
                    signal, context, exit_reason, f"Exit validation failed: {exit_reason.value}"
                )

            approved_entry_price = signal.suggested_entry_price or context.current_market_price
            intent = ApprovedTradeIntent(
                intent_id=str(uuid.uuid4()),
                intent_generated_timestamp=context.evaluation_timestamp,
                signal_id=signal.signal_id,
                fingerprint=signal.fingerprint,
                reaffirmation_key=signal.reaffirmation_key,
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                risk_config_version=self.limits.config_version,
                risk_config_hash=self.limits.config_hash,
                instrument_id=signal.instrument_id,
                signal_type=signal.signal_type,
                direction=signal.direction,
                permitted_quantity=exit_qty,
                approved_entry_price=approved_entry_price,
                approved_stop_loss=None,
                approved_take_profit=None,
                risk_reward_ratio=None,
                calculated_monetary_risk=0.0,
                allocated_capital=0.0,
                binding_constraint="HELD_POSITION_BOUND",
                market_timestamp=signal.market_timestamp,
                signal_generated_timestamp=signal.generated_timestamp,
                expiry_timestamp=signal.expiry_timestamp,
                metadata={"path": "EXIT"},
            )
            return RiskDecision(decision=RiskDecisionType.APPROVED, approved_intent=intent)

        # =====================================================================
        # ENTRY / POSITION-INCREASING PATH
        # =====================================================================
        with self._lock:
            # 1. Stateful Deduplication Gate
            if self.admission_state.is_admitted(signal.fingerprint):
                return self._reject(
                    signal,
                    context,
                    RiskRejectionReason.DUPLICATE_SIGNAL,
                    f"Active duplicate intent already admitted for fingerprint {signal.fingerprint}",
                )

            # 2. Pre-Sizing Entry Gates (Account health, drawdown, daily loss, stop geometry)
            pre_reason = validate_entry_gates(signal, context, self.limits)
            if pre_reason is not None:
                return self._reject(
                    signal, context, pre_reason, f"Pre-sizing entry gate failed: {pre_reason.value}"
                )

            # 3. Position Sizing
            sizing_result = self.sizer.calculate_entry_size(signal, context, self.limits)

            # 4. Post-Sizing Entry Gates (Lot floor, leverage caps, exposure caps)
            post_reason = validate_entry_post_sizing(sizing_result, signal, context, self.limits)
            if post_reason is not None:
                return self._reject(
                    signal,
                    context,
                    post_reason,
                    f"Post-sizing entry gate failed: {post_reason.value}",
                )

            # 5. Admitted: Record in admission state
            self.admission_state.admit(signal.fingerprint, context.evaluation_timestamp)

            approved_entry_price = signal.suggested_entry_price or context.current_market_price
            intent = ApprovedTradeIntent(
                intent_id=str(uuid.uuid4()),
                intent_generated_timestamp=context.evaluation_timestamp,
                signal_id=signal.signal_id,
                fingerprint=signal.fingerprint,
                reaffirmation_key=signal.reaffirmation_key,
                strategy_id=signal.strategy_id,
                strategy_version=signal.strategy_version,
                risk_config_version=self.limits.config_version,
                risk_config_hash=self.limits.config_hash,
                instrument_id=signal.instrument_id,
                signal_type=signal.signal_type,
                direction=signal.direction,
                permitted_quantity=sizing_result.permitted_quantity,
                approved_entry_price=approved_entry_price,
                approved_stop_loss=signal.suggested_stop_loss,
                approved_take_profit=signal.suggested_take_profit,
                risk_reward_ratio=signal.risk_reward_ratio,
                calculated_monetary_risk=sizing_result.calculated_monetary_risk,
                allocated_capital=sizing_result.allocated_capital,
                binding_constraint=sizing_result.binding_constraint,
                market_timestamp=signal.market_timestamp,
                signal_generated_timestamp=signal.generated_timestamp,
                expiry_timestamp=signal.expiry_timestamp,
                metadata={"path": "ENTRY"},
            )
            return RiskDecision(decision=RiskDecisionType.APPROVED, approved_intent=intent)
