# TRADEGO PHASE 6 — TECHNICAL ARCHITECTURE & DESIGN SPECIFICATION
## RISK MANAGEMENT & PORTFOLIO CONTROL LAYER

```
===============================================================================
STATUS: DESIGN REVISED — AWAITING ARCHITECTURE RE-REVIEW
VERSION: 1.2.0
DATE: 2026-09-14
UPSTREAM CONTRACT: Phase 5 Strategy & Signal Intelligence (SignalCandidate) [FROZEN]
DOWNSTREAM CONTRACT: Phase 7 Execution Layer (ApprovedTradeIntent / RiskRejection) [FUTURE]
LOCATION: docs/phase_6_risk_management_design.md
===============================================================================
```

---

## 1. TITLE

**Tradego Phase 6: Risk Management & Portfolio Control Layer — Technical Architecture & Specification.**

---

## 2. STATUS

**DESIGN REVISED — AWAITING ARCHITECTURE RE-REVIEW.**  
No source code, unit test, or runtime modifications are authorized or made in this document.  
Phases 1, 2, 3, 4, and 5 are **OFFICIALLY FROZEN** baselines.

---

## 3. SCOPE

Phase 6 defines the deterministic Risk Management & Portfolio Control boundary within Tradego. It accepts candidate trade signals ([SignalCandidate](file:///d:/msg/Devang/Tradego/services/signals/models.py#L74-L137)) produced by Phase 5, evaluates them against portfolio state, account equity, risk limits, instrument specifications, and market conditions, and emits either:
1. An **[ApprovedTradeIntent](#11-core-data-contracts)** specifying an exact, permitted, broker-independent trade allocation and geometry, OR
2. A **[RiskRejection](#11-core-data-contracts)** detailing the exact, auditable rule failure that vetoed the candidate signal.

### Authoritative Asset Class Scope for Phase 6 v1:
- **Currently Supported:** Indian cash equities only (`InstrumentType.EQUITY` on `Exchange.NSE` or `Exchange.BSE`).
- **NOT Supported in Phase 6 v1:** Futures, Options, and derivative margin calculations.
- **Derivative Prohibition Invariant:** Any derivative instrument (Futures or Options) presented to Phase 6 v1 is strictly rejected as unsupported (`UNSUPPORTED_INSTRUMENT_TYPE`); no derivative trade may be emitted as an `ApprovedTradeIntent` under the cash-equity model.
- **Future Extension:** Futures and Options remain architecturally reserved for future extension through the `CapitalRequirementCalculator` abstraction.

Phase 6 acts as the sole gatekeeper between strategy intelligence (Phase 5) and future order execution (Phase 7).

---

## 4. SOURCE OF TRUTH

The frozen baselines and approved specifications of Tradego constitute the sole source of truth:
1. **Phase 1 — Market Data Gateway:** [services/market_gateway/](file:///d:/msg/Devang/Tradego/services/market_gateway) ([MarketEvent](file:///d:/msg/Devang/Tradego/services/market_gateway/models.py))
2. **Phase 2 — Real-Time Market State:** [services/market_state/](file:///d:/msg/Devang/Tradego/services/market_state) ([InstrumentId](file:///d:/msg/Devang/Tradego/services/market_state/instrument.py#L42-L144), [InstrumentMetadata](file:///d:/msg/Devang/Tradego/services/market_state/instrument.py#L147-L157), [InstrumentStateSnapshot](file:///d:/msg/Devang/Tradego/services/market_state/state.py))
3. **Phase 3 — Candle & Time-Series Aggregation:** [services/candles/](file:///d:/msg/Devang/Tradego/services/candles) ([Candle](file:///d:/msg/Devang/Tradego/services/candles/models.py))
4. **Phase 4 — Quantitative Analytics & Features:** [services/analytics/](file:///d:/msg/Devang/Tradego/services/analytics) ([FeatureSnapshot](file:///d:/msg/Devang/Tradego/services/analytics/models.py), [FeatureQuality](file:///d:/msg/Devang/Tradego/services/analytics/models.py))
5. **Phase 5 — Strategy & Signal Intelligence:** [services/signals/](file:///d:/msg/Devang/Tradego/services/signals) ([SignalCandidate](file:///d:/msg/Devang/Tradego/services/signals/models.py#L74-L137), [PositionView](file:///d:/msg/Devang/Tradego/services/signals/models.py#L45-L71), [TriggerMode](file:///d:/msg/Devang/Tradego/services/signals/models.py#L17-L20))

---

## 5. EXISTING WORKSPACE INSPECTION & UPSTREAM CONTRACT REVIEW

An exhaustive inspection of the Tradego workspace confirms the upstream data structures available to Phase 6:

### Upstream Components
- **`InstrumentId` & `InstrumentMetadata`** ([services/market_state/instrument.py](file:///d:/msg/Devang/Tradego/services/market_state/instrument.py)):
  - Canonical hashable key: `symbol`, `exchange` (NSE, BSE, MCX, NFO, CDS), `instrument_type` (EQUITY, INDEX, FUTURES, OPTIONS), `expiry`, `strike`, `option_type`.
  - Static trading rules: `lot_size: int` (default 1), `tick_size: float` (default 0.05), `price_precision: int` (default 2), `freeze_quantity: Optional[int]`.
- **`SignalCandidate`** ([services/signals/models.py:L74-L137](file:///d:/msg/Devang/Tradego/services/signals/models.py#L74-L137)):
  - Fully frozen, slotted dataclass emitted by Phase 5 strategies.
  - Carries: `signal_id`, `fingerprint` (SHA-256 canonical hash), `reaffirmation_key` (SHA-256 setup anchor hash), `strategy_id`, `strategy_version`, `config_hash`, `instrument_id`, `signal_type` (ENTRY_LONG, ENTRY_SHORT, EXIT_LONG, EXIT_SHORT), `direction` (+1/-1), `trigger_mode` (BAR_CLOSE, INTRABAR_PREVIEW), `confidence_score` (0.0 to 1.0), `suggested_entry_price`, `suggested_stop_loss`, `suggested_take_profit`, `risk_reward_ratio`, `market_timestamp`, `availability_timestamp`, `generated_timestamp`, `expiry_timestamp`, `is_confirmed`.
  - **Zero Execution Parameters:** Prohibited fields (`quantity`, `broker_params`, `order_id`, `account_id`) are 100% absent.
- **`PositionView`** ([services/signals/models.py:L45-L71](file:///d:/msg/Devang/Tradego/services/signals/models.py#L45-L71)):
  - Read-only snapshot of current position provided to Phase 5 strategies.
  - Fields: `net_quantity`, `entry_price`, `entry_time`, `highest_price_since_entry`, `lowest_price_since_entry`, `unrealized_pnl_estimate`.

---

## 6. PHASE 5 INPUT CONTRACT

Phase 6 receives candidate signals strictly via the immutable `SignalCandidate` contract. Every `SignalCandidate` satisfies:
1. $T_{market} \le T_{availability} \le T_{generated}$
2. If $T_{expiry}$ is present: $T_{generated} < T_{expiry}$
3. Normalized conviction score $\in [0.0, 1.0]$
4. Explicit canonical `fingerprint` and `reaffirmation_key`
5. Read-only metadata mapping wrapped in `MappingProxyType`.

Phase 6 never mutates any field of `SignalCandidate`.

---

## 7. PHASE 6 RESPONSIBILITIES

Phase 6 is solely responsible for:
1. **Asset Eligibility Enforcement:** Restricting Phase 6 v1 trade approvals exclusively to Indian cash equities (`InstrumentType.EQUITY` on `Exchange.NSE` or `Exchange.BSE`) and rejecting all derivative contracts (Futures and Options) with `UNSUPPORTED_INSTRUMENT_TYPE`.
2. **Signal Vetting & Causality Check:** Verifying expiry, staleness, and timestamp monotonicity against the evaluation clock.
3. **Aggregated Feature Quality Adherence:** Rejecting signals formed on invalid/stale/warming-up quantitative features based on the upstream quality flag.
4. **Stateful Admission Control & Deduplication:** Using canonical fingerprints and reaffirmation keys to prevent duplicate approvals or accidental pyramiding.
5. **Bifurcated Entry and Exit Evaluation:** Processing new risk commitments through strict sizing and leverage gates, while ensuring risk-reducing exits are never blocked by new-entry circuit breakers.
6. **Position Conflict Resolution:** Reconciling incoming signals against existing positions (e.g. rejecting an `ENTRY_LONG` if currently short without prior exit).
7. **Portfolio & Account Health Checks:** Enforcing daily loss limits, maximum drawdown limits, and maximum concurrent positions on entry signals.
8. **Broker-Independent Position Sizing:** Computing allowed position quantity for cash equities based on monetary risk per trade, stop distance, instrument lot size, and cash capital allocation limits.
9. **Exposure & Leverage Control:** Enforcing instrument-level, strategy-level, and portfolio-level gross/net exposure caps.
10. **Deterministic Decision Emission:** Emitting strictly either an `ApprovedTradeIntent` or a `RiskRejection`.

---

## 8. PHASE 6 NON-RESPONSIBILITIES (OUT OF SCOPE)

Phase 6 explicitly **MUST NOT**:
1. Place, route, modify, or cancel orders with any broker or exchange.
2. Know about broker APIs, tokens, credentials, or sessions.
3. Generate broker-specific order payloads or order IDs.
4. Manage order execution algorithms (TWAP, VWAP, iceberg, smart order routing).
5. Perform fill reconciliation, slippage modeling, or exchange trade confirmation processing.
6. Mutate position state directly (positions are supplied as read-only snapshots from the portfolio accounting authority).
7. Execute asynchronous background tasks, database queries, network calls, or IPC.
8. Utilize LLMs, AI agents, MCP tools, or machine learning runtime inference.
9. Evaluate or approve derivative trades (Futures, Options) under cash notional or exchange margin in Phase 6 v1.
10. Retrieve or calculate broker-specific / exchange SPAN margins for derivatives in Phase 6 v1.

---

## 9. ARCHITECTURE DIAGRAM

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                 TRADEGO PIPELINE                                        │
└─────────────────────────────────────────────────────────────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 1: Market Data Gateway         │
                       └───────────────────────────────────────┘
                                           │ MarketEvent
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 2: Real-Time Market State      │
                       └───────────────────────────────────────┘
                                           │ InstrumentStateSnapshot
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 3: Candle Engine               │
                       └───────────────────────────────────────┘
                                           │ Candle (Closed / Forming)
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 4: Quantitative Analytics      │
                       └───────────────────────────────────────┘
                                           │ FeatureSnapshot
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  Phase 5: Strategy & Signal Intel     │
                       └───────────────────────────────────────┘
                                           │ SignalCandidate
                                           ▼
═════════════════════════════════════════════════════════════════════════════════════════════
                       ┌───────────────────────────────────────┐
                       │  PHASE 6: RISK & PORTFOLIO CONTROL    │
                       ├───────────────────────────────────────┤
                       │  1. Asset Eligibility Validator       │
                       │     (Rejects Derivatives in v1)       │
                       │  2. Signal & Causality Validator      │
                       │  3. Stateful Deduplication Registry   │
                       │  4. BIFURCATED PATH DISPATCHER:       │
                       │     ├─► EXIT / RISK-REDUCING PATH     │
                       │     └─► ENTRY / POSITION-INCREASING   │
                       └───────────────────────────────────────┘
                                  │                 │
              [REJECTED]          │                 │          [APPROVED]
                                  ▼                 ▼
                       ┌────────────────────┐ ┌───────────────────────────┐
                       │   RiskRejection    │ │    ApprovedTradeIntent    │
                       └────────────────────┘ └───────────────────────────┘
                                                        │
════════════════════════════════════════════════════════╪════════════════════════════════════
                                                        ▼
                                       ┌──────────────────────────────────┐
                                       │  Phase 7: Execution Layer        │
                                       │  (Slicing, Algorithmic Routing)  │
                                       └──────────────────────────────────┘
                                                        │
                                                        ▼
                                       ┌──────────────────────────────────┐
                                       │  Phase 8: Broker Integration     │
                                       │  (Order Placement, Fills, APIs)  │
                                       └──────────────────────────────────┘
```

---

## 10. BIFURCATED DATA FLOW: SEPARATE ENTRY AND EXIT PATHS

A central architectural mandate of Phase 6 is that **ENTRY (position-increasing)** and **EXIT (risk-reducing)** candidate signals flow through separate evaluation paths:

```
                                  ┌──────────────────────┐
                                  │   SignalCandidate    │
                                  └──────────┬───────────┘
                                             │
                                             ▼
                             [Step 0: Eligibility, Temporal & Quality]
                             - Asset Class Gate: EQUITY on NSE/BSE only (Derivatives REJECTED)
                             - Expiry & Latency Staleness Gate
                             - Aggregated FeatureQuality Gate
                             - Timestamp Causality Gate
                                             │
                                             ▼
                                  [Path Classification]
                                  Is SignalType an EXIT?
                                    │              │
                   YES (Risk-Reducing)             NO (Position-Increasing)
                           │                               │
                           ▼                               ▼
               ┌────────────────────────┐      ┌────────────────────────┐
               │    EXIT RISK PATH      │      │    ENTRY RISK PATH     │
               ├────────────────────────┤      ├────────────────────────┤
               │ 1. Position Existence  │      │ 1. Deduplication Gate  │
               │    & Direction Check   │      │ 2. Position Conflict   │
               │ 2. Sizing:             │      │ 3. Account Health:     │
               │    Q = min(req, held)  │      │    - Daily Loss Limit  │
               │ 3. Monetary Risk = 0   │      │    - Max Drawdown      │
               │ 4. NEVER BLOCKED BY:   │      │    - Max Positions     │
               │    - Daily Loss        │      │ 4. Stop Geometry:      │
               │    - Drawdown Lockout  │      │    - Inverted/Zero Stop│
               │    - Max Positions     │      │    - Min/Max Stop Bps  │
               │    - Leverage Caps     │      │    - Min R:R Ratio     │
               │    - Cash Bounds       │      │ 5. Position Sizing:    │
               │                        │      │    - Monetary Risk     │
               │                        │      │    - Lot Floor Rounding│
               │                        │      │    - Capital Allocation│
               │                        │      │ 6. Exposure & Leverage:│
               │                        │      │    - Gross Leverage    │
               │                        │      │    - Net Leverage      │
               │                        │      │    - Strategy Budget   │
               └───────────┬────────────┘      └───────────┬────────────┘
                           │                               │
                           └───────────────┬───────────────┘
                                           │
                                           ▼
                                 [RiskDecision Emission]
                                 - ApprovedTradeIntent (if passed)
                                 - RiskRejection (if failed)
```

### 10.1 Fundamental Distinction Between Entry and Exit
The purpose of risk controls is to **prevent the assumption of uncompensated additional risk**, not to trap an existing position in the market. When an account breaches its daily loss limit or drawdown circuit breaker, the system enforces an **Entry Lockout**; existing positions must still be permitted to exit or reduce risk.

---

## 11. CORE DATA CONTRACTS

All contracts are **frozen, slotted dataclasses** decorated with `@dataclass(frozen=True, slots=True)` to ensure strict immutability, memory efficiency, and deterministic hashing.

### 11.1 `RiskDecisionType` (Enum)
```python
class RiskDecisionType(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
```

### 11.2 `RiskRejectionReason` (Enum)
```python
class RiskRejectionReason(str, Enum):
    # Asset Class & Eligibility (Common)
    UNSUPPORTED_INSTRUMENT_TYPE = "UNSUPPORTED_INSTRUMENT_TYPE"

    # Temporal & Signal Validity (Common)
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SIGNAL_STALE = "SIGNAL_STALE"
    SIGNAL_FUTURE_DATED = "SIGNAL_FUTURE_DATED"
    INVALID_TIMESTAMPS = "INVALID_TIMESTAMPS"
    DEGRADED_FEATURE_QUALITY = "DEGRADED_FEATURE_QUALITY"
    
    # Geometry & Stop Loss (Entry Path Only)
    INVALID_STOP_DIRECTION = "INVALID_STOP_DIRECTION"
    ZERO_STOP_DISTANCE = "ZERO_STOP_DISTANCE"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    INSUFFICIENT_RISK_REWARD = "INSUFFICIENT_RISK_REWARD"
    
    # Account & Portfolio Constraints (Entry Path Only)
    MISSING_PORTFOLIO_STATE = "MISSING_PORTFOLIO_STATE"
    ACCOUNT_EQUITY_NON_POSITIVE = "ACCOUNT_EQUITY_NON_POSITIVE"
    MAX_DAILY_LOSS_EXCEEDED = "MAX_DAILY_LOSS_EXCEEDED"
    MAX_DRAWDOWN_EXCEEDED = "MAX_DRAWDOWN_EXCEEDED"
    MAX_CONCURRENT_POSITIONS_REACHED = "MAX_CONCURRENT_POSITIONS_REACHED"
    GROSS_LEVERAGE_EXCEEDED = "GROSS_LEVERAGE_EXCEEDED"
    NET_LEVERAGE_EXCEEDED = "NET_LEVERAGE_EXCEEDED"
    INSTRUMENT_EXPOSURE_EXCEEDED = "INSTRUMENT_EXPOSURE_EXCEEDED"
    STRATEGY_RISK_BUDGET_EXCEEDED = "STRATEGY_RISK_BUDGET_EXCEEDED"
    
    # Position Conflict & Deduplication (Entry Path Only)
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    CONFLICTING_POSITION = "CONFLICTING_POSITION"
    MAX_PYRAMIDING_EXCEEDED = "MAX_PYRAMIDING_EXCEEDED"
    
    # Sizing & Capital (Entry Path Only)
    INSUFFICIENT_CAPITAL_FOR_MIN_LOT = "INSUFFICIENT_CAPITAL_FOR_MIN_LOT"
    INSUFFICIENT_AVAILABLE_CASH = "INSUFFICIENT_AVAILABLE_CASH"
    ZERO_PERMITTED_QUANTITY = "ZERO_PERMITTED_QUANTITY"

    # Exit Path Specific
    NO_POSITION_TO_EXIT = "NO_POSITION_TO_EXIT"
    OPPOSING_EXIT_DIRECTION = "OPPOSING_EXIT_DIRECTION"
    ZERO_EXIT_QUANTITY = "ZERO_EXIT_QUANTITY"
```

### 11.3 `PositionSnapshot`
Immutable view of an instrument's open position at a point in time:
```python
@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    instrument_id: InstrumentId
    net_quantity: int                    # Positive = Long, Negative = Short, 0 = Flat
    average_entry_price: float           # Average entry fill price
    current_market_price: float          # Mark price (LTP or midpoint)
    unrealized_pnl: float                # (current_price - entry_price) * net_qty
    realized_pnl: float                  # Cumulative closed PnL for this position
    opened_timestamp: datetime           # Timestamp when position was initiated
    last_updated_timestamp: datetime    # Timestamp of latest fill or mark update
    strategy_id: Optional[str] = None    # Strategy attribution (if known)

    @property
    def is_flat(self) -> bool:
        return self.net_quantity == 0

    @property
    def is_long(self) -> bool:
        return self.net_quantity > 0

    @property
    def is_short(self) -> bool:
        return self.net_quantity < 0

    @property
    def market_value(self) -> float:
        """Gross monetary cash-equity value of position."""
        return abs(self.net_quantity) * self.current_market_price

    @property
    def directional_exposure(self) -> float:
        """Signed monetary cash-equity exposure (+ for long, - for short)."""
        return self.net_quantity * self.current_market_price
```

### 11.4 `PortfolioSnapshot`
Aggregate point-in-time view of all open positions:
```python
@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    snapshot_timestamp: datetime
    positions: Mapping[InstrumentId, PositionSnapshot]  # Wrapped in MappingProxyType

    def __post_init__(self) -> None:
        if isinstance(self.positions, dict):
            object.__setattr__(self, "positions", MappingProxyType(self.positions))

    @property
    def open_positions_count(self) -> int:
        return sum(1 for pos in self.positions.values() if not pos.is_flat)

    @property
    def total_gross_exposure(self) -> float:
        return sum(pos.market_value for pos in self.positions.values())

    @property
    def total_net_exposure(self) -> float:
        return sum(pos.directional_exposure for pos in self.positions.values())

    def get_position(self, instrument_id: InstrumentId) -> Optional[PositionSnapshot]:
        return self.positions.get(instrument_id)

    def get_strategy_exposure(self, strategy_id: str) -> float:
        return sum(
            pos.market_value
            for pos in self.positions.values()
            if pos.strategy_id == strategy_id and not pos.is_flat
        )
```

### 11.5 `AccountRiskState`
Point-in-time snapshot of account balances and equity metrics:
```python
@dataclass(frozen=True, slots=True)
class AccountRiskState:
    account_id: str
    currency: str                        # Base currency (e.g. "INR")
    total_equity: float                  # NAV: cash + unrealized PnL of open positions
    available_cash: float                # Free uncommitted cash
    peak_equity: float                   # High water mark (for drawdown evaluation)
    realized_pnl_today: float            # Closed PnL accumulated today
    unrealized_pnl_current: float        # Mark-to-market PnL of all open positions
    last_updated_timestamp: datetime

    @property
    def total_pnl_today(self) -> float:
        return self.realized_pnl_today + self.unrealized_pnl_current

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0.0:
            return 0.0
        return max(0.0, (self.peak_equity - self.total_equity) / self.peak_equity)
```

### 11.6 `RiskLimits`
Immutable, versioned risk parameter set:
```python
@dataclass(frozen=True, slots=True)
class RiskLimits:
    config_version: str                                  # e.g. "1.0.0"
    config_hash: str                                     # SHA-256 canonical hash
    max_risk_per_trade_pct: float = 0.01                 # 1% max account-level risk per trade
    max_risk_per_trade_absolute: Optional[float] = None  # Optional absolute monetary cap
    max_capital_allocation_per_trade_pct: float = 0.20   # 20% max capital allocation per trade
    max_gross_leverage: float = 2.0                      # Max total gross exposure / equity
    max_net_leverage: float = 1.0                        # Max total net exposure / equity
    max_concurrent_positions: int = 10                   # Max open positions
    max_daily_loss_pct: float = 0.03                     # 3% max daily loss limit
    max_drawdown_pct: float = 0.10                       # 10% peak-to-trough limit
    max_instrument_exposure_pct: float = 0.25            # 25% max exposure per instrument
    min_risk_reward_ratio: float = 1.5                   # Required geometry R:R (entry only)
    min_stop_distance_bps: float = 10.0                  # 10 bps minimum stop distance
    max_stop_distance_bps: float = 500.0                 # 500 bps (5%) maximum stop distance
    max_signal_age_seconds: float = 60.0                 # Max age for BAR_CLOSE signals
    allow_degraded_features: bool = False                # Strictly reject DEGRADED by default
    strategy_budgets_pct: Optional[Mapping[str, float]] = None # Strategy-specific risk budget fraction

    def __post_init__(self) -> None:
        if self.strategy_budgets_pct is not None and isinstance(self.strategy_budgets_pct, dict):
            object.__setattr__(self, "strategy_budgets_pct", MappingProxyType(self.strategy_budgets_pct))
```

### 11.7 `ApprovedTradeIntent` (Semantic Equivalence vs Runtime Identity)
The authoritative handoff contract emitted to Phase 7:
```python
@dataclass(frozen=True, slots=True)
class ApprovedTradeIntent:
    # 1. Non-Semantic Runtime Identifiers (Excluded from Deterministic Equivalence)
    intent_id: str                       # UUIDv4 runtime instance token
    intent_generated_timestamp: datetime # System timestamp when intent was instantiated

    # 2. Semantic Provenance & Identification
    signal_id: str                       # Upstream SignalCandidate.signal_id
    fingerprint: str                     # SignalCandidate.fingerprint
    reaffirmation_key: str               # SignalCandidate.reaffirmation_key
    strategy_id: str
    strategy_version: str
    risk_config_version: str
    risk_config_hash: str
    instrument_id: InstrumentId

    # 3. Approved Trading Intent & Geometry
    signal_type: SignalType
    direction: int                       # +1 Long, -1 Short
    permitted_quantity: int              # Strictly > 0, multiple of lot_size
    approved_entry_price: float
    approved_stop_loss: Optional[float]  # None for unconstrained market exits
    approved_take_profit: Optional[float]
    risk_reward_ratio: Optional[float]
    calculated_monetary_risk: float      # 0.0 for risk-reducing exits
    allocated_capital: float             # 0.0 for exits
    binding_constraint: str              # Rule that bound position size

    # 4. Temporal Horizons & Diagnostics
    market_timestamp: datetime           # From upstream signal
    signal_generated_timestamp: datetime # From upstream signal
    expiry_timestamp: Optional[datetime] # Intent execution deadline
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.permitted_quantity <= 0:
            raise ValueError(f"ApprovedTradeIntent permitted_quantity must be positive, got {self.permitted_quantity}")
        if self.metadata is not None and isinstance(self.metadata, dict):
            object.__setattr__(self, "metadata", MappingProxyType(self.metadata))

    def is_semantically_equivalent(self, other: "ApprovedTradeIntent") -> bool:
        """
        Evaluates deterministic mathematical equivalence.
        Explicitly excludes runtime instance fields (intent_id, intent_generated_timestamp).
        """
        if not isinstance(other, ApprovedTradeIntent):
            return False
        return (
            self.signal_id == other.signal_id
            and self.fingerprint == other.fingerprint
            and self.reaffirmation_key == other.reaffirmation_key
            and self.strategy_id == other.strategy_id
            and self.strategy_version == other.strategy_version
            and self.risk_config_version == other.risk_config_version
            and self.risk_config_hash == other.risk_config_hash
            and self.instrument_id == other.instrument_id
            and self.signal_type == other.signal_type
            and self.direction == other.direction
            and self.permitted_quantity == other.permitted_quantity
            and self.approved_entry_price == other.approved_entry_price
            and self.approved_stop_loss == other.approved_stop_loss
            and self.approved_take_profit == other.approved_take_profit
            and self.risk_reward_ratio == other.risk_reward_ratio
            and self.calculated_monetary_risk == other.calculated_monetary_risk
            and self.allocated_capital == other.allocated_capital
            and self.binding_constraint == other.binding_constraint
            and self.market_timestamp == other.market_timestamp
            and self.signal_generated_timestamp == other.signal_generated_timestamp
            and self.expiry_timestamp == other.expiry_timestamp
        )
```

### 11.8 `RiskRejection`
The authoritative rejection audit object:
```python
@dataclass(frozen=True, slots=True)
class RiskRejection:
    signal_id: str
    fingerprint: str
    strategy_id: str
    instrument_id: InstrumentId
    reason_code: RiskRejectionReason
    violating_rule: str
    message: str
    evaluation_timestamp: datetime
    diagnostic_data: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.diagnostic_data is not None and isinstance(self.diagnostic_data, dict):
            object.__setattr__(self, "diagnostic_data", MappingProxyType(self.diagnostic_data))
```

### 11.9 `RiskDecision`
The complete decision envelope returned by `RiskEngine.evaluate()`:
```python
@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision: RiskDecisionType
    approved_intent: Optional[ApprovedTradeIntent] = None
    rejection: Optional[RiskRejection] = None

    def __post_init__(self) -> None:
        if self.decision == RiskDecisionType.APPROVED and self.approved_intent is None:
            raise ValueError("RiskDecision.APPROVED must include an approved_intent.")
        if self.decision == RiskDecisionType.REJECTED and self.rejection is None:
            raise ValueError("RiskDecision.REJECTED must include a rejection.")
```

---

## 12. RISK CONTEXT & PORTFOLIO SNAPSHOT CONTRACT

The `RiskContext` bundles all environmental, portfolio, and account state necessary for risk evaluation:

```python
@dataclass(frozen=True, slots=True)
class RiskContext:
    account_state: AccountRiskState
    portfolio_snapshot: PortfolioSnapshot # REQUIRED: Non-optional
    instrument_metadata: InstrumentMetadata
    evaluation_timestamp: datetime       # Explicit clock supplied by caller
    current_market_price: float          # Fresh mark price (LTP or quote midpoint)
    feature_quality: FeatureQuality = FeatureQuality.VALID # Aggregated upstream quality flag

    def __post_init__(self) -> None:
        if self.portfolio_snapshot is None:
            raise ValueError("RiskContext requires a valid, non-null PortfolioSnapshot.")
        if self.account_state is None:
            raise ValueError("RiskContext requires a valid, non-null AccountRiskState.")
```

### Resolution of `MISSING_PORTFOLIO_STATE`
1. `portfolio_snapshot: PortfolioSnapshot` is **strictly non-optional** in `RiskContext`.
2. `RiskRejectionReason.MISSING_PORTFOLIO_STATE` serves as a **caller guardrail**: If an external caller attempts to invoke the risk engine without being able to supply a valid `PortfolioSnapshot` or `RiskContext`, the engine immediately short-circuits and emits a `RiskRejection(reason_code=RiskRejectionReason.MISSING_PORTFOLIO_STATE)`.
3. The type contract of `RiskContext` is never weakened to permit `None`.

### Aggregated Feature Quality Contract
`feature_quality: FeatureQuality` represents the **aggregated quality score** of the upstream quantitative features that triggered the candidate signal. Phase 6 does not inspect individual Phase 4 indicator objects; it relies on the aggregated flag passed from the strategy evaluation context.

---

## 13. CAPITAL MODEL & DERIVATIVE MARGIN BOUNDARY

### 13.1 Phase 6 v1 Cash-Equity Capital Model
In Phase 6 v1, committed capital is defined under pure **Cash-Equity Semantics** (representing 100% unleveraged cash notional):
$$Capital_{cash\_equity} = \text{permitted\_quantity} \times \text{approved\_entry\_price}$$

This capital model applies strictly and exclusively to Indian Cash Equities (`InstrumentType.EQUITY` on `Exchange.NSE` or `Exchange.BSE`).

### 13.2 Derivative Boundary & Prohibition Invariant (Futures & Options)
For derivative instruments (Futures and Options), the simple formula $\text{quantity} \times \text{entry\_price}$ **is not a complete broker/exchange margin model**. Derivatives require complex SPAN margins, exposure margins, delivery margins, and options premium turnover rules.

Therefore, the authoritative Phase 6 v1 rules are established as follows:
1. **Currently Supported in Phase 6 v1:** Indian cash equities only (`Exchange.NSE` / `Exchange.BSE`, `InstrumentType.EQUITY`).
2. **NOT Supported in Phase 6 v1:** Futures, Options, and any derivative margin calculations.
3. **Derivative Prohibition Invariant:** Do NOT allow a derivative instrument to pass Phase 6 v1 merely by applying $\text{quantity} \times \text{entry\_price}$ as cash capital. Any candidate signal for Futures or Options presented to Phase 6 v1 is strictly rejected as unsupported (`UNSUPPORTED_INSTRUMENT_TYPE`); no derivative trade may be emitted as an `ApprovedTradeIntent` under the cash-equity model.
4. **Future Extension Abstraction:**
   ```python
   class CapitalRequirementCalculator(ABC):
       """Abstract calculator for position capital or margin requirements."""
       @abstractmethod
       def calculate_required_capital(
           self, instrument_metadata: InstrumentMetadata, entry_price: float, quantity: int
       ) -> float: ...
   ```
   Phase 6 v1 defaults to `CashNotionalCapitalCalculator`. Exchange SPAN files, broker margin APIs, and portfolio margin engines belong to future execution/broker phases and are **EXPLICITLY OUT OF SCOPE** for Phase 6 v1.

---

## 14. RISK LIMITS TAXONOMY: ENTRY VS EXIT APPLICABILITY

Risk rules apply selectively based on whether the signal increases or reduces risk:

| Rule Domain | Applies to ENTRY | Applies to EXIT | Rationale |
|---|---|---|---|
| **Asset Class & Eligibility** | **YES** | **YES** | **Rejects all derivatives in v1; equities only.** |
| **Signal Expiry & Staleness** | **YES** | **YES** | Outdated signals must never be executed. |
| **Aggregated Feature Quality** | **YES** | **YES** | Signals formed on invalid data are untrusted. |
| **Timestamp Causality** | **YES** | **YES** | Fundamental causal ordering invariant. |
| **Position Existence Check** | NO | **YES** | Exit requires an active opposing position. |
| **Stateful Deduplication** | **YES** | NO | Exits must be allowed to complete. |
| **Position Conflict Check** | **YES** | NO | Rejects entry opposing current position. |
| **Daily Loss Limit** | **YES** | **NO** | **Entry lockout only; exits MUST reduce risk.** |
| **Max Drawdown Circuit Breaker**| **YES** | **NO** | **Entry lockout only; exits MUST reduce risk.** |
| **Max Concurrent Positions** | **YES** | **NO** | Exits reduce, never increase, position count. |
| **Stop Geometry & Bounds** | **YES** | **NO** | Exits close position; no new stop is set. |
| **Minimum Risk/Reward Ratio** | **YES** | **NO** | Exits execute geometry, do not define it. |
| **Discrete Lot Sizing Formula** | **YES** | **NO** | Exit quantity is bound to existing position. |
| **Gross / Net Leverage Caps** | **YES** | **NO** | Exits reduce gross/net leverage. |
| **Strategy Risk Budget Ceiling** | **YES** | **NO** | Exits release, rather than consume, budget. |

---

## 15. POSITION SIZING & RISK MATHEMATICS

### 15.1 Entry Position Sizing Algorithm
Calculates the permitted discrete quantity for position-increasing signals:

#### Step 0: Asset Class & Eligibility Verification
- Require `SignalCandidate.instrument_id.instrument_type == InstrumentType.EQUITY`.
- Require `SignalCandidate.instrument_id.exchange in (Exchange.NSE, Exchange.BSE)`.
- If violated (e.g. `FUTURES`, `OPTIONS`, `COMMODITY`, `CURRENCY`): REJECT immediately with `UNSUPPORTED_INSTRUMENT_TYPE`. Under no circumstances may a derivative trade pass Phase 6 v1 under cash-equity capital sizing.

#### Step 1: Geometry & Stop Distance Validation
- $D_{stop} = |P_{entry} - P_{stop}|$
- For Long: require $P_{stop} < P_{entry}$. For Short: require $P_{stop} > P_{entry}$.
- If violated: REJECT with `INVALID_STOP_DIRECTION`.
- If $D_{stop} \le 0$: REJECT with `ZERO_STOP_DISTANCE`.
- $D_{bps} = \frac{D_{stop}}{P_{entry}} \times 10,000$.
- Require $\text{min\_stop\_distance\_bps} \le D_{bps} \le \text{max\_stop\_distance\_bps}$.
- Require $\text{risk\_reward\_ratio} \ge \text{min\_risk\_reward\_ratio}$.

#### Step 2: Unambiguous Strategy Risk Budget Mathematics
Let:
- $E$ = `AccountRiskState.total_equity`
- $R_{pct}$ = `RiskLimits.max_risk_per_trade_pct`
- $R_{abs}$ = `RiskLimits.max_risk_per_trade_absolute`

The account-level per-trade risk ceiling is:
$$R_{account} = \begin{cases} \min(E \times R_{pct}, R_{abs}), & \text{if } R_{abs} \text{ is set} \\ E \times R_{pct}, & \text{otherwise} \end{cases}$$

**Semantic Meaning of `strategy_budgets_pct`:**
`strategy_budgets_pct[strategy_id]` is the **maximum fraction of the account-level per-trade risk ceiling** allocated to that strategy:
$$R_{strat} = R_{account} \times \text{strategy\_budgets\_pct}[\text{strategy\_id}]$$

The active monetary risk budget for the trade is:
$$R_{budget} = \begin{cases} \min(R_{account}, R_{strat}), & \text{if strategy budget is defined} \\ R_{account}, & \text{otherwise} \end{cases}$$

*Concrete Example:*
- Account Equity $E = \text{₹}1,000,000$
- `max_risk_per_trade_pct` = $0.01$ (1%) $\implies R_{account} = \text{₹}10,000$
- `strategy_budgets_pct["EMA_VWAP_TREND_CONT"]` = $0.40$ (40%)
- Strategy Risk Ceiling: $R_{strat} = \text{₹}10,000 \times 0.40 = \text{₹}4,000$.
- Trade will be sized to risk at most $\text{₹}4,000$ on stop hit.

#### Step 3: Raw Constraint Quantities
$$Q_{risk} = \frac{R_{budget}}{D_{stop}}$$
$$Q_{capital} = \frac{E \times \text{max\_capital\_allocation\_per\_trade\_pct}}{P_{entry}}$$
$$Q_{cash} = \frac{\text{available\_cash}}{P_{entry}}$$
$$Q_{inst} = \frac{(E \times \text{max\_instrument\_exposure\_pct}) - \text{CurrentExposure}}{P_{entry}}$$
$$Q_{raw} = \min(Q_{risk}, Q_{capital}, Q_{cash}, Q_{inst})$$

#### Step 4: Discrete Lot Floor Rounding
Let $L$ = `InstrumentMetadata.lot_size`.
$$Lots = \left\lfloor \frac{Q_{raw}}{L} \right\rfloor$$
- If $Lots \le 0$: REJECT with `INSUFFICIENT_CAPITAL_FOR_MIN_LOT`.
- Permitted quantity: $Q_{permitted} = Lots \times L$.
- If $Q_{freeze}$ is set: $Q_{permitted} = \min(Q_{permitted}, Q_{freeze})$.

#### Step 5: Recalculate Committed Metrics
- $Risk_{actual} = Q_{permitted} \times D_{stop}$
- $Capital_{actual} = Q_{permitted} \times P_{entry}$

---

### 15.2 Exit Sizing Algorithm
For risk-reducing signals (`EXIT_LONG`, `EXIT_SHORT`, `SCALE_OUT`):
1. Verify instrument eligibility (`InstrumentType.EQUITY` on `Exchange.NSE` or `Exchange.BSE`). If violated: REJECT with `UNSUPPORTED_INSTRUMENT_TYPE`.
2. Locate instrument in `PortfolioSnapshot.positions`.
3. If position is absent or `is_flat`: REJECT with `NO_POSITION_TO_EXIT`.
4. Verify directional alignment:
   - `EXIT_LONG` requires `position.is_long` (net_quantity > 0).
   - `EXIT_SHORT` requires `position.is_short` (net_quantity < 0).
   - If opposing: REJECT with `OPPOSING_EXIT_DIRECTION`.
5. Permitted quantity:
   - Full exit: $Q_{permitted} = |\text{position.net\_quantity}|$
   - Partial exit (`SCALE_OUT`): $Q_{permitted} = \min(\text{requested\_quantity}, |\text{position.net\_quantity}|)$
6. Floor round to `lot_size` if applicable. If $Q_{permitted} \le 0$: REJECT with `ZERO_EXIT_QUANTITY`.
7. Set:
   - `calculated_monetary_risk = 0.0`
   - `allocated_capital = 0.0`
   - `approved_stop_loss = None`

---

## 16. EXPOSURE & LEVERAGE MODEL

Aggregate portfolio exposure is tracked across three dimensions:

### 16.1 Gross and Net Leverage
Let:
- $\text{GrossExposure} = \sum_{i} |\text{net\_qty}_i| \times \text{Price}_i$
- $\text{NetExposure} = \sum_{i} \text{net\_qty}_i \times \text{Price}_i$

**Pre-Trade Projected Leverage Gate (Entry Path Only):**
$$\text{ProjectedGrossLeverage} = \frac{\text{GrossExposure} + Capital_{actual}}{E}$$
$$\text{ProjectedNetLeverage} = \frac{|\text{NetExposure} + (Direction \times Capital_{actual})|}{E}$$

- If $\text{ProjectedGrossLeverage} > \text{max\_gross\_leverage}$: REJECT with `GROSS_LEVERAGE_EXCEEDED`.
- If $\text{ProjectedNetLeverage} > \text{max\_net_leverage}$: REJECT with `NET_LEVERAGE_EXCEEDED`.

### 16.2 Instrument Concentration
$$\text{ProjectedInstrumentExposure} = \text{CurrentInstrumentExposure} + Capital_{actual}$$
- If $\frac{\text{ProjectedInstrumentExposure}}{E} > \text{max\_instrument\_exposure\_pct}$: REJECT with `INSTRUMENT_EXPOSURE_EXCEEDED`.

---

## 17. RISK VETO MODEL

### The Fundamental Axiom
$$\mathbf{SignalCandidate} \ne \mathbf{ApprovedTradeIntent}$$

Strategy intelligence emits trade proposals; Risk Management maintains final veto authority. A signal that is valid to a strategy can be rejected by risk control without modifying or invalidating the strategy.

### Rejection Reason Hierarchy (Short-Circuit Order)
1. **Critical Eligibility, Environmental & Temporal Failures:** `UNSUPPORTED_INSTRUMENT_TYPE`, `MISSING_PORTFOLIO_STATE`, `ACCOUNT_EQUITY_NON_POSITIVE`, `SIGNAL_EXPIRED`, `SIGNAL_STALE`, `INVALID_TIMESTAMPS`, `DEGRADED_FEATURE_QUALITY`.
2. **Exit Alignment Failures (Exit Path):** `NO_POSITION_TO_EXIT`, `OPPOSING_EXIT_DIRECTION`, `ZERO_EXIT_QUANTITY`.
3. **Conflict & Deduplication Failures (Entry Path):** `DUPLICATE_SIGNAL`, `CONFLICTING_POSITION`, `MAX_PYRAMIDING_EXCEEDED`.
4. **Macro Account Health Failures (Entry Path):** `MAX_DAILY_LOSS_EXCEEDED`, `MAX_DRAWDOWN_EXCEEDED`, `MAX_CONCURRENT_POSITIONS_REACHED`.
5. **Signal Geometry Failures (Entry Path):** `INVALID_STOP_DIRECTION`, `ZERO_STOP_DISTANCE`, `STOP_TOO_TIGHT`, `STOP_TOO_WIDE`, `INSUFFICIENT_RISK_REWARD`.
6. **Sizing & Exposure Failures (Entry Path):** `INSUFFICIENT_CAPITAL_FOR_MIN_LOT`, `GROSS_LEVERAGE_EXCEEDED`, `NET_LEVERAGE_EXCEEDED`, `INSTRUMENT_EXPOSURE_EXCEEDED`, `STRATEGY_RISK_BUDGET_EXCEEDED`.

---

## 18. SIGNAL EXPIRY & STALENESS

Phase 6 enforces strict temporal boundaries:
1. **Explicit Expiry:** If `SignalCandidate.expiry_timestamp` is set:
   - If $\text{evaluation\_timestamp} \ge \text{expiry\_timestamp}$: REJECT with `SIGNAL_EXPIRED`.
2. **Staleness (Latency) Gate:**
   - $\text{SignalAge} = (\text{evaluation\_timestamp} - \text{SignalCandidate.availability\_timestamp}).\text{total\_seconds()}$
   - If $\text{SignalAge} > \text{RiskLimits.max\_signal_age_seconds}$: REJECT with `SIGNAL_STALE`.
3. **Future-Dated Protection:**
   - If `SignalCandidate.generated\_timestamp > \text{evaluation\_timestamp}$: REJECT with `SIGNAL_FUTURE_DATED`.
4. **Zero Clock Leakage:**
   - All temporal evaluations use `RiskContext.evaluation_timestamp`. No call to `datetime.now()` is permitted in the evaluation pipeline.

---

## 19. DATA QUALITY POLICY

Phase 6 respects Phase 4 `FeatureQuality` semantics via the aggregated `RiskContext.feature_quality` flag:
- `FeatureQuality.VALID`: Permitted for evaluation.
- `FeatureQuality.DEGRADED`:
  - If `RiskLimits.allow_degraded_features == False` (default): REJECT with `DEGRADED_FEATURE_QUALITY`.
  - If `RiskLimits.allow_degraded_features == True`: Sizing proceeds using the already-penalized Phase 5 confidence score.
- `FeatureQuality.WARMING_UP`, `STALE`, `INVALID`: REJECT with `DEGRADED_FEATURE_QUALITY`.

---

## 20. STATEFUL ADMISSION CONTROL & DEDUPLICATION

Phase 6 separates:
1. **Stateless Risk Evaluation:** Pure mathematical evaluation of rules.
2. **Stateful Admission Control:** In-memory registry tracking admitted active intents.

### Deterministic State Inputs:
The stateful admission cache is treated as an **explicit input** to evaluation:
$$\text{Evaluate}(\text{SignalCandidate}, \text{RiskContext}, \text{RiskLimits}, \text{AdmissionState})$$

### Admission Invariants:
1. **First Arrival:** When an entry signal arrives with a unique `fingerprint` and passes all gates $\to$ `APPROVED`, and its `fingerprint` is recorded in `AdmissionState`.
2. **Repeated Arrival:** When an identical signal arrives while its fingerprint is active in `AdmissionState` $\to$ `REJECTED` with `DUPLICATE_SIGNAL`.
3. This state transition is intentional admission control and fully deterministic with respect to the input state.

---

## 21. CONFIGURATION VERSIONING

All risk limits reside in immutable `RiskLimits` instances identified by:
1. `config_version: str` (e.g. "1.0.0")
2. `config_hash: str` (SHA-256 hash of canonical sorted JSON configuration dictionary).

```python
def compute_risk_config_hash(config_dict: Dict[str, Any]) -> str:
    canonical_json = json.dumps(config_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
```

Every emitted `ApprovedTradeIntent` records `risk_config_version` and `risk_config_hash`.

---

## 22. AUDITABILITY

Every risk decision is fully traceable and explainable:
- For approvals: `ApprovedTradeIntent` records the `binding_constraint`, `calculated_monetary_risk`, `allocated_capital`, parent `fingerprint`, and `is_semantically_equivalent()` method.
- For rejections: `RiskRejection` records the `reason_code`, `violating_rule`, human-readable explanation, and `diagnostic_data` snapshot (e.g. current equity, current drawdown, stop distance).

Zero persistence code is added to Phase 6; the contracts themselves are natively auditable.

---

## 23. DETERMINISM & SEMANTIC EQUIVALENCE

### Semantic Equivalence Rule
Given identical:
1. `SignalCandidate`
2. `RiskContext`
3. `RiskLimits`
4. `AdmissionState`

The resulting `ApprovedTradeIntent` objects will be **100% semantically equivalent** across all runs, environments, and replays:
$$\text{intent}_1.\text{is\_semantically\_equivalent}(\text{intent}_2) == \text{True}$$

`intent_id` (UUIDv4) and `intent_generated_timestamp` are explicitly classified as volatile runtime instance tokens and excluded from mathematical equivalence checks.

---

## 24. CONCURRENCY MODEL

1. **Thread Safety:** `RiskEngine` maintains an internal reentrant lock (`threading.RLock`) safeguarding the active intent registry and in-memory deduplication cache.
2. **Synchronous Execution:** Evaluation is strictly synchronous and CPU-bound. No background threads, async tasks, or multiprocessing are introduced.

---

## 25. PERFORMANCE TARGETS

Phase 6 is on the latency-critical path.

| Metric | Target | Rationale |
|---|---|---|
| **Risk Evaluation p50** | $< 15.0 \text{ \mu s}$ | Synchronous in-memory validation and arithmetic. |
| **Risk Evaluation p95** | $< 35.0 \text{ \mu s}$ | Cache lookup and multi-constraint check. |
| **Risk Evaluation p99** | $< 60.0 \text{ \mu s}$ | Tail latency under lock contention. |
| **Memory per Risk Engine** | $< 50.0 \text{ KB}$ | Slotted, frozen objects. |

*(Note: These are architectural optimization targets, not strict CPython hardware guarantees.)*

---

## 26. BACKTEST / LIVE EQUIVALENCE (PARITY)

The risk evaluation contract is identical in Live and Backtest:
- **Live Mode:** `RiskContext` is populated from live state store, live portfolio snapshot, and real-time feed timestamp.
- **Backtest Mode:** `RiskContext` is populated from simulated historical ledger and historical simulation clock.
- `RiskEngine` has zero awareness of whether it runs in live or simulation mode.

---

## 27. FAILURE SAFETY (FAIL-SAFE DEFAULTS)

If any required risk information is missing, invalid, or ambiguous, Phase 6 **FAILS SAFE**:
- Unsupported asset type (Derivatives): REJECT (`UNSUPPORTED_INSTRUMENT_TYPE`).
- Missing `PortfolioSnapshot`: REJECT (`MISSING_PORTFOLIO_STATE`).
- Non-positive equity ($E \le 0$): REJECT (`ACCOUNT_EQUITY_NON_POSITIVE`).
- Inverted stop loss: REJECT (`INVALID_STOP_DIRECTION`).
- Sizing yields $< 1$ lot: REJECT (`INSUFFICIENT_CAPITAL_FOR_MIN_LOT`).
- Stale market price: REJECT (`SIGNAL_STALE`).

Under no circumstances does uncertainty result in trade permission.

---

## 28. MULTI-STRATEGY DESIGN

1. **Strategy Budgets:** `RiskLimits.strategy_budgets_pct` assigns risk allocation fractions to specific strategies.
2. **Independent Budgets:** A strategy cannot consume more than its fractional share $R_{strat} = R_{account} \times \text{strategy\_budgets\_pct}[\text{strategy\_id}]$.
3. **Cross-Strategy Netting:** Opposite signals across different strategies for the same instrument are rejected by the conflict validator.

---

## 29. MULTI-ASSET EXTENSION BOUNDARY

### Authoritative Asset Class Support Matrix:
- **Currently Supported in Phase 6 v1:** Indian Cash Equities only (`Exchange.NSE` and `Exchange.BSE`, `InstrumentType.EQUITY`) using discrete integer share quantities under Cash-Equity capital semantics.
- **NOT Supported in Phase 6 v1:** Futures (NFO/MCX), Options (NFO/BSE), Commodities (MCX), Currencies (CDS), and exchange/broker derivative margin models.
- **Architecturally Reserved for Future Extension:** Futures and Options remain architecturally reserved for future extension via `CapitalRequirementCalculator` abstraction (incorporating SPAN, exposure margin, and options premium turnover in Phase 7/8 or future margin service).
- **Prohibition Invariant:** No derivative contract may pass Phase 6 v1 under cash-equity capital sizing. Any candidate signal for a derivative instrument must be rejected immediately with `UNSUPPORTED_INSTRUMENT_TYPE`.

---

## 30. PHASE 7 (EXECUTION LAYER) HANDOFF CONTRACT

Phase 6 concludes by emitting an `ApprovedTradeIntent`.  
In Phase 6 v1, all emitted `ApprovedTradeIntent` objects represent **cash-equity commitments only**.

| Handled by Phase 6 (Risk) | Handled by Phase 7/8 (Execution & Broker) |
|---|---|
| Permitted integer share quantity (`permitted_quantity`) | Order placement (`Order`, `BrokerOrder`, `OrderRequest`) |
| Approved entry/stop/target geometry | Broker order IDs (`broker_order_id`, `exchange_order_id`) |
| Calculated monetary risk & allocated capital | Order type selection (Market, Limit, SL-Limit, Iceberg) |
| Execution horizon (`expiry_timestamp`) | Execution algorithms (TWAP, VWAP, child order slicing) |
| Binding constraint audit metadata | Order replacement, modification, or cancellation |
| Veto / Rejection of candidate signals | Fill tracking, slippage measurement, exchange trade matching |

Phase 7 subscribes to `ApprovedTradeIntent` and translates it into physical order operations.

---

## 31. PROPOSED MODULE STRUCTURE

The approved directory structure for Phase 6 is under `services/risk/`:

```
services/risk/
    __init__.py              # Public exports (RiskEngine, ApprovedTradeIntent, RiskDecision, etc.)
    models.py                # Frozen dataclasses (RiskDecision, ApprovedTradeIntent, RiskRejection, PositionSnapshot, etc.)
    context.py               # RiskContext and AccountRiskState models
    limits.py                # RiskLimits dataclass and configuration hashing
    position_sizing.py       # Deterministic position sizing calculator
    validators.py            # Bifurcated Entry and Exit risk validators
    engine.py                # Central RiskEngine coordinator
```

---

## 32. TEST ARCHITECTURE

A comprehensive test suite of 22 test domains will be implemented under `tests/unit/`:

```
tests/unit/
    test_risk_models.py                  # Model immutability, slots, semantic equivalence excluding UUID
    test_position_sizing.py              # Mathematical correctness of Steps 0-5 sizing, floor rounding
    test_risk_entry_vs_exit.py           # Verification of bifurcated entry/exit evaluation paths
    test_risk_limits_and_veto.py         # Limit enforcement, daily loss lockout, drawdown lockout
    test_risk_deduplication.py           # Stateful admission control & canonical fingerprint caching
    test_risk_temporal_and_quality.py    # Expiry, staleness, aggregated feature quality flag
    test_risk_engine_concurrency.py      # Thread safety, benchmarks, memory scaling
```

### Mandatory Test Invariants:
1. **Derivative Prohibition Invariant:** Any derivative instrument (Futures or Options) presented to Phase 6 v1 is rejected as unsupported (`UNSUPPORTED_INSTRUMENT_TYPE`); no derivative trade may be emitted as an `ApprovedTradeIntent` under the cash-equity model.
2. **Semantic Equivalence:** Two `ApprovedTradeIntent` instances generated from identical state match on `is_semantically_equivalent()` while having different `intent_id` values.
3. **Exit Resilience:** When daily loss limit is breached or max drawdown is exceeded, entry signals are rejected with `MAX_DAILY_LOSS_EXCEEDED` / `MAX_DRAWDOWN_EXCEEDED`, while exit signals are successfully approved.
4. **Exit Sizing:** Exits do not perform stop-loss distance sizing and return `calculated_monetary_risk = 0.0`.
5. **Strategy Risk Budget:** A strategy configured with 40% budget receives exactly $R_{budget} = R_{account} \times 0.40$.
6. **PortfolioSnapshot Required:** Passing `None` to `RiskContext` raises an explicit error.
7. **Lot Floor Rounding:** Permitted quantities are strictly integer multiples of `lot_size`.

---

## 33. ACCEPTANCE GATES

Phase 6 implementation will be accepted only if:
- [ ] **Gate A:** Frozen baselines (Phases 1–5) are 100% untouched.
- [ ] **Gate B:** Zero broker or execution dependencies introduced.
- [ ] **Gate C:** Zero network, database, or disk I/O in risk evaluation path.
- [ ] **Gate D:** 100% deterministic outputs for identical inputs and admission state.
- [ ] **Gate E:** Complete immutability of all Phase 6 contracts.
- [ ] **Gate F:** Zero look-ahead bias or system clock leakage.
- [ ] **Gate G:** Fail-safe rejection on missing or ambiguous data.
- [ ] **Gate H:** Bifurcated entry/exit evaluation: risk-reducing exits are never blocked by entry circuit breakers.
- [ ] **Gate I:** Runtime identifiers (`intent_id`, `intent_generated_timestamp`) excluded from semantic equivalence.
- [ ] **Gate J:** Strategy risk budget semantics mathematically proven: $R_{strat} = R_{account} \times \text{budget\_pct}$.
- [ ] **Gate K:** Cash-equity capital model applies strictly to Indian cash equities. Any derivative instrument presented to Phase 6 v1 is rejected as unsupported (`UNSUPPORTED_INSTRUMENT_TYPE`); no derivative trade may be `ApprovedTradeIntent` under the cash-equity model. Futures and Options remain architecturally reserved for future extension via `CapitalRequirementCalculator`.
- [ ] **Gate L:** Full test suite passes with 0 failures and 0 errors.
- [ ] **Gate M:** Risk evaluation latency p50 $< 15.0 \text{ \mu s}$.

---

## 34. THREAT & FAILURE ANALYSIS

| Threat / Edge Case | Detection Mechanism | Immediate Decision | Safe Behavior |
|---|---|---|---|
| **Derivative Instrument in Phase 6 v1 (`FUTURES` / `OPTIONS`)** | `instrument_id.instrument_type != InstrumentType.EQUITY` | `RiskDecisionType.REJECTED` | Emits `UNSUPPORTED_INSTRUMENT_TYPE`; prevents misapplying cash-equity capital sizing to leveraged derivatives. |
| **Zero Stop Distance** ($P_{entry} = P_{stop}$) | $|P_{entry} - P_{stop}| \le 0$ | `RiskDecisionType.REJECTED` | Emits `ZERO_STOP_DISTANCE`; prevents divide-by-zero. |
| **Inverted Stop Direction** (Stop > Entry on Long) | $Direction \times (P_{entry} - P_{stop}) \le 0$ | `RiskDecisionType.REJECTED` | Emits `INVALID_STOP_DIRECTION`; prevents inverted risk exposure. |
| **Microscopic Stop Loss** ($D_{bps} < 10$) | $D_{bps} < \text{min\_stop\_distance\_bps}$ | `RiskDecisionType.REJECTED` | Emits `STOP_TOO_TIGHT`; protects against runaway lot sizing on noise. |
| **Negative Equity** ($E \le 0$) | `account_state.total_equity <= 0` | `RiskDecisionType.REJECTED` | Emits `ACCOUNT_EQUITY_NON_POSITIVE`; halts trading immediately. |
| **Drawdown Circuit Breaker Active** | `drawdown_pct >= max_drawdown_pct` | `REJECTED` (Entry) / `APPROVED` (Exit) | Emits `MAX_DRAWDOWN_EXCEEDED` on entries; permits exits to de-risk. |
| **Daily Loss Limit Breached** | `total_pnl_today <= -max_daily_loss` | `REJECTED` (Entry) / `APPROVED` (Exit) | Emits `MAX_DAILY_LOSS_EXCEEDED` on entries; permits exits to de-risk. |
| **Stale Signal Burst** | $\text{SignalAge} > 60\text{s}$ | `RiskDecisionType.REJECTED` | Emits `SIGNAL_STALE`; drops delayed signals. |
| **Opposite Position Open** (Long signal when Short) | `position.is_short` on Long Entry | `RiskDecisionType.REJECTED` | Emits `CONFLICTING_POSITION`; forces explicit exit first. |
| **Duplicate Signal Flood** | Active fingerprint cache match | `RiskDecisionType.REJECTED` | Emits `DUPLICATE_SIGNAL`; prevents accidental pyramiding. |
| **Missing Portfolio Snapshot** | Caller cannot supply `PortfolioSnapshot` | `RiskDecisionType.REJECTED` | Emits `MISSING_PORTFOLIO_STATE`; prevents trading in the dark. |
| **NaN / Infinity in Prices** | `math.isnan(val) or math.isinf(val)` | `RiskDecisionType.REJECTED` | Emits `INVALID_SIGNAL`; avoids numerical corruption. |

---

## 35. EXPLICIT OUT-OF-SCOPE ITEMS

The following are strictly forbidden from Phase 6:
1. Broker order routing or API integration.
2. Order ID generation or execution state tracking.
3. Asynchronous execution engines or message queues (RabbitMQ, Kafka).
4. Relational database or key-value persistence (PostgreSQL, SQLite, Redis).
5. AI/LLM/MCP decision making or copilot tools.
6. Execution algorithms (TWAP, VWAP, Iceberg).
7. Exchange SPAN margin engines or real-time derivative margin calls.

---

## 36. IMPORTANT REVIEW QUESTIONS (EXPLICIT ANSWERS)

1. **What exactly enters Phase 6?**  
   An immutable Phase 5 `SignalCandidate` alongside an immutable `RiskContext` (`AccountRiskState`, non-null `PortfolioSnapshot`, `InstrumentMetadata`, `evaluation_timestamp`, aggregated `feature_quality`), `RiskLimits`, and current `AdmissionState`. Evaluated strictly for Indian cash equities (`InstrumentType.EQUITY` on `Exchange.NSE` / `Exchange.BSE`) in Phase 6 v1; all derivatives are rejected as unsupported.
2. **What exactly leaves Phase 6?**  
   A typed `RiskDecision` containing either an `ApprovedTradeIntent` (if passed) or a `RiskRejection` (if vetoed).
3. **Can Phase 6 ever place an order?**  
   **NO.** Order placement is strictly Phase 7/8.
4. **Can Phase 6 ever modify/cancel an order?**  
   **NO.** Order lifecycle management is strictly Phase 7/8.
5. **Can Phase 6 depend on a broker?**  
   **NO.** Phase 6 is completely broker-agnostic.
6. **Can Phase 6 depend on an LLM or AI agent?**  
   **NO.** Phase 6 is 100% deterministic and locally evaluated.
7. **Can Phase 6 approve a trade if required risk information is missing?**  
   **NO.** It fails safe and rejects immediately.
8. **Can identical inputs produce different risk decisions?**  
   **NO.** Decisions are mathematically deterministic with respect to the input context and admission state.
9. **Can Phase 6 introduce look-ahead?**  
   **NO.** All checks adhere to the caller's explicit `evaluation_timestamp`.
10. **Can Phase 5 SignalCandidate be mutated?**  
    **NO.** `SignalCandidate` is a frozen slotted dataclass.
11. **Can Phase 6 silently override risk limits?**  
    **NO.** Risk limits are hard invariant boundaries.
12. **Is every approval/rejection explainable?**  
    **YES.** Full audit metadata and typed reason codes are provided on every decision.

---

## 37. ARCHITECTURE SUGGESTIONS — NOT IMPLEMENTED

The following concepts were analyzed during inspection but are **NOT** incorporated into Phase 6 to preserve strict architectural boundaries:
1. *Dynamic Volatility-Adjusted Sizing (Kelly Criterion / Volatility Parity):*  
   *Suggestion:* Scale trade risk dynamically using Phase 4 ATR/Historical Volatility.  
   *Disposition:* **NOT IMPLEMENTED.** Fixed fractional sizing is the approved baseline; dynamic sizing should be a strategy concern or Phase 6.1 extension.
2. *Cross-Asset Correlation Matrix:*  
   *Suggestion:* Real-time covariance matrix to penalize simultaneous correlated sector exposure.  
   *Disposition:* **NOT IMPLEMENTED.** Requires complex rolling covariance calculation that belongs in an advanced quantitative portfolio phase.
3. *Intrabar Trailing Stop Ratchet in Risk Layer:*  
   *Suggestion:* Allow Phase 6 to dynamically adjust stop prices intrabar.  
   *Disposition:* **NOT IMPLEMENTED.** In intrabar mode, Phase 5 evaluates signals; Phase 7 manages order state. Phase 6 only approves or rejects intents.

---

## 38. FINAL APPROVAL CHECKLIST

Before authorizing implementation of Phase 6:
- [x] Workspace inspection complete.
- [x] Upstream Phase 5 contracts thoroughly reviewed.
- [x] Cash-equity capital model and derivative prohibition invariant explicitly established (derivatives rejected in Phase 6 v1).
- [x] Separate Entry and Exit risk paths explicitly mapped.
- [x] `intent_id` runtime distinction and semantic equivalence method defined.
- [x] `PortfolioSnapshot` contract verified as required and non-null.
- [x] Aggregated `feature_quality` boundary defined.
- [x] Stateful admission control vs determinism clarified.
- [x] Strategy risk budget ceiling mathematics ($R_{strat} = R_{account} \times \text{budget\_pct}$) proven.
- [x] Zero Phase 1–5 files modified or scheduled for modification.
- [x] Zero dependencies added.
- [x] Fail-safe threat analysis fully mapped.
- [x] All 12 review questions answered with zero ambiguity.
- [x] Design document formatted and saved to `docs/phase_6_risk_management_design.md`.

```
===============================================================================
END OF DESIGN SPECIFICATION — PHASE 6 RISK & PORTFOLIO CONTROL
STATUS: DESIGN REVISED — AWAITING ARCHITECTURE RE-REVIEW
===============================================================================
```
