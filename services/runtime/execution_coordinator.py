"""
Tradego Risk-to-Execution Coordinator (Phase 8).

Coordinates the handoff from Phase 6 ApprovedTradeIntent to Phase 7 ExecutionPlanner
and ExecutionRouter. Enforces planning validity, idempotency, and router session safety.
"""

from datetime import datetime
import logging
from typing import Optional

from services.execution.models import OrderType, TimeInForce
from services.execution.state import ExecutionState
from services.execution.planner import ExecutionPlanner
from services.execution.router import ExecutionRouter
from services.risk.models import ApprovedTradeIntent
from services.signals.models import SignalType
from .guards import TradingGuard
from .portfolio import PortfolioRuntimeState

logger = logging.getLogger("runtime.execution_coord")


class ExecutionCoordinator:
    """
    Translates risk authorizations into canonical execution submissions via Phase 7.
    """

    def __init__(
        self,
        router: ExecutionRouter,
        portfolio_state: PortfolioRuntimeState,
        guard: TradingGuard,
    ) -> None:
        self._router = router
        self._portfolio_state = portfolio_state
        self._guard = guard

    def execute_intent(
        self,
        intent: ApprovedTradeIntent,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
        current_time: Optional[datetime] = None,
    ) -> Optional[ExecutionState]:
        """
        Synthesizes canonical OrderRequest via ExecutionPlanner and submits to ExecutionRouter.
        Fails closed on any planning or router validation error.
        """
        # 1. TradingGuard Check
        is_entry = intent.signal_type in (
            SignalType.ENTRY_LONG,
            SignalType.ENTRY_SHORT,
            SignalType.SCALE_IN,
        )

        if is_entry and not self._guard.can_submit_speculative_entry():
            logger.warning(
                f"[ExecutionCoordinator] Entry intent {intent.intent_id} blocked by TradingGuard ({self._guard.state.value})."
            )
            return None

        # 2. Plan Order via Phase 7 ExecutionPlanner
        current_position = self._portfolio_state.get_position(intent.instrument_id)
        eval_time = current_time or intent.market_timestamp or intent.intent_generated_timestamp
        try:
            order_request = ExecutionPlanner.plan_order(
                intent=intent,
                current_position=current_position,
                order_type=order_type,
                limit_price=limit_price,
                time_in_force=time_in_force,
                current_time=eval_time,
            )
        except Exception as e:
            logger.error(
                f"[ExecutionCoordinator] Execution planning failed for intent {intent.intent_id}: {e}",
                exc_info=True,
            )
            return None

        # 3. Submit Order to ExecutionRouter
        try:
            state = self._router.submit(order_request)
            return state
        except Exception as e:
            logger.error(
                f"[ExecutionCoordinator] Router submission failed for order {order_request.client_order_id}: {e}",
                exc_info=True,
            )
            return None
