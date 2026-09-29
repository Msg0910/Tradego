"""
Tradego Phase 7 Execution Layer & Order Lifecycle Management.

Authoritative package export for execution domain contracts, order planning,
lifecycle state machines, broker abstraction, simulated paper matching, and position accounting.
"""

from services.execution.accounting import PositionAccounting
from services.execution.adapter import BrokerExecutionAdapter
from services.execution.config import ExecutionConfig
from services.execution.models import (
    CanonicalOrderStatus,
    ExecutionFailureReason,
    Fill,
    OrderAcknowledgement,
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    PositionEffect,
    PositionReconciliationAdjustment,
    SubmissionOutcomeType,
    SubmissionResult,
    TimeInForce,
)
from services.execution.paper_adapter import PaperExecutionAdapter, TokenBucketRateLimiter
from services.execution.planner import ExecutionPlanner, derive_order_side_and_position_effect
from services.execution.router import ExecutionRouter
from services.execution.state import (
    ExecutionState,
    ExecutionStateRegistry,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
)

__all__ = [
    # Canonical Enums
    "OrderSide",
    "PositionEffect",
    "OrderType",
    "TimeInForce",
    "OrderPurpose",
    "CanonicalOrderStatus",
    "ExecutionFailureReason",
    "SubmissionOutcomeType",
    # Domain Contracts
    "OrderRequest",
    "OrderAcknowledgement",
    "SubmissionResult",
    "Fill",
    "OrderUpdate",
    "PositionReconciliationAdjustment",
    # Configuration
    "ExecutionConfig",
    # Execution Planning
    "ExecutionPlanner",
    "derive_order_side_and_position_effect",
    # Execution State Machine
    "ExecutionState",
    "ExecutionStateRegistry",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    # Broker Abstraction
    "BrokerExecutionAdapter",
    # Paper Execution
    "PaperExecutionAdapter",
    "TokenBucketRateLimiter",
    # Execution Router
    "ExecutionRouter",
    # Position Accounting
    "PositionAccounting",
]
