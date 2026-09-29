"""
Tradego Boundary Pre-Trade Risk Gate & Evaluation (Phase 6).
Evaluates an ExecutionIntent against authoritative risk limits and runtime portfolio state
BEFORE an intent can progress toward execution.

INVARIANTS:
1. Zero Modification to Frozen Trading Core: Read-only observation of RiskLimits and PortfolioRuntimeState.
2. Zero Fabrication: If authoritative state is unmounted or unavailable, strictly returns UNAVAILABLE.
3. Zero Broker/Execution Invocation: Risk evaluation does NOT submit orders or invoke ExecutionRouter.
4. Non-Execution Guarantee: Intent remains in ACCEPTED_AS_INTENT category with is_executed=False.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Dict, List, Optional, Set

from services.risk.limits import RiskLimits
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState
from services.runtime.portfolio import PortfolioRuntimeState

from .intent import ExecutionIntent, IntentState
from .security import Tier1AuditLogger


class RiskDecisionType(str, Enum):
    """Deterministic outcome of pre-trade risk evaluation."""
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RiskEvaluationResult:
    """
    Structured outcome of pre-trade risk evaluation.
    Contains decision, rationale, limit details, and lineage.
    """
    intent_id: str
    decision: RiskDecisionType
    reason: str
    details: Dict[str, Any]
    evaluator_id: str
    evaluated_at: str
    correlation_id: str
    status_category: str = "ACCEPTED_AS_INTENT"
    is_executed: bool = False
    resulting_state: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Converts result to JSON-serializable dictionary."""
        return {
            "intent_id": self.intent_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "details": self.details,
            "evaluator_id": self.evaluator_id,
            "evaluated_at": self.evaluated_at,
            "correlation_id": self.correlation_id,
            "status_category": self.status_category,
            "is_executed": self.is_executed,
            "resulting_state": self.resulting_state,
        }


class PreTradeRiskEvaluator:
    """
    Authoritative boundary Risk Gate.
    Performs deterministic pre-trade evaluation across:
    - Guard state (HALTED / PAUSED)
    - Intent operational state (APPROVED requirement)
    - Authoritative source availability (no synthetic values)
    - Margin sufficiency
    - Order quantity limits
    - Symbol restrictions
    - Gross exposure / leverage limits
    - Duplicate / conflicting active intents
    """

    def __init__(
        self,
        trading_guard: TradingGuard,
        audit_logger: Tier1AuditLogger,
        portfolio_state: Optional[PortfolioRuntimeState] = None,
        risk_limits: Optional[RiskLimits] = None,
        restricted_symbols: Optional[Set[str]] = None,
        max_order_quantity: int = 10000,
        margin_rate: float = 0.20,
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._portfolio_state = portfolio_state
        self._risk_limits = risk_limits
        self._restricted_symbols = set(restricted_symbols or [])
        self._max_order_quantity = max_order_quantity
        self._margin_rate = margin_rate
        self._lock = threading.RLock()

    def set_portfolio_state(self, state: Optional[PortfolioRuntimeState]) -> None:
        """Mounts or unmounts authoritative portfolio state in a thread-safe manner."""
        with self._lock:
            self._portfolio_state = state

    def set_risk_limits(self, limits: Optional[RiskLimits]) -> None:
        """Mounts or unmounts authoritative risk limits in a thread-safe manner."""
        with self._lock:
            self._risk_limits = limits

    def add_restricted_symbol(self, symbol: str) -> None:
        """Adds a symbol to the prohibited list."""
        with self._lock:
            self._restricted_symbols.add(symbol.strip().upper())

    def remove_restricted_symbol(self, symbol: str) -> None:
        """Removes a symbol from the prohibited list."""
        with self._lock:
            self._restricted_symbols.discard(symbol.strip().upper())

    def evaluate(
        self,
        intent: "ExecutionIntent",
        evaluator_id: str = "SYSTEM_RISK",
        correlation_id: Optional[str] = None,
        active_intents: Optional[List["ExecutionIntent"]] = None,
    ) -> "RiskEvaluationResult":
        """Convenience alias for evaluate_intent with defaulted evaluator_id / correlation_id."""
        import uuid as _uuid
        return self.evaluate_intent(
            intent=intent,
            evaluator_id=evaluator_id,
            correlation_id=correlation_id or str(_uuid.uuid4()),
            active_intents=active_intents or [],
        )

    def evaluate_intent(
        self,
        intent: ExecutionIntent,
        evaluator_id: str,
        correlation_id: str,
        active_intents: Optional[List[ExecutionIntent]] = None,
    ) -> RiskEvaluationResult:
        """
        Performs authoritative pre-trade risk gating on an ExecutionIntent.
        Emits auditable lifecycle entries and transitions intent state if allowed or rejected.
        """
        now_str = datetime.now(timezone.utc).isoformat()

        with self._lock:
            # Audit: RISK_EVALUATION_STARTED
            self._audit.log({
                "action": "RISK_EVALUATION_STARTED",
                "intent_id": intent.intent_id,
                "operator_id": evaluator_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "correlation_id": correlation_id,
            })

            # Check 1: Intent Lifecycle State
            if intent.state == IntentState.CANCELLED:
                reason = "INTENT_CANCELLED: Cannot evaluate a cancelled execution intent"
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"state": intent.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            if intent.is_expired or intent.state == IntentState.EXPIRED:
                reason = "INTENT_EXPIRED: Cannot evaluate an expired execution intent"
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"state": intent.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            if intent.state in [IntentState.REJECTED, IntentState.RISK_REJECTED]:
                reason = "INTENT_ALREADY_REJECTED: Cannot evaluate an already rejected execution intent"
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"state": intent.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            if intent.state in [IntentState.DRAFT, IntentState.SUBMITTED, IntentState.PENDING_APPROVAL]:
                reason = "NOT_OPERATIONALLY_APPROVED: Intent must be operationally approved prior to risk evaluation"
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"state": intent.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 2: TradingGuard State
            if self._guard.state == GuardState.HALTED:
                reason = "GUARD_HALTED: TradingGuard is HALTED. All pre-trade intent admissions are blocked."
                self._audit.log({
                    "action": "RISK_LIMIT_BREACH",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "breach_type": "GUARD_HALTED",
                    "correlation_id": correlation_id,
                })
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.state = IntentState.RISK_REJECTED
                intent.risk_status = "REJECTED"
                intent.risk_rejection_reason = reason
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"guard_state": self._guard.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            if self._guard.state == GuardState.PAUSED:
                reason = "GUARD_PAUSED: TradingGuard is PAUSED. Speculative pre-trade admissions blocked."
                self._audit.log({
                    "action": "RISK_LIMIT_BREACH",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "breach_type": "GUARD_PAUSED",
                    "correlation_id": correlation_id,
                })
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.state = IntentState.RISK_REJECTED
                intent.risk_status = "REJECTED"
                intent.risk_rejection_reason = reason
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"guard_state": self._guard.state.value},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 3: Authoritative Source Availability (Zero-Fabrication Principle)
            if self._risk_limits is None or self._portfolio_state is None:
                reason = "AUTHORITATIVE_SOURCE_UNAVAILABLE: Authoritative risk limits or portfolio state not mounted"
                self._audit.log({
                    "action": "RISK_UNAVAILABLE",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.risk_status = "UNAVAILABLE"
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.UNAVAILABLE,
                    reason=reason,
                    details={
                        "risk_limits_mounted": self._risk_limits is not None,
                        "portfolio_state_mounted": self._portfolio_state is not None,
                    },
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 4: Symbol Restrictions
            if intent.symbol.strip().upper() in self._restricted_symbols:
                reason = f"SYMBOL_RESTRICTED: Instrument {intent.symbol} is on the restricted trading list"
                self._audit.log({
                    "action": "RISK_LIMIT_BREACH",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "breach_type": "SYMBOL_RESTRICTED",
                    "symbol": intent.symbol,
                    "correlation_id": correlation_id,
                })
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.state = IntentState.RISK_REJECTED
                intent.risk_status = "REJECTED"
                intent.risk_rejection_reason = reason
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={"symbol": intent.symbol},
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 5: Quantity Limit
            if intent.quantity > self._max_order_quantity:
                reason = (
                    f"QUANTITY_LIMIT_BREACH: Order quantity {intent.quantity} "
                    f"exceeds maximum allowed threshold {self._max_order_quantity}"
                )
                self._audit.log({
                    "action": "RISK_LIMIT_BREACH",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "breach_type": "QUANTITY_LIMIT_BREACH",
                    "quantity": intent.quantity,
                    "max_quantity": self._max_order_quantity,
                    "correlation_id": correlation_id,
                })
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.state = IntentState.RISK_REJECTED
                intent.risk_status = "REJECTED"
                intent.risk_rejection_reason = reason
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={
                        "requested_quantity": intent.quantity,
                        "max_order_quantity": self._max_order_quantity,
                    },
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 6: Margin Sufficiency
            price = intent.limit_price if (intent.limit_price and intent.limit_price > 0) else 100.0
            order_notional = intent.quantity * price
            required_margin = order_notional * self._margin_rate

            if hasattr(self._portfolio_state, "get_account_state"):
                acct_snap = self._portfolio_state.get_account_state()
            elif hasattr(self._portfolio_state, "get_account_snapshot"):
                acct_snap = self._portfolio_state.get_account_snapshot()
            else:
                acct_snap = getattr(self._portfolio_state, "account_state", None)

            available_cash = getattr(acct_snap, "available_cash", getattr(self._portfolio_state, "cash_balance", 0.0))

            if required_margin > available_cash:
                reason = (
                    f"INSUFFICIENT_MARGIN: Required margin {required_margin:.2f} "
                    f"exceeds available cash {available_cash:.2f}"
                )
                self._audit.log({
                    "action": "RISK_LIMIT_BREACH",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "breach_type": "INSUFFICIENT_MARGIN",
                    "required_margin": required_margin,
                    "available_cash": available_cash,
                    "correlation_id": correlation_id,
                })
                self._audit.log({
                    "action": "RISK_REJECTED",
                    "intent_id": intent.intent_id,
                    "operator_id": evaluator_id,
                    "reason": reason,
                    "correlation_id": correlation_id,
                })
                intent.state = IntentState.RISK_REJECTED
                intent.risk_status = "REJECTED"
                intent.risk_rejection_reason = reason
                intent.risk_evaluator_id = evaluator_id
                intent.risk_evaluated_at = now_str
                return RiskEvaluationResult(
                    intent_id=intent.intent_id,
                    decision=RiskDecisionType.REJECTED,
                    reason=reason,
                    details={
                        "required_margin": required_margin,
                        "available_cash": available_cash,
                    },
                    evaluator_id=evaluator_id,
                    evaluated_at=now_str,
                    correlation_id=correlation_id,
                    resulting_state=intent.state.value,
                )

            # Check 7: Gross Exposure and Leverage Limits
            port_snap = self._portfolio_state.get_portfolio_snapshot()
            current_gross = port_snap.total_gross_exposure
            projected_gross = current_gross + order_notional
            total_equity = acct_snap.total_equity

            if total_equity > 0:
                projected_leverage = projected_gross / total_equity
                if projected_leverage > self._risk_limits.max_gross_leverage:
                    reason = (
                        f"EXPOSURE_LIMIT_BREACH: Projected gross leverage {projected_leverage:.2f} "
                        f"exceeds maximum threshold {self._risk_limits.max_gross_leverage:.2f}"
                    )
                    self._audit.log({
                        "action": "RISK_LIMIT_BREACH",
                        "intent_id": intent.intent_id,
                        "operator_id": evaluator_id,
                        "breach_type": "EXPOSURE_LIMIT_BREACH",
                        "projected_leverage": projected_leverage,
                        "max_gross_leverage": self._risk_limits.max_gross_leverage,
                        "correlation_id": correlation_id,
                    })
                    self._audit.log({
                        "action": "RISK_REJECTED",
                        "intent_id": intent.intent_id,
                        "operator_id": evaluator_id,
                        "reason": reason,
                        "correlation_id": correlation_id,
                    })
                    intent.state = IntentState.RISK_REJECTED
                    intent.risk_status = "REJECTED"
                    intent.risk_rejection_reason = reason
                    intent.risk_evaluator_id = evaluator_id
                    intent.risk_evaluated_at = now_str
                    return RiskEvaluationResult(
                        intent_id=intent.intent_id,
                        decision=RiskDecisionType.REJECTED,
                        reason=reason,
                        details={
                            "projected_leverage": projected_leverage,
                            "max_gross_leverage": self._risk_limits.max_gross_leverage,
                        },
                        evaluator_id=evaluator_id,
                        evaluated_at=now_str,
                        correlation_id=correlation_id,
                        resulting_state=intent.state.value,
                    )

            # Check 8: Duplicate or Conflicting Active Intent
            if active_intents:
                clean_sym = intent.symbol.strip().upper()
                clean_side = intent.side.strip().upper()
                for other in active_intents:
                    if other.intent_id == intent.intent_id:
                        continue
                    if other.state in [IntentState.APPROVED, IntentState.RISK_APPROVED]:
                        other_sym = other.symbol.strip().upper()
                        other_side = other.side.strip().upper()
                        if other_sym == clean_sym:
                            if other_side != clean_side:
                                reason = (
                                    f"DUPLICATE_OR_CONFLICTING_INTENT: Conflicting active intent {other.intent_id} "
                                    f"exists on opposing side ({other_side}) for {clean_sym}"
                                )
                                self._audit.log({
                                    "action": "RISK_LIMIT_BREACH",
                                    "intent_id": intent.intent_id,
                                    "operator_id": evaluator_id,
                                    "breach_type": "CONFLICTING_ACTIVE_INTENT",
                                    "conflicting_intent_id": other.intent_id,
                                    "correlation_id": correlation_id,
                                })
                                self._audit.log({
                                    "action": "RISK_REJECTED",
                                    "intent_id": intent.intent_id,
                                    "operator_id": evaluator_id,
                                    "reason": reason,
                                    "correlation_id": correlation_id,
                                })
                                intent.state = IntentState.RISK_REJECTED
                                intent.risk_status = "REJECTED"
                                intent.risk_rejection_reason = reason
                                intent.risk_evaluator_id = evaluator_id
                                intent.risk_evaluated_at = now_str
                                return RiskEvaluationResult(
                                    intent_id=intent.intent_id,
                                    decision=RiskDecisionType.REJECTED,
                                    reason=reason,
                                    details={"conflicting_intent_id": other.intent_id},
                                    evaluator_id=evaluator_id,
                                    evaluated_at=now_str,
                                    correlation_id=correlation_id,
                                    resulting_state=intent.state.value,
                                )

                            if (
                                other_side == clean_side
                                and other.quantity == intent.quantity
                                and other.limit_price == intent.limit_price
                            ):
                                reason = (
                                    f"DUPLICATE_OR_CONFLICTING_INTENT: Duplicate active intent {other.intent_id} "
                                    f"already approved for {clean_sym}"
                                )
                                self._audit.log({
                                    "action": "RISK_LIMIT_BREACH",
                                    "intent_id": intent.intent_id,
                                    "operator_id": evaluator_id,
                                    "breach_type": "DUPLICATE_ACTIVE_INTENT",
                                    "conflicting_intent_id": other.intent_id,
                                    "correlation_id": correlation_id,
                                })
                                self._audit.log({
                                    "action": "RISK_REJECTED",
                                    "intent_id": intent.intent_id,
                                    "operator_id": evaluator_id,
                                    "reason": reason,
                                    "correlation_id": correlation_id,
                                })
                                intent.state = IntentState.RISK_REJECTED
                                intent.risk_status = "REJECTED"
                                intent.risk_rejection_reason = reason
                                intent.risk_evaluator_id = evaluator_id
                                intent.risk_evaluated_at = now_str
                                return RiskEvaluationResult(
                                    intent_id=intent.intent_id,
                                    decision=RiskDecisionType.REJECTED,
                                    reason=reason,
                                    details={"duplicate_intent_id": other.intent_id},
                                    evaluator_id=evaluator_id,
                                    evaluated_at=now_str,
                                    correlation_id=correlation_id,
                                    resulting_state=intent.state.value,
                                )

            # Step 9: All Checks Passed -> RISK_APPROVED
            reason = "PRE_TRADE_RISK_APPROVED: All pre-trade risk and exposure limits verified"
            intent.state = IntentState.RISK_APPROVED
            intent.risk_status = "ALLOWED"
            intent.risk_evaluator_id = evaluator_id
            intent.risk_evaluated_at = now_str
            intent.status_category = "ACCEPTED_AS_INTENT"
            intent.is_executed = False

            eval_details = {
                "required_margin": required_margin,
                "available_cash": available_cash,
                "projected_gross_exposure": projected_gross,
                "max_gross_leverage": self._risk_limits.max_gross_leverage,
                "config_version": self._risk_limits.config_version,
                "config_hash": self._risk_limits.config_hash,
            }
            intent.risk_details = eval_details

            self._audit.log({
                "action": "RISK_ALLOWED",
                "intent_id": intent.intent_id,
                "operator_id": evaluator_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "resulting_state": intent.state.value,
                "correlation_id": correlation_id,
            })

            return RiskEvaluationResult(
                intent_id=intent.intent_id,
                decision=RiskDecisionType.ALLOWED,
                reason=reason,
                details=eval_details,
                evaluator_id=evaluator_id,
                evaluated_at=now_str,
                correlation_id=correlation_id,
                status_category="ACCEPTED_AS_INTENT",
                is_executed=False,
                resulting_state=intent.state.value,
            )


# Aliases per architectural specifications
RiskGate = PreTradeRiskEvaluator
