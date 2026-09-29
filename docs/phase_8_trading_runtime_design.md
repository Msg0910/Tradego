# TRADEGO PHASE 8 ARCHITECTURE DESIGN
## TRADING RUNTIME & SYSTEM ORCHESTRATION

---

### GOVERNANCE & DOCUMENT METADATA
* **Authoritative Document:** `docs/phase_8_trading_runtime_design.md`
* **Version:** `1.2.0-DRAFT`
* **Status:** `PHASE 8 DESIGN — DRAFT / AWAITING ARCHITECTURE REVIEW`
* **Upstream Frozen Phases:**
  - Phase 1 — Market Data Gateway (`services/market_gateway/`, v1.0.0 FROZEN)
  - Phase 2 — Real-Time Market State (`services/market_state/`, v1.0.0 FROZEN)
  - Phase 3 — Candle & Time-Series Aggregation (`services/candles/`, v1.0.0 FROZEN)
  - Phase 4 — Feature & Quantitative Analytics (`services/analytics/`, v1.0.0 FROZEN)
  - Phase 5 — Strategy & Signal Intelligence (`services/signals/`, v1.0.0 FROZEN)
  - Phase 6 — Risk Management & Portfolio Control (`services/risk/`, v1.0.0 FROZEN)
  - Phase 7 — Execution Layer & Order Lifecycle (`services/execution/`, v1.2.0 FROZEN)
* **Governance Rule:**
  Phases 1 through 7 are accepted, verified, and strictly frozen. Zero modifications, refactorings, optimizations, or interface changes are permitted in upstream code. Phase 8 acts exclusively as the system orchestration and coordination layer without altering any frozen component.

---

## 1. EXECUTIVE SUMMARY

The Tradego architecture establishes discrete, highly specialized, and formally frozen trading system layers:
* Ingesting and normalizing live network ticks (**Phase 1**),
* Maintaining per-instrument state and L2 order books (**Phase 2**),
* Aggregating multi-timeframe OHLCV candles and volume profiles (**Phase 3**),
* Computing mathematical indicators and order-flow microstructure features (**Phase 4**),
* Evaluating rule-based quantitative strategies and arbitrating signals (**Phase 5**),
* Enforcing multi-tier capital limits, temporal causality, and position sizing (**Phase 6**), and
* Managing order lifecycle states, idempotency, reference-price safety, and paper execution fills (**Phase 7**).

**Phase 8 (Trading Runtime & System Orchestration)** is the coordinating runtime architecture that binds these seven frozen subsystems into a deterministic, high-throughput, fail-closed automated trading engine. Phase 8 introduces **no new trading logic**, **no new risk rules**, **no new order types**, and **no new market data normalizers**.

Key revisions in Version 1.2:
1. **Monotonic Evaluation Watermark for `BAR_CLOSE`:** Replaced the non-authoritative LRU cache with an authoritative monotonic watermark (`last_evaluated_boundary[strategy_id, instrument_id, timeframe]`). Guaranteed mathematically that repeated deliveries or delayed historical bars can never move the watermark backwards or trigger duplicate evaluations.
2. **Explicit `INTRABAR_PREVIEW` Semantics:** Disentangled evaluation frequency controls ($\Delta t_{\text{eval}} \ge 100\text{ ms}$) from semantic signal identity (`fingerprint`, `reaffirmation_key`) and execution idempotency (`idempotency_key`).
3. **Formal Three-Tier Deduplication Hierarchy:** Clearly separated Scheduler deduplication $\ne$ Signal deduplication $\ne$ Order idempotency across Phases 8, 5, 6, and 7.
4. **Comprehensive Fail-Closed Failure Semantics:** Detailed precise failure containment and state actions for strategy exceptions, risk errors, planning errors, router rejections, and duplicate detections.
5. **Seven Mandatory Concrete Scenarios:** Full technical walkthroughs for duplicate closed bars, late bars, consecutive bars, repeated preview setups, genuinely changed preview setups, risk failures, and duplicate intents.

---

## 2. SCOPE

### 2.1 In Scope
* System-level orchestration of frozen Phase 1–7 components.
* Runtime initialization, configuration loading, component wiring, and verified startup sequencing.
* Synchronous in-memory pipeline coordination across `MarketDataGateway`, `InstrumentStateStore`, `CandleEngine`, `FeatureEngine`, `SignalEngine`, `RiskEngine`, `ExecutionPlanner`, `ExecutionRouter`, `PaperExecutionAdapter`, and `PositionAccounting`.
* Authoritative strategy scheduling consuming Phase 5 `TriggerMode` contracts (`BAR_CLOSE` vs `INTRABAR_PREVIEW`) with monotonic watermark deduplication.
* Signal-to-Risk coordination: packaging `PortfolioSnapshot` and `AccountState` into `RiskContext`, evaluating `RiskDecision`, and pruning rejections.
* Risk-to-Execution coordination: converting `ApprovedTradeIntent` into canonical `OrderRequest` via `ExecutionPlanner` and dispatching via `ExecutionRouter`.
* Execution-to-Accounting feedback: processing `Fill` events, invoking `PositionAccounting.apply_fill()`, and propagating refreshed `PositionSnapshot` instances into risk and strategy contexts.
* Multi-instrument concurrency model respecting per-instrument locks and Phase 7 order locks without global bottlenecking.
* Centralized trading guard and multi-level kill switch (`RUNNING` $\to$ `PAUSED` $\to$ `HALTED`).
* Deterministic manual recovery from `HALTED` state.
* Graceful, fail-safe shutdown sequencing guaranteeing orderly cancellation of working orders and preservation of accounting state.
* Subsystem failure isolation ensuring single-instrument or single-strategy exceptions cannot corrupt unrelated assets or cause runaway order generation.
* Historical replay boundary specification for deterministic backtesting and simulation.
* Non-blocking observability architecture utilizing bounded ring buffers for latency telemetry and audit logging.

### 2.2 Out of Scope
* Modifying, refactoring, or optimizing any code or test in Phases 1 through 7.
* Live broker API integration, live credential management, or wire protocol dispatch (deferred to dedicated live broker adapter phases).
* Persistent database engines (PostgreSQL, SQLite, TimescaleDB, QuestDB) or message brokers (Kafka, RabbitMQ, Redis) on the execution hot path.
* Graphical user interfaces (GUI), web dashboards, or REST/WebSocket external API servers.
* AI, Large Language Model (LLM), or Model Context Protocol (MCP) tooling inside the execution loop.
* Complex algorithmic order slicing (TWAP, VWAP, POV execution algorithms) beyond canonical Phase 7 market/limit orders.
* Distributed multi-node clustering or cross-process IPC.

---

## 3. NON-GOALS

1. **Not a "God Object":** Phase 8 shall not construct a single monolithic `TradingEngine` or `SystemManager` class that embeds business logic. Responsibilities are divided into decoupled, single-responsibility runtime modules.
2. **Not a Strategy Incubator:** Phase 8 does not generate trading ideas, alpha signals, or indicator mathematics. It only invokes frozen Phase 5 strategies.
3. **Not a Risk Formulator:** Phase 8 does not calculate risk budgets, drawdown thresholds, or position sizes. It provides context to the frozen Phase 6 `RiskEngine` and strictly enforces its binary decision.
4. **Not an Order State Machine:** Phase 8 does not track order lifecycle transitions (`SUBMITTED`, `ACKNOWLEDGED`, `FILLED`, etc.). It delegates all order tracking to frozen Phase 7 `ExecutionState` and `ExecutionRouter`.
5. **Not a Position Accounting Engine:** Phase 8 does not implement position lot tracking, FIFO cost-basis math, or PnL formulas. It delegates position mutations exclusively to the static methods of Phase 7 `PositionAccounting`.
6. **Not a Live Trading Release:** Phase 8 v1 is strictly a `PAPER` trading and simulated execution runtime. Live execution capabilities are deliberately locked out.

---

## 4. FROZEN PHASE 1–7 DEPENDENCIES & INTERFACE VERIFICATION

Phase 8 acts as a consumer of the frozen upstream APIs. Every integration touchpoint has been inspected directly in the source code and verified:

| Phase | Package / Module | Frozen Class / Interface | Exact Method Signature & Verified Behavior |
|---|---|---|---|
| **Phase 1** | `services.market_gateway.gateway` | `MarketDataGateway` | `add_listener(listener: Callable[[MarketEvent], None]) -> None`<br>`start(timeout: float) -> None`<br>`stop() -> None`<br>`subscribe(symbol_id: str) -> None` |
| **Phase 1** | `services.market_gateway.models` | `MarketEvent` | Root immutable tick payload driving pipeline progression ($T_1$). |
| **Phase 2** | `services.market_state.store` | `InstrumentStateStore` | `on_market_event(event: MarketEvent) -> None`<br>`add_listener(listener: Callable[[InstrumentStateSnapshot], None]) -> None`<br>`get_snapshot(instrument_id: InstrumentId) -> Optional[InstrumentStateSnapshot]` |
| **Phase 2** | `services.market_state.instrument` | `InstrumentId`, `InstrumentRegistry` | Canonical asset identity, exchange resolution (`resolve(provider, token)`). |
| **Phase 3** | `services.candles.engine` | `CandleEngine` | `on_market_event(event: MarketEvent) -> List[Candle]`<br>`add_candle_listener(callback: Callable[[Candle], None]) -> None`<br>`get_active_candle(instrument_id, timeframe) -> Optional[Candle]` |
| **Phase 3** | `services.candles.calendar` | `ExchangeCalendar`, `IndianMarketCalendar` | Trading session boundaries, holiday schedules, and market hours validation. |
| **Phase 4** | `services.analytics.engine` | `FeatureEngine` | `on_candle_closed(candle: Candle) -> List[FeatureValue]`<br>`on_state_changed(snapshot, event) -> List[FeatureValue]`<br>`get_features(instrument_id) -> Optional[FeatureSnapshot]`<br>`get_active_preview(instrument_id, timeframe) -> Dict[str, FeatureValue]` |
| **Phase 5** | `services.signals.engine` | `SignalEngine`, `DeterministicSignalArbiter` | `evaluate_instrument(instrument_id, trigger_mode, evaluation_timestamp, candle, position) -> List[SignalCandidate]`<br>`on_candle_closed(candle: Candle) -> List[SignalCandidate]`<br>`on_state_changed(snapshot, event) -> List[SignalCandidate]`<br>`add_signal_listener(callback) -> None`<br>`set_position_provider(provider) -> None` |
| **Phase 5** | `services.signals.fingerprint` | Functions | `compute_signal_fingerprint(...) -> str`<br>`compute_reaffirmation_key(...) -> str` |
| **Phase 6** | `services.risk.engine` | `RiskEngine`, `AdmissionState` | `evaluate(signal: SignalCandidate, context: Optional[RiskContext]) -> RiskDecision`<br>`admission_state.is_admitted(fingerprint: str) -> bool` |
| **Phase 6** | `services.risk.models` | `RiskDecision`, `ApprovedTradeIntent`, `PortfolioSnapshot`, `PositionSnapshot` | Immutable contracts emitted/consumed at $T_3 \to T_4$. |
| **Phase 7** | `services.execution.planner` | `ExecutionPlanner` | `plan_order(intent: ApprovedTradeIntent, current_position: Optional[PositionSnapshot], order_type, limit_price, time_in_force, current_time, metadata) -> OrderRequest` |
| **Phase 7** | `services.execution.router` | `ExecutionRouter` | `submit(request: OrderRequest) -> ExecutionState`<br>`cancel(client_order_id: str) -> None`<br>`is_accepting_orders: bool` |
| **Phase 7** | `services.execution.paper_adapter` | `PaperExecutionAdapter` | `on_market_event(event: MarketEvent) -> None`<br>`submit_order(request: OrderRequest) -> SubmissionResult`<br>`register_fill_callback(callback: Callable[[Fill], None]) -> None` |
| **Phase 7** | `services.execution.accounting` | `PositionAccounting` | `apply_fill(current_snapshot: Optional[PositionSnapshot], fill: Fill, strategy_id: Optional[str]) -> PositionSnapshot`<br>`apply_adjustment(current_snapshot, adjustment) -> PositionSnapshot` |

*(INTERFACE ALIGNMENT NOTE: `PositionAccounting.apply_fill` is a static functional method taking the previous snapshot and returning a new snapshot. Phase 8 `PortfolioRuntimeState` holds the authoritative dictionary of active snapshots and invokes this method upon receiving an authoritative `Fill`).*

---

## 5. ARCHITECTURE DIAGRAM

```
======================================================================================================================
                                          TRADEGO TRADING RUNTIME (PHASE 8)
======================================================================================================================

 [ External Market Feed ]
             │
             ▼
 ┌───────────────────────────┐
 │   MarketDataGateway (P1)  │
 └─────────────┬─────────────┘
               │
               ▼  MarketEvent (T1)
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                              RUNTIME COORDINATOR                                                 │
 │                                                                                                                  │
 │   ┌───────────────────────────┐       ┌───────────────────────────┐                                              │
 │   │ InstrumentStateStore (P2) │       │    CandleEngine (P3)      │                                              │
 │   └─────────────┬─────────────┘       └─────────────┬─────────────┘                                              │
 │                 │                                   │                                                            │
 │                 │ Snapshot (Path B)                 │ Closed Candle (Path A)                                     │
 │                 ▼                                   ▼                                                            │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                      FeatureEngine (P4)                       │                                              │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                                   ▼ FeatureSnapshot / Previews                                                   │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │             StrategyScheduler & SignalEngine (P5)             │◄── StrategyContext (w/ PositionView)        │
 │   │    [BAR_CLOSE: Monotonic Watermark | INTRABAR: Rate Limited]   │                                              │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                                   ▼ SignalCandidate (T2)                                                         │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                     TradingGuard / Kill Switch                │───► [DROPPED if HALTED / PAUSED]             │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │ Pass                                                                         │
 │                                   ▼                                                                              │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                       RiskEngine (P6)                         │◄── RiskContext (w/ PortfolioSnapshot)        │
 │   │     [AdmissionState: Deduplicates In-Flight Fingerprints]     │                                              │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                  ┌────────────────┴────────────────┐                                                             │
 │                  ▼                                 ▼                                                             │
 │         [ RiskRejection ]               ApprovedTradeIntent (T3-T4)                                              │
 │            (Audit Log)                             │                                                             │
 │                                                    ▼                                                             │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                    ExecutionPlanner (P7)                      │                                              │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                                   ▼ OrderRequest (T5)                                                            │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                     ExecutionRouter (P7)                      │                                              │
 │   │          [ExecutionStateRegistry: Idempotency Check]          │                                              │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                                   ▼ (T6)                                                                         │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │                  PaperExecutionAdapter (P7)                   │◄── Ingests MarketEvent for Matching          │
 │   └───────────────────────────────┬───────────────────────────────┘                                              │
 │                                   │                                                                              │
 │                                   ▼ Fill (T9)                                                                    │
 │   ┌───────────────────────────────────────────────────────────────┐                                              │
 │   │         PortfolioRuntimeState (P8) / Accounting (P7)          │───► Calls PositionAccounting.apply_fill()    │
 │   │                  [Authoritative View Layer]                   │───► Emits PositionSnapshot (T10)             │
 └───┴───────────────────────────────┬───────────────────────────────┴──────────────────────────────────────────────┘
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │  TelemetryRingBuffer (P8)   │───► Background Telemetry Worker (Zero Hot-Path I/O)
                      └─────────────────────────────┘
```

---

## 6. RUNTIME RESPONSIBILITIES & THREE-TIER DEDUPLICATION

### 6.1 Explicit Layer Responsibilities
To eliminate ambiguity between evaluation timing, signal identity, and order execution, Phase 8 establishes a formal three-tier separation of concerns:

```
+-------------------+-------------------------------------------+-----------------------------------------------+
| Architecture Tier | Component Authority                       | Dedicated Responsibility                      |
+-------------------+-------------------------------------------+-----------------------------------------------+
| TIER 1: SCHEDULING| Phase 8 StrategyScheduler                 | • Evaluation timing & cadence control         |
|                   |                                           | • Monotonic bar watermark deduplication       |
|                   |                                           | • Suppresses redundant evaluations on ticks   |
+-------------------+-------------------------------------------+-----------------------------------------------+
| TIER 2: SIGNAL    | Phase 5 Strategy & SignalEngine           | • Semantic signal identification              |
|                   |                                           | • SignalCandidate.fingerprint (SHA-256)       |
|                   |                                           | • SignalCandidate.reaffirmation_key (SHA-256) |
+-------------------+-------------------------------------------+-----------------------------------------------+
| TIER 3: RISK      | Phase 6 RiskEngine & AdmissionState       | • Capital allocation & causality gate         |
|                   |                                           | • In-flight fingerprint admission check       |
|                   |                                           | • Blocks duplicate intents on active setups   |
+-------------------+-------------------------------------------+-----------------------------------------------+
| TIER 4: EXECUTION | Phase 7 ExecutionPlanner & Router         | • Order identity (client_order_id)            |
|                   |                                           | • Execution idempotency (SHA-256 key)         |
|                   |                                           | • Single-active-order-per-intent enforcement  |
+-------------------+-------------------------------------------+-----------------------------------------------+
```

### 6.2 The Fundamental Distinction
$$\text{Scheduler Deduplication} \quad \ne \quad \text{Signal Deduplication} \quad \ne \quad \text{Order Idempotency}$$
* **Scheduler Dedup** governs **WHEN** a strategy function is allowed to run.
* **Signal Dedup** governs **WHAT** the strategy decided mathematically and whether it represents an existing in-flight setup.
* **Order Idempotency** governs **HOW** a broker order is recognized and prevented from double-filling.

---

## 7. STRATEGY SCHEDULING & WATERMARK DEDUPLICATION

The `StrategyScheduler` implements deterministic evaluation semantics tailored to the strategy's declared `TriggerMode`.

### 7.1 Monotonic Evaluation Watermark (`TriggerMode.BAR_CLOSE`)
An LRU cache is a bounded-memory eviction structure, not an authoritative correctness guarantee. Under high bar throughput, an evicted bar could theoretically re-trigger evaluation. Phase 8 replaces the LRU with an **authoritative monotonic evaluation watermark**.

#### Watermark State
The scheduler maintains an in-memory dictionary:
$$\text{last\_evaluated\_boundary}[\text{strategy\_id}, \; \text{instrument\_id}, \; \text{timeframe}] \longrightarrow \text{datetime}$$

#### Watermark Semantics & Rules
1. **Evaluation Boundary:** For `BAR_CLOSE`, the boundary is strictly the finalized candle's end timestamp:
   $$\text{evaluation\_boundary} = \text{candle.end\_time}$$
2. **Initialization:** Upon system startup or strategy registration, each watermark entry is initialized to:
   $$\text{last\_evaluated\_boundary}[\dots] = \text{datetime.min.replace(tzinfo=timezone.utc)}$$
3. **First Valid Closed Candle:** The first closed candle arrives with $\text{candle.end\_time} > \text{datetime.min}$. Since $\text{evaluation\_boundary} > \text{last\_evaluated\_boundary}$, the evaluation proceeds.
4. **Watermark Progression Rule:**
   $$\begin{cases} \text{If } \text{evaluation\_boundary} \le \text{last\_evaluated\_boundary}: & \textbf{SUPPRESS (Drop redundant invocation)} \\ \text{If } \text{evaluation\_boundary} > \text{last\_evaluated\_boundary}: & \textbf{EVALUATE} \end{cases}$$
5. **Advancement Timing:** The watermark advances **strictly after** the strategy evaluation cycle finishes (or upon logging an isolated evaluation exception). Advancing after evaluation guarantees that if the scheduling call aborts prior to strategy dispatch (e.g. system is paused), the bar is not prematurely consumed.
6. **No Watermark Regression:** An older or delayed historical candle arriving with $\text{candle.end\_time} < \text{last\_evaluated\_boundary}$ cannot move the watermark backwards. It is suppressed instantly.
7. **Multi-Timeframe Independence:** Timeframe is an explicit element of the key. Processing a 1-minute closed candle advances the M1 watermark but leaves the M5 watermark completely unaffected.
8. **Multi-Strategy Independence:** Each strategy maintains its own isolated watermark per instrument and timeframe.

---

### 7.2 `INTRABAR_PREVIEW` Semantics & Rate Limiting

#### Evaluation Frequency vs. Signal Deduplication
For strategies declaring `TriggerMode.INTRABAR_PREVIEW`, ticks arrive continuously on active forming candles.
* **The 100 ms Rate Limit is an Evaluation-Frequency Control ONLY:**
  $$\Delta t_{\text{eval}} = \text{current\_time} - \text{last\_eval\_time}[\text{strategy\_id}, \text{instrument\_id}] \ge 100\text{ ms}$$
  This prevents CPython CPU thread starvation under bursts. **It does NOT provide duplicate-order protection.**
* **Signal Semantic Identity (Phase 5):**
  When evaluated, the strategy computes:
  - `SignalCandidate.fingerprint`: Canonical SHA-256 of the exact decision payload (prices, direction, setup, timestamps).
  - `SignalCandidate.reaffirmation_key`: Canonical SHA-256 of the underlying continuous setup (`strategy_id`, `instrument_id`, `setup`, `direction`, `setup_anchor_timestamp`).

#### Signal Classification per Intrabar Preview Cycle
Phase 8 categorizes the outcome of an intrabar evaluation into three mutually exclusive cases:

1. **Case A: NEW ACTIONABLE SIGNAL**
   * A new technical setup emerges, or an existing setup resets its anchor timestamp.
   * `SignalCandidate.fingerprint` is unique.
   * `RiskEngine.admission_state.is_admitted(fingerprint) == False`.
   * **Action:** Passes into `RiskEngine`, sizes capital, admits fingerprint, emits `ApprovedTradeIntent`, dispatches `OrderRequest`.
2. **Case B: REAFFIRMATION OF AN EXISTING SETUP**
   * The strategy re-evaluates an active forming bar on a new tick. The technical setup is still valid with the same anchor timestamp and price geometry.
   * `SignalCandidate.reaffirmation_key` matches the active in-flight setup.
   * `SignalCandidate.fingerprint` matches the previously admitted decision.
   * **Action:** `RiskEngine` detects `admission_state.is_admitted(fingerprint) == True`.
   * **Action:** Fails closed with `RiskDecision(decision=REJECTED, reason=DUPLICATE_SIGNAL)`. **Zero orders are submitted.**
3. **Case C: DUPLICATE EVALUATION / RAPID TICK BURST**
   * Ticks arrive $< 100\text{ ms}$ apart.
   * **Action:** Suppressed at Tier 1 by the scheduler rate limiter before invoking Phase 5.

---

## 8. EVENT LINEAGE, CORRELATION & CRYPTOGRAPHIC TRACEABILITY

Every trade executed by the Tradego runtime retains strict mathematical, cryptographic, and temporal lineage from the originating tick through to final position update.

### 8.1 Lineage Boundary Mapping
Phase 8 establishes complete lineage without modifying upstream frozen contracts:

```
[T1] MarketEvent
       │  • exchange_timestamp, local_receive_monotonic_ns, provider_symbol_id
       ▼
[T2] SignalCandidate (Phase 5)
       │  • signal_id: Unique UUIDv4
       │  • fingerprint: SHA-256(strategy_id, instrument_id, direction, trigger_timestamp, ...)
       │  • reaffirmation_key: SHA-256(strategy_id, instrument_id, setup, direction, anchor_ts)
       ▼
[T3-T4] ApprovedTradeIntent (Phase 6)
       │  • intent_id: Unique Intent UUIDv4
       │  • signal_id, fingerprint, reaffirmation_key (Copied from SignalCandidate)
       │  • risk_config_version, risk_config_hash (Signed risk authorization)
       │  • market_timestamp (T1), intent_generated_timestamp (T4)
       ▼
[T5] OrderRequest (Phase 7)
       │  • client_order_id: Synthesized deterministically: f"TG-{strat[:4]}-{date}-{intent[:12]}"
       │  • intent_id, signal_id, strategy_id (Propagated intact)
       │  • idempotency_key: SHA-256(client_order_id : fingerprint : permitted_qty : reaffirmation_key)
       │  • creation_timestamp (T5), market_timestamp (T1)
       ▼
[T9] Fill (Phase 7)
       │  • fill_id: Broker/Adapter fill sequence ID
       │  • client_order_id: Correlates back to OrderRequest and Intent
       │  • exchange_timestamp, local_receive_monotonic_ns
       ▼
[T10] PositionSnapshot (Phase 7 & 8)
          • instrument_id, strategy_id
          • last_updated_timestamp (T10)
```

### 8.2 Phase 8 Runtime Correlation Record
Phase 8 records end-to-end lineage via a dedicated, slotted dataclass emitted to the telemetry buffer:
```python
@dataclass(frozen=True, slots=True)
class RuntimeCorrelationRecord:
    client_order_id: str
    intent_id: str
    signal_id: str
    signal_fingerprint: str
    reaffirmation_key: str
    idempotency_key: str
    instrument_canonical_id: str
    strategy_id: str
    t1_market_receive_ns: int
    t2_signal_generated_ns: int
    t3_risk_evaluated_ns: int
    t4_intent_approved_ns: int
    t5_order_planned_ns: int
    t6_order_submitted_ns: int
    t7_wire_dispatched_ns: int
    t8_order_acked_ns: int
    t9_fill_received_ns: int
    t10_position_updated_ns: int
```

---

## 9. BACKPRESSURE & EVENT LOSS POLICY

Under extreme market bursts ($> 50,000\text{ ticks/sec}$), the runtime enforces strict, fail-closed bounded buffer behavior.

### 9.1 Event Classification & Drop Policy

```
+---------------------------------------------------------------------------------------------------------------+
| CRITICAL STATE EVENTS (DROPPING STRICTLY PROHIBITED — FAIL CLOSED ON OVERFLOW)                                 |
+----------------------------------+------------------------------------+---------------------------------------+
| Event Type                       | Impact of Event Loss               | Invariant & Action                    |
+----------------------------------+------------------------------------+---------------------------------------+
| Fill                             | Corrupts net position and PnL      | NEVER DROPPED. Synchronous dispatch.  |
| OrderUpdate / SubmissionResult   | Causes order state desynchronicity | NEVER DROPPED. Synchronous dispatch.  |
| Closed Candle (is_closed == True)| Corrupts indicator series history  | NEVER DROPPED. Sequential dispatch.   |
| PositionReconciliationAdjustment | Breaches authoritative book balance| NEVER DROPPED. Synchronous dispatch.  |
| GuardTripEvent                   | Fails to enforce trading halts     | NEVER DROPPED. Highest priority call. |
+----------------------------------+------------------------------------+---------------------------------------+
```

```
+---------------------------------------------------------------------------------------------------------------+
| NON-CRITICAL TELEMETRY & DIAGNOSTIC EVENTS (DROPPING PERMISSIBLE UNDER BOUNDED OVERFLOW)                       |
+----------------------------------+------------------------------------+---------------------------------------+
| Event Type                       | Overflow Behavior                  | Protective Containment                |
+----------------------------------+------------------------------------+---------------------------------------+
| Intermediate MarketEvent Quote   | Coalesced (conflated) to latest    | Intermediate ticks dropped; latest top|
| (No bar close, no order matched) | top-of-book tick in Gateway queue  | of book preserved. State stays fresh. |
| RuntimeCorrelationRecord         | Dropped from ring buffer           | Monotonic dropped_telemetry_count     |
| ComponentHeartbeatRecord         | Overwritten in circular buffer     | incremented atomically. Hot path safe.|
+----------------------------------+------------------------------------+---------------------------------------+
```

### 9.2 Critical Processing Failure Semantics
If an internal queue, lock, or worker cannot safely accept or process a `CRITICAL_STATE_EVENT`:
1. The runtime **FAILS CLOSED**.
2. `TradingGuard` immediately transitions to `HALTED`.
3. `ExecutionRouter.is_accepting_orders` is set to `False`.
4. No further speculative entries are admitted.
5. The incident is logged as a critical safety breach requiring manual inspection.

---

## 10. PORTFOLIORUNTIMESTATE RESPONSIBILITY BOUNDARY

`PortfolioRuntimeState` is an in-memory coordination, state aggregation, and view layer. It maintains a consistent, unified reality for risk and strategy evaluations without duplicating upstream domain logic.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                     PORTFOLIORUNTIMESTATE RESPONSIBILITIES                                       │
├──────────────────────────────────────┬───────────────────────────────────────────────────────────────────────────┤
│ WHAT IT OWNS                         │ • In-memory map of active PositionSnapshots (keyed by InstrumentId).      │
│                                      │ • AccountState cache (cash balance, total collateral, margin used).       │
│                                      │ • Trailing high/low watermark price cache per open position.              │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ WHAT IT READS                        │ • Authoritative Fill events emitted by PaperExecutionAdapter.             │
│                                      │ • InstrumentStateSnapshot mark prices (LTP) from InstrumentStateStore.    │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ WHAT IT EXPOSES                      │ • get_position(instrument_id) -> Optional[PositionSnapshot]               │
│                                      │ • get_portfolio_snapshot() -> PortfolioSnapshot (for Phase 6 RiskContext) │
│                                      │ • get_position_view(instrument_id) -> Optional[PositionView] (for Phase 5)│
│                                      │ • get_account_state() -> AccountState (for Phase 6 RiskContext)           │
├──────────────────────────────────────┼───────────────────────────────────────────────────────────────────────────┤
│ WHAT IT IS STRICTLY FORBIDDEN        │ 1. FORBIDDEN to calculate position sizing (owned by Phase 6 Sizer).       │
│ TO CALCULATE                         │ 2. FORBIDDEN to evaluate risk limits or drawdowns (owned by Phase 6).     │
│                                      │ 3. FORBIDDEN to compute transactional lot accounting or realized PnL math │
│                                      │    directly (must invoke static PositionAccounting.apply_fill()).         │
│                                      │ 4. FORBIDDEN to manage order lifecycle states (owned by Phase 7 State).   │
│                                      │ 5. FORBIDDEN to generate trading signals or advice.                       │
└──────────────────────────────────────┴───────────────────────────────────────────────────────────────────────────┘
```

---

## 11. HALTED STATE & MANUAL RECOVERY POLICY

Trading safety demands that halting the runtime is fail-closed, deterministic, and protected against dangerous automated recovery loops.

### 11.1 Preferred Safety Model
$$\text{HALTED} \longrightarrow \text{Explicit Manual / Administrative Recovery} \longrightarrow \text{READY} \longrightarrow \text{RUNNING}$$

### 11.2 Prohibited Automated Recoveries
Automated recovery from `HALTED` back to `RUNNING` is **STRICTLY PROHIBITED** under any of the following conditions:
1. **Risk Breach:** Drawdown or daily loss threshold exceeded.
2. **Accounting Inconsistency:** Position quantity mismatch or negative equity detected.
3. **Execution UNKNOWN:** Order left in `UNKNOWN` state awaiting broker reconciliation.
4. **Cold Reconciliation Failure:** Inability to balance local state against external broker records.
5. **Critical Transport Drop:** Primary market data socket disconnect during active market hours.

### 11.3 Emergency Exit Allowance
When in the `HALTED` state:
* **Speculative Entries:** 100% BLOCKED.
* **Working Entry Orders:** CANCELLED immediately via `ExecutionRouter.cancel()`.
* **Working Exit Orders:** RETAINED and allowed to fill.
* **Emergency Risk-Reducing Exits:** PERMITTED. Exits submitted with `PositionEffect.CLOSE` or `PositionEffect.REDUCE` are permitted to execute through the router to facilitate capital de-risking.

### 11.4 Manual Recovery Procedure
To return the engine to service:
1. Operator inspects diagnostic telemetry and resolves root cause.
2. Operator calls `runtime.recover(credentials, confirmation_token)`.
3. The runtime re-validates all component states, runs a reconciliation check, and transitions to `READY`.
4. The operator issues an explicit `runtime.resume()` command to transition from `READY` to `RUNNING`.

---

## 12. CONCURRENCY MODEL & SAME-INSTRUMENT FIFO

Tradego Phase 8 v1 implements a **Synchronous Single-Dispatch Architecture with Per-Instrument Serialization**.

### 12.1 Concurrency Architecture for Phase 8 v1
1. **Transport Isolation:** Market data arrives asynchronously on provider network worker threads.
2. **Synchronous In-Memory Hot Path:** The gateway dispatches the normalized `MarketEvent` synchronously down the pipeline:
   $$\text{Gateway} \longrightarrow \text{Store} \longrightarrow \text{Candle} \longrightarrow \text{Feature} \longrightarrow \text{Scheduler} \longrightarrow \text{Risk} \longrightarrow \text{Router} \longrightarrow \text{Adapter} \longrightarrow \text{Accounting}$$
3. **Per-Instrument Locking:**
   * `InstrumentStateStore` and `InstrumentCandleSeries` lock at the `InstrumentId` level using `threading.RLock`.
   * Processing ticks for `RELIANCE` does not block or contend with ticks for `TCS`.
4. **Per-Order Locking:** Phase 7 `ExecutionState` protects individual orders with per-order locks.
5. **Portfolio State Atomicity:** `PortfolioRuntimeState` uses an `RLock` for instantaneous fill updates and publishes immutable snapshots (`PortfolioSnapshot`, `PositionView`), allowing readers to query state without locking.
6. **Zero Global Pipeline Lock:** There is no global lock wrapping the overall execution loop.

### 12.2 Strict Lock Hierarchy (Deadlock Prevention)
Whenever multiple locks must be acquired, they must strictly follow descending hierarchy order:
$$\text{TradingGuard Lock} \longrightarrow \text{Portfolio State Lock} \longrightarrow \text{Per-Instrument Lock} \longrightarrow \text{Per-Order Lock}$$

---

## 13. COMPREHENSIVE FAILURE SEMANTICS

Phase 8 defines unambiguous, fail-closed handling for every category of subsystem error:

```
+-------------------------------+-----------------------------------+-------------------------------------------+
| Failure Event                 | Immediate Impact                  | Containment & Fail-Closed Action          |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 1. Strategy Exception         | Strategy raises unhandled error   | Caught in Scheduler exception boundary.   |
|                               | during evaluate(context)          | Error count incremented. Watermark marked.|
|                               |                                   | ZERO signal emitted. Other symbols safe.  |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 2. Risk Evaluation Exception  | RiskEngine raises unhandled error | Caught in SignalRiskCoordinator.          |
|                               | during evaluate(signal, context)  | FAILS CLOSED: Decision = REJECTED.        |
|                               |                                   | ZERO order created. Audit log recorded.   |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 3. Execution Planning Failure | Expired intent, non-positive qty, | Caught in ExecutionCoordinator.           |
|                               | or invalid price geometry         | Intent discarded. ZERO order submitted.   |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 4. Router Rejection           | Session closed, notional cap hit, | Router returns ExecutionState(REJECTED).  |
|                               | or instrument quarantined         | Intent released. Adapter NOT invoked.     |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 5. Paper Adapter Rejection    | Rate limit queue exceeded or      | State marked REJECTED / FAILED.           |
|                               | order parameters invalid          | Accounting NOT updated. Logged to health. |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 6. Duplicate Signal Detected  | Signal fingerprint already active | RiskEngine returns REJECTED               |
|                               | in AdmissionState                 | (reason=DUPLICATE_SIGNAL). ZERO order.    |
+-------------------------------+-----------------------------------+-------------------------------------------+
| 7. Duplicate Order Detected   | client_order_id or intent_id      | ExecutionStateRegistry blocks creation.   |
|                               | already registered in Router      | Existing state returned. ZERO new order.  |
+-------------------------------+-----------------------------------+-------------------------------------------+
```

---

## 14. MANDATORY CONCRETE SCENARIOS

### SCENARIO A: Same Closed Candle Delivered Twice
* **Trigger:** Provider reconnect causes `CandleEngine` to re-emit the 09:20 closed candle for `RELIANCE` ($\text{end\_time} = \text{09:20:00}$).
* **Execution:**
  1. First delivery: $\text{last\_evaluated\_boundary} = \text{09:15:00}$. Since $\text{09:20:00} > \text{09:15:00}$, `evaluate_instrument()` executes. Watermark advances to `09:20:00`.
  2. Second delivery: $\text{evaluation\_boundary} = \text{09:20:00}$. Scheduler checks $\text{09:20:00} \le \text{09:20:00}$.
* **Outcome:** Second `BAR_CLOSE` evaluation is **immediately suppressed**.

### SCENARIO B: Older Closed Candle Arrives Late
* **Trigger:** Network packet delay delivers a 09:15 closed candle after the 09:20 closed candle has already processed.
* **Execution:**
  1. Current watermark is `09:20:00`.
  2. Late candle has $\text{end\_time} = \text{09:15:00}$.
  3. Scheduler evaluates: $\text{09:15:00} \le \text{09:20:00}$.
* **Outcome:** Late candle is **suppressed without modifying the watermark**. Watermark never regresses.

### SCENARIO C: Two Consecutive Closed Candles
* **Trigger:** Market ticks close the 09:20 candle, followed 5 minutes later by the 09:25 candle.
* **Execution:**
  1. Bar 1: $\text{09:20:00} > \text{09:15:00} \implies$ Evaluated. Watermark advances to `09:20:00`.
  2. Bar 2: $\text{09:25:00} > \text{09:20:00} \implies$ Evaluated. Watermark advances to `09:25:00`.
* **Outcome:** Both candles are **independently and sequentially evaluated**.

### SCENARIO D: Same INTRABAR Setup Evaluated on Multiple Ticks
* **Trigger:** Microstructure strategy monitors forming bar. Three consecutive ticks arrive at 09:21:01, 09:21:02, 09:21:03 reflecting the same breakout setup.
* **Execution:**
  1. Tick 1: Evaluated. Strategy emits `SignalCandidate` with `fingerprint = FP_A` and `reaffirmation_key = RK_A`. Risk approves and records `FP_A` in `AdmissionState`. Order TG-001 is placed.
  2. Tick 2: $\Delta t \ge 100\text{ ms}$, so evaluation proceeds. Strategy emits reaffirmed setup with identical `FP_A`. `RiskEngine` detects `admission_state.is_admitted(FP_A) == True`. Risk returns `REJECTED (DUPLICATE_SIGNAL)`.
  3. Tick 3: Evaluated. Identical `FP_A` is rejected again by Risk admission state.
* **Outcome:** Only Tick 1 results in an order. Ticks 2 & 3 are **safely pruned at the Risk boundary without duplicate orders**.

### SCENARIO E: Strategy Produces a Genuinely Changed Setup During INTRABAR_PREVIEW
* **Trigger:** On Tick 4 (09:21:10), price surges through a secondary breakout level. Strategy recalculates geometry with an adjusted stop loss and higher conviction.
* **Execution:**
  1. Phase 5 computes a **new** `fingerprint = FP_B` (since price geometry and availability timestamp changed).
  2. `RiskEngine` checks `admission_state.is_admitted(FP_B) == False`.
  3. Risk evaluates new setup, sizes position, admits `FP_B`, and emits `ApprovedTradeIntent`.
* **Outcome:** Genuinely changed setup is **lawfully evaluated and admitted as an updated trade intent**.

### SCENARIO F: Risk Engine Fails Unexpectedly
* **Trigger:** An unhandled `ZeroDivisionError` or corrupted account state occurs inside `RiskEngine.evaluate()`.
* **Execution:**
  1. `SignalRiskCoordinator` wraps risk evaluation in a strict try/except boundary.
  2. Exception is caught, logged, and health error counter incremented.
  3. Coordinator forces `RiskDecisionType.REJECTED`.
* **Outcome:** **ZERO order is created.** The runtime fails closed.

### SCENARIO G: Execution Router Receives a Duplicate Intent
* **Trigger:** Upstream duplicate delivery attempts to submit the same `ApprovedTradeIntent` twice to `ExecutionRouter`.
* **Execution:**
  1. Intent 1: Planned into `OrderRequest(client_order_id="TG-001", intent_id="INT-1")`. Registered in `ExecutionStateRegistry`.
  2. Intent 2: `ExecutionPlanner` produces `OrderRequest` with identical `intent_id="INT-1"`.
  3. `ExecutionRouter.submit()` checks `ExecutionStateRegistry.has_active_intent("INT-1") == True`.
* **Outcome:** Second submission is **blocked by Phase 7 idempotency**. Zero duplicate order dispatched to adapter.

---

## 15. ACCEPTANCE CRITERIA

Phase 8 acceptance is split into two rigorous tiers:

### Tier 1: Hard Safety & Correctness Gates (Mandatory for Acceptance)
* **G1 (Immutability):** Zero modifications to any file in `services/market_gateway/`, `services/market_state/`, `services/candles/`, `services/analytics/`, `services/signals/`, `services/risk/`, or `services/execution/`.
* **G2 (Upstream Regression):** 100% pass across all 262 existing unit tests in Phases 1–7.
* **G3 (Phase 8 Unit Tests):** 100% pass across all planned Phase 8 runtime unit tests.
* **G4 (Compilation):** `python -m compileall services tests` completes with exit code 0.
* **G5 (Live Lockout):** Initializing `RuntimeConfig` with `RuntimeMode.LIVE` immediately raises an uncatchable fatal exception.
* **G6 (Risk Gate):** Every order must possess an associated `ApprovedTradeIntent`. Zero orders can be submitted directly from signals or strategies.
* **G7 (Router Gate):** Every order must pass through `ExecutionRouter.submit()`. Zero orders can access `PaperExecutionAdapter` directly.
* **G8 (BAR_CLOSE Watermark Correctness):** Closed bar evaluations advance monotonically; duplicate closed candles are suppressed; late candles never move the watermark backwards.
* **G9 (INTRABAR Evaluation & Idempotency):** The 100 ms rate limit controls frequency only; repeated preview setups are rejected by Risk `AdmissionState`; genuinely new setups are lawfully admitted.
* **G10 (Same-Instrument FIFO):** Sequential ticks on the same instrument maintain strict temporal order without race conditions.
* **G11 (Critical Event Non-Dropping):** Fills, adjustments, and order updates are never dropped under backpressure.
* **G12 (Fail-Closed Risk Failures):** Unhandled risk engine exceptions reject the trade with zero order placement.
* **G13 (Quarantine Enforcement):** Quarantined instruments block new entries while permitting exits.
* **G14 (Lineage Traceability):** Every completed order maintains full cryptographic and temporal lineage ($T_1 \to T_{10}$).
* **G15 (Safe Shutdown):** Shutdown cleanly cancels working entry orders and drains pending fills.
* **G16 (Manual Halted Recovery):** `HALTED` state requires explicit operator credentials to transition to `READY`.
* **G17 (Hot Path Purity):** Zero AI/LLM/MCP calls, zero database writes, and zero blocking disk I/O on the execution path.
* **G18 (Bounded Memory):** All queues and buffers enforce explicit maximum capacities.

### Tier 2: Performance Optimization Targets (Directional Engineering Benchmarks)
* **T1:** Complete in-memory paper trade path ($T_1 \to T_{10}$) achieves $p50 < 100.0\text{ \mu s}$.
* **T2:** Strategy scheduling and watermark check overhead remains $< 15.0\text{ \mu s}$ ($p50$).
* **T3:** Telemetry ring buffer insertion overhead remains $< 2.0\text{ \mu s}$ ($p50$).

---

## 16. PROPOSED MODULE STRUCTURE

The Phase 8 implementation will reside exclusively in a new package, `services/runtime/`:

```
services/runtime/
    ├── __init__.py                  # Public exports: TradingRuntime, RuntimeConfig, RuntimeState
    ├── models.py                    # Runtime enums, lifecycle events, correlation dataclasses
    ├── config.py                    # RuntimeConfig (immutable slotted dataclass)
    ├── lifecycle.py                 # RuntimeLifecycleManager (state machine, startup/shutdown)
    ├── guards.py                    # TradingGuard (kill switch, pause/halt logic, mode guards)
    ├── scheduler.py                 # StrategyScheduler (monotonic watermark & rate limiter)
    ├── coordinator.py               # PipelineCoordinator (in-memory component wiring)
    ├── signal_risk_coordinator.py   # SignalRiskCoordinator (signal to risk context & decision)
    ├── execution_coordinator.py     # ExecutionCoordinator (intent to execution planner & router)
    ├── portfolio.py                 # PortfolioRuntimeState (synchronized position/cash view)
    ├── health.py                    # HealthMonitor (heartbeat, error rate, failure containment)
    └── telemetry.py                 # TelemetryCollector (lockless ring buffer & latency tracker)
```

---

## 17. FORMAL INVARIANTS

* **I1:** Phase 1–7 frozen source code, interfaces, tests, and configuration schemas remain 100% immutable.
* **I2:** No strategy or signal generator may directly access or submit orders to `BrokerExecutionAdapter`.
* **I3:** No `SignalCandidate` can bypass `RiskEngine.evaluate()`.
* **I4:** No order can bypass `ExecutionRouter.submit()`.
* **I5:** Live trading is architecturally impossible in Phase 8 v1.
* **I6:** `PAPER` mode is the exclusive executable trading runtime.
* **I7:** A risk engine rejection or unhandled exception strictly results in `NO ORDER`.
* **I8:** Unresolved order execution follows Phase 7 `UNKNOWN` quarantine and reconciliation rules.
* **I9:** Event processing for the same instrument maintains strict FIFO temporal ordering.
* **I10:** Duplicate market events, duplicate candles, or duplicate signals cannot produce duplicate orders.
* **I11:** A failed upstream subsystem cannot silently produce an approved trade.
* **I12:** Shutdown prevents uncontrolled new entries and executes orderly cancel drain.
* **I13:** When `TradingGuard` is `HALTED` or `PAUSED`, speculative entries are strictly blocked.
* **I14:** Emergency risk-reducing exits remain permissible during `HALTED` state.
* **I15:** No AI, LLM, or MCP tooling may execute on the market-to-order critical path.
* **I16:** Unbounded queues are strictly forbidden; all runtime buffers must enforce maximum capacities.
* **I17:** Zero blocking external I/O (disk, network, database) is permitted on the synchronous hot path.
* **I18:** Every executable order retains complete cryptographic and temporal lineage back to $T_1$.
* **I19:** Runtime operating mode is validated as `PAPER` before market data processing begins.
* **I20:** Phase 8 does not duplicate business logic already owned by frozen Phases 1 through 7.

---

## 18. VERSION & GOVERNANCE RECORD

| Version | Date | Author | Status | Description |
|---|---|---|---|---|
| `1.0.0-DRAFT` | 2026-09-15 | System Architecture Implementer | Superseded | Initial Phase 8 Trading Runtime architecture design document. |
| `1.1.0-DRAFT` | 2026-09-15 | System Architecture Implementer | Superseded | Addressed initial review findings on lineage, backpressure, and recovery. |
| `1.2.0-DRAFT` | 2026-09-15 | System Architecture Implementer | Draft | Final architecture corrections: Monotonic BAR_CLOSE evaluation watermark, explicit INTRABAR_PREVIEW frequency vs identity semantics, three-tier deduplication hierarchy, comprehensive fail-closed failure semantics, and seven mandatory concrete operational scenarios. |
