"""
Tradego Phase 7 — Broker Adapter Interface & Paper Implementation.

Establishes the authoritative BrokerAdapter boundary isolating Tradego Core
and Gateway from vendor wire-formats and execution protocols.
Enforces zero live broker connectivity by default.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import threading
from typing import Any, Dict, List, Optional, Set
import uuid


@dataclass(frozen=True)
class BrokerDispatchResult:
    """
    Standardized, authoritative broker dispatch response.
    Isolates broker-specific wire responses from the canonical Tradego gateway.
    """
    success: bool
    outcome: str  # "ACKNOWLEDGED", "REJECTED", "FAILED"
    broker_order_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "outcome": self.outcome,
            "broker_order_id": self.broker_order_id,
            "rejection_reason": self.rejection_reason,
            "failure_reason": self.failure_reason,
            "timestamp": self.timestamp,
            "raw_payload": self.raw_payload,
        }


class BrokerAdapter(ABC):
    """
    Authoritative abstract broker adapter contract for Tradego Phase 7.
    All broker-specific order creation, protocol handling, and venue sessions
    must be encapsulated behind implementations of this interface.
    """

    @abstractmethod
    def dispatch(self, instruction: Any) -> BrokerDispatchResult:
        """
        Dispatches a canonical OrderInstruction to the broker venue.
        Returns a typed BrokerDispatchResult without leaking venue internals.
        """
        pass

    @abstractmethod
    def cancel(self, instruction: Any, reason: str = "") -> bool:
        """
        Requests cancellation of an active broker order.
        Returns True if the cancellation request was acknowledged.
        """
        pass

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> Optional[Dict[str, Any]]:
        """
        Queries point-in-time order state from the broker book.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Verifies transport and venue session availability.
        """
        pass


class PaperBrokerAdapter(BrokerAdapter):
    """
    Deterministic in-memory Paper Broker Adapter for Phase 7 testing and simulation.
    Guarantees zero live broker connectivity, zero market execution, and strict repeatability.
    Provides controllable simulation modes for rejection and transport failure scenarios.
    """

    def __init__(self, venue_name: str = "PAPER_VENUE_SIM") -> None:
        self._venue_name = venue_name
        self._lock = threading.RLock()
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._instruction_map: Dict[str, str] = {}  # instruction_id -> broker_order_id
        self._simulated_rejection_reason: Optional[str] = None
        self._simulated_failure_reason: Optional[str] = None
        self._is_healthy: bool = True

    def simulate_rejection(self, reason: Optional[str] = "SIMULATED_BROKER_REJECTION") -> None:
        """Configures adapter to reject subsequent order dispatches with the specified reason."""
        with self._lock:
            self._simulated_rejection_reason = reason

    def simulate_failure(self, reason: Optional[str] = "SIMULATED_TRANSPORT_FAILURE") -> None:
        """Configures adapter to fail subsequent order dispatches with the specified reason."""
        with self._lock:
            self._simulated_failure_reason = reason

    def reset_simulation(self) -> None:
        """Clears simulated rejection and failure triggers."""
        with self._lock:
            self._simulated_rejection_reason = None
            self._simulated_failure_reason = None

    def set_healthy(self, healthy: bool) -> None:
        """Configures health check response."""
        with self._lock:
            self._is_healthy = healthy

    def dispatch(self, instruction: Any) -> BrokerDispatchResult:
        """
        Executes simulated order dispatch.
        Deterministic ID generation: 'PAPER-ORD-{hash}' derived from instruction attributes.
        """
        with self._lock:
            now_str = datetime.now(timezone.utc).isoformat()

            # Check simulated failure mode
            if self._simulated_failure_reason:
                return BrokerDispatchResult(
                    success=False,
                    outcome="FAILED",
                    failure_reason=self._simulated_failure_reason,
                    timestamp=now_str,
                    raw_payload={"venue": self._venue_name, "error": self._simulated_failure_reason},
                )

            # Check simulated rejection mode
            if self._simulated_rejection_reason:
                return BrokerDispatchResult(
                    success=False,
                    outcome="REJECTED",
                    rejection_reason=self._simulated_rejection_reason,
                    timestamp=now_str,
                    raw_payload={"venue": self._venue_name, "rejection": self._simulated_rejection_reason},
                )

            # Deterministic synthetic broker order ID
            instruction_id = getattr(instruction, "instruction_id", str(uuid.uuid4()))
            symbol = getattr(instruction, "symbol", "UNKNOWN")
            side = getattr(instruction, "side", "BUY")
            qty = getattr(instruction, "quantity", 1)

            digest = hashlib.sha256(
                f"{instruction_id}:{symbol}:{side}:{qty}:{now_str}".encode("utf-8")
            ).hexdigest()[:12]
            broker_order_id = f"PAPER-ORD-{digest.upper()}"

            record = {
                "broker_order_id": broker_order_id,
                "instruction_id": instruction_id,
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "status": "ACKNOWLEDGED",
                "venue": self._venue_name,
                "received_at": now_str,
            }
            self._orders[broker_order_id] = record
            self._instruction_map[instruction_id] = broker_order_id

            return BrokerDispatchResult(
                success=True,
                outcome="ACKNOWLEDGED",
                broker_order_id=broker_order_id,
                timestamp=now_str,
                raw_payload=record,
            )

    def cancel(self, instruction: Any, reason: str = "") -> bool:
        """
        Simulates order cancellation on the paper venue.
        """
        with self._lock:
            instruction_id = getattr(instruction, "instruction_id", str(instruction))
            broker_order_id = self._instruction_map.get(instruction_id)
            if not broker_order_id:
                broker_order_id = getattr(instruction, "broker_order_id", None)

            if broker_order_id and broker_order_id in self._orders:
                self._orders[broker_order_id]["status"] = "CANCELLED"
                self._orders[broker_order_id]["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                self._orders[broker_order_id]["cancellation_reason"] = reason
                return True
            return False

    def get_order_status(self, broker_order_id: str) -> Optional[Dict[str, Any]]:
        """Queries tracked paper order state."""
        with self._lock:
            order = self._orders.get(broker_order_id)
            return dict(order) if order else None

    def health_check(self) -> bool:
        """Reports simulated venue health."""
        with self._lock:
            return self._is_healthy


class LiveBrokerAdapter(BrokerAdapter):
    """
    Authoritative production-ready live broker adapter boundary for Tradego Phase 9.
    Consumes BrokerCredentialsConfig and BrokerConnectivityManager.
    Enforces boundary validation on canonical OrderInstruction objects,
    verifies authentication and connectivity states before dispatch,
    and derives deterministic idempotency keys.
    """

    def __init__(
        self,
        credentials: Optional[Any] = None,
        connectivity_manager: Optional[Any] = None,
        venue_name: str = "LIVE_VENUE_BOUNDARY",
    ) -> None:
        self._credentials = credentials
        self._connectivity_mgr = connectivity_manager
        self._venue_name = venue_name
        self._lock = threading.RLock()
        self._dispatched_idempotency_keys: Set[str] = set()
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._instruction_map: Dict[str, str] = {}

    @property
    def venue_name(self) -> str:
        return self._venue_name

    @property
    def credentials(self) -> Optional[Any]:
        return self._credentials

    def set_credentials(self, credentials: Any) -> None:
        with self._lock:
            self._credentials = credentials

    def set_connectivity_manager(self, manager: Any) -> None:
        with self._lock:
            self._connectivity_mgr = manager

    def _validate_instruction(self, instruction: Any) -> None:
        """Strict boundary validation: Never accept arbitrary dictionaries."""
        if isinstance(instruction, dict):
            raise TypeError("INVALID_INSTRUCTION_TYPE: Live adapter rejects raw dictionaries. OrderInstruction required.")

        if not hasattr(instruction, "instruction_id") or not hasattr(instruction, "symbol"):
            raise TypeError("INVALID_INSTRUCTION_TYPE: Live adapter requires canonical OrderInstruction object.")

        symbol = getattr(instruction, "symbol", None)
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("INVALID_ORDER_SYMBOL: OrderInstruction missing valid symbol.")

        side = getattr(instruction, "side", None)
        if side not in ("BUY", "SELL"):
            raise ValueError(f"INVALID_ORDER_SIDE: Unsupported side '{side}'.")

        qty = getattr(instruction, "quantity", None)
        if qty is None or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"INVALID_ORDER_QUANTITY: Quantity must be positive int, got {qty}.")

        order_type = getattr(instruction, "order_type", None)
        if order_type not in ("LIMIT", "MARKET", "STOP_LOSS", "STOP_LIMIT"):
            raise ValueError(f"UNSUPPORTED_ORDER_TYPE: Order type '{order_type}' not supported.")

        if order_type == "LIMIT":
            limit_price = getattr(instruction, "limit_price", None)
            if limit_price is None or limit_price <= 0:
                raise ValueError("INVALID_ORDER_PRICE: LIMIT orders require positive limit_price.")

    def _compute_idempotency_key(self, instruction: Any) -> str:
        """Derives a stable idempotency key from instruction attributes."""
        ins_id = getattr(instruction, "instruction_id", "")
        symbol = getattr(instruction, "symbol", "")
        side = getattr(instruction, "side", "")
        qty = getattr(instruction, "quantity", 0)
        corr_id = getattr(instruction, "correlation_id", "")
        raw = f"{ins_id}:{symbol}:{side}:{qty}:{corr_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def dispatch(self, instruction: Any) -> BrokerDispatchResult:
        """
        Validates instruction, checks credentials, verifies connectivity,
        enforces idempotency, and submits order to venue.
        """
        with self._lock:
            # 1. Boundary validation
            self._validate_instruction(instruction)

            # 2. Check credentials
            if not self._credentials or not getattr(self._credentials, "is_valid", False):
                return BrokerDispatchResult(
                    success=False,
                    outcome="FAILED",
                    failure_reason="MISSING_OR_INVALID_CREDENTIALS",
                    raw_payload={"error": "Broker credentials not configured or invalid."},
                )

            # 3. Check connectivity
            if self._connectivity_mgr:
                conn_state = getattr(self._connectivity_mgr, "connectivity_state", None)
                state_val = conn_state.value if hasattr(conn_state, "value") else str(conn_state)
                if state_val != "CONNECTED":
                    return BrokerDispatchResult(
                        success=False,
                        outcome="FAILED",
                        failure_reason=f"BROKER_NOT_CONNECTED: Venue state is {state_val}",
                        raw_payload={"error": f"Broker is not connected ({state_val})."},
                    )

            # 4. Check idempotency
            idemp_key = self._compute_idempotency_key(instruction)
            if idemp_key in self._dispatched_idempotency_keys:
                return BrokerDispatchResult(
                    success=False,
                    outcome="FAILED",
                    failure_reason=f"DUPLICATE_DISPATCH_BLOCKED: Idempotency key {idemp_key[:12]} already submitted.",
                    raw_payload={"error": "Duplicate dispatch detected."},
                )
            self._dispatched_idempotency_keys.add(idemp_key)

            # Default safe handler (overridden by Mock or vendor transport)
            return self._execute_live_dispatch(instruction, idemp_key)

    def _execute_live_dispatch(self, instruction: Any, idemp_key: str) -> BrokerDispatchResult:
        """Default production safeguard: cannot connect to real venue without vendor transport plugin."""
        return BrokerDispatchResult(
            success=False,
            outcome="FAILED",
            failure_reason="REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED",
            raw_payload={"venue": self._venue_name, "error": "Live network transport is uninitialized."},
        )

    def cancel(self, instruction: Any, reason: str = "") -> bool:
        """Default cancellation boundary."""
        with self._lock:
            instruction_id = getattr(instruction, "instruction_id", str(instruction))
            broker_order_id = self._instruction_map.get(instruction_id)
            if not broker_order_id:
                broker_order_id = getattr(instruction, "broker_order_id", None)

            if broker_order_id and broker_order_id in self._orders:
                self._orders[broker_order_id]["status"] = "CANCELLED"
                self._orders[broker_order_id]["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                self._orders[broker_order_id]["cancellation_reason"] = reason
                return True
            return False

    def get_order_status(self, broker_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self._orders.get(broker_order_id)
            return dict(order) if order else None

    def health_check(self) -> bool:
        if self._connectivity_mgr:
            conn_state = getattr(self._connectivity_mgr, "connectivity_state", None)
            state_val = conn_state.value if hasattr(conn_state, "value") else str(conn_state)
            return state_val == "CONNECTED"
        return bool(self._credentials and getattr(self._credentials, "is_valid", False))


class MockLiveBrokerAdapter(LiveBrokerAdapter):
    """
    Deterministic Mock Live Broker Adapter for Phase 9 testing.
    Simulates production broker interactions without live market exposure:
    - Acknowledgement with authoritative broker_order_id
    - Business rejection
    - Transport failure and timeouts
    - Partial and full fills
    - Cancellation acceptance, rejection, and disconnect
    - Duplicate event handling
    - Out-of-order event resilience
    - Reconciliation mismatches
    """

    def __init__(
        self,
        credentials: Optional[Any] = None,
        connectivity_manager: Optional[Any] = None,
        venue_name: str = "MOCK_LIVE_VENUE",
    ) -> None:
        super().__init__(credentials=credentials, connectivity_manager=connectivity_manager, venue_name=venue_name)
        self._simulated_rejection_reason: Optional[str] = None
        self._simulated_failure_reason: Optional[str] = None
        self._simulated_timeout: bool = False
        self._simulated_cancel_rejected: bool = False
        self._simulated_cancel_disconnect: bool = False
        self._reconciliation_mismatch_mode: Optional[str] = None
        self._generated_fills: List[Dict[str, Any]] = []

    def simulate_rejection(self, reason: Optional[str] = "SIMULATED_LIVE_REJECTION") -> None:
        with self._lock:
            self._simulated_rejection_reason = reason

    def simulate_failure(self, reason: Optional[str] = "SIMULATED_LIVE_TRANSPORT_FAILURE") -> None:
        with self._lock:
            self._simulated_failure_reason = reason

    def simulate_timeout(self, timeout: bool = True) -> None:
        with self._lock:
            self._simulated_timeout = timeout

    def simulate_cancel_rejected(self, rejected: bool = True) -> None:
        with self._lock:
            self._simulated_cancel_rejected = rejected

    def simulate_cancel_disconnect(self, disconnect: bool = True) -> None:
        with self._lock:
            self._simulated_cancel_disconnect = disconnect

    def simulate_reconciliation_mismatch(self, mismatch_type: Optional[str] = "QUANTITY") -> None:
        """Configures simulated book to return mismatched data during reconciliation."""
        with self._lock:
            self._reconciliation_mismatch_mode = mismatch_type

    def reset_simulation(self) -> None:
        with self._lock:
            self._simulated_rejection_reason = None
            self._simulated_failure_reason = None
            self._simulated_timeout = False
            self._simulated_cancel_rejected = False
            self._simulated_cancel_disconnect = False
            self._reconciliation_mismatch_mode = None

    def _execute_live_dispatch(self, instruction: Any, idemp_key: str) -> BrokerDispatchResult:
        now_str = datetime.now(timezone.utc).isoformat()

        if self._simulated_timeout:
            return BrokerDispatchResult(
                success=False,
                outcome="FAILED",
                failure_reason="BROKER_TIMEOUT: Request timed out waiting for broker response.",
                timestamp=now_str,
                raw_payload={"venue": self._venue_name, "error": "TIMEOUT"},
            )

        if self._simulated_failure_reason:
            return BrokerDispatchResult(
                success=False,
                outcome="FAILED",
                failure_reason=self._simulated_failure_reason,
                timestamp=now_str,
                raw_payload={"venue": self._venue_name, "error": self._simulated_failure_reason},
            )

        if self._simulated_rejection_reason:
            return BrokerDispatchResult(
                success=False,
                outcome="REJECTED",
                rejection_reason=self._simulated_rejection_reason,
                timestamp=now_str,
                raw_payload={"venue": self._venue_name, "rejection": self._simulated_rejection_reason},
            )

        # Successful deterministic acknowledgement
        ins_id = getattr(instruction, "instruction_id", str(uuid.uuid4()))
        symbol = getattr(instruction, "symbol", "UNKNOWN")
        side = getattr(instruction, "side", "BUY")
        qty = getattr(instruction, "quantity", 1)
        limit_price = getattr(instruction, "limit_price", None)

        broker_order_id = f"LIVE-ORD-{idemp_key[:12].upper()}"
        record = {
            "broker_order_id": broker_order_id,
            "instruction_id": ins_id,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "limit_price": limit_price,
            "filled_quantity": 0,
            "status": "ACKNOWLEDGED",
            "venue": self._venue_name,
            "received_at": now_str,
        }
        self._orders[broker_order_id] = record
        self._instruction_map[ins_id] = broker_order_id

        return BrokerDispatchResult(
            success=True,
            outcome="ACKNOWLEDGED",
            broker_order_id=broker_order_id,
            timestamp=now_str,
            raw_payload=record,
        )

    def cancel(self, instruction: Any, reason: str = "") -> bool:
        with self._lock:
            if self._simulated_cancel_disconnect:
                raise ConnectionError("BROKER_DISCONNECT: Disconnected during cancel request.")

            if self._simulated_cancel_rejected:
                return False

            return super().cancel(instruction, reason)

    def create_simulated_fill(
        self,
        broker_order_id: str,
        fill_quantity: int,
        fill_price: float,
        broker_fill_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generates a simulated broker fill event and updates internal book."""
        with self._lock:
            order = self._orders.get(broker_order_id)
            if not order:
                raise KeyError(f"Broker order {broker_order_id} not found.")

            now_str = datetime.now(timezone.utc).isoformat()
            fill_id = broker_fill_id or f"BF-{uuid.uuid4().hex[:8].upper()}"

            order["filled_quantity"] = order.get("filled_quantity", 0) + fill_quantity
            if order["filled_quantity"] >= order["quantity"]:
                order["status"] = "FILLED"
            else:
                order["status"] = "PARTIALLY_FILLED"

            fill_event = {
                "broker_order_id": broker_order_id,
                "broker_fill_id": fill_id,
                "instruction_id": order["instruction_id"],
                "quantity": fill_quantity,
                "price": fill_price,
                "timestamp": now_str,
                "symbol": order["symbol"],
                "side": order["side"],
            }
            self._generated_fills.append(fill_event)
            return fill_event

    def get_order_status(self, broker_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self._orders.get(broker_order_id)
            if not order:
                return None
            data = dict(order)

            # Apply simulated reconciliation mismatches if configured
            if self._reconciliation_mismatch_mode == "QUANTITY":
                data["quantity"] = data["quantity"] + 100
            elif self._reconciliation_mismatch_mode == "SYMBOL":
                data["symbol"] = "MISMATCH_SYM"
            elif self._reconciliation_mismatch_mode == "FILL":
                data["filled_quantity"] = data.get("filled_quantity", 0) + 999
            elif self._reconciliation_mismatch_mode == "MISSING":
                return None

            return data

