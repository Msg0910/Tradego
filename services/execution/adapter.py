"""
Tradego Phase 7 Execution Layer — Broker Execution Adapter Interface.

Defines the abstract contract isolating broker protocols, network sessions,
and vendor wire-formats from Tradego Core.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from services.execution.models import (
    OrderAcknowledgement,
    OrderRequest,
    OrderUpdate,
    SubmissionResult,
)
from services.risk.models import PositionSnapshot


class BrokerExecutionAdapter(ABC):
    """
    Abstract contract for all Tradego execution adapters.
    Completely isolates broker protocols, authentication, and wire-formats from Tradego Core.
    """

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> SubmissionResult:
        """
        Submits an order to the venue and returns a typed SubmissionResult
        categorizing the outcome (ACKNOWLEDGED, REJECTED, RETRYABLE_FAILURE, AMBIGUOUS_UNKNOWN).
        """
        pass

    @abstractmethod
    def cancel_order(self, client_order_id: str, broker_order_id: str) -> bool:
        """
        Requests cancellation of an active working order on the venue.
        Returns True if the cancellation was accepted/confirmed.
        """
        pass

    @abstractmethod
    def replace_order(
        self,
        client_order_id: str,
        broker_order_id: str,
        new_price: Optional[float],
        new_quantity: Optional[int],
    ) -> OrderAcknowledgement:
        """
        Requests modification of price and/or quantity for an active working order.
        Returns the updated OrderAcknowledgement with the active broker order ID.
        """
        pass

    @abstractmethod
    def get_order_status(
        self, client_order_id: str, broker_order_id: Optional[str]
    ) -> OrderUpdate:
        """
        Queries authoritative status of a single order by client or broker order ID.
        """
        pass

    @abstractmethod
    def get_open_orders(self) -> List[OrderUpdate]:
        """
        Fetches all active open orders currently working on the broker/venue matching engine.
        """
        pass

    @abstractmethod
    def get_positions(self) -> List[PositionSnapshot]:
        """
        Fetches net open positions recorded on the broker book.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Verifies transport connectivity, authentication, and rate limiter status.
        """
        pass
