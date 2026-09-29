"""
Tradego Phase 7 Execution Layer — Domain Models and Contracts.

Defines all canonical enums, slotted frozen dataclasses, order requests,
acknowledgements, typed submission outcomes, execution fills, updates,
and point-in-time position reconciliation adjustments.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from services.market_state.instrument import InstrumentId


class OrderSide(str, Enum):
    """Authoritative trading order side."""
    BUY = "BUY"
    SELL = "SELL"


class PositionEffect(str, Enum):
    """Explicit effect of an order on the underlying position."""
    OPEN = "OPEN"          # Initiating a new position from flat
    CLOSE = "CLOSE"        # Completely closing an open position to flat
    INCREASE = "INCREASE"  # Adding to an existing position (SCALE_IN)
    REDUCE = "REDUCE"      # Partially trimming an existing position (SCALE_OUT)


class OrderType(str, Enum):
    """Supported execution order types for Phase 7 v1."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    """Order validity and lifetime constraints."""
    DAY = "DAY"            # Valid until market close
    IOC = "IOC"            # Immediate or Cancel


class OrderPurpose(str, Enum):
    """Categorization of execution intent for risk and reporting."""
    ENTRY = "ENTRY"        # Position-increasing
    EXIT = "EXIT"          # Position-reducing / de-risking
    SCALE = "SCALE"        # Partial scale in or scale out


class CanonicalOrderStatus(str, Enum):
    """Exhaustive lifecycle status of an order within Tradego."""
    CREATED = "CREATED"                    # Planned locally, not yet validated
    VALIDATED = "VALIDATED"                # Passed pre-submission safety checks
    SUBMITTING = "SUBMITTING"              # In-flight to broker adapter
    ACKNOWLEDGED = "ACKNOWLEDGED"          # Accepted by broker, order ID assigned
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # Partial execution confirmed; remainder open
    FILLED = "FILLED"                      # 100% executed; terminal state
    CANCEL_PENDING = "CANCEL_PENDING"      # Cancellation requested; awaiting broker ack
    CANCELLED = "CANCELLED"                # Unfilled remainder confirmed cancelled; terminal
    REPLACE_PENDING = "REPLACE_PENDING"    # Price/size modification in-flight
    REPLACED = "REPLACED"                  # Modification confirmed by broker
    REJECTED = "REJECTED"                  # Rejected by pre-trade validation or broker; terminal
    EXPIRED = "EXPIRED"                    # Unfilled order expired by exchange/TIF; terminal
    FAILED = "FAILED"                      # Fatal transport or adapter failure; terminal
    UNKNOWN = "UNKNOWN"                    # Ambiguous submission status; requires reconciliation


class ExecutionFailureReason(str, Enum):
    """Exhaustive taxonomy of execution-level failure reasons."""
    INTENT_EXPIRED = "INTENT_EXPIRED"
    INTENT_ALREADY_EXECUTED = "INTENT_ALREADY_EXECUTED"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    INVALID_ORDER_QUANTITY = "INVALID_ORDER_QUANTITY"
    INVALID_ORDER_PRICE = "INVALID_ORDER_PRICE"
    INVALID_ORDER_SIDE = "INVALID_ORDER_SIDE"
    EXCEEDS_APPROVED_QUANTITY = "EXCEEDS_APPROVED_QUANTITY"
    EXCHANGE_CLOSED = "EXCHANGE_CLOSED"
    EXCHANGE_HALTED = "EXCHANGE_HALTED"
    MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    CONNECTION_LOST = "CONNECTION_LOST"
    BROKER_REJECTED = "BROKER_REJECTED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    PRICE_OUT_OF_BOUNDS = "PRICE_OUT_OF_BOUNDS"
    RECONCILIATION_DESYNC = "RECONCILIATION_DESYNC"


class SubmissionOutcomeType(str, Enum):
    """
    Typed categorization of broker adapter order submission outcomes.
    Prevents collapsing distinct failure modes into ambiguous states.
    """
    ACKNOWLEDGED = "ACKNOWLEDGED"            # Broker/exchange confirmed receipt; order active
    REJECTED = "REJECTED"                    # Pre-trade or broker business rejection; fatal/terminal
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"  # Transport drop BEFORE wire dispatch; retryable
    AMBIGUOUS_UNKNOWN = "AMBIGUOUS_UNKNOWN"  # Timeout/drop AFTER wire dispatch; requires reconciliation


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """
    Canonical, broker-independent order instruction synthesized by ExecutionPlanner.
    Contains stable identity, exact trading parameters, reference price, and temporal horizons.
    """
    # 1. Identity & Provenance (Stable across retries)
    client_order_id: str                 # Stable, unique Tradego client order identifier
    intent_id: str                       # Upstream ApprovedTradeIntent.intent_id
    signal_id: str                       # Upstream SignalCandidate.signal_id
    strategy_id: str
    instrument_id: InstrumentId

    # 2. Trading Parameters & Geometry
    side: OrderSide                      # BUY or SELL
    position_effect: PositionEffect      # OPEN, CLOSE, INCREASE, REDUCE
    order_type: OrderType                # MARKET or LIMIT
    quantity: int                        # Discrete share count (strictly > 0)
    price: Optional[float]               # Required for LIMIT; None for MARKET
    reference_price: float               # Authoritative entry/reference price from ApprovedTradeIntent (strictly > 0.0)
    time_in_force: TimeInForce           # DAY or IOC
    order_purpose: OrderPurpose          # ENTRY, EXIT, SCALE
    # 3. Temporal Horizons & Idempotency
    creation_timestamp: datetime         # Time order was synthesized locally
    expiry_timestamp: Optional[datetime] # Order validity execution deadline
    idempotency_key: str                 # Deterministic SHA-256 deduplication key
    order_version: int = 1               # Monotonic order version (increments on REPLACED)
    metadata: Optional[Tuple[Tuple[str, str], ...]] = None # Deeply immutable key-value pairs

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"OrderRequest quantity must be positive, got {self.quantity}")
        if self.reference_price <= 0.0:
            raise ValueError(f"OrderRequest reference_price must be positive, got {self.reference_price}")
        if self.order_type == OrderType.LIMIT:
            if self.price is None or self.price <= 0.0:
                raise ValueError(f"OrderType.LIMIT requires a positive price, got {self.price}")
        elif self.order_type == OrderType.MARKET:
            if self.price is not None:
                raise ValueError(f"OrderType.MARKET price must be None, got {self.price}")
        if self.order_version <= 0:
            raise ValueError(f"OrderRequest order_version must be positive, got {self.order_version}")


@dataclass(frozen=True, slots=True)
class OrderAcknowledgement:
    """Authoritative acknowledgement emitted by broker or matching engine upon order acceptance."""
    client_order_id: str
    broker_order_id: str
    order_version: int
    exchange_timestamp: Optional[datetime]   # Venue wall-clock timestamp (chronology/audit)
    local_received_timestamp: datetime       # Host wall-clock timestamp (UTC)
    local_receive_monotonic_ns: int          # High-resolution integer from time.perf_counter_ns()


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Typed result of an order submission attempt returned by BrokerExecutionAdapter."""
    outcome: SubmissionOutcomeType
    acknowledgement: Optional[OrderAcknowledgement] = None
    rejection_reason: Optional[ExecutionFailureReason] = None
    error_message: Optional[str] = None
    retry_delay_ms: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Authoritative execution event emitted by exchange matching or paper engine."""
    fill_id: str                         # Canonical Tradego fill identity (deduplication key)
    client_order_id: str                 # Correlated Tradego OrderRequest
    broker_order_id: str                 # Correlated Broker reference
    instrument_id: InstrumentId
    side: OrderSide
    fill_quantity: int                   # Executed share count (strictly > 0)
    fill_price: float                    # Execution price per share (strictly > 0.0)
    exchange_timestamp: datetime         # Venue wall-clock execution timestamp (chronology/audit)
    local_received_timestamp: datetime   # Local host wall-clock timestamp (UTC audit/logging)
    local_receive_monotonic_ns: int      # High-resolution integer from time.perf_counter_ns()
    exchange_trade_id: Optional[str] = None # Authoritative venue trade token (Tier 1)
    broker_exec_id: Optional[str] = None    # Authoritative broker execution token (Tier 2)
    fee_estimate: float = 0.0            # Estimated brokerage and exchange turnover fees

    def __post_init__(self) -> None:
        if self.fill_quantity <= 0:
            raise ValueError(f"Fill quantity must be positive, got {self.fill_quantity}")
        if self.fill_price <= 0.0:
            raise ValueError(f"Fill price must be positive, got {self.fill_price}")
        if self.local_receive_monotonic_ns <= 0:
            raise ValueError(f"local_receive_monotonic_ns must be a positive integer, got {self.local_receive_monotonic_ns}")


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """Point-in-time status update emitted by broker adapter during order polling or websocket streaming."""
    client_order_id: str
    broker_order_id: str
    status: CanonicalOrderStatus
    cumulative_filled_quantity: int
    remaining_quantity: int
    average_price: float
    timestamp: datetime
    monotonic_ns: int
    error_message: Optional[str] = None


@dataclass(frozen=True, slots=True)
class PositionReconciliationAdjustment:
    """
    Authoritative point-in-time position adjustment emitted during reconciliation.
    Distinct from continuous fill events; adjusts ledger without fabricating trades.
    """
    instrument_id: InstrumentId
    broker_quantity: int
    local_quantity_before: int
    quantity_delta: int
    reconciliation_reason: str          # e.g. "COLD_START_SYNC", "RECOVERY_OVERRIDE"
    timestamp: datetime                 # Wall-clock timestamp of adjustment
    audit_metadata: Optional[Tuple[Tuple[str, str], ...]] = None
