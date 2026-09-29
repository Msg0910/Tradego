# TRADEGO PHASE 7 — TECHNICAL ARCHITECTURE & DESIGN SPECIFICATION
## EXECUTION LAYER & ORDER LIFECYCLE MANAGEMENT

```
===============================================================================
STATUS: DESIGN FINAL — READY FOR ARCHITECTURE FREEZE
VERSION: 1.2.0
DATE: 2026-09-14
UPSTREAM CONTRACT: Phase 6 Risk Management & Portfolio Control (ApprovedTradeIntent) [FROZEN]
DOWNSTREAM CONTRACT: Broker Adapters / Execution Providers & Portfolio Fill Boundary
LOCATION: docs/phase_7_execution_design.md
===============================================================================
```

---

## 1. EXECUTIVE SUMMARY

Phase 7 defines the **Execution Layer** of the Tradego algorithmic trading system. It operates downstream of the frozen Phase 6 Risk Management & Portfolio Control Layer. Its primary role is to translate risk-approved trade authorizations ([ApprovedTradeIntent](file:///d:/msg/Devang/Tradego/services/risk/models.py#L159-L234)) into broker-agnostic order requests, manage the complete asynchronous order lifecycle, interface with broker execution adapters through a unified interface, process partial and full execution fills, enforce idempotency against race conditions and connection failures, reconcile local execution state with exchange/broker reality, and emit authoritative fill events to the portfolio accounting boundary.

Phase 7 maintains absolute separation between **trading intent**, **risk authorization**, **canonical order instructions**, **broker-specific orders**, **execution fills**, and **portfolio positions**. It introduces zero dependencies, preserves the frozen Phase 1–6 baselines, and is architected to guarantee identical order lifecycle semantics across both simulated paper execution and future live broker integration (such as DhanHQ, Zerodha, or other institutional brokers).

---

## 2. PHASE 7 OBJECTIVE

The Phase 7 Execution Layer achieves the following primary architectural objectives:
1. **Transform Risk Approvals into Canonical Orders:** Ingest `ApprovedTradeIntent` and produce a minimal, canonical, broker-independent `OrderRequest`.
2. **Broker-Agnostic Execution Abstraction:** Decouple Tradego core execution logic from external broker APIs through an abstract `BrokerExecutionAdapter` interface.
3. **Rigorous Order Lifecycle State Machine:** Maintain a deterministic, single-direction lifecycle state machine handling transitions from creation, validation, submission, and acknowledgement through partial fills, complete fills, cancellations, replacements, and terminal rejections.
4. **Stable-Identity Idempotency & Duplicate Prevention:** Guarantee that network timeouts, process restarts, duplicate broker callbacks, and transport retries never duplicate orders or double-count fills by maintaining globally stable `client_order_id` values across retries.
5. **Robust Partial Fill & Remainder Accounting:** Maintain exact integer quantity conservation across multiple child fills and remainder cancellations without drifting from authorized risk limits.
6. **Failure Recovery & State Reconciliation:** Provide deterministic reconciliation mechanisms to resolve state discrepancies between local execution memory and broker/exchange order books following reconnects or system restarts.
7. **Paper / Live Execution Parity:** Execute paper trading and live trading through the exact same state machine, routing abstractions, idempotency registry, and fill accounting—varying only the concrete terminal adapter while acknowledging real-world execution divergences.
8. **Auditable Latency & Timestamp Tracking:** Measure sub-millisecond execution stages across monotonic clocks and record granular exchange, broker, and local processing timestamps.

---

## 3. FROZEN ARCHITECTURE DEPENDENCIES

Phase 7 depends upon and consumes frozen upstream contracts from Phases 1 through 6 without modifying any existing interface:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 TRADEGO PIPELINE                                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 1: Market Data Gateway         │
                       │  - MarketEvent (price, depth, ticks)  │ [FROZEN]
                       └───────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 2: Real-Time Market State      │
                       │  - InstrumentId, InstrumentMetadata   │ [FROZEN]
                       │  - ExchangeCalendar, TradingSession   │
                       └───────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 3: Candle Engine               │
                       │  - Candle (1m, 5m bar timestamps)     │ [FROZEN]
                       └───────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 4: Quantitative Analytics      │
                       │  - FeatureSnapshot, FeatureQuality    │ [FROZEN]
                       └───────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 5: Strategy & Signal Intel     │
                       │  - SignalCandidate, SignalType        │ [FROZEN]
                       │  - PositionView                       │
                       └───────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 6: Risk & Portfolio Control    │
                       │  - ApprovedTradeIntent (AUTHORIZATION)│ [FROZEN]
                       │  - PositionSnapshot, PortfolioSnapshot│
                       └───────────────────────────────────────┘
                                           │
 ══════════════════════════════════════════╪══════════════════════════════════════════════
                                           │ ApprovedTradeIntent
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  PHASE 7: EXECUTION LAYER (THIS SPEC) │
                       │  - ExecutionPlanner                   │
                       │  - Canonical OrderRequest             │
                       │  - Order Lifecycle State Machine      │
                       │  - ExecutionRouter                    │
                       │  - BrokerExecutionAdapter (Paper/Live)│
                       │  - Authoritative Fill Accounting      │
                       └───────────────────────────────────────┘
                                           │
                                           ▼ Authoritative Fills
                       ┌───────────────────────────────────────┐
                       │  Position & Portfolio Accounting      │
                       │  (Ledger, Cash, Holdings Update)      │
                       └───────────────────────────────────────┘
```

### Key Frozen Contracts Consumed by Phase 7:
- **`InstrumentId`** ([services/market_state/instrument.py:L42-L72](file:///d:/msg/Devang/Tradego/services/market_state/instrument.py#L42-L72)): Canonical identifier containing `symbol`, `exchange`, `instrument_type`, `expiry`, `strike`, `option_type`.
- **`InstrumentMetadata`** ([services/market_state/instrument.py:L147-L157](file:///d:/msg/Devang/Tradego/services/market_state/instrument.py#L147-L157)): Static trading parameters: `lot_size`, `tick_size`, `price_precision`, `freeze_quantity`.
- **`ExchangeCalendar`** ([services/market_state/calendar.py](file:///d:/msg/Devang/Tradego/services/market_state/calendar.py)): Trading session state machine verifying active exchange trading sessions (`REGULAR_TRADING`) vs halts or post-market sessions.
- **`SignalType`** ([services/signals/models.py:L23-L31](file:///d:/msg/Devang/Tradego/services/signals/models.py#L23-L31)): Semantic action: `ENTRY_LONG`, `ENTRY_SHORT`, `EXIT_LONG`, `EXIT_SHORT`, `SCALE_IN`, `SCALE_OUT`.
- **`PositionView`** ([services/signals/models.py:L45-L71](file:///d:/msg/Devang/Tradego/services/signals/models.py#L45-L71)): Read-only snapshot of current position metrics provided to strategy intelligence.
- **`ApprovedTradeIntent`** ([services/risk/models.py:L159-L234](file:///d:/msg/Devang/Tradego/services/risk/models.py#L159-L234)): The authoritative, immutable execution mandate emitted by Phase 6:
  - Provenance: `intent_id`, `signal_id`, `fingerprint`, `reaffirmation_key`, `strategy_id`, `strategy_version`, `risk_config_version`, `risk_config_hash`.
  - Intent geometry: `instrument_id`, `signal_type`, `direction` (+1/-1), `permitted_quantity` (> 0), `approved_entry_price`, `approved_stop_loss`, `approved_take_profit`, `binding_constraint`.
  - Temporal: `market_timestamp`, `signal_generated_timestamp`, `expiry_timestamp`.

---

## 4. PHASE 7 BOUNDARY

### 4.1 In Scope
1. **Intent Ingestion & Validation:** Ingesting `ApprovedTradeIntent`, validating temporal expiry, checking active execution status, and ensuring zero tampering with authorized quantities.
2. **Session & Calendar Guard:** Verifying that the target exchange is currently open for regular trading before placing orders.
3. **Execution Planning:** Translating `ApprovedTradeIntent` into canonical `OrderRequest` specifications (deriving canonical `OrderSide`, `PositionEffect`, order type, price, time-in-force, and stable `client_order_id`).
4. **Canonical Order Lifecycle Management:** Tracking individual order transitions from initial creation to final terminal states (`FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `FAILED`).
5. **Execution Routing:** Routing canonical requests to the active `BrokerExecutionAdapter` based on configuration mode (`PAPER` vs `LIVE`).
6. **Broker Adapter Abstraction:** Defining the uniform contract for order submission, cancellation, replacement, status polling, and reconciliation.
7. **Partial Fill Accounting:** Maintaining exact fill aggregation, cumulative fill tracking, remaining quantity calculation, and weighted average execution price.
8. **Idempotency & Deduplication:** Enforcing two-tier idempotency (Intent-to-Order and Order-to-Broker) with stable client order IDs to prevent accidental double-execution under timeouts, reconnects, or retries.
9. **Failure Classification & Bounded Retries:** Distinguishing fatal rejections from transient connection drops, enforcing strict non-infinite retry budgets, and entering safe `UNKNOWN` states when submission status is indeterminate.
10. **Cancellation & Replacement Handling:** Managing asynchronous cancellation requests, handling fill-vs-cancel race conditions, and maintaining monotonic order versions upon replacement.
11. **State Reconciliation & Recovery:** Querying broker order books on restart or reconnect, identifying orphan/unacknowledged orders, and syncing execution states without inferring fills.
12. **Authoritative Fill Emission:** Emitting immutable `Fill` events to update downstream portfolio position state.
13. **Paper Execution Engine:** Providing a deterministic simulated matching adapter evaluating against real `MarketEvent` ticks and order book depth.
14. **Sub-Millisecond Telemetry & Timestamps:** Tracking timestamps from market tick through order submission, broker roundtrip, and final execution fill.

### 4.2 Out of Scope
1. **Trading Strategy & Signal Generation:** Deciding *when* or *why* to trade belongs strictly to Phase 5.
2. **Portfolio Risk & Position Sizing:** Deciding *if* a trade is permitted and *how much* capital or share quantity is allocated belongs strictly to Phase 6. Phase 7 CANNOT increase quantity beyond `permitted_quantity`.
3. **Market Data Normalization & Ingestion:** Ingesting raw market feeds belongs to Phase 1.
4. **Time-Series Aggregation:** Candle construction belongs to Phase 3.
5. **Quantitative Feature Calculations:** Indicator mathematics belongs to Phase 4.
6. **Direct Broker Authentication / SDKs in Core:** Broker-specific session tokens, HTTP requests, and protobuf/JSON payload schemas belong strictly inside isolated `BrokerExecutionAdapter` implementations, not Tradego Core.
7. **Distributed Message Brokers:** Redis, Kafka, Redpanda, or RabbitMQ are explicitly prohibited from the execution core.
8. **AI / LLM / Machine Learning Inference:** AI models or MCP tools are strictly prohibited from the execution hot path.
9. **Live Broker Order Placement in Phase 7 v1:** Phase 7 v1 delivers the execution architecture and paper execution engine; live broker activation occurs only after explicit promotion.

---

## 5. CORE ARCHITECTURAL AXIOMS

Phase 7 establishes six fundamental, inviolable distinctions across the trading pipeline:

$$\mathbf{SignalCandidate} \ne \mathbf{ApprovedTradeIntent} \ne \mathbf{OrderRequest} \ne \mathbf{BrokerOrder} \ne \mathbf{Fill} \ne \mathbf{PositionState}$$

1. **A Signal is NOT an Order:** A `SignalCandidate` is an analytical hypothesis formulated by Phase 5. It carries zero authorization, zero lot sizing, and zero execution parameters.
2. **A Risk Approval is NOT an Order:** An `ApprovedTradeIntent` is a risk authorization boundary. It specifies the maximum permitted risk budget and ceiling quantity, but does not specify broker routing, client order IDs, time-in-force, or broker status.
3. **An Order Request is NOT a Broker Order:** An `OrderRequest` is Tradego's internal instruction. It may be rejected locally, queued, delayed by rate limits, or refused by the broker gateway before entering an exchange book.
4. **A Broker Order is NOT a Fill:** An acknowledged `BrokerOrder` merely confirms that an exchange or broker accepted an order into its matching engine. It provides zero guarantee of liquidity or execution.
5. **A Fill is the Sole Execution Authority:** An authoritative `Fill` is an immutable record of an executed trade emitted by an exchange matching engine. Fills cannot be assumed, predicted, or inferred from timeouts or HTTP 200 responses.
6. **A Submitted Order is NOT a Position:** Portfolio positions are updated exclusively in response to confirmed, authoritative `Fill` events or formal exchange reconciliation—never upon order submission or acknowledgement.

---

## 6. EXECUTION DATA FLOW

The end-to-end execution data flow from Phase 6 risk approval to portfolio fill emission is depicted below:

```
                            ┌────────────────────────┐
                            │  ApprovedTradeIntent   │ (From Phase 6 RiskEngine)
                            └───────────┬────────────┘
                                        │
                                        ▼
                            ┌────────────────────────┐
                            │    ExecutionPlanner    │
                            │  - Check Session/Cal   │
                            │  - Validate Expiry     │
                            │  - Derive OrderSide    │
                            │  - Derive PosEffect    │
                            │  - Stable ClientId     │
                            │  - Compute Idempotency │
                            └───────────┬────────────┘
                                        │
                                        ▼
                            ┌────────────────────────┐
                            │      OrderRequest      │ (Canonical & Broker-Agnostic)
                            └───────────┬────────────┘
                                        │
                                        ▼
                            ┌────────────────────────┐
                            │    ExecutionRouter     │
                            │  - Record in Registry  │
                            │  - Enforce Active Lock │
                            │  - Route to Adapter    │
                            └───────────┬────────────┘
                                        │
                   ┌────────────────────┴────────────────────┐
                   ▼                                         ▼
      ┌─────────────────────────┐               ┌─────────────────────────┐
      │  PaperExecutionAdapter  │               │   LiveBrokerAdapter     │
      │  (Simulated Fill Engine)│               │  (e.g. DhanHQ/Zerodha)  │
      │  - TokenBucket Limiter  │               │  - TokenBucket Limiter  │
      └────────────┬────────────┘               └────────────┬────────────┘
                   │                                         │
                   │ Simulated Matching                      │ Network / REST / WS
                   ▼                                         ▼
      ┌─────────────────────────┐               ┌─────────────────────────┐
      │ MarketEvent / Depth Qty │               │ Exchange Matching Engine│
      └────────────┬────────────┘               └────────────┬────────────┘
                   │                                         │
                   └────────────────────┬────────────────────┘
                                        │
                                        ▼
                            ┌────────────────────────┐
                            │     ExecutionEvent     │
                            │  - OrderAcknowledgement│
                            │  - OrderUpdate         │
                            │  - Authoritative Fill  │
                            └───────────┬────────────┘
                                        │
                                        ▼
                            ┌────────────────────────┐
                            │     ExecutionState     │
                            │  - Lifecycle Transition│
                            │  - Order Version       │
                            │  - Cumulative Fills    │
                            │  - Remaining Quantity  │
                            └───────────┬────────────┘
                                        │
                                        ▼ Authoritative Fill
                            ┌────────────────────────┐
                            │ Position & Portfolio   │
                            │  Accounting Authority  │
                            │  (PositionSnapshot)    │
                            └────────────────────────┘
```

---

## 7. DOMAIN OBJECT MODEL

All Phase 7 data contracts are defined as **frozen, slotted dataclasses** (`@dataclass(frozen=True, slots=True)`) to ensure strict immutability, thread safety, memory efficiency, and deterministic hashing. Mutable execution tracking state is encapsulated within dedicated, lock-protected classes.

### 7.1 Canonical Enums

```python
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
    DAY = "DAY"                  # Valid until market close
    IOC = "IOC"                  # Immediate or Cancel


class OrderPurpose(str, Enum):
    """Categorization of execution intent for risk and reporting."""
    ENTRY = "ENTRY"              # Position-increasing
    EXIT = "EXIT"                # Position-reducing / de-risking
    SCALE = "SCALE"              # Partial scale in or scale out


class CanonicalOrderStatus(str, Enum):
    """Exhaustive lifecycle status of an order within Tradego."""
    CREATED = "CREATED"                  # Planned locally, not yet validated
    VALIDATED = "VALIDATED"              # Passed pre-submission safety checks
    SUBMITTING = "SUBMITTING"            # In-flight to broker adapter
    ACKNOWLEDGED = "ACKNOWLEDGED"        # Accepted by broker, order ID assigned
    PARTIALLY_FILLED = "PARTIALLY_FILLED"# Partial execution confirmed; remainder open
    FILLED = "FILLED"                    # 100% executed; terminal state
    CANCEL_PENDING = "CANCEL_PENDING"    # Cancellation requested; awaiting broker ack
    CANCELLED = "CANCELLED"              # Unfilled remainder confirmed cancelled; terminal
    REPLACE_PENDING = "REPLACE_PENDING"  # Price/size modification in-flight
    REPLACED = "REPLACED"                # Modification confirmed by broker
    REJECTED = "REJECTED"                # Rejected by pre-trade validation or broker; terminal
    EXPIRED = "EXPIRED"                  # Unfilled order expired by exchange/TIF; terminal
    FAILED = "FAILED"                    # Fatal transport or adapter failure; terminal
    UNKNOWN = "UNKNOWN"                  # Ambiguous submission status; requires reconciliation


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
```

### 7.2 Typed Submission Result & Acknowledgement Contracts

```python
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
```

### 7.3 Submission Outcome Mapping & Deterministic Lifecycle Behavior

| `SubmissionOutcomeType` | Trigger Scenario Examples | Deterministic Lifecycle Behavior | Can Retry? | Final / Next State |
|---|---|---|---|---|
| **`ACKNOWLEDGED`** | Broker accepts order and issues venue/broker order ID. | Record `broker_order_id` and timestamp; register order version; begin working fill tracking. | **NO** (Active) | `ACKNOWLEDGED` |
| **`REJECTED`** | Pre-trade risk rejection; insufficient margin / funds; circuit filter breach; invalid strike; market closed; broker business rule veto. | Record `rejection_reason` and error message in audit log; release intent lock immediately; do not retry. | **NO** | `REJECTED` (Terminal) |
| **`RETRYABLE_FAILURE`** | Local socket exhaustion; TCP connect timeout strictly *before* wire dispatch; transient 429 with `Retry-After`. | Apply bounded exponential backoff using *identical* `client_order_id`. If retry budget exhausted, record failure and release intent lock. | **YES** (Max 2) | `SUBMITTING` $\to$ `FAILED` (if exhausted) |
| **`AMBIGUOUS_UNKNOWN`** | HTTP read timeout; TCP reset or connection drop *after* wire dispatch; HTTP 500/502/503/504 gateway response. | Do NOT blind-retry. Transition state to `UNKNOWN`. Quarantine order; lock instrument against automated new entries; trigger authoritative reconciliation against broker order book. | **NO** (Reconciliation) | `UNKNOWN` (Quarantine) |

---

## 8. ORDER REQUEST CONTRACT & SIDE / EFFECT DERIVATION

### 8.1 Canonical `OrderRequest` (Broker-Independent)

`OrderRequest` is the definitive, immutable order contract emitted by the `ExecutionPlanner`. It contains **zero broker-specific tokens, tags, or session identifiers**, removes unused fields (e.g. `trigger_price`), and uses deeply immutable metadata:

```python
@dataclass(frozen=True, slots=True)
class OrderRequest:
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
    order_purpose: OrderPurpose          # ENTRY or EXIT
    order_version: int = 1               # Monotonic order version (increments on REPLACED)

    # 3. Temporal Horizons & Idempotency
    creation_timestamp: datetime         # Time order was synthesized locally
    expiry_timestamp: Optional[datetime] # Order validity execution deadline
    idempotency_key: str                 # Deterministic SHA-256 deduplication key
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
```

### 8.2 Canonical Reference-Price Policy for Market Order Notional Safety

Naive order routers calculate estimated notional as `quantity * (price or 0.0)`. For `MARKET` orders where `price = None`, this evaluates to `0.0`, allowing unpriced market orders to **completely bypass `max_order_notional` safety caps**!

Tradego establishes an explicit canonical reference-price policy:
1. **Source of Truth:** For `MARKET` orders, estimated notional is derived using `OrderRequest.reference_price`, which is sourced directly from upstream `ApprovedTradeIntent.approved_entry_price` (which is already strictly $> 0.0$ as validated in Phase 6).
2. **Limit Order Policy:** For `LIMIT` orders, estimated notional is calculated using `OrderRequest.price`.
3. **Zero-Price Prohibition:** `price = None` must **NEVER** evaluate to zero for safety cap calculations. If a price or reference price is non-positive or missing, the order is immediately rejected locally with `ExecutionFailureReason.INVALID_ORDER_PRICE`.
4. **Mathematical Formulation:**
   $$\text{estimated\_notional} = \begin{cases} \text{quantity} \times \text{price}, & \text{if } \text{order\_type} == \text{LIMIT} \\ \text{quantity} \times \text{reference\_price}, & \text{if } \text{order\_type} == \text{MARKET} \end{cases}$$
5. **Universal Enforcement:** The notional safety ceiling ($\text{estimated\_notional} \le \text{max\_order\_notional}$) is enforced identically and symmetrically across both `MARKET` and `LIMIT` orders before wire transmission.

### 8.3 Order Side vs. Position Direction Resolution with Explicit `PositionEffect`

A critical architectural mandate is that **Position Direction** (+1 Long, -1 Short) must never be conflated with **Order Side** (BUY, SELL). 

Tradego explicitly derives `OrderSide`, `OrderPurpose`, and `PositionEffect` from upstream `ApprovedTradeIntent.signal_type`, `direction`, and current position presence:

```python
def derive_order_side_and_position_effect(
    signal_type: SignalType, direction: int, is_position_open: bool = False
) -> Tuple[OrderSide, OrderPurpose, PositionEffect]:
    """
    Deterministically maps ApprovedTradeIntent signal_type and direction to canonical
    OrderSide, OrderPurpose, and PositionEffect. Guarantees that exits never invert side.
    """
    match signal_type:
        case SignalType.ENTRY_LONG:
            effect = PositionEffect.INCREASE if is_position_open else PositionEffect.OPEN
            return OrderSide.BUY, OrderPurpose.ENTRY, effect

        case SignalType.ENTRY_SHORT:
            effect = PositionEffect.INCREASE if is_position_open else PositionEffect.OPEN
            return OrderSide.SELL, OrderPurpose.ENTRY, effect

        case SignalType.EXIT_LONG:
            # Exiting long position requires a SELL order with CLOSE effect
            return OrderSide.SELL, OrderPurpose.EXIT, PositionEffect.CLOSE

        case SignalType.EXIT_SHORT:
            # Exiting short position requires a BUY order with CLOSE effect
            return OrderSide.BUY, OrderPurpose.EXIT, PositionEffect.CLOSE

        case SignalType.SCALE_IN:
            side = OrderSide.BUY if direction > 0 else OrderSide.SELL
            return side, OrderPurpose.SCALE, PositionEffect.INCREASE

        case SignalType.SCALE_OUT:
            # Scaling out of long requires SELL; scaling out of short requires BUY
            side = OrderSide.SELL if direction > 0 else OrderSide.BUY
            return side, OrderPurpose.SCALE, PositionEffect.REDUCE

        case _:
            raise ValueError(f"Unsupported SignalType for execution: {signal_type}")
```

#### Mapping Verification Table:
| `SignalType` | Intent Direction | Current Position | Canonical `OrderSide` | Canonical `PositionEffect` |
|---|---|---|---|---|
| `ENTRY_LONG` | +1 (Long) | Flat | `BUY` | `OPEN` |
| `ENTRY_LONG` | +1 (Long) | Long | `BUY` | `INCREASE` |
| `ENTRY_SHORT`| -1 (Short) | Flat | `SELL` | `OPEN` |
| `ENTRY_SHORT`| -1 (Short) | Short | `SELL` | `INCREASE` |
| `EXIT_LONG`  | +1 (Long) | Long | `SELL` | `CLOSE` |
| `EXIT_SHORT` | -1 (Short) | Short | `BUY` | `CLOSE` |
| `SCALE_IN`   | +1 (Long) | Long | `BUY` | `INCREASE` |
| `SCALE_IN`   | -1 (Short) | Short | `SELL` | `INCREASE` |
| `SCALE_OUT`  | +1 (Long) | Long | `SELL` | `REDUCE` |
| `SCALE_OUT`  | -1 (Short) | Short | `BUY` | `REDUCE` |

This eliminates all ambiguity: `ApprovedTradeIntent` contains complete data to derive the exact order side and position effect without modifying upstream Phase 6 contracts.

---

## 9. ORDER LIFECYCLE STATE MACHINE & REPLACED SEMANTICS

Tradego defines a strict, finite, deterministic state machine governing the lifecycle of every order.

```
                    ┌─────────────────────────┐
                    │         CREATED         │
                    └────────────┬────────────┘
                                 │
                                 ▼ (Validation Passes)
                    ┌─────────────────────────┐
                    │        VALIDATED        │
                    └────────────┬────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
     (Network Send) ▼                         ▼ (Pre-Trade Check Fails)
       ┌─────────────────────────┐ ┌─────────────────────────┐
       │       SUBMITTING        │ │        REJECTED         │ [TERMINAL]
       └────────────┬────────────┘ └─────────────────────────┘
                    │
         ┌──────────┴──────────┬────────────────────────┐
         │                     │                        │
         ▼ (Broker Confirms)   ▼ (Broker Vetoes)        ▼ (Timeout / Dropped)
 ┌───────────────┐     ┌───────────────┐        ┌───────────────┐
 │ ACKNOWLEDGED  │     │   REJECTED    │        │    UNKNOWN    │ (Reconcile)
 └───────┬───────┘     └───────────────┘        └───────┬───────┘
         │                [TERMINAL]                    │
         ├──────────────────────────────────────────────┤
         │                                              │
         ▼ (Fill Qty < Total)                           │ (Fill Confirmed)
 ┌───────────────┐                                      │
 │PARTIAL_FILLED ├──────────────────┐                   │
 └───────┬───────┘                  │                   │
         │                          │                   │
         ▼ (Fill Qty == Total)      ▼ (Cancel Rest)     │
 ┌───────────────┐          ┌───────────────┐           │
 │    FILLED     │          │   CANCELLED   │           │
 └───────────────┘          └───────────────┘           │
    [TERMINAL]                 [TERMINAL]               │
                                                        │
         ┌──────────────────────────────────────────────┘
         ▼ (Reconciliation Finds Order In Exchange)
   [To ACKNOWLEDGED / PARTIAL / FILLED / CANCELLED]
```

### 9.1 Legal Transition Matrix

| Initial State | Allowed Target States | Triggering Event | Next Status Type |
|---|---|---|---|
| `CREATED` | `VALIDATED`, `REJECTED` | Internal pre-submission validation | Non-terminal / Terminal |
| `VALIDATED` | `SUBMITTING`, `FAILED` | Submission dispatch or local transport error | Non-terminal / Terminal |
| `SUBMITTING` | `ACKNOWLEDGED`, `REJECTED`, `UNKNOWN`, `FAILED` | Broker ack, broker veto, network timeout, transport failure | Non-terminal / Terminal / Recovery |
| `ACKNOWLEDGED` | `PARTIALLY_FILLED`, `FILLED`, `CANCEL_PENDING`, `REPLACE_PENDING`, `EXPIRED`, `REJECTED` | Execution fill, cancel request, replace request, TIF expiry | Non-terminal / Terminal |
| `PARTIALLY_FILLED` | `PARTIALLY_FILLED`, `FILLED`, `CANCEL_PENDING`, `EXPIRED` | Subsequent fill, cancel request, TIF expiry | Non-terminal / Terminal |
| `CANCEL_PENDING` | `CANCELLED`, `PARTIALLY_FILLED`, `FILLED` | Broker confirms cancel, or race fill arrives before cancel | Terminal / Non-terminal |
| `REPLACE_PENDING`| `REPLACED`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED` | Broker accepts replace (transitions to `REPLACED` then `ACKNOWLEDGED`), or race fill arrives | Non-terminal / Terminal |
| `REPLACED` | `ACKNOWLEDGED` | Adapter applies updated version parameters | Working state |
| `UNKNOWN` | `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `FAILED` | Authoritative resolution via adapter reconciliation query | As resolved |
| `FILLED` | **NONE (TERMINAL)** | Terminal state; all incoming events must match or be dropped | Terminal |
| `CANCELLED` | **NONE (TERMINAL)** | Terminal state; remaining quantity is zero | Terminal |
| `REJECTED` | **NONE (TERMINAL)** | Terminal state; order was vetoed | Terminal |
| `EXPIRED` | **NONE (TERMINAL)** | Terminal state; order reached end of validity | Terminal |
| `FAILED` | **NONE (TERMINAL)** | Terminal state; fatal transport breakdown | Terminal |

### 9.2 Order Versioning & Replacement Identity (`REPLACED` Semantics)

When `replace_order()` is executed on an active working order:
1. `ExecutionState` transitions `ACKNOWLEDGED` $\to$ `REPLACE_PENDING`.
2. The logical Tradego order is governed by a persistent, stable `client_order_id`. Within this logical order, replacements establish an explicit identity hierarchy:
   ```
   Tradego logical order: client_order_id
       ├── order_version 1 → broker_order_id A (initial working placement)
       └── order_version 2 → broker_order_id B (post-replacement working placement)
   ```
3. **Broker-Side Re-Identification:** In many broker architectures (e.g. FIX Cancel/Replace or REST modify endpoints), the venue cancels order `A` and issues a completely new order identity `B` for the modified version. Tradego accommodates this without losing logical order continuity:
   - `OrderRequest.client_order_id` remains the primary parent key.
   - `order_version` increments monotonically ($1 \to 2$).
   - The adapter and registry register `broker_order_id B` and map it back to `client_order_id`.
4. **Correlation of Callbacks & Race Fills Across Versions:**
   - `ExecutionState` maintains a version map: `broker_id_history: Dict[str, int]` mapping each historical `broker_order_id` to its corresponding `order_version`.
   - `ExecutionStateRegistry` indexes both `client_order_id` and all associated `broker_order_id` references (`broker_order_id -> client_order_id`).
   - If a fill arrives referencing old `broker_order_id A` (e.g. executed at the exchange matching engine immediately before the replace request was processed): the fill is correlated to `client_order_id` via the registry mapping, credited to `cumulative_filled_quantity`, and logged under `order_version 1`.
   - If a fill arrives referencing new `broker_order_id B`: it is correlated to `client_order_id`, credited to `cumulative_filled_quantity`, and logged under `order_version 2`.
   - **Conservation Invariant:** Total filled quantity across all versions is strictly conserved:
     $$Q_{filled} = \sum f(\text{broker\_order\_id A}) + \sum f(\text{broker\_order\_id B}) \le Q_{req}$$
5. **State Progression:**
   - Upon broker acceptance: state transitions `REPLACE_PENDING` $\to$ `REPLACED` $\to$ `ACKNOWLEDGED` at `order_version = 2`.
   - If the broker rejects the modification: the state transitions from `REPLACE_PENDING` **back to the original working state** (`ACKNOWLEDGED` or `PARTIALLY_FILLED`) at `order_version = 1`, retaining `broker_order_id A` as active.

---

## 10. PARTIAL FILL MODEL & CANONICAL FILL IDENTITY

Execution in cash equities frequently results in child executions across multiple price levels. Tradego defines explicit, mathematically closed partial fill accounting:

### 10.1 Fill Data Contract & Four-Tier Fill Identity Hierarchy

In paper environments or certain broker workflows, a native exchange-assigned trade ID may be absent or delayed. However, the fallback `FILL-{client_order_id}-{fill_sequence}` must **NOT** be assumed restart-safe for live execution unless the sequence has an explicit, authoritative persistence guarantee.

Tradego establishes a strict **Four-Tier Fill Identity Hierarchy**:
1. **Tier 1 (Exchange Trade ID):** `exchange_trade_id` when provided by the venue matching engine via the broker feed. This is the authoritative primary identity.
2. **Tier 2 (Broker Authoritative Execution ID):** Broker-native unique execution identifier (`broker_exec_id`) when the broker assigns a persistent, unique execution reference but abstracts the exchange trade token.
3. **Tier 3 (Deterministic Synthetic Fill ID — PAPER / Simulation Only):** `f"FILL-{client_order_id}-{fill_sequence}"` is permitted **ONLY** where the execution adapter authoritatively owns the complete execution sequence (specifically `PaperExecutionAdapter` or deterministic backtesting environments where process restart collisions are impossible).
4. **Tier 4 (Ambiguous Live Identity — Enters Reconciliation):** In `LIVE` execution, if a fill callback arrives lacking both `exchange_trade_id` and `broker_exec_id`, the adapter and engine **MUST NOT invent an arbitrary synthetic sequence ID** that could collide across process restarts or concurrent callback arrivals. Instead, the ambiguous event enters `RECONCILIATION_REQUIRED` / `UNKNOWN`, and the engine queries the authoritative broker trade book (`get_trades()`) to obtain verified, unique trade records before crediting position changes.

```python
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
```

### 10.2 Mathematical Accounting Invariants

Let an order have requested quantity $Q_{req}$. As fills $f_1, f_2, \dots, f_k$ arrive:
1. **Cumulative Filled Quantity ($Q_{filled}$):**
   $$Q_{filled}^{(k)} = \sum_{i=1}^k f_i.\text{fill\_quantity}$$
2. **Remaining Quantity ($Q_{rem}$):**
   $$Q_{rem}^{(k)} = Q_{req} - Q_{filled}^{(k)}$$
   *Invariant:* $Q_{rem}^{(k)} \ge 0$. If an incoming fill causes $Q_{filled} > Q_{req}$, the execution engine **short-circuits and enters `UNKNOWN`**, flagging an over-execution anomaly.
3. **Volume-Weighted Average Execution Price ($\bar{P}_{exec}$):**
   $$\bar{P}_{exec}^{(k)} = \frac{\sum_{i=1}^k \left( f_i.\text{fill\_quantity} \times f_i.\text{fill\_price} \right)}{Q_{filled}^{(k)}}$$
4. **Lifecycle State Progression:**
   $$\text{Status} = \begin{cases} \text{FILLED}, & \text{if } Q_{rem}^{(k)} = 0 \\ \text{PARTIALLY\_FILLED}, & \text{if } 0 < Q_{rem}^{(k)} < Q_{req} \end{cases}$$

---

## 11. IDEMPOTENCY MODEL & STABLE-IDENTITY CONTRACT

Idempotency is mandatory to prevent double execution resulting from network timeouts, duplicate broker callbacks, reconnects, or process restarts.

### 11.1 Stable Canonical Identity & Broker Idempotency Responsibility

In earlier designs, appending `{submission_attempt}` to `client_order_id` destroyed stability across retries. If an initial submission succeeded on the exchange matching engine but the network acknowledgement was dropped, retrying with a modified `client_order_id` caused the broker to place a **second, duplicate order**!

Tradego establishes the following precise idempotency boundary:
1. **Tradego Core Guarantee:** Tradego guarantees a **strictly stable and immutable canonical identity** (`client_order_id`) across all retry attempts and lifecycle stages of the same `OrderRequest`:
   $$\text{client\_order\_id} = \text{f"TG-\{strategy\_id[:4]\}-\{date\_str\}-\{intent\_id[:12]\}"}$$
2. **Adapter Responsibility for Venue Idempotency:** Tradego does **NOT** naively assume that all third-party broker gateways natively provide automatic duplicate rejection based on a client tag. The concrete `BrokerExecutionAdapter` is authoritatively responsible for mapping Tradego's stable `client_order_id` to broker-specific idempotency and deduplication mechanisms (e.g. `correlationId` in DhanHQ, `tag` / `client_id` in Zerodha, `ClOrdID` in FIX 4.2/4.4).
3. **Non-Idempotent Broker Fallback:** If a specific broker or exchange API cannot guarantee native idempotent resubmission on its gateway, any ambiguous submission outcome (`AMBIGUOUS_UNKNOWN`) **MUST NOT be resubmitted blindly**. Instead, the order must immediately enter `UNKNOWN` status and undergo authoritative reconciliation against the venue order book before any subsequent submission attempt is permitted.

### 11.2 Four-Tier Identity Hierarchy

Tradego segregates identity across four distinct structural layers:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. INTENT IDENTITY: intent_id (UUIDv4 emitted by Phase 6 RiskEngine)                   │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 2. CANONICAL ORDER IDENTITY: client_order_id (STABLE across all retries)               │
│    Format: TG-{strategy_id[:4]}-{date_str}-{intent_id[:12]}                            │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 3. BROKER ORDER IDENTITY: broker_order_id (Exchange/Broker assigned reference)         │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 4. FILL IDENTITY: fill_id (Canonical fill token: exchange_trade_id or synthetic)       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### 11.3 Strengthened UNKNOWN & Reconciliation Identity Contract

When an order enters `UNKNOWN` (e.g., read timeout or dropped socket during submission):
1. **Primary Reconciliation Key:** Matching is performed strictly by `client_order_id`.
2. **Reconciliation Query:** The engine invokes `adapter.get_order_status(client_order_id)`. If the broker API does not support single-order lookup by client ID, the adapter fetches all open orders (`adapter.get_open_orders()`) and searches by client order tag.
3. **Resolution Outcomes:**
   - **Case 1 (Found on Broker):** Attach the broker's `broker_order_id` to local `ExecutionState`. Adopt the broker's authoritative status (`ACKNOWLEDGED`, `PARTIALLY_FILLED`, or `FILLED`).
   - **Case 2 (Definitively Absent on Broker):** If the broker explicitly confirms that no order exists with that `client_order_id` after a mandatory grace period ($t_{grace} \ge 5.0\text{ s}$ to account for exchange gateway ingestion lag), mark local state `FAILED` and release intent lock.
   - **Case 3 (Broker Unreachable or Ambiguous):** The order **remains in `UNKNOWN` quarantine**. Automated new entries for that instrument are blocked until an operator or reconnect cycle achieves authoritative state.

---

## 12. RETRY POLICY & FAILURE CLASSIFICATION

Trading systems must never employ unconstrained or naive retry loops (`retry forever`). All failures are categorized into three deterministic action tiers:

### 12.1 Failure Action Matrix

| Failure Category | Concrete Scenarios | Immediate Action | Can Retry? | Max Retries |
|---|---|---|---|---|
| **Tier 1: Safe to Retry** | TCP connect timeout *before* request dispatched; local socket exhaustion; rate limit 429 with `Retry-After`. | Backoff monotonically and retry submission using identical `client_order_id`. | **YES** | 2 |
| **Tier 2: Fatal / Never Retry** | Broker business rejection (insufficient funds, circuit breaker, invalid strike, market closed); pre-trade validation error; intent expired; terminal order state. | Mark order `REJECTED` or `FAILED`. Log audit trail. Release intent lock. | **NO** | 0 |
| **Tier 3: Ambiguous / Reconcile** | Read timeout waiting for HTTP response; TCP reset *after* packet dispatched; HTTP 500/502/503/504 gateway errors. | Mark order `UNKNOWN`. Block further automated entries for instrument. Trigger reconciliation. | **NO (Until Reconciled)** | 0 |

### 12.2 Bounded Deterministic Backoff
When Tier 1 retries are authorized, backoff intervals are strictly bounded:
$$t_{backoff} = \min\left(t_{base} \times 2^{\text{attempt}}, \quad t_{max}\right)$$
For Phase 7: $t_{base} = 50\text{ ms}$, $t_{max} = 200\text{ ms}$, maximum 2 retry attempts.

---

## 13. CANCEL / REPLACE MODEL & RACE HANDLING

### 13.1 Cancellation Protocol
1. Client requests cancel for `client_order_id`.
2. Execution engine verifies order is in an active, cancellable state (`ACKNOWLEDGED`, `PARTIALLY_FILLED`).
3. Order transitions to `CANCEL_PENDING`.
4. Adapter issues cancel request carrying `broker_order_id` and `client_order_id`.

### 13.2 Race Conditions Resolution

#### Scenario A: Fill Arrives Before Cancel Reaches Matching Engine
```
Tradego Core                    Broker / Exchange
    │                                   │
    ├───── Send CancelRequest ─────────►│ Order already matched!
    │                                   ├─► Match Qty 100
    │◄──── Emit Fill (Qty 100) ─────────┤
    │                                   │
    │◄──── Reject Cancel ("Filled") ────┤
    ▼                                   ▼
[State = FILLED]                  [State = FILLED]
```
- **Rule:** The matching engine is the sole authority on execution timing. If a fill occurred before cancellation was processed, the fill is **authoritative**. Tradego transitions order state to `FILLED`. The subsequent cancel rejection is logged as a benign race outcome.

#### Scenario B: Partial Fill Followed by Successful Cancellation
```
Tradego Core                    Broker / Exchange
    │                                   │
    │◄──── Emit Fill (Qty 30) ──────────┤ (Remaining = 70)
    ├───── Send CancelRequest ─────────►│ Cancel remaining 70!
    │◄──── Confirm Cancel (Qty 70) ─────┤
    ▼                                   ▼
[State = CANCELLED, Q_filled = 30]
```
- **Rule:** Unfilled remaining quantity is cancelled. Executed partial fills remain valid and credited to portfolio positions. Terminal state is `CANCELLED` with `cumulative_filled_quantity = 30`.

#### Scenario C: Replacement Correlation & Race Fill Resolution
```
Tradego Core                    Broker / Exchange
    │                                   │
    │ [order_version 1, broker_id A]    │
    ├───── Send ReplaceRequest ────────►│ Race condition! Order A fills at venue!
    │                                   ├─► Match Qty 50 on broker_id A
    │◄──── Emit Fill (broker_id A) ─────┤
    │                                   ├─► Venue accepts Replace, assigns broker_id B
    │◄──── Confirm Replace (broker_id B)┤ (New remaining Qty = 50)
    ▼                                   ▼
[Cumulative Fills = 50, Version = 2]
```
- **Rule:** When a replacement request is in-flight:
  1. `ExecutionState` maps both `broker_order_id A` and `broker_order_id B` to the same logical `client_order_id`.
  2. Fills arriving with old identity `A` are valid and credited to `cumulative_filled_quantity`.
  3. Subsequent fills arriving with new identity `B` are credited to the same `cumulative_filled_quantity`.
  4. Total execution is strictly bounded by the original authorized order quantity: $Q_{filled} \le Q_{req}$.
  5. If the race fill on `A` completely satisfies the requested quantity ($Q_{filled} == Q_{req}$), the pending replace on the venue is cancelled or acknowledged as redundant, and the order terminates cleanly as `FILLED`.

---

## 14. EXECUTION ROUTER & SESSION/CALENDAR GUARD

The `ExecutionRouter` is the central broker-independent dispatch gateway. It isolates Tradego core from concrete broker transports, performs exchange-local timezone conversion for calendar session checks, enforces canonical reference-price notional safety, and deterministically handles typed submission outcomes:

```python
class ExecutionRouter:
    """
    Broker-independent routing coordinator. Dispatches canonical OrderRequests
    to the active BrokerExecutionAdapter and coordinates state tracking.
    """
    def __init__(
        self,
        adapter: "BrokerExecutionAdapter",
        registry: "ExecutionStateRegistry",
        calendar: Optional[Any] = None,   # Phase 2 / Phase 3 ExchangeCalendar
        config: Optional["ExecutionConfig"] = None,
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._calendar = calendar
        self._config = config or ExecutionConfig()
        self._lock = threading.RLock()
        self._is_accepting_orders = False

    def submit(self, request: OrderRequest) -> "ExecutionState":
        with self._lock:
            if not self._is_accepting_orders:
                raise RuntimeError("ExecutionRouter is not accepting orders (system not reconciled or offline).")

            # 1. Trading Session Guard with Explicit Exchange Timezone Conversion
            if self._calendar is not None:
                # ExchangeCalendar operates in exchange-local time (e.g. Asia/Kolkata for NSE/BSE)
                exchange_tz = getattr(self._calendar, "timezone", None)
                if exchange_tz is None:
                    try:
                        from zoneinfo import ZoneInfo
                        exchange_tz = ZoneInfo("Asia/Kolkata")
                    except Exception:
                        from datetime import timezone as dt_tz, timedelta
                        exchange_tz = dt_tz(timedelta(hours=5, minutes=30))

                # Normalize naive or UTC creation_timestamp to exchange-local datetime
                ts = request.creation_timestamp
                if ts.tzinfo is None:
                    from datetime import timezone as dt_tz
                    ts = ts.replace(tzinfo=dt_tz.utc)
                exchange_local_dt = ts.astimezone(exchange_tz)

                session = self._calendar.resolve_session(request.instrument_id, exchange_local_dt)
                if not session.is_regular_trading(exchange_local_dt):
                    raise ValueError(
                        f"Exchange {request.instrument_id.exchange} is not in regular trading session "
                        f"at exchange-local time {exchange_local_dt}."
                    )

            # 2. Canonical Reference-Price Policy for Notional Safety Guard
            # price=None for MARKET orders must NEVER evaluate to zero for safety calculations
            if request.order_type == OrderType.LIMIT:
                notional_price = request.price
            elif request.order_type == OrderType.MARKET:
                notional_price = request.reference_price
            else:
                raise ValueError(f"Unsupported OrderType: {request.order_type}")

            if notional_price is None or notional_price <= 0.0:
                raise ValueError(f"Invalid reference/limit price for notional calculation: {notional_price}")

            estimated_notional = request.quantity * notional_price
            if estimated_notional > self._config.max_order_notional:
                raise ValueError(
                    f"Order estimated notional ₹{estimated_notional:.2f} exceeds "
                    f"max limit ₹{self._config.max_order_notional:.2f}"
                )

            # 3. Enforce Intent Idempotency
            if self._registry.has_active_intent(request.intent_id):
                raise ValueError(f"Intent {request.intent_id} already has an active working order.")

            # 4. Register Initial State
            state = self._registry.create_state(request)
            state.transition_to(CanonicalOrderStatus.VALIDATED)
            state.transition_to(CanonicalOrderStatus.SUBMITTING)

            # 5. Dispatch to Adapter with Typed Outcome Handling
            attempt = 0
            while True:
                try:
                    result: SubmissionResult = self._adapter.submit_order(request)
                except Exception as e:
                    # Unexpected transport exception during dispatch
                    state.record_failure(f"Transport exception during dispatch: {e}")
                    state.transition_to(CanonicalOrderStatus.UNKNOWN)
                    break

                match result.outcome:
                    case SubmissionOutcomeType.ACKNOWLEDGED:
                        if result.acknowledgement is None:
                            state.record_failure("Missing acknowledgement payload on ACKNOWLEDGED outcome.")
                            state.transition_to(CanonicalOrderStatus.UNKNOWN)
                        else:
                            state.record_acknowledgement(result.acknowledgement)
                            state.transition_to(CanonicalOrderStatus.ACKNOWLEDGED)
                        break

                    case SubmissionOutcomeType.REJECTED:
                        reason = result.rejection_reason or ExecutionFailureReason.BROKER_REJECTED
                        state.record_rejection(reason, result.error_message or "Broker pre-trade rejection")
                        state.transition_to(CanonicalOrderStatus.REJECTED)
                        self._registry.release_intent(request.intent_id)
                        break

                    case SubmissionOutcomeType.RETRYABLE_FAILURE:
                        attempt += 1
                        if attempt <= self._config.max_submission_retries:
                            backoff_ms = min(self._config.retry_base_delay_ms * (2 ** attempt), 200.0)
                            time.sleep(backoff_ms / 1000.0)
                            continue  # Retry with identical request (stable client_order_id)
                        else:
                            state.record_failure(
                                f"Submission retries exhausted ({attempt - 1} retries): {result.error_message}"
                            )
                            state.transition_to(CanonicalOrderStatus.FAILED)
                            self._registry.release_intent(request.intent_id)
                            break

                    case SubmissionOutcomeType.AMBIGUOUS_UNKNOWN:
                        state.record_failure(f"Ambiguous submission status: {result.error_message}")
                        state.transition_to(CanonicalOrderStatus.UNKNOWN)
                        # Quarantined in UNKNOWN; triggers reconciliation without blind retry
                        break

            return state
```

---

## 15. BROKER ADAPTER INTERFACE & RATE-LIMIT OWNERSHIP

### 15.1 Broker Adapter Contract
Every execution backend—whether Paper or Live—must implement the uniform, abstract `BrokerExecutionAdapter` interface:

```python
class BrokerExecutionAdapter(ABC):
    """
    Abstract contract for all Tradego execution adapters.
    Completely isolates broker protocols, auth, and wire-formats from Tradego Core.
    """
    @abstractmethod
    def submit_order(self, request: OrderRequest) -> "SubmissionResult":
        """
        Submits an order to the venue and returns a typed SubmissionResult
        categorizing the outcome (ACKNOWLEDGED, REJECTED, RETRYABLE_FAILURE, AMBIGUOUS_UNKNOWN).
        """
        ...

    @abstractmethod
    def cancel_order(self, client_order_id: str, broker_order_id: str) -> bool: ...

    @abstractmethod
    def replace_order(
        self, client_order_id: str, broker_order_id: str, new_price: Optional[float], new_quantity: Optional[int]
    ) -> "OrderAcknowledgement": ...

    @abstractmethod
    def get_order_status(self, client_order_id: str, broker_order_id: Optional[str]) -> "OrderUpdate": ...

    @abstractmethod
    def get_open_orders(self) -> List["OrderUpdate"]: ...

    @abstractmethod
    def get_positions(self) -> List[PositionSnapshot]: ...

    @abstractmethod
    def health_check(self) -> bool: ...
```

### 15.2 Rate-Limit Ownership & Bounded Behavior
- **Ownership:** Rate limiting belongs strictly inside the `BrokerExecutionAdapter`. Core Tradego execution remains broker-agnostic and does not contain vendor-specific rate limits (e.g. Dhan 10 req/s).
- **Algorithm:** Token Bucket algorithm implemented inside the adapter.
- **Bounded Buffer & Fail-Fast Behavior:**
  - When tokens are available: request executes immediately.
  - When tokens are temporarily exhausted: adapter buffers requests up to a **bounded queue depth** (`rate_limit_max_queue_depth = 50`) for a maximum duration (`rate_limit_max_wait_ms = 500 ms`).
  - If the queue is full or wait time exceeds 500 ms: the adapter **fails fast** by raising `ExecutionFailureReason.RATE_LIMIT_EXCEEDED`, preventing unbounded thread starvation or latency accumulation.

---

## 16. PAPER EXECUTION ARCHITECTURE

Paper trading is the mandatory first execution mode in Phase 7. It implements the identical `BrokerExecutionAdapter` interface while simulating order execution against live market data feeds:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        PaperExecutionAdapter                           │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Receives canonical OrderRequest from ExecutionRouter                │
│ 2. Subscribes to Phase 1 MarketEvent ticks and Phase 2 Depth snapshots │
│ 3. Simulates Matching Rules:                                           │
│    - MARKET BUY: Fills immediately at current Level 1 Ask price        │
│    - MARKET SELL: Fills immediately at current Level 1 Bid price       │
│    - LIMIT BUY: Fills when market LTP <= Limit Price                   │
│    - LIMIT SELL: Fills when market LTP >= Limit Price                  │
│ 4. Applies Simulated Slippage Model:                                   │
│    - Configurable base slippage in bps (e.g. 2.0 bps for large cap)    │
│    - Size penalty when quantity exceeds Level 1 book depth             │
│ 5. Emits canonical OrderAcknowledgement, OrderUpdate, and Fill events  │
│ 6. Zero broker API calls, zero network latency                         │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 17. FUTURE LIVE EXECUTION BOUNDARY (GENERIC BROKER ADAPTERS)

The live execution architecture defines a generic adapter pattern accommodating third-party broker APIs (e.g. DhanHQ, Zerodha Kite, or institutional fix gateways) without coupling Tradego Core to any specific vendor:

```
┌────────────────────────────────────────────────────────────────────────┐
│                 Live Broker Adapter (e.g. DhanAdapter)                 │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Translates canonical OrderRequest to broker-specific wire format:   │
│    - client_order_id -> correlationId / tag                            │
│    - instrument_id -> broker security token (via InstrumentRegistry)   │
│    - side (BUY/SELL) -> transactionType (BUY/SELL)                     │
│    - order_type -> orderType (MARKET/LIMIT)                            │
│    - time_in_force -> validity (DAY/IOC)                               │
│    - productType -> CNC / Delivery                                     │
│ 2. Enforces Token Bucket rate limiting before network dispatch         │
│ 3. Manages authenticated HTTPS REST sessions and WebSocket streams     │
│ 4. Normalizes broker status strings to CanonicalOrderStatus           │
│ 5. Emits normalized Fill and OrderAcknowledgement events to Core       │
└────────────────────────────────────────────────────────────────────────┘
```

*Governance Note:* No live credentials or network calls to any live broker are made in Phase 7. The live adapter is strictly an architectural boundary defined for future promotion.

---

## 18. SAFE LIVE STARTUP & RECONCILIATION SEQUENCE

When the Tradego execution engine initializes or recovers from network disruption, it enforces a strict **6-phase sequential startup protocol** before accepting any new orders:

```
[Phase A: Bootstrap] ──► [Phase B: State Fetch] ──► [Phase C: Order Reconcile]
                                                            │
[Phase F: Accept Orders] ◄── [Phase E: Guard Check] ◄── [Phase D: Position Reconcile]
```

### Step-by-Step Cold-Boot Sequence:
1. **Phase A (Adapter Bootstrap & Health Check):** Establish broker transport; verify authentication; call `adapter.health_check()`. If unhealthy, abort startup.
2. **Phase B (Broker State Acquisition):** Query all open orders (`adapter.get_open_orders()`) and active positions (`adapter.get_positions()`).
3. **Phase C (Order Reconciliation):** Compare broker open orders against local journal:
   - Re-link unacknowledged local orders with matching broker orders via `client_order_id`.
   - Identify orphan orders on broker; alert operator.
4. **Phase D (Position Reconciliation):** Compare broker net positions against local `PortfolioSnapshot`. If discrepancy detected, emit `PositionReconciliationAdjustment` event.
5. **Phase E (Guardrail & Calendar Verification):** Confirm exchange session is open (`REGULAR_TRADING`), kill-switch is inactive, and token-bucket rate limiter is initialized.
6. **Phase F (Enable Order Intake):** Set `router.is_accepting_orders = True`. Only now is the engine authorized to accept new `ApprovedTradeIntent` inputs.

---

## 19. POSITION UPDATE BOUNDARY: FILLS VS. RECONCILIATION

Tradego strictly separates continuous **Fill-Driven Updates** from point-in-time **Position Reconciliation Adjustments**:

### 19.1 Continuous Fill-Driven Position Updates
Fills represent continuous execution deltas. Each `Fill` event triggers transactional accounting:
- Net quantity updates: $Q_{net} \leftarrow Q_{net} \pm \text{fill\_quantity}$.
- Weighted average price updates on position-increasing fills.
- Realized PnL is booked on position-reducing fills.
- Emits updated `PositionSnapshot`.

### 19.2 Point-in-Time Position Reconciliation Adjustments
Reconciliation adjustments represent point-in-time state synchronization (e.g. startup alignment or corporate action adjustment):
- Occurs **only** during cold startup or periodic reconciliation audits when broker positions differ from local records ($\Delta Q = Q_{broker} - Q_{local} \ne 0$).
- Emits an explicit, canonical `PositionReconciliationAdjustment` event documenting the variance and justification (`COLD_START_SYNC`, `RECOVERY_OVERRIDE`).
- **PROHIBITED ACTION:** Manufacturing synthetic execution fills or fictional broker fee events to force ledger alignment.

```python
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
```

---

## 20. FAILURE & FAIL-SAFE MODEL

Tradego operates under strict **Fail-Closed** defaults:

```
┌────────────────────────────────────┬───────────────────────────────────┐
│ Detected Condition                 │ Fail-Safe Response                │
├────────────────────────────────────┼───────────────────────────────────┤
│ Expired ApprovedTradeIntent        │ REJECT order; do not submit.      │
│ Duplicate client_order_id          │ REJECT order; halt duplicate.     │
│ Quantity > permitted_quantity      │ REJECT order; flag risk breach.   │
│ Estimated Notional > Limit         │ REJECT order; flag notional cap.  │
│ Exchange closed or halted          │ REJECT order; session closed.     │
│ Inverted or non-positive price     │ REJECT order; flag geometry error.│
│ Adapter connectivity down          │ REJECT order; do not queue blind. │
│ Network read timeout on send       │ Mark UNKNOWN; initiate reconcile. │
│ Fill quantity > requested quantity │ Mark UNKNOWN; halt trading on sym.│
│ Illegal state transition detected  │ Throw exception; freeze order.    │
└────────────────────────────────────┴───────────────────────────────────┘
```

---

## 21. CONCURRENCY & ORDER SERIALIZATION

1. **Per-Order Actor Serialization:** Order updates, fill notifications, and user cancellation requests targeting the same `client_order_id` are serialized using a reentrant lock (`threading.RLock`) per order. This guarantees that simultaneous cancel and fill callbacks cannot corrupt `cumulative_filled_quantity` or lifecycle status.
2. **Synchronous Core Processing:** The core state machine and fill accounting run synchronously in-memory. Asynchronous worker threads are restricted exclusively to I/O transport boundaries (network HTTP/WebSocket polling).
3. **Zero Shared Mutable State Across Orders:** Each order maintains independent state. High throughput across multiple instruments is achieved via lock striping (order-level locks).

---

## 22. TIMESTAMP SEMANTICS: WALL-CLOCK VS. MONOTONIC TIMESTAMPS

Tradego enforces a strict conceptual and operational separation between **Wall-Clock Datetimes** and **Monotonic Integer Timestamps**:
1. **Wall-Clock Datetimes (`datetime`):** Used exclusively for chronological sequencing, audit logging, post-trade reconstruction, regulatory compliance, and exchange calendar validation. Because wall-clock datetimes are vulnerable to NTP clock adjustments, daylight saving transitions, host system clock slewing, and leap seconds, they **MUST NEVER** be used to calculate microsecond durations or latency metrics.
   - `exchange_timestamp: datetime` — Exchange venue matching wall-clock time (UTC or exchange-local timezone).
   - `local_received_timestamp: datetime` — Local host wall-clock time (UTC) recorded upon packet arrival.
2. **Monotonic Integer Timestamps (`int` via `time.perf_counter_ns()`):** Used exclusively for all latency benchmarks, processing stage durations, network round-trip times (RTT), and rate-limiter token bucket tracking. Monotonic integer nanoseconds never jump backwards, drift with NTP, or adjust with system time changes.
   - `local_receive_monotonic_ns: int` — Monotonic integer timestamp recorded via `time.perf_counter_ns()` immediately upon byte/packet arrival.

### Ten-Stage Granular Execution Journey:

```
T1. Market Event Tick (Exchange matching timestamp: exchange_timestamp datetime)
T2. Signal Generated (Strategy evaluation: local_datetime + perf_counter_ns integer)
T3. Risk Evaluation (Risk evaluation: local_datetime + perf_counter_ns integer)
T4. ApprovedTradeIntent Generated (Risk authorization: local_datetime + perf_counter_ns integer)
T5. OrderRequest Created (Execution planning: creation_timestamp datetime + perf_counter_ns integer)
T6. Order Submission Start (Pre-wire dispatch: perf_counter_ns integer)
T7. Broker Wire Dispatched (Network egress: perf_counter_ns integer)
T8. Broker Acknowledgement Received (local_received_timestamp datetime + local_receive_monotonic_ns integer)
T9. Fill Execution Timestamp (Exchange trade matching: exchange_timestamp datetime)
T10. Position Updated Timestamp (Portfolio ledger update: local_datetime + perf_counter_ns integer)
```

---

## 23. LATENCY MEASUREMENT MODEL & PERFORMANCE PHILOSOPHY

### 23.1 Optimization Targets vs. Acceptance Gates
Following the governance precedent established in Phase 6, all microsecond performance numbers are **architectural optimization targets**, NOT rigid failure gates or automatic acceptance hurdles. CPython execution is subject to interpreter bytecode dispatch, memory management, and OS thread scheduling; benchmarks document performance profiles across realistic workloads.

### 23.2 Telemetry Instrumentation via Monotonic Clocks (`perf_counter_ns`)

**Mandatory Rule:** All duration and latency calculations MUST use `time.perf_counter_ns()` integer nanoseconds. Wall-clock `datetime` objects are strictly for chronology/audit only.
- **Internal Planning Latency:** $\Delta t_{plan} = (T_{5, mono} - T_{4, mono}) / 1000.0\text{ \mu s}$.
- **Network Round-Trip Time (RTT):** $\Delta t_{RTT} = (T_{8, mono} - T_{6, mono}) / 1000.0\text{ \mu s}$.
- **Router Dispatch Latency:** $\Delta t_{dispatch} = (T_{6, mono} - T_{5, mono}) / 1000.0\text{ \mu s}$.
- **Fill Processing Latency:** $\Delta t_{fill} = (T_{10, mono} - T_{fill\_rcv, mono}) / 1000.0\text{ \mu s}$ (from packet arrival monotonic integer to portfolio position update).
- **Exchange Latency:** Tracked only when exchange timestamps are provided by venue feeds; never synthesized or inferred from local clock deltas.

---

## 24. HOT-PATH REQUIREMENTS & PROHIBITIONS

The execution hot path encompasses all code executed between `ApprovedTradeIntent` ingestion and order wire dispatch, and between fill arrival and position update.

### Strictly Prohibited on Hot Path:
- LLM or AI inference calls.
- Blocking disk file I/O or synchronous logging to disk.
- Database queries or ORM transactions.
- Redis / Kafka / network IPC message passing.
- Unbounded memory allocations or large JSON serialization cycles.

### Permitted on Hot Path:
- In-memory slotted object instantiation.
- CPU arithmetic, integer math, and enum comparisons.
- Thread-safe dictionary lookups.
- Asynchronous bounded queue push for audit logging (non-blocking, e.g. `put_nowait`).

---

## 25. AUDITABILITY & TRACEABILITY

Every trade execution must be 100% reconstructible post-trade:
$$\text{Strategy} \to \text{SignalCandidate} \to \text{ApprovedTradeIntent} \to \text{OrderRequest} \to \text{BrokerOrder} \to \text{Fills} \to \text{PositionSnapshot}$$

Each emitted event carries:
- `client_order_id`, `intent_id`, `signal_id`, `fingerprint`.
- `strategy_id`, `strategy_version`, `risk_config_hash`.
- Microsecond monotonic durations for each processing phase.
- Exact broker rejection codes or execution error strings.

---

## 26. REFINED PAPER / LIVE PARITY SPECIFICATION

Tradego enforces strict contract parity while explicitly documenting unavoidable physical simulation divergences:

### 26.1 Strict Contractual Parity
- **Identical Domain Models:** Both modes consume canonical `OrderRequest` and emit canonical `Fill` and `OrderAcknowledgement` events.
- **Identical State Machine:** Both modes enforce the identical 14-state lifecycle machine and legal transition rules.
- **Identical Fill Math:** Position accounting, remaining quantity math, and volume-weighted average fill calculations are 100% identical.

### 26.2 Documented Simulation Divergences
- **Queue Priority:** Paper trading simulates execution at current top-of-book prices; it cannot model queue position priority inside the exchange matching engine.
- **Market Impact:** Paper trading assumes liquidity is available at quoted bid/ask; it cannot simulate price displacement caused by large order sizes affecting other market participants.
- **Latency & Slippage:** Paper execution uses monotonic timers with simulated slippage models (bps); live execution experiences real internet latency (5–50 ms) and real exchange slippage.

---

## 27. INSTRUMENT & ORDER-TYPE SCOPE

### 27.1 Instrument Scope (Phase 7 v1)
- **Supported:** Indian Cash Equities (`InstrumentType.EQUITY` on `Exchange.NSE` and `Exchange.BSE`).
- **Prohibited:** Futures, Options, Commodities, and Currencies remain strictly unsupported in v1 (consistent with Phase 6). Any order request for a derivative instrument must be rejected immediately.

### 27.2 Order-Type Scope (Phase 7 v1)
- **`MARKET` Orders:** Supported for immediate execution in liquid cash equities.
- **`LIMIT` Orders:** Supported with explicit price boundaries for passive execution.
- **Stop / Stop-Limit / Iceberg:** Out of scope for Phase 7 v1; reserved for future algorithmic order managers.

---

## 28. CONFIGURATION & VERSIONING

Phase 7 parameters reside in an immutable, slotted `ExecutionConfig` dataclass:

```python
@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    config_version: str = "1.1.0"
    execution_mode: str = "PAPER"        # "PAPER" or "LIVE"
    default_time_in_force: TimeInForce = TimeInForce.DAY
    max_order_notional: float = 200000.0 # Configurable live notional cap per order (₹2 Lakhs default)
    max_submission_retries: int = 2
    retry_base_delay_ms: float = 50.0
    submission_timeout_seconds: float = 5.0
    rate_limit_max_queue_depth: int = 50
    rate_limit_max_wait_ms: float = 500.0
    paper_base_slippage_bps: float = 2.0
    enable_reconciliation_on_start: bool = True
    reconciliation_grace_period_seconds: float = 5.0
```

---

## 29. LIVE SAFETY GATES

To safeguard real capital, live execution requires passing a formal promotion lifecycle:

```
[DEV / BACKTEST] ──► [PAPER EXECUTION] ──► [SHADOW MODE] ──► [LIVE EXECUTION]
```

### Mandatory Live Activation Prerequisites:
1. **Explicit Environment Promotion:** `execution_mode == "LIVE"` must be configured explicitly; never defaulted.
2. **Account Whitelisting:** Account ID must match configured live production account.
3. **Configurable Exposure Ceiling:** Hard stop rejecting any single live order exceeding `config.max_order_notional`.
4. **Adapter Health Verification:** Live execution is immediately vetoed if `adapter.health_check()` fails or WebSocket feeds are desynchronized.
5. **Kill-Switch Enforcement:** Immediate manual disable flag halting all live order submission.

---

## 30. FORMAL PHASE 7 INVARIANTS

The execution engine must strictly enforce 20 formal system invariants:
1. **No Execution Without Intent:** No order can be planned or submitted without a valid `ApprovedTradeIntent`.
2. **Quantity Monotonicity:** An order cannot request a quantity greater than `ApprovedTradeIntent.permitted_quantity`.
3. **Single Order per Intent:** One `ApprovedTradeIntent` cannot create multiple working orders simultaneously.
4. **Stable Client Order ID & Idempotency Mapping:** `client_order_id` is invariant across transport retries; adapters map this identity to broker-specific deduplication mechanisms. If broker deduplication is absent, indeterminate submissions must be reconciled before re-submitting.
5. **Fill Conservation:** The cumulative filled quantity can never exceed requested order quantity ($Q_{filled} \le Q_{req}$) across all order versions.
6. **Duplicate Fill Idempotency:** Duplicate fill events sharing the same `fill_id` are discarded without mutating state.
7. **Terminal Immutability:** Terminal states (`FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`, `FAILED`) can never transition to any other state.
8. **Market Order Notional Safety:** Market order estimated notional strictly evaluates against `reference_price`; `price=None` never evaluates to zero for safety cap calculations.
9. **Timeout is Not Rejection:** An unacknowledged timeout does not imply rejection; it must enter `UNKNOWN`.
10. **Timeout is Not Fill:** An unacknowledged timeout can never be assumed filled.
11. **Positions Driven by Fills Only:** Portfolio positions are updated exclusively by authoritative fills or formal reconciliation (`PositionReconciliationAdjustment`).
12. **Cancel Uncertainty:** A cancel request does not guarantee zero fills; in-flight race fills are authoritative.
13. **Safe Retries Only:** Retries are strictly prohibited when an order submission status is ambiguous (`AMBIGUOUS_UNKNOWN`).
14. **Paper/Live Parity:** Core execution logic is identical across paper and live modes.
15. **Broker Isolation:** Broker-specific formats, status codes, and APIs must never leak into Tradego core models.
16. **Risk Veto Precedence:** Execution can never override or loosen Phase 6 risk approvals.
17. **Side & Effect Invariance:** Exit execution must strictly reduce or close position; an exit can never inadvertently increase position.
18. **Monotonic Latency:** All local latency metrics use monotonic clocks (`perf_counter_ns()`); wall-clock datetimes are strictly for chronology/audit.
19. **Cold Restart Safety:** Restart recovery executes the safe 6-phase startup sequence before accepting orders.
20. **Fail Closed:** Any ambiguous, malformed, or corrupt execution event forces the order into `UNKNOWN` and halts automated entry.

---

## 31. FUTURE PERSISTENCE BOUNDARY & ASYNCHRONOUS AUDIT QUEUE

While Phase 7 core operates strictly in-memory, an asynchronous event emission boundary is established for downstream audit logging and cold recovery:
1. **Asynchronous Emission:** Event logging is decoupled from the execution hot path to prevent I/O latency from degrading order routing.
2. **Bounded Buffer:** The queue enforces a strictly bounded capacity (e.g. `maxsize = 10000`) to prevent unbounded memory growth during disk or database persistence lag.
3. **Non-Blocking Hot-Path Push:** `ExecutionEvent` instances are pushed from the hot path using non-blocking primitives (e.g. `put_nowait()`). If the bounded buffer fills completely due to downstream storage stalls, the engine logs a diagnostic alert or sheds telemetry—it **NEVER blocks, stalls, or delays active order execution**.
4. **Fault Isolation:** Downstream persistence failures (disk exhaustion, SQLite database lock contention, file permission errors, background writer crashes) cannot propagate back to or disrupt the execution hot path.
5. **Decoupled Queue Mechanism:** The concrete queue structure is an infrastructure adapter implementation choice (e.g. `queue.Queue(maxsize=...)` or custom bounded ring-buffer); the architecture does not mandate an inherently "lock-free deque".

---

## 32. ACCEPTANCE TEST PLAN

Phase 7 acceptance will require passing 13 comprehensive test suites:

| Test Suite | Scope & Verification |
|---|---|
| **1. Contract Compatibility** | Verify slotted, frozen immutability of `OrderRequest`, `Fill`, `OrderAcknowledgement`. |
| **2. Side & Effect Mapping Tests** | Verify `derive_order_side_and_position_effect` for Long/Short entries, exits, scale in/out. |
| **3. State Machine Legal Transitions** | Verify all legal transitions across the lifecycle state machine. |
| **4. State Machine Illegal Transitions**| Verify all illegal transitions (e.g. `FILLED` to `CANCELLED`) fail closed. |
| **5. Idempotency & Stable IDs** | Verify `client_order_id` stability across retries and duplicate fill suppression. |
| **6. Partial Fill Accounting** | Verify multi-fill aggregation, weighted average price math, and remainder handling. |
| **7. Cancel & Replace Race Handling** | Verify fill-before-cancel and fill-before-replace race resolution. |
| **8. Failure & Retry Policy** | Verify Tier 1 bounded retries, Tier 2 fatal rejections, and Tier 3 UNKNOWN routing. |
| **9. Paper Adapter Execution** | Verify simulated MARKET and LIMIT fills against `MarketEvent` ticks. |
| **10. Safe Startup Reconciliation** | Verify 6-phase cold startup sequence, UNKNOWN resolution, and orphan detection. |
| **11. Position Update Boundary** | Verify that fills correctly update `PositionSnapshot` while reconciliation emits adjustments. |
| **12. Multi-Threaded Concurrency** | Verify thread safety under 8 concurrent worker threads with per-order locks. |
| **13. Benchmark & Latency Telemetry** | Profile execution planning ($p50 < 10\text{ \mu s}$) and router dispatch ($p50 < 15\text{ \mu s}$). |

---

## 33. PERFORMANCE BENCHMARK PLAN

### Target Optimization Latencies (CPython 3.12 In-Memory)
- **Execution Planning ($T_5 - T_4$):** Optimization target: $p50 < 10.0\text{ \mu s}, \quad p95 < 20.0\text{ \mu s}$
- **Router Dispatch & State Registration ($T_6 - T_5$):** Optimization target: $p50 < 15.0\text{ \mu s}, \quad p95 < 30.0\text{ \mu s}$
- **Fill Processing & Position Delta ($T_{10} - T_9$):** Optimization target: $p50 < 10.0\text{ \mu s}, \quad p95 < 20.0\text{ \mu s}$
- **Memory Footprint:** Optimization target: $< 100\text{ KB}$ per 1,000 active order states.

*Benchmark Philosophy:* These values serve as directional engineering targets for CPU efficiency, not contractual gating tests.

---

## 34. RISKS & OPEN QUESTIONS CLASSIFICATION

All architectural issues and questions are explicitly classified:

### 34.1 BLOCKER
- **None.**

### 34.2 REQUIRED BEFORE IMPLEMENTATION
- **None.** All 15 previous architecture review findings and all 9 second-order issues identified during final re-review have been explicitly and comprehensively resolved.

### 34.3 ACCEPTABLE DESIGN TRADEOFF
1. **In-Memory Registry for v1:** Phase 7 v1 relies on in-memory order registries with cold-restart broker reconciliation rather than a distributed write-ahead log (WAL). This provides sub-15 µs performance while meeting recovery needs.

### 34.4 FUTURE EXTENSION
1. **Derivatives Execution (Futures/Options):** Extending `OrderRequest` to support derivative contracts once Phase 6 introduces SPAN margin calculators.
2. **Live Broker Integration:** Implementing concrete broker execution adapters (e.g. DhanHQ, Zerodha, or institutional FIX gateways) in future execution milestones.
3. **Advanced Algorithmic Order Types:** TWAP, VWAP, and Iceberg parent-child execution managers.

### 34.5 OPEN QUESTIONS
- **None.**

---

## 35. ARCHITECTURE DECISIONS (ADRs)

- **ADR 7.1: Stable Canonical Identity & Adapter Idempotency Responsibility:** Tradego Core guarantees stable `client_order_id` across retries; adapters map this identity to broker-specific deduplication mechanisms. If broker deduplication is absent, indeterminate submissions must be reconciled before re-submitting.
- **ADR 7.2: Decoupled Order Side & Explicit Position Effect:** Canonical `OrderSide` and `PositionEffect` (`OPEN`, `CLOSE`, `INCREASE`, `REDUCE`) are derived via pure functional mapping from `SignalType` and `direction`.
- **ADR 7.3: Four-Tier Fill Identity Hierarchy & Ambiguous Live Reconciliation:** Fills are identified via a strict 4-tier hierarchy. Live fills lacking both exchange and broker IDs never invent synthetic IDs; they enter reconciliation.
- **ADR 7.4: Separation of Fills from Position Reconciliation:** Fills drive continuous transactional ledger updates; `PositionReconciliationAdjustment` drives periodic state synchronization without manufacturing fake trades.
- **ADR 7.5: Adapter Ownership of Rate Limiting:** Rate limiting is managed inside `BrokerExecutionAdapter` using a token bucket with bounded buffer and fail-fast rejection.
- **ADR 7.6: Indeterminate Submissions Enter UNKNOWN:** Network timeouts during submission never trigger blind retries; they transition to `UNKNOWN` and enter reconciliation.
- **ADR 7.7: Fills as Sole Position Authority:** Order submissions and acknowledgements never alter portfolio positions; only confirmed fills adjust positions.
- **ADR 7.8: Canonical Reference-Price Policy for Market Order Notional Safety:** Market orders strictly evaluate estimated notional against `OrderRequest.reference_price` sourced from `ApprovedTradeIntent.approved_entry_price`. `price=None` never evaluates to zero for safety calculations.
- **ADR 7.9: Typed Submission Outcomes:** Submissions return a typed `SubmissionResult` distinguishing `ACKNOWLEDGED`, `REJECTED`, `RETRYABLE_FAILURE`, and `AMBIGUOUS_UNKNOWN`, mapping deterministically to lifecycle states without blanket exception-to-UNKNOWN collapse.
- **ADR 7.10: Monotonic Integer Clocks for Latency Telemetry:** All duration and latency calculations strictly use `time.perf_counter_ns()` integer nanoseconds; wall-clock `datetime` objects are reserved for chronological audit and compliance only.
- **ADR 7.11: Replacement Identity Hierarchy & Multi-Version Fill Correlation:** Logical orders maintain stable `client_order_id` across replacements while versioning child broker orders (`order_version 1 -> broker_order_id A`, `order_version 2 -> broker_order_id B`), correlating callbacks and race fills to conserve total quantity.
- **ADR 7.12: Exchange-Local Calendar Timezone Conversion:** Trading session validation explicitly converts UTC creation timestamps to exchange-local time owned by `ExchangeCalendar` prior to session evaluation.
- **ADR 7.13: Non-Blocking Bounded Asynchronous Audit Queue:** Persistence boundary requires asynchronous, bounded, non-blocking queue mechanics with complete fault isolation from the hot path.

---

## 36. OUT-OF-SCOPE ITEMS

- Live broker network activation and production credentials.
- Database persistence schemas and SQL migrations.
- Web dashboards and UI execution views.
- Derivative margin calculations.
- Machine learning or LLM-based execution optimization.

---

## 37. FINAL STATUS

```
===============================================================================
PHASE 7 DESIGN FINAL — READY FOR ARCHITECTURE FREEZE
===============================================================================
```
No source code, unit tests, or runtime modifications have been made. Phases 1 through 6 remain 100% frozen and verified.
