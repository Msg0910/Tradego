# TRADEGO UI ↔ API / EVENT BOUNDARY ARCHITECTURE SPECIFICATION
## Institutional Presentation Boundary, State Export & Event Distribution Protocol
### Document Version: 1.1.0-DRAFT | Status: Ready for Senior Architecture Review
### Upstream Status: Phases 1–8 Approved and Frozen | UI Architecture v1.1 Approved and Frozen

---

## 1. PURPOSE

This document establishes the definitive, design-only specification for the **Tradego UI ↔ API / Event Boundary Architecture**. It defines the formal architectural contract, state export mechanisms, synchronization protocols, and command routing pipeline operating between the frozen in-memory trading core and external presentation clients.

The primary mission of this boundary is to:
1. **Insulate the Core Hot Path:** Ensure that market data ingestion, quantitative analytics, signal arbitration, risk management, and order routing operate with zero synchronous delay, zero network I/O contention, and complete isolation from client presentation overhead.
2. **Project Authoritative Trading Truth:** Translate in-memory engine state into immutable, sequence-validated, point-in-time projections without manufacturing synthetic state or altering backend domain semantics.
3. **Control Inbound Operational Mutations:** Provide a deterministic, authenticated, and audited gate for operator control commands and paper trading actions, strictly preserving Phase 6 Risk and Phase 7 Execution constraints.

```
+-----------------------------------------------------------------------------+
|                     TRADING TRUTH vs. PRESENTATION BOUNDARY                 |
|                                                                             |
| The Tradego Backend Engine is the SOLE AUTHORITATIVE SOURCE of:             |
| - market state        - orders                - lifecycle state             |
| - positions           - fills                 - risk parameters             |
| - PnL                 - accounting balances   - T1-T10 telemetry            |
|                                                                             |
| The Boundary Layer PROJECTS backend state and INGESTS audited commands.     |
| Neither the Boundary Layer nor the UI Client ever manufactures trading truth.|
+-----------------------------------------------------------------------------+
```

---

## 2. SCOPE & BOUNDARIES

### 2.1 In-Scope (Design Specifications)
- **Architectural Process Boundary:** Structural separation models between the trading engine, state export, API distribution, and UI clients.
- **State Export Model:** Technology-neutral egress boundary translating engine domain models into capability-aware DTOs and point-in-time snapshots.
- **Authoritative Snapshot Consistency Contract:** Formal causal relationship between engine state, snapshot boundary, $S_{\text{snap}}$, and subsequent event sequences.
- **Event Distribution & Enveloping:** Event categorization, versioned enveloping (`TradegoEventEnvelope<T>`), and sequence tracking.
- **Authoritative Synchronization:** Snapshot/stream handshake, sequence gap detection, and event delivery frontier semantics.
- **Inbound Command Pipeline:** Segregation of administrative vs. trading commands, validation routing, and execution result tracking.
- **Backpressure & Conflation Boundaries:** Isolation of engine execution from slow clients, burst streaming, and rendering throttles.
- **Multi-Client Consistency:** Unified sequence scoping and resynchronization models across concurrent operator consoles.
- **Observability & Lineage Display:** End-to-end latency measurement contracts ($T_1 \dots T_{10}$) and timestamp boundaries.
- **Comparative Trade-Off Analyses:** Detailed matrices for API styles (REST, WebSocket, SSE, gRPC) and process deployment topologies.

### 2.2 Out-of-Scope (Strict Non-Goals)
- **NO Source Code Modifications:** Zero changes to frozen Phases 1–8 (`services/`, `strategies/`, `brokers/`, `config/`).
- **NO Test Modifications:** Zero changes to existing automated test suites (`tests/`).
- **NO UI Implementation:** No React, Next.js, HTML, CSS, charts, Zustand stores, or npm package installations.
- **NO API Server Code:** No FastAPI, Flask, aiohttp, Starlette, or Node.js server generation.
- **NO Database or Middleware:** No SQL schemas, ORMs, Redis caches, Kafka brokers, or external message queues.
- **NO Premature Security Technology Selection:** Zero binding decisions on TLS versions, JWT/OAuth, mTLS, or identity providers; all concrete security technologies are deferred to the Security Architecture Review.
- **NO Derivative or Multi-Asset Expansions:** Active scope is strictly Indian cash equities (NSE/BSE). Futures, options, and Greeks remain deferred to future roadmap phases.
- **NO AI/LLM on the Trading Path:** Artificial Intelligence is strictly confined to offline, read-only research; it never touches execution or runtime gating.

---

## 3. CURRENT SYSTEM BASELINE

Inspection of the verified repository establishes the exact upstream foundation:
1. **Engine Implementation:** Phases 1 through 8 operate as pure Python in-memory modules with synchronous, deterministic dispatch across the paper trading path.
2. **Active Core Services:**
   - `MarketDataGateway` (Phase 1): Ingests and normalizes live ticks.
   - `InstrumentStateStore` (Phase 2): Real-time per-symbol L1 quotes and 5-level order book depth.
   - `CandleEngine` (Phase 3): Multi-timeframe synthesis (1m, 5m, 15m, 1h, 1d).
   - `FeatureEngine` (Phase 4): Real-time microstructure indicators and moving averages.
   - `SignalEngine` (Phase 5): Deterministic signal generation and candidate arbitration.
   - `RiskEngine` (Phase 6): Sizing rules, capital controls, drawdown monitors, and daily loss vetoes.
   - `ExecutionRouter` & `PaperExecutionAdapter` (Phase 7): Idempotent order planning, matching, and lifecycle states.
   - `PipelineCoordinator`, `TradingGuard`, `PortfolioRuntimeState`, `HealthMonitor`, `TelemetryCollector` (Phase 8): Central system orchestration, lifecycle machine, fail-closed kill switches, and $T_1 \dots T_{10}$ correlation tracking.
3. **Current Infrastructure Reality:**
   - **No external network listeners exist:** There is currently no HTTP server, WebSocket gateway, IPC socket, or database daemon.
   - **Internal Telemetry:** `TelemetryCollector` maintains an internal, thread-safe bounded deque of `RuntimeCorrelationRecord` instances. No external streaming exporter or tap has been implemented.
4. **Current Test Baseline:** 293 tests collected: 292 passed, 0 failed, 1 skipped.

---

## 4. ARCHITECTURAL PRINCIPLES

1. **Uncompromising Hot-Path Isolation (`UI-01`):** Under no circumstances may client serialization, slow consumers, network stalls, or HTTP/WebSocket timeouts introduce blocking latency into the trading loop.
2. **Backend Sole Authority (`UI-02`):** The trading engine is the sole source of truth. The presentation layer never computes, modifies, or assumes trading state.
3. **Zero Optimistic Execution Truth (`UI-03`):** Orders are never projected as submitted, acknowledged, or filled based on local client actions. State transitions exist only when validated by an authoritative backend event.
4. **Mandatory Risk & Guard Gating (`UI-04`, `UI-05`):** All inbound operational mutations must traverse Phase 6 Risk and Phase 8 `TradingGuard`. Direct access to routers, adapters, or brokers is physically prohibited.
5. **Deterministic Resynchronization (`UI-08`, `UI-13`):** Client state recovery is deterministic. Sequence gaps must be reliably detected via delivery frontiers and resolved via authoritative snapshots without silent packet loss.
6. **Separation of Presentation Conflation (`UI-09`):** Conflating market quotes for smooth 60fps browser rendering is strictly an egress presentation policy; it never alters the engine's internal tick ingestion or evaluation rate.
7. **No Synthetic Truth on Failure:** When components fail or disconnect, the presentation layer reflects ambiguity and staleness rather than synthesizing plausible data.

---

## 5. BOUNDARY ARCHITECTURE (ARCHITECTURAL PROCESS BOUNDARY)

The end-to-end communication topology establishes a clean, decoupled boundary between the trading engine and presentation consumers:

```
   ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
   │                           TRADEGO TRADING ENGINE (PHASES 1–8)                                 │
   │                                                                                               │
   │   MarketEvent (T1) ──► StateStore ──► CandleEngine ──► FeatureEngine ──► SignalEngine (T2)   │
   │                                                                               │               │
   │   Telemetry (T10) ◄── Accounting ◄── Fill (T9) ◄── Router ◄── Risk (T3-4) ◄───┘               │
   └──────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                  │ Asynchronous State Export Boundary
                                                  │ (Bounded non-blocking egress boundary)
                                                  ▼
   ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
   │                            STATE EXPORT LAYER (EGRESS PROJECTION)                             │
   │                                                                                               │
   │   • Point-in-Time Snapshot Generator (Produces authoritative state snapshots)                 │
   │   • Event Projector & Enveloper (Wraps engine domain events into TradegoEventEnvelope)        │
   │   • Global Sequence Counter & Monotonic Watermark Assignor                                    │
   │   • Conflation & Coalescing Filter (Throttles presentation ticks without dropping trades)     │
   └──────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                  │ Internal Inter-Process / IPC Transport
                                                  ▼
   ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
   │                      API / EVENT DISTRIBUTION LAYER (GATEWAY SERVICE)                         │
   │                                                                                               │
   │   • Connection Manager (Session lifecycle, authentication, client subscription tracking)      │
   │   • WebSocket Stream Dispatcher (Broadcasts enveloped event streams to subscribers)          │
   │   • REST Snapshot & Query Service (Serves cold snapshots, lineage records, configuration)     │
   │   • Authenticated Command Ingestion Gate (Enforces RBAC, validates schemas, logs audit trail) │
   └───────────────────────────────┬─────────────────────────────────────────┬─────────────────────┘
                                   │ WebSocket (Push Streams)                │ REST (Pull / Commands)
                                   ▼                                         ▼
   ┌───────────────────────────────────────────────────────────────────────────────────────────────┐
   │                                 TRADEGO UI CLIENT (BROWSER)                                   │
   │                                                                                               │
   │   • Stream Buffer & Sequence Validator (Gap detection, delivery frontier evaluation)          │
   │   • Local Presentation State Store (Projected state, viewports, table filters, draft inputs)  │
   │   • 60fps RequestAnimationFrame (rAF) Batch Renderer                                          │
   └───────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Existing vs. Future Proposed Boundary Components
To avoid architectural ambiguity, existing system elements are strictly delineated from future proposed components:

| Component | Architecture Role | Status | Description |
| :--- | :--- | :--- | :--- |
| **Phases 1–8 Trading Engine** | Core Trading Truth | **CURRENT / FROZEN** | Implemented, tested, deterministic in-memory trading core. |
| **TelemetryCollector** | Internal Latency Logging | **CURRENT / FROZEN** | Thread-safe bounded deque for $T_1 \dots T_{10}$ records. |
| **State Export Layer** | Non-Blocking Egress Boundary | **FUTURE PROPOSED** | Dedicated egress boundary pushing/pulling state without interfering with the trading hot path. |
| **API Distribution Gateway** | Web Sockets & REST Endpoints | **FUTURE PROPOSED** | Independent gateway managing network connections and subscriptions. |
| **Command Ingestion Boundary** | Authenticated Command Gate | **FUTURE PROPOSED** | Security validation, command audit logging, and domain routing. |
| **Tradego UI Client** | Presentation & Monitoring | **FUTURE PROPOSED** | Browser-based trading workstation consuming versioned envelopes. |

---

## 6. STATE EXPORT ARCHITECTURE

The State Export Layer is responsible for extracting authoritative engine data and converting it into network-distributable projections without introducing mutex contention or memory leaks.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 STATE EXPORT PIPELINE                                       │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  [Engine Hot Path]                                                                          │
│         │                                                                                   │
│         ▼ (Non-blocking state export handoff)                                               │
│  [Egress Buffer Boundary] (Bounded non-blocking egress boundary)                            │
│         │                                                                                   │
│         ▼ (Asynchronous consumer worker / thread)                                           │
│  [State Export Processor]                                                                   │
│         ├─► 1. Assign Monotonic 64-bit Event Sequence (S_curr)                              │
│         ├─► 2. Stamp Server UTC ISO-8601 Timestamp                                          │
│         ├─► 3. Attach Unique Event ID (UUIDv4) & Correlation ID                             │
│         ├─► 4. Apply Conflation Filter (Drop intermediate quotes if buffer fills)           │
│         └─► 5. Package into TradegoEventEnvelope<T>                                         │
│         │                                                                                   │
│         ▼ (Push to API Distribution Layer)                                                  │
│  [Event Dispatch Bus]                                                                       │
│                                                                                             │
│  NOTE: Concrete egress mechanism and buffer capacity are NOT YET DECIDED                   │
│        and require separate technology/performance evaluation.                              │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 6.1 State Export Invariants & Boundary Constraints
1. **Hot-Path Non-Interference:** State export must not synchronously block or materially interfere with the Phase 1–8 trading hot path.
2. **Egress Implementation Status (NOT YET DECIDED):** The concrete implementation mechanism and capacity of the egress boundary are **NOT YET DECIDED** and require a separate technology/performance evaluation. Candidate mechanisms listed for architectural comparison only include:
   - In-process bounded queue
   - Shared-memory transport
   - Local IPC / named pipes
   - Other reviewed mechanism
   Under no circumstances is a concrete mechanism or buffer capacity frozen in this boundary specification.
3. **Strict Drop Policy for Presentation-Only Data:** If downstream network buffers experience extreme congestion, the exporter may drop or coalesce market data quotes (`MARKET` category). Under no circumstances may it drop transactional trading events (`ORDER`, `FILL`, `RISK`, `RUNTIME`, `POSITION`).
4. **Monotonic Sequence Integrity:** Sequences are assigned sequentially at the export boundary before network serialization, establishing the authoritative delivery timeline.

---

## 7. SNAPSHOT CONTRACT

The Authoritative Snapshot provides a complete point-in-time state projection required for client bootstrapping, reconnect recovery, and sequence gap resolution.

### 7.1 Authoritative Snapshot Consistency Contract
The boundary architecture establishes a strict causal relationship between engine state, snapshot boundaries, snapshot sequence ($S_{\text{snap}}$), and event stream sequences:

```
   ENGINE STATE
        │
        ▼
   AUTHORITATIVE SNAPSHOT BOUNDARY
        │
        ▼
      S_snap (Exact sequence frontier represented by the snapshot)
        │
        ├─────────────────────────────────────────┐
        ▼                                         ▼
   SNAPSHOT PAYLOAD                          LIVE EVENT STREAM
   (Reflects ALL state where seq <= S_snap)   (Strictly emits events where seq > S_snap)
```

The snapshot consistency contract mandates:
1. **Consistent Engine State Boundary:** Every authoritative snapshot represents one logically consistent engine state boundary across all engine sub-domains (runtime state, risk limits, open orders, filled positions, accounting balances, and system health).
2. **Exact Sequence Frontier ($S_{\text{snap}}$):** $S_{\text{snap}}$ identifies the exact sequence frontier represented by that snapshot.
3. **Events Preceding or Included in Snapshot:** All state mutations and events represented by the snapshot have sequence $\le S_{\text{snap}}$.
4. **Events Following Snapshot Boundary:** All state-mutating events occurring strictly after the snapshot boundary have sequence $> S_{\text{snap}}$.
5. **Client Reconciliation Baseline:** Client reconciliation begins unconditionally from:
   $$last_processed_sequence = S_snap$$
   and applies only valid subsequent events where $\text{sequence} > S_{\text{snap}}$.
6. **Implementation Mechanism Neutrality:** The boundary specification defines this consistency requirement as an immutable architectural contract, **NOT** an implementation prescription. It explicitly does **NOT** prescribe:
   - Locks or mutex schemes
   - Copy-on-write mechanisms
   - Shared memory layouts
   - Specific buffer topologies
   - Database transactions
   - Specific snapshot generation routines  
   The concrete mechanism for obtaining a consistent state boundary remains a future implementation decision.

### 7.2 Conceptual Snapshot Contract Payload
```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                            AUTHORITATIVE SNAPSHOT CONTRACT                                  │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  snapshot_id: UUIDv4                                                                        │
│  snapshot_timestamp: UTC ISO-8601 String                                                    │
│  authoritative_sequence: uint64 (S_snap)                                                    │
│                                                                                             │
│  runtime_state:                                                                             │
│    • runtime_mode: "PAPER" | "SHADOW" | "LIVE"                                              │
│    • lifecycle_state: "RUNNING" | "PAUSED" | "HALTED" | "DEGRADED"                          │
│    • guard_state: "NORMAL" | "PAUSED" | "HALTED" | "EMERGENCY_FLATTEN"                      │
│    • trip_reason: Optional GuardTripReason code                                             │
│    • trip_details: Optional diagnostic string                                               │
│                                                                                             │
│  account_risk:                                                                              │
│    • account_id: String                                                                     │
│    • total_capital: float                                                                   │
│    • available_cash: float                                                                  │
│    • realized_pnl: float                                                                    │
│    • unrealized_pnl: float                                                                  │
│    • daily_drawdown_pct: float                                                              │
│    • peak_drawdown_pct: float                                                               │
│    • daily_loss_limit_remaining: float                                                      │
│    • active_risk_vetoes: List of active constraint descriptions                             │
│                                                                                             │
│  market_state:                                                                              │
│    • quotes: Map<InstrumentId, MarketQuoteDTO> (LTP, bid, ask, spread, VWAP, is_stale)      │
│    • order_books: Map<InstrumentId, OrderBookDTO> (Top 5 bid/ask depth levels)              │
│                                                                                             │
│  positions: List<PositionDTO>                                                               │
│    • instrument_id, strategy_id, net_quantity, avg_entry_price, cmp, unrealized_pnl,        │
│      realized_pnl, trailing_high_price, trailing_low_price, opened_timestamp                │
│                                                                                             │
│  orders: List<OrderDTO>                                                                     │
│    • client_order_id, intent_id, strategy_id, instrument_id, side, position_effect,         │
│      order_type, quantity, filled_quantity, status, idempotency_key, creation_timestamp    │
│                                                                                             │
│  system_health:                                                                             │
│    • feed_stagnation_ms: float                                                              │
│    • exception_burst_rate: float (errors/sec)                                               │
│    • active_heartbeats: Map<ComponentName, HeartbeatStatus>                                 │
│                                                                                             │
│  lineage_watermark:                                                                         │
│    • latest_completed_lineage_ref: Optional CorrelationId                                   │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 7.3 Authoritative vs. UI-Derived Presentation Fields
To prevent architectural contamination, snapshot fields are strictly categorized:

| Field Category | Origin | Mutability by API/UI | Examples |
| :--- | :--- | :--- | :--- |
| **Authoritative Backend Fields** | Phase 1–8 Engine Truth | **IMMUTABLE** | `authoritative_sequence`, `positions`, `orders`, `realized_pnl`, `guard_state` |
| **UI Presentation Fields** | Computed Locally in UI | **CLIENT MEMORY ONLY** | Formatted currency strings, % PnL color coding, sort orders, table pagination, crosshair prices |

---

## 8. EVENT STREAM CONTRACT

All real-time updates pushed from the API distribution gateway to client connections must be packaged inside a standardized, versioned envelope:

```typescript
// ============================================================================
// AUTHORITATIVE EVENT ENVELOPE (DESIGN CONTRACT ONLY)
// ============================================================================

export interface TradegoEventEnvelope<T = unknown> {
  event_id: string;                // Unique UUIDv4 per discrete event instance
  event_type: string;              // Specific event identifier (e.g. "ORDER_FILLED")
  event_version: string;           // Semantic schema version of payload (e.g. "1.0.0")
  sequence: number;                // Monotonically increasing 64-bit sequence counter
  correlation_id: string;          // Originating trigger identifier (tick or client action)
  server_timestamp: string;        // Server / emitted UTC ISO-8601 timestamp
  exchange_timestamp?: string;     // Authoritative exchange timestamp (where applicable)
  availability_timestamp?: string; // Point-in-time data availability timestamp (where applicable)
  instrument_id?: string;          // Canonical instrument identifier (e.g. "NSE:TCS:EQUITY")
  payload: T;                      // Typed domain data transfer object
}
```

### 8.1 Architectural Event Categories
Events are classified into 10 high-level functional domains:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               ARCHITECTURAL EVENT CATEGORIES                                │
├───────────────────┬───────────────────────────────────┬─────────────────────────────────────┤
│ Category          │ Typical Event Types               │ Conflation Policy                   │
├───────────────────┼───────────────────────────────────┼─────────────────────────────────────┤
│ 1. MARKET         │ QUOTE_TICK, DEPTH_UPDATE, BAR_DONE│ Coalescible (Latest value wins)     │
│ 2. SIGNAL         │ CANDIDATE_EMITTED, ARBITRATED_VETO│ Non-Coalescible (Audit trail)       │
│ 3. RISK           │ LIMIT_BREACHED, GUARD_TRIPPED     │ Non-Coalescible (Critical safety)   │
│ 4. ORDER          │ ACKNOWLEDGED, CANCELLED, REJECTED │ Non-Coalescible (Transactional)     │
│ 5. FILL           │ EXECUTION_MATCH, PARTIAL_FILL     │ Non-Coalescible (Accounting basis)  │
│ 6. POSITION       │ LOT_OPENED, LOT_CLOSED, WATERMARK │ Non-Coalescible (Balance impact)    │
│ 7. RUNTIME        │ STATE_CHANGED, MODE_SWITCH        │ Non-Coalescible (Lifecycle)         │
│ 8. HEALTH         │ STAGNATION_ALERT, EXCEPTION_BURST │ Coalescible (Diagnostic status)     │
│ 9. LINEAGE        │ T1_T10_RECORD_COMMITTED           │ Non-Coalescible (Traceability)      │
│ 10. COMMAND_RESULT│ COMMAND_ACKNOWLEDGED, FAILED      │ Non-Coalescible (Client dispatch)   │
└───────────────────┴───────────────────────────────────┴─────────────────────────────────────┘
```

---

## 9. SYNCHRONIZATION & SEQUENCE MODEL

The boundary guarantees deterministic synchronization across browser lifecycles, tab suspensions, and transient network disconnections.

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                                CANONICAL SYNCHRONIZATION FLOW
══════════════════════════════════════════════════════════════════════════════════════════════════════

      CLIENT                             API GATEWAY                         STATE EXPORTER
        │                                     │                                     │
        ├──────── Establish Session ─────────►│                                     │
        │                                     ├───── Subscribe Egress Stream ──────►│
        │◄─────── Stream Connected ───────────┤                                     │
        │                                     │                                     │
        │ [START INBOUND BUFFERING]           │                                     │
        │ (Buffer all streaming envelopes)    │                                     │
        │                                     │                                     │
        ├──────── Request Snapshot ──────────►│                                     │
        │    (GET /api/v1/state/snapshot)     ├────── Fetch Authoritative State ───►│
        │                                     │◄───── Return S_snap Snapshot ───────┤
        │◄─────── Deliver Snapshot ───────────┤                                     │
        │                                     │                                     │
        │ [APPLY SNAPSHOT BASELINE]           │                                     │
        │ Set last_seq = S_snap               │                                     │
        │                                     │                                     │
        │ [RECONCILE BUFFERED EVENTS]         │                                     │
        │ Drop events where seq <= S_snap     │                                     │
        │ Verify seq == S_snap + 1            │                                     │
        │                                     │                                     │
        │ [EVALUATE DELIVERY FRONTIER]        │                                     │
        │ Check for confirmed sequence gaps   │                                     │
        │                                     │                                     │
        │◄─────── Live Stream Events ─────────┼─────────────────────────────────────┤
        │                                     │                                     │
```

### 9.1 Inbound Sequence Evaluation Rules
Every incoming enveloped event is processed through three immutable causal branches:

$$	ext{Evaluate: } S_{	ext{incoming}} 	ext{ against } S_{	ext{last\_processed}}$$

1. **Duplicate / Replay ($S_{	ext{incoming}} \le S_{	ext{last\_processed}}$):**
   - Event has already been superseded by the snapshot or a prior stream message.
   - Discard immediately from projection pipeline; perform zero state mutation.
2. **Contiguous Next Event ($S_{	ext{incoming}} == S_{	ext{last\_processed}} + 1$):**
   - Valid next event in strict causal order.
   - Apply payload to local projection store; advance watermark: $S_{	ext{last\_processed}} = S_{	ext{incoming}}$.
3. **Sequence Gap Detected ($S_{	ext{incoming}} > S_{	ext{last\_processed}} + 1$):**
   - **Delivery Frontier Rule:** A sequence gap shall be declared **only** when the stream/session contract provides an authoritative delivery frontier/high-water sequence, or otherwise establishes that the missing intermediate sequence cannot still arrive. Temporary network reordering or brief transit jitter must not by itself trigger catastrophic resynchronization.
   - **Confirmed Gap Action:** Once verified via the delivery frontier, quarantine incoming stream updates, flag the UI projection as stale (`STALE STATE — SYNCHRONIZING`), and trigger an authoritative snapshot request.

### 9.2 Sequence Scope Specification
- **Primary Design Contract:** A **global, monotonically increasing 64-bit integer** per engine runtime instance across all state-mutating events.
- **Domain-Partitioned Alternative:** In the event that market quote density ($>50,000	ext{ ticks/sec}$) overwhelms client sequence evaluation, the boundary design permits splitting into two independent sequence scopes:
  1. `stream:market_data:<instrument_id>`: Independent sequence counter for high-frequency price feeds.
  2. `stream:trading_engine:global`: Unified sequence counter for orders, fills, risk, positions, and lifecycle.
  Both scopes independently enforce the deterministic handshake and gap evaluation rules.

---

## 10. CONNECTION LIFECYCLE

The presentation client transitions through seven discrete operational connection states:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                CONNECTION STATE MACHINE                                     │
├───────────────────┬───────────────────────────────┬─────────────────────────────────────────┤
│ State             │ Presentation Behavior         │ Operator Action Permissions             │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 1. CONNECTING     │ Grey badge: "CONNECTING..."   │ All trading & control actions blocked   │
│ 2. CONNECTED      │ Blue badge: "SESSION OPEN"    │ Inbound buffer active; mutations blocked│
│ 3. SYNCING        │ Amber badge: "SYNCING STATE"  │ Ordinary mutations blocked;             │
│                   │ Data tables 50% opacity       │ Emergency Flatten available via Guard   │
│ 4. LIVE           │ Green badge: "CONNECTED"      │ All authorized actions active           │
│                   │ Real-time 60fps rendering     │                                         │
│ 5. DEGRADED       │ Amber pulse: "FEED DEGRADED"  │ Trading permitted; risk warnings active │
│                   │ Visual warnings displayed     │                                         │
│ 6. RESYNC_REQ     │ Amber solid: "RESYNC NEEDED"  │ Ordinary mutations disabled;            │
│                   │ Stream quarantined            │ Automatic snapshot triggered            │
│ 7. DISCONNECTED   │ Red badge: "DISCONNECTED"     │ All action buttons disabled;            │
│                   │ Workspace frozen / dimmed     │ Exponential reconnection backoff active │
└───────────────────┴───────────────────────────────┴─────────────────────────────────────────┘
```

---

## 11. COMMAND BOUNDARY & OPERATIONAL TAXONOMY

All state-mutating interactions initiated from UI workspaces must traverse an audited, authenticated command ingestion pipeline.

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                                  INBOUND COMMAND PIPELINE
══════════════════════════════════════════════════════════════════════════════════════════════════════

      UI CLIENT                    COMMAND GATEWAY                   DOMAIN SERVICE           ENGINE CORE
          │                               │                                 │                      │
          ├──── Submit Command Request ──►│                                 │                      │
          │     (Token + Payload)         │                                 │                      │
          │                               ├── 1. Authenticate Identity      │                      │
          │                               ├── 2. Validate Authorization     │                      │
          │                               ├── 3. Assign Command ID & Trace  │                      │
          │                               ├── 4. Record Audit Log Entry     │                      │
          │                               │                                 │                      │
          │◄─── Command Acknowledged ─────┤ (Status: SUBMITTING -           │                      │
          │                               │  backend-confirmed submission)  │                      │
          │                               │                                 │                      │
          │                               ├───── Route Authorized Action ──►│                      │
          │                               │                                 ├── Validate Rules ───►│
          │                               │                                 │   (Risk / Guard)     │
          │                               │                                 │                      │
          │                               │◄──── Authoritative Result ──────┼── Execution Event ───┤
          │◄─── Command Final Result ─────┤      (Success / Veto / Error)   │                      │
          │     (Enveloped Event)         │                                 │                      │
```

### 11.1 Structural Command Prohibitions
To preserve trading determinism and prevent unauthorized execution paths, the following routes are physically prohibited by the boundary design:
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{ExecutionRouter directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Broker Adapter directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Market Data Provider directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Direct Portfolio / Accounting Mutation}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Direct Risk Limit Bypass}$

### 11.2 Command Taxonomy
Commands are strictly segregated into two functional classes:

#### A. Administrative / Control Commands (Engine Lifecycle)
1. `PAUSE`: Transitions `TradingGuard` from `NORMAL` to `PAUSED`. Blocks new strategy evaluations; working orders remain active.
2. `RESUME`: Transitions `TradingGuard` from `PAUSED` to `NORMAL`. Re-enables automated strategy evaluation.
3. `KILL_SWITCH`: Immediately trips `TradingGuard` into `HALTED`. Fails closed: blocks all speculative entries, cancels working orders, retains emergency exit capability.
4. `RECOVERY`: Operator-mediated recovery from `HALTED` to `NORMAL`. Requires dual-factor operator confirmation token.
5. `SHUTDOWN`: Orderly engine termination sequence; drains queues and flushes telemetry.

#### B. Trading Commands (Order & Position Operations)
1. `MANUAL_PAPER_ENTRY`: Proposed manual limit/market order entry. **Must route via `ManualOrderService` into Phase 6 Risk.**
2. `MANUAL_PAPER_EXIT`: Discretionary position reduction or closure. Routes through Phase 6 Risk with `PositionEffect.CLOSE`.
3. `CANCEL_ORDER`: Requests withdrawal of an open working order via `ExecutionRouter.cancel()`.
4. `REPLACE_ORDER`: Atomic cancel-replace request validating price and quantity modifications.
5. `EMERGENCY_FLATTEN`: System-wide portfolio unwind. Invokes `TradingGuard.emergency_flatten()`, submitting risk-reducing market exits.

### 11.3 Future Manual Order Boundary Specification
The `ManualOrderService` is a **FUTURE PROPOSED** backend boundary not implemented in Phases 1–8. When implemented, it must adhere strictly to the following contract:

```
┌──────────────────────────────────┐
│          UI MANUAL FORM          │
└─────────────────┬────────────────┘
                  │ Operator parameters (Instrument, Side, Qty, LimitPrice)
                  ▼
┌──────────────────────────────────┐
│   FUTURE MANUAL ORDER SERVICE    │
│  • Synthesizes SignalCandidate   │
│  • Tags metadata: SOURCE=MANUAL  │
│  • Generates stable Signal ID    │
└─────────────────┬────────────────┘
                  │ SignalCandidate
                  ▼
┌──────────────────────────────────┐
│   PHASE 6 RISK MANAGEMENT GATE   │◄── MANDATORY GATE (CANNOT BE BYPASSED)
│  • Capital allocation check      │
│  • Max position size limit       │
│  • Daily loss limit verification │
│  • Emits ApprovedTradeIntent     │
└─────────────────┬────────────────┘
                  │ ApprovedTradeIntent
                  ▼
┌──────────────────────────────────┐
│     PHASE 7 EXECUTION ROUTER     │◄── MANDATORY ROUTER (CANNOT BE BYPASSED)
│  • Trading session calendar guard│
│  • Idempotency key evaluation    │
│  • Dispatches to Paper Adapter   │
└──────────────────────────────────┘
```

---

## 12. COMMAND RESULT MODEL & LIFECYCLE

When an operator submits a command, the API gateway transports authoritative lifecycle transitions back to the client:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               COMMAND LIFECYCLE PHASES                                      │
├──────────────────────┬──────────────────────────────────────────────────────────────────────┤
│ Result Status        │ Meaning & Invariant                                                  │
├──────────────────────┼──────────────────────────────────────────────────────────────────────┤
│ 1. ACCEPTED          │ API Gateway received command, validated schema, verified identity.   │
│ 2. SUBMITTING        │ Backend-confirmed command processing/submission state.               │
│ 3. COMPLETED         │ Authoritative execution confirmed (e.g. order planned/acked, guard). │
│ 4. REJECTED          │ Vetoed by business logic (e.g. daily loss exceeded, risk gate trip). │
│ 5. FAILED            │ System error during routing or dispatch (e.g. calendar validation).  │
│ 6. AMBIGUOUS         │ Transport dropped before confirmation; client must query audit log.  │
└──────────────────────┴──────────────────────────────────────────────────────────────────────┘
```

### 12.1 UI Order State vs. Authoritative Order State
To prevent premature or false representation of execution truth, client presentation states are strictly decoupled from engine execution states:

```
┌──────────────────────────────────┐       ┌──────────────────────────────────┐
│      CLIENT-SIDE PRESENTATION    │       │     AUTHORITATIVE BACKEND        │
│               STATES             │       │         ENGINE STATES            │
├──────────────────────────────────┤       ├──────────────────────────────────┤
│ LOCAL_REQUESTED                  │       │ (Engine unaware of draft form)   │
│ SUBMITTING                       │──────►│ Backend-Confirmed Processing     │
│                                  │       │                                  │
│ (Awaiting Backend Event)         │──────►│ RiskEngine.evaluate()            │
│ (Awaiting Backend Event)         │──────►│ ExecutionPlanner.plan_order()    │
│                                  │       │                                  │
│ ACKNOWLEDGED                     │◄──────│ CanonicalOrderStatus.ACKNOWLEDGED│
│ PARTIALLY_FILLED                 │◄──────│ CanonicalOrderStatus.PARTIAL_FILL│
│ FILLED                           │◄──────│ CanonicalOrderStatus.FILLED      │
│ CANCELLED                        │◄──────│ CanonicalOrderStatus.CANCELLED   │
│ REJECTED                         │◄──────│ CanonicalOrderStatus.REJECTED    │
│ UNKNOWN (Quarantine)             │◄──────│ State Unreconciled / Desynced    │
└──────────────────────────────────┘       └──────────────────────────────────┘
```

- **`LOCAL_REQUESTED`:** Represents client-side presentation/request state only. It exists solely within local client memory while the operator fills or submits an order form, prior to confirmed API gateway ingestion. It never implies backend acceptance.
- **`SUBMITTING`:** Represents a backend-confirmed command processing/submission state according to the future API command contract. It is confirmed once the gateway has validated the command schema, verified authorization, and queued it for downstream domain routing. **API transport receipt alone must NOT automatically become authoritative execution state.**
- **`ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, and `UNKNOWN`:** These states remain authoritative execution truth. The UI renders them **only** when represented by backend execution events/contracts emitted by Phase 7. The UI must never fabricate execution truth.

---

## 13. DATA AUTHORITY MATRIX

This matrix defines the absolute source of truth and mutation rights across all functional domains:

| Functional Domain | Sole Source of Truth | UI Presentation Role | API Gateway Role | Can UI Mutate? |
| :--- | :--- | :--- | :--- | :--- |
| **Market Quotes (L1)** | Phase 2 `InstrumentStateStore` | Display LTP, spread, volume | Stream updates, coalesce | **NO** |
| **Order Book Depth (L2)** | Phase 2 `InstrumentStateStore` | Render 5-level ladder | Stream depth snapshots | **NO** |
| **Candles & Bars** | Phase 3 `CandleEngine` | Render TradingView charts | Deliver finalized bars | **NO** |
| **Technical Features** | Phase 4 `FeatureEngine` | Overlay indicator curves | Export calculated values | **NO** |
| **Strategy Signals** | Phase 5 `SignalEngine` | Display candidate cards & hash | Stream signal envelopes | **NO** |
| **Risk Decisions & Limits** | Phase 6 `RiskEngine` | Display drawdown & loss gauges | Export limits & veto logs | **NO** |
| **Working Orders** | Phase 7 `ExecutionRouter` | Display working grid | Stream order updates | **NO** |
| **Execution Fills** | Phase 7 `ExecutionState` | Render fill ledger & tape | Deliver immutable fills | **NO** |
| **Positions & Watermarks** | Phase 8 `PortfolioRuntimeState`| Render open lots & watermarks | Stream position snapshots | **NO** |
| **Cash & Equity Balance** | Phase 8 `PortfolioRuntimeState`| Render equity curve & cash | Stream portfolio updates | **NO** |
| **Runtime Lifecycle** | Phase 8 `PipelineCoordinator` | Display state machine badge | Stream lifecycle events | **NO** |
| **Trading Guard State** | Phase 8 `TradingGuard` | Display Normal/Paused/Halted | Stream guard trip alerts | **NO** |
| **System Health** | Phase 8 `HealthMonitor` | Render heartbeat meters | Stream diagnostic telemetry | **NO** |
| **Lineage & Latency** | Phase 8 `TelemetryCollector` | Display $T_1 \dots T_{10}$ timings | Deliver correlation records | **NO** |
| **Control Commands** | Phase 8 `TradingGuard` | Command dispatch button | Authenticate & route | **YES (Audited)** |
| **Manual Paper Orders** | Phase 6/7 via `ManualService` | Ticket entry form | Authenticate & route | **YES (Audited)** |

---

## 14. BACKPRESSURE & FLOW CONTROL

High-frequency market bursts (e.g. 50,000 updates/second during market open) must not degrade client browser responsiveness or introduce memory starvation at the gateway.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                BACKPRESSURE REGULATION TIERS                                │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  [Tier 1: Engine Hot Path]                                                                  │
│  • Bounded non-blocking egress boundary; zero waiting on downstream consumers.              │
│  • If egress buffer reaches saturation, drop or coalesce non-critical presentation ticks.   │
│                                                                                             │
│  [Tier 2: API Gateway Egress]                                                               │
│  • Conflation Engine: Merges consecutive L1 quote updates for the same symbol within a      │
│    configurable presentation window (e.g. 50ms).                                            │
│  • Transactional Priority Queue: Order, fill, and risk events bypass the conflation queue   │
│    and are dispatched immediately.                                                          │
│                                                                                             │
│  [Tier 3: UI Client Processing]                                                             │
│  • Inbound Event Buffer: Absorbs WebSocket messages off the network thread.                 │
│  • Batch Rendering: Flushes state changes into React/DOM updates strictly synchronized      │
│    with browser render refresh rates (60fps requestAnimationFrame).                          │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 14.1 Conflation Policy Invariant (`UI-09`)
Market data conflation is strictly an egress presentation policy. It coalesces display updates for human operators. **Under no circumstances does presentation conflation throttle, alter, or delay tick processing within the Phase 1–8 trading engine.**

---

## 15. MULTI-CLIENT CONSISTENCY

In multi-operator environments (e.g. lead trader, risk officer, quantitative observer viewing concurrent screens), the boundary ensures uniform situational awareness:

1. **Unified Sequence Space:** All clients consume events stamped from the same monotonically increasing sequence space.
2. **Independent Session Buffering:** Each client maintains an independent WebSocket connection and stream buffer. A network stall on an observer tablet does not stall an operator workstation.
3. **Stateless Snapshot Parity:** Point-in-time snapshots generated at sequence $S_{	ext{snap}}$ provide identical state projections to any requesting client.
4. **Broadcast Notification of Mutations:** When Operator A issues an administrative `PAUSE` or emergency `KILL_SWITCH`, the resultant engine event envelope is broadcast concurrently to all connected sessions, ensuring instant visual alignment.

---

## 16. SECURITY & ACCESS CONTROL BOUNDARY

Detailed security architecture and concrete technology selection are explicitly deferred to the **SECURITY ARCHITECTURE REVIEW**. The boundary architecture establishes the following non-negotiable conceptual security requirements:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                              CONCEPTUAL SECURITY REQUIREMENTS                               │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  1. Authenticated Client Identity:                                                          │
│     • Boundary must verify and authenticate the identity of connecting clients and callers.│
│                                                                                             │
│  2. Authorization & Operation Scoping:                                                      │
│     • Explicit permission checks governing stream subscriptions, snapshot queries, and      │
│       operational actions.                                                                  │
│                                                                                             │
│  3. Command Authorization:                                                                  │
│     • Segregated validation ensuring only authorized operators can issue administrative     │
│       lifecycle or trading commands.                                                        │
│                                                                                             │
│  4. Replay & Tamper Protection:                                                             │
│     • Inbound mutating commands require unique command identities and replay protection     │
│       mechanisms to prevent duplicate or stale execution.                                   │
│                                                                                             │
│  5. Command Identity & Correlation:                                                         │
│     • Every command must carry end-to-end correlation tracking across boundary layers.      │
│                                                                                             │
│  6. Transport Integrity & Confidentiality:                                                  │
│     • Strong transport-layer encryption and tamper protection across external network       │
│       boundaries.                                                                           │
│                                                                                             │
│  7. Auditability:                                                                           │
│     • Deterministic audit logging of all authenticated commands, timestamps, caller         │
│       identities, and operational outcomes.                                                 │
│                                                                                             │
│  NOTE: Concrete technologies (TLS versions, JWT/OAuth, mTLS, API keys, IdP, nonce           │
│        algorithms, audit databases) are NOT YET DECIDED and remain deferred to the          │
│        dedicated Security Architecture Review.                                              │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 17. OBSERVABILITY & EXECUTION LINEAGE

The boundary layer preserves and projects the frozen Phase 8 $T_1 \dots T_{10}$ lineage measurement model without alteration.

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                                    T1–T10 LINEAGE DISSECTION
══════════════════════════════════════════════════════════════════════════════════════════════════════

   T1: Market Tick Ingestion (Gateway)
   T2: Strategy Signal Evaluation (SignalEngine)
   T3: Risk Evaluation Begin (RiskEngine)
   T4: Trade Intent Approved (RiskEngine)
   T5: Order Planned (ExecutionPlanner)
   T6: Router Dispatched to Adapter (Router)
   T7: Wire Ingress Timestamp (Wire Dispatch)
   T8: Adapter Order Acknowledged (ExecutionState)
   T9: Fill Event Ingested (ExecutionState)
   T10: Position & Portfolio Updated (PortfolioState)
   ───────────────────────────────────────────────────
   Total Pipeline Latency = T10 - T1 (Nanoseconds)
```

### 17.1 Distinction of Measurement Boundaries
To maintain rigorous scientific integrity in telemetry displays:
- **Authoritative Engine Telemetry ($T_1 \dots T_{10}$):** $T_1 \dots T_{10}$ measurements originate from the authoritative Phase 8 telemetry contract and are transported without recomputation. They represent actual engine execution speed.
- **API / Egress Timestamp ($T_{	ext{egress}}$):** Transport measurement stamped when the event envelope or snapshot is emitted by the API distribution gateway.
- **Client Ingress Timestamp ($T_{	ext{ui\_receive}}$):** Presentation/network measurement recorded by the UI client upon packet ingress for local diagnostic latency evaluation.
- **No Synthetic Lineage:** The UI displays backend $T_1 \dots T_{10}$ metrics verbatim. The UI must never recompute, synthesize, or infer internal engine timings from browser network timestamps.

---

## 18. VERSIONING STRATEGY

To ensure independent evolvability of frontend consoles and backend services, versioning is enforced across all contract interfaces:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   VERSIONING CONTRACTS                                      │
├──────────────────────┬──────────────────────┬───────────────────────────────────────────────┤
│ Contract Interface   │ Version Identifier   │ Compatibility & Deprecation Policy            │
├──────────────────────┼──────────────────────┼───────────────────────────────────────────────┤
│ Event Envelope       │ event_version        │ SemVer (MAJOR.MINOR.PATCH). Unknown minor     │
│                      │ (e.g. "1.0.0")       │ fields accepted; major bump quarantined.      │
│ Data Transfer DTOs   │ dto_version          │ Explicit DTO versioning (e.g. "1.0").         │
│                      │ (e.g. "1.0")         │ Strict field typing; no arbitrary schemas.    │
│ State Snapshot       │ snapshot_version     │ Versioned snapshot schema manifest.           │
│ Command Schema       │ command_version      │ Versioned command parameters and validation.  │
│ Command Result       │ result_version       │ Versioned result payload contracts.           │
└──────────────────────┴──────────────────────┴───────────────────────────────────────────────┘
```

---

## 19. FAILURE SEMANTICS & RESILIENCE

The boundary architecture adheres strictly to the rule: **NO SYNTHETIC TRADING TRUTH.** When subsystems fail, the UI must clearly reflect uncertainty:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                FAILURE RESOLUTION MATRIX                                    │
├───────────────────────────┬───────────────────────────────────┬─────────────────────────────┤
│ Failure Scenario          │ System Action                     │ UI Presentation Behavior    │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Engine Process Offline    │ Gateway terminates stream session │ Red banner: "ENGINE DOWN"   │
│                           │ Disconnects active WebSockets     │ All action buttons disabled │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Snapshot Unavailable      │ Client initiates exponential      │ Amber badge: "SYNC FAILED"  │
│                           │ backoff retry loop (max 5000ms)   │ Grids dimmed to 50% opacity │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ WebSocket Dropped         │ Reconnection loop activated       │ Amber pulsing: "RECONNECT"  │
│                           │ Inbound buffer primed on socket   │ Grids display stale watermark│
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Confirmed Sequence Gap    │ Quarantines incoming stream       │ Amber tag: "GAP DETECTED"   │
│                           │ Issues immediate snapshot request │ Disables ordinary mutations │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Malformed / Bad Event     │ Drops message to quarantine log   │ Warning notification posted │
│                           │ Does NOT mutate local projection  │ System telemetry logs alert │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Unsupported Schema Major  │ Rejects message; marks quarantine │ Persistent red banner:      │
│                           │ Increments schema error counter   │ "CLIENT UPDATE REQUIRED"    │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Command Timeout           │ Gateway reports ambiguous status  │ Warning modal displayed;    │
│                           │ Queries engine audit log          │ Operator guided to verify   │
└───────────────────────────┴───────────────────────────────────┴─────────────────────────────┘
```

---

## 20. API STYLE DECISION & COMPARATIVE EVALUATION

The choice of network transport between the API Distribution Layer and UI clients represents a major architectural decision. Performance values are treated as future measurable targets, not transport guarantees. The following matrix evaluates the candidate architectural patterns:

| Evaluation Dimension | 1. Pure REST (Pull) | 2. WebSocket (Full Duplex) | 3. Server-Sent Events (SSE) | 4. gRPC-Web (Streaming) | 5. Hybrid (REST + WebSocket) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Snapshot Delivery** | **Optimal:** High-speed cacheable payloads. | Sub-optimal: Large binary frames over socket. | Unsuitable: Chunked one-way stream. | Good: Protobuf binary payload. | **Optimal:** Clean REST GET endpoints. |
| **Real-Time Event Stream** | Unsuitable: Inefficient polling, high latency. | **Optimal:** Persistent low-latency event streaming (target). | Good: Lightweight text-based push. | Good: Multiplexed HTTP/2 streams. | **Optimal:** Dedicated persistent push. |
| **Bidirectional Commands** | Good: Standard HTTP POST methods. | Good: Client frames over socket. | Unsuitable: Requires separate REST API. | Good: Bidirectional streaming. | **Optimal:** Audited HTTP POST commands. |
| **Browser Compatibility** | Universal (HTTP/1.1, HTTP/2) | Universal (RFC 6455) | Universal (EventSource API) | Requires gRPC-Web proxy translation. | **Universal (Standard web tech)** |
| **Reconnection Ergonomics** | Stateless (Trivial) | Requires explicit handshake & resume. | Built-in browser reconnection & IDs. | Complex stream restart mechanics. | **Structured (Standard handshake)** |
| **Causal Ordering & Gaps** | None (Independent requests) | **Strict:** FIFO frame delivery. | Strict: FIFO text stream. | Strict: HTTP/2 frame stream. | **Strict:** Enveloped sequence checks. |
| **Backpressure Handling** | Client pull pace governs load. | TCP flow control + frame queue drops. | Browser buffer management. | HTTP/2 stream flow control. | **Multi-tier backpressure control.** |
| **Operational Complexity** | Minimal (Standard API gateways) | Moderate (Stateful socket servers) | Low (HTTP connection retention) | High (Envoy proxy, proto generation)| **Moderate (Well-understood pattern)** |
| **Inspection & Debugging** | Trivial (cURL, DevTools) | Clean (Browser WS frame inspector) | Clean (Browser EventStream inspector)| Complex (Requires binary decoders)  | **Optimal (Standard DevTools support)**|
| **Suitability for Tradego** | Inadequate for live tick feeds. | Inadequate for large cold snapshots. | Asymmetric (Needs separate REST). | Over-engineered for browser MVP. | **RECOMMENDED PROPOSAL** |

### 20.1 Architectural Recommendation: Hybrid Pattern (Proposal Only)
- **Snapshot & Command Transport:** Standard HTTP/1.1 (or HTTP/2) JSON REST. Used for point-in-time state snapshots (`GET /api/v1/state/snapshot`), historical lineage queries, and authenticated command submissions (`POST /api/v1/commands/control`, `POST /api/v1/commands/order`).
- **Event Streaming Transport:** Persistent, bi-directional WebSocket (RFC 6455). Used exclusively for persistent low-latency event streaming of versioned `TradegoEventEnvelope` messages.
- *Governance Notice:* This recommendation constitutes an **architectural design proposal only**. Technology selection and package installation require formal approval in the Implementation Readiness Review.

---

## 21. FUTURE DEPLOYMENT TOPOLOGY OPTIONS

The structural decoupling of the Trading Engine and API Distribution Layer allows three potential deployment topologies:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               DEPLOYMENT TOPOLOGY OPTIONS                                   │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  [OPTION A: IN-PROCESS DUAL-THREAD]                                                         │
│  ┌─────────────────────────────────────────────────────────────────────────┐               │
│  │ Single Host Process (Python Virtual Machine)                            │               │
│  │  ┌─────────────────────────┐         ┌──────────────────────────────┐   │               │
│  │  │ Trading Engine (P1–P8)  │ ──────► │ API Gateway (Asyncio Worker) │   │ ──► [Browser] │
│  │  └─────────────────────────┘  Queue  └──────────────────────────────┘   │               │
│  └─────────────────────────────────────────────────────────────────────────┘               │
│                                                                                             │
│  [OPTION B: SEPARATE PROCESS ON SAME HOST]                                                  │
│  ┌───────────────────────────┐          ┌──────────────────────────────┐                   │
│  │ Trading Engine Process    │ ───────► │ API Distribution Process     │ ──► [Browser]     │
│  │ (High-Priority Core)      │ IPC/SHM  │ (Network Daemon)             │                   │
│  └───────────────────────────┘          └──────────────────────────────┘                   │
│                                                                                             │
│  [OPTION C: DISTRIBUTED SERVICES]                                                           │
│  ┌───────────────────────────┐          ┌──────────────────────────────┐                   │
│  │ Trading Host              │ ───────► │ API Gateway Cluster          │ ──► [Browser]     │
│  │ (Isolated Engine Appliance│ Network  │ (Load-Balanced Frontends)    │                   │
│  └───────────────────────────┘          └──────────────────────────────┘                   │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 21.1 Comparative Deployment Trade-Off Analysis

| Evaluation Dimension | Option A: In-Process Dual-Thread | Option B: Separate Process (Same Host) | Option C: Distributed Services |
| :--- | :--- | :--- | :--- |
| **Inter-Layer Latency Target** | Sub-microsecond (In-memory queue pointer) | Low (10–50 $\mu\text{s}$ via IPC / SHM) | Moderate (100–500 $\mu\text{s}$ over LAN) |
| **Fault Isolation** | Poor (Gateway crash/OOM impacts engine) | **High** (Engine isolated from socket leaks)| **Maximum** (Physical server boundary) |
| **GIL Contention (Python)**| Risk of GIL contention under load | **Zero GIL contention** (Separate VM) | Zero GIL contention (Separate VM) |
| **Engineering Complexity** | Minimal (Single deployable artifact) | Moderate (Requires robust IPC boundary) | High (Network partitions, service mesh) |
| **Host Security Perimeter** | Combined perimeter | **Tiered:** Engine has zero public ports | Tiered: Engine strictly on private LAN |
| **Architectural Candidate Status** | **Candidate for Development & Paper** | **Proposed Candidate (Subject to Evaluation)**| Deferred to Multi-Broker Live Phase |

**Mandatory Topology Governance:** Options A, B, and C remain architectural candidates. Option B is a proposed candidate but is NOT a frozen implementation decision. **Final process topology requires performance, failure-isolation, operational, and security evaluation.**

---

## 22. TECHNOLOGY DECISION STATUS

All technological elements referenced across the boundary design are categorized by decision state:

| Subsystem Component | Candidate Technology | Architectural Status | Governance & Evaluation Notes |
| :--- | :--- | :--- | :--- |
| **Core Trading Engine** | Pure Python 3.12 (In-Memory) | **CURRENT / FROZEN** | Verified in Phases 1–8. Zero changes authorized. |
| **Telemetry Buffer** | Thread-safe Bounded Deque | **CURRENT / FROZEN** | Verified in Phase 8. Non-blocking latency logger. |
| **Snapshot Transport** | HTTP REST (JSON) | **PROPOSED** | Subject to Implementation Readiness Review. |
| **Stream Transport** | WebSocket (RFC 6455) | **PROPOSED** | Subject to Implementation Readiness Review. |
| **Serialization Format**| JSON (Initial Transport) | **PROPOSED** | Clean debugging; binary serialization deferred. |
| **Gateway Process Model**| Option B (Separate Process IPC)| **PROPOSED CANDIDATE**| Subject to topology performance and security evaluation. |
| **Egress Buffer Channel**| Bounded Non-Blocking Egress Boundary| **NOT YET DECIDED** | Mechanism and capacity require technology/performance evaluation. |
| **Security Architecture**| Authentication / RBAC / Audit | **NOT YET DECIDED** | Deferred to dedicated Security Architecture Review. |
| **Binary Protocol Format**| Protocol Buffers / MsgPack | **NOT YET DECIDED** | Recorded as Architecture Suggestion only. |

---

## 23. OPEN ARCHITECTURAL QUESTIONS FOR SENIOR REVIEW

1. **Egress Boundary Mechanism & Capacity:** What concrete bounded non-blocking egress mechanism (e.g. in-process bounded queue, shared-memory transport, or local IPC) and capacity depth best satisfy hot-path non-interference under high-throughput market conditions?
2. **Snapshot Caching Depth:** Should the API distribution gateway maintain an independent cache of the latest snapshot sequence $S_{\text{snap}}$, or should every client snapshot request synchronously poll the engine state store?
3. **Market Quote Conflation Horizon:** What is the optimal default conflation interval for L1 quote streaming to browser clients (e.g. 25ms, 50ms, or 100ms) to maximize visual fluidity while minimizing client rendering thrashing?
4. **Historical Lineage Retention Limits:** What memory bound should be enforced on historical $T_1 \dots T_{10}$ lineage records accessible via the boundary API (e.g. last 1,000 trades vs. full intraday retention)?
5. **Command Delivery Frontier Mechanism:** What formal transport-level acknowledgment model (e.g. sequence heartbeats, high-water acknowledgments) will best establish the authoritative delivery frontier required to prevent premature gap declaration during transient network lag?

---

## 24. STRICT NON-GOALS

The following items are explicitly prohibited from this phase and subsequent boundary implementation:
- **No Frontend Framework Installation:** Do not install Next.js, React, Tailwind, Vite, or UI libraries.
- **No Server Code Generation:** Do not write FastAPI apps, WebSocket handlers, or routing scripts.
- **No Database Creation:** Do not define SQLite, PostgreSQL, Redis, or Kafka configurations.
- **No Derivative Contracts:** Do not define Greeks, option strikes, or margin offset interfaces.
- **No Live Trading Broker Adapters:** Do not integrate Interactive Brokers, Zerodha, or live broker APIs.
- **No AI / LLM on Hot Path:** Do not introduce artificial intelligence into trading decisions or risk evaluations.

---

## 25. MANDATORY ARCHITECTURE INVARIANTS

The UI ↔ API Boundary Architecture strictly enforces the fourteen approved UI invariants:

- **INVARIANT-UI-01 (Hot-Path Isolation):** UI transport, serialization, networking, browser rendering, and client backpressure MUST NOT synchronously block the Phase 1–8 trading pipeline.
- **INVARIANT-UI-02 (Sole Authority):** The backend runtime is the sole authoritative owner of market state, signals, risk decisions, orders, fills, positions, portfolio balances, runtime lifecycle, and system health. The browser NEVER becomes the authoritative source of truth.
- **INVARIANT-UI-03 (No Optimistic Trading Truth):** The UI shall never treat local optimistic state as execution truth. An order is never represented as submitted, acknowledged, or filled until confirmed by an authoritative backend event.
- **INVARIANT-UI-04 (Mandatory Risk Gate):** Under no circumstances may the UI bypass Phase 6 Risk Management. Every proposed order must pass through the authoritative risk engine. Direct dispatch (`UI → ExecutionRouter`) is strictly prohibited.
- **INVARIANT-UI-05 (Mandatory Execution Gate):** The UI shall never bypass the Phase 7 `ExecutionRouter`. Direct communication between the UI and broker adapters or paper adapters is strictly prohibited.
- **INVARIANT-UI-06 (No Direct Gateway Access):** The UI shall never connect directly to market data providers or feed gateways. All market quotes consumed by the UI flow through the normalized backend state store.
- **INVARIANT-UI-07 (No Synthetic Accounting):** The UI shall never manufacture or locally calculate authoritative fills, positions, cost basis, or realized PnL.
- **INVARIANT-UI-08 (Authoritative Snapshot Resync):** The UI must synchronize with backend state through an authoritative initial snapshot and detect stream sequence gaps, triggering resynchronization when gaps occur.
- **INVARIANT-UI-09 (Independent Conflation Policy):** Market data conflation (e.g. 50ms display throttling) is strictly a client presentation/rendering policy. It MUST NOT alter or throttle the underlying Phase 1–8 market-event processing rate or trading logic.
- **INVARIANT-UI-10 (No Engine Semantic Changes):** UI features, controls, and projections must conform strictly to existing Phase 1–8 contracts and never alter backend state machine semantics.
- **INVARIANT-UI-11 (Backpressure Insulation):** Client-side rendering slowdowns, tab throttling, or network buffer congestion must be isolated at the presentation boundary and cannot propagate backpressure into the trading runtime.
- **INVARIANT-UI-12 (Contract Consumption):** Future UI features must consume approved, versioned backend contracts without introducing undocumented or ad-hoc interfaces.
- **INVARIANT-UI-13 (Deterministic Handshake & Zero Silent Loss):** Snapshot and event-stream activation must provide a deterministic handoff point such that every event after the authoritative snapshot sequence is either delivered to the client or detected as a sequence gap requiring resynchronization. Silent event loss is prohibited.
- **INVARIANT-UI-14 (Command Boundary Isolation):** All mutating UI actions must route through an Authenticated Command Boundary to the appropriate backend domain service and safety guards. Direct UI invocation of `ExecutionRouter`, broker adapters, or market data feeds is structurally prohibited.

---

## 26. ACCEPTANCE CRITERIA & REVIEW CHECKLIST

- [x] Defined conceptual process boundary between Engine, State Export, API Gateway, and UI Client.
- [x] Strictly separated CURRENT EXISTING SYSTEM from FUTURE PROPOSED SYSTEM.
- [x] Defined technology-neutral bounded non-blocking egress boundary (uncommitted mechanism and capacity).
- [x] Formulated explicit Authoritative Snapshot Consistency Contract ($S_{\text{snap}}$ sequence frontier, sequence $\le S_{\text{snap}}$ preceding, sequence $> S_{\text{snap}}$ succeeding).
- [x] Maintained implementation neutrality for snapshot consistency (no prescribed locks, copy-on-write, or transactions).
- [x] Formulated Event Stream Contract with `TradegoEventEnvelope<T>` across 10 architectural categories.
- [x] Preserved frozen UI v1.1 synchronization lifecycle (Snapshot $\to$ Buffer $\to$ Reconcile from $S_{\text{snap}} \to$ Live).
- [x] Incorporated Event Delivery Frontier requirement for gap declaration.
- [x] Defined Connection Lifecycle across 7 discrete operational states.
- [x] Specified Authenticated Command Boundary separating Control Commands from Trading Commands.
- [x] Explicitly prohibited direct routes (`UI → Router`, `UI → Broker`, `UI → Market Provider`).
- [x] Preserved Emergency Flatten semantics routed strictly through `TradingGuard.emergency_flatten()`.
- [x] Clarified Command Result Model: `LOCAL_REQUESTED` is client-side only; `SUBMITTING` is backend-confirmed processing; execution truth strictly from Phase 7.
- [x] Established multi-tier Backpressure handling protecting the trading engine without frozen queue mechanics.
- [x] Addressed Multi-Client Consistency across concurrent operator workstations.
- [x] Defined technology-neutral Security Requirements, explicitly deferring concrete mechanisms to Security Architecture Review.
- [x] Preserved $T_1 \dots T_{10}$ lineage measurement model, remoremoving hardcoded clock referencesand distinguishing engine vs. transport vs. client timestamps.
- [x] Replaced universal/guaranteed performance claims with measurable target language ("persistent low-latency event streaming").
- [x] Retained Options A, B, and C as architectural candidates, marking Option B as proposed candidate requiring topology evaluation.
- [x] Provided Comprehensive API Style Comparison Matrix with hybrid recommendation.
- [x] Categorized technology decisions as CURRENT, PROPOSED, and NOT YET DECIDED.
- [x] Preserved all mandatory UI Architecture Invariants (UI-01 through UI-14).
- [x] ZERO Phase 1–8 source files or tests modified; ZERO packages installed; ZERO implementation code created.

---

**END OF BOUNDARY ARCHITECTURE SPECIFICATION**  
*Tradego UI ↔ API / Event Boundary Architecture — Ready for Senior Architecture Review.*  

*Status: NOT FROZEN | NOT APPROVED*
