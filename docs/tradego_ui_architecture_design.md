# TRADEGO UI ARCHITECTURE & DESIGN SYSTEM SPECIFICATION
## Institutional Trading Terminal, Observability & Control Platform
### Document Version: 1.1.0-DRAFT | Status: Awaiting Senior Architecture Review
### Upstream Status: Phases 1–8 Approved and Frozen (Indian Cash Equities)

---

## 1. EXECUTIVE SUMMARY

Tradego is a deterministic, fail-closed algorithmic trading platform. Phases 1 through 8 have established and frozen the authoritative in-memory trading core:
- **Phase 1:** Market Data Gateway (tick normalization & transport isolation)
- **Phase 2:** Real-Time Market State (per-instrument L1/L2 state store & book depth)
- **Phase 3:** Candle & Time-Series Aggregation (deterministic multi-timeframe candle synthesis)
- **Phase 4:** Feature & Quantitative Analytics (microstructure features & indicators)
- **Phase 5:** Strategy & Signal Intelligence (rule-based evaluation, watermarks & arbitration)
- **Phase 6:** Risk Management & Portfolio Control (capital gates, drawdowns & sizing)
- **Phase 7:** Execution Layer & Order Lifecycle (idempotency, session guards & paper matching)
- **Phase 8:** Trading Runtime & System Orchestration (lifecycle, guards & T1–T10 lineage)

The trading runtime operates with verified in-memory determinism for Indian cash equities in paper trading mode.

**This document establishes the authoritative, design-only specification for the Tradego User Interface (Tradego UI) v1.1.**

The Tradego UI is designed as an **Observability, Monitoring, Control, Trading, and Quantitative Research Terminal**. It gives operators, quantitative researchers, and risk officers comprehensive visibility into live system state without becoming entangled in execution-critical paths.

### Fundamental Architectural Tenet
```
+-----------------------------------------------------------------------------+
|                     TRADING TRUTH vs. PRESENTATION PROJECTION               |
|                                                                             |
| The Tradego Backend is the SOLE AUTHORITATIVE SOURCE of all trading truth:  |
| - market state                                                              |
| - positions                                                                 |
| - PnL                                                                       |
| - risk                                                                      |
| - orders                                                                    |
| - fills                                                                     |
| - runtime state                                                             |
|                                                                             |
| The browser NEVER becomes the authoritative source of:                      |
| - market state                                                              |
| - positions                                                                 |
| - PnL                                                                       |
| - risk                                                                      |
| - orders                                                                    |
| - fills                                                                     |
| - runtime state                                                             |
|                                                                             |
| The Tradego UI is strictly a CLIENT-SIDE PROJECTION and presentation layer. |
| The UI NEVER calculates, mutates, or authorizes authoritative trading state.|
+-----------------------------------------------------------------------------+
```

---

## 2. CURRENT SYSTEM INSPECTION & BASELINE CONTEXT

Inspection of the frozen codebase (`services/`, `strategies/`, `docs/`, `tests/`) establishes the exact upstream foundation:
1. **Asset Class Scope:** Phase 6 risk rules and Phase 7 execution logic currently support **Indian cash equities** (NSE/BSE). Derivatives (futures, options, Greeks, margin offsets) are deferred to future phases and are **not** present in the core trading engine.
2. **Backend Services:** Phases 1–8 are implemented as pure Python in-memory service modules. No public REST API server, WebSocket server, or external database exists on the core engine.
3. **Internal Engine Profiling:** `TelemetryCollector` provides an internal in-memory bounded queue of `RuntimeCorrelationRecord` instances ($T_1 \dots T_{10}$) for runtime latency measurements. No external UI tap, lockless ring buffer tap, or streaming exporter currently exists. `InstrumentStateStore`, `ExecutionStateRegistry`, and `PortfolioRuntimeState` maintain authoritative snapshots in memory.
4. **Current Test Baseline:** 293 tests collected: 292 passed, 0 failed, 1 skipped.

---

## 3. UI ARCHITECTURE INVARIANTS

The following invariants are absolute design constraints. Any future frontend or API design that violates any invariant is structurally rejected:

### 3.1 Mandatory Architectural Invariants
- **UI never enters the trading hot path.**
- **UI never bypasses risk.**
- **UI never communicates directly with brokers.**
- **UI never communicates directly with market-data providers.**
- **UI never owns authoritative positions/PnL/orders.**
- **UI never treats local optimistic state as execution truth.**
- **UI reconnects through authoritative snapshot synchronization.**
- **UI detects event gaps.**
- **Snapshot and event-stream activation must provide a deterministic handoff point such that every event after the authoritative snapshot sequence is either delivered to the client or detected as a sequence gap requiring resynchronization. Silent event loss is prohibited.**
- **All mutating UI actions must route through the authenticated command boundary; direct access to routers, brokers, or market feeds is strictly prohibited.**
- **UI does not alter Phase 1–8 semantics.**
- **UI rendering backpressure cannot propagate into trading runtime.**
- **Future UI features must consume approved backend contracts.**

### 3.2 Detailed Engineering Invariant Specifications
* **INVARIANT-UI-01 (Hot-Path Isolation):** UI transport, serialization, networking, browser rendering, and client backpressure MUST NOT synchronously block the Phase 1–8 trading pipeline. UI transport and presentation operations must not synchronously block the Phase 1–8 trading pipeline.
* **INVARIANT-UI-02 (Sole Authority):** The backend runtime is the sole authoritative owner of market state, signals, risk decisions, orders, fills, positions, portfolio balances, runtime lifecycle, and system health. The browser NEVER becomes the authoritative source of market state, positions, PnL, risk, orders, fills, or runtime state.
* **INVARIANT-UI-03 (No Optimistic Trading Truth):** The UI shall never treat local optimistic state as execution truth. An order is never represented as submitted, acknowledged, or filled until confirmed by an authoritative backend event.
* **INVARIANT-UI-04 (Mandatory Risk Gate):** Under no circumstances may the UI bypass Phase 6 Risk Management. Every proposed order—whether automated or operator-initiated—must pass through the authoritative risk engine. Direct dispatch (`UI → ExecutionRouter`) is strictly prohibited.
* **INVARIANT-UI-05 (Mandatory Execution Gate):** The UI shall never bypass the Phase 7 `ExecutionRouter`. Direct communication between the UI and broker adapters or paper adapters is strictly prohibited.
* **INVARIANT-UI-06 (No Direct Gateway Access):** The UI shall never connect directly to market data providers or feed gateways. All market quotes consumed by the UI flow through the normalized backend state store.
* **INVARIANT-UI-07 (No Synthetic Accounting):** The UI shall never manufacture or locally calculate authoritative fills, positions, cost basis, or realized PnL.
* **INVARIANT-UI-08 (Authoritative Snapshot Resync):** The UI must synchronize with backend state through an authoritative initial snapshot and detect stream sequence gaps, triggering resynchronization when gaps occur.
* **INVARIANT-UI-09 (Independent Conflation Policy):** Market data conflation (e.g. 50ms display throttling) is strictly a client presentation/rendering policy. It MUST NOT alter or throttle the underlying Phase 1–8 market-event processing rate or trading logic.
* **INVARIANT-UI-10 (No Engine Semantic Changes):** UI features, controls, and projections must conform strictly to existing Phase 1–8 contracts and never alter backend state machine semantics.
* **INVARIANT-UI-11 (Backpressure Insulation):** Client-side rendering slowdowns, tab throttling, or network buffer congestion must be isolated at the presentation boundary and cannot propagate backpressure into the trading runtime.
* **INVARIANT-UI-12 (Contract Consumption):** Future UI features must consume approved, versioned backend contracts without introducing undocumented or ad-hoc interfaces.
* **INVARIANT-UI-13 (Deterministic Handshake & Zero Silent Loss):** Snapshot and event-stream activation must provide a deterministic handoff point such that every event after the authoritative snapshot sequence is either delivered to the client or detected as a sequence gap requiring resynchronization. Silent event loss is prohibited.
* **INVARIANT-UI-14 (Command Boundary Isolation):** All mutating UI actions must route through an Authenticated Command Boundary to the appropriate backend domain service and safety guards. Direct UI invocation of `ExecutionRouter`, broker adapters, or market data feeds is structurally prohibited.

---

## 4. UI GOALS & NON-GOALS

### 4.1 UI Goals
1. **High-Density Institutional Workspace:** A dark-mode, multi-pane desktop terminal optimized for sustained monitoring across 1080p, 1440p, and 4K displays.
2. **Authoritative Lineage Display:** Clear, interactive inspection of backend $T_1 \dots T_{10}$ latency measurements and cryptographic fingerprints for every executed trade.
3. **Fail-Closed Safety Visualization:** Unambiguous visual indicators for operational states (`RUNNING`, `PAUSED`, `HALTED`), active daily loss limits, and emergency kill switches.
4. **Resilient Presentation Synchronization:** Deterministic state recovery across network disconnects, tab reloads, and WebSocket reconnection cycles.
5. **Decoupled Architecture:** Strict separation between client presentation state, API transport envelopes, and backend domain models.

### 4.2 UI Non-Goals
1. **NO Hot-Path Logic in the Browser:** The UI will not size positions, validate risk parameters, compute SHA-256 idempotency keys, or synthesize client order IDs.
2. **NO Direct External Integrations:** No direct connections from browser clients to external broker endpoints, third-party market data feeds, or external databases.
3. **NO Mobile-First Compromises:** The platform is explicitly an institutional desktop trading workstation. No mobile-first layout degradations will be introduced.
4. **NO AI on the Trading Path:** Artificial Intelligence and LLM agents will never evaluate signals, approve trades, or dispatch orders. AI is strictly confined to offline, retrospective research and advisory queries.
5. **NO Unverified Framework Deployments:** Technology recommendations in this document are design proposals subject to an implementation readiness review prior to package installation.

---

## 5. FUTURE UI PRESENTATION / API BOUNDARY

The communication boundary between the Tradego Backend and the Tradego UI is a **future dedicated subsystem**. It serves as an asynchronous translation and distribution layer:

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                          FUTURE UI PRESENTATION / API BOUNDARY
══════════════════════════════════════════════════════════════════════════════════════════════════════

   ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
   │                           TRADEGO TRADING ENGINE (PHASES 1–8)                                │
   │                                                                                              │
   │   MarketEvent (T1) ──► State ──► Candle ──► Feature ──► Strategy (T2)                        │
   │                                                             │                                │
   │   Telemetry (T10) ◄── Accounting ◄── Fill (T9) ◄── Router ◄─┴── Risk (T3-4) ──► Intent (T5)  │
   └──────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                  │ Asynchronous State Export (Future Egress Boundary)
                                                  │ (Implementation designed separately; zero hot-path blocking)
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
   │                       FUTURE API / EVENT DISTRIBUTION LAYER (SEPARATE DESIGN)                │
   │                                                                                              │
   │   • State Snapshot Serializer (Produces authoritative point-in-time state projections)       │
   │   • Streaming Event Publisher (Packages runtime events into versioned EventEnvelopes)        │
   │   • Sequence Counter & Heartbeat Manager                                                     │
   │   • Authenticated Command Ingestion Gate (Enforces RBAC before passing to TradingGuard)      │
   └───────────────────────────────┬─────────────────────────────────────────┬────────────────────┘
                                   │ WebSocket (Push Streams)                │ REST (Pull / Command)
                                   │ (Enveloped events, quotes, updates)     │ (Snapshots, queries, commands)
                                   ▼                                         ▼
   ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
   │                                  TRADEGO UI CLIENT (BROWSER)                                 │
   │                                                                                              │
   │   ┌──────────────────────────────────────────────────────────────────────────────────────┐   │
   │   │                      INBOUND STREAM BUFFER & SEQUENCE VALIDATOR                      │   │
   │   │       • Sequence gap detection  • Snapshot reconciler  • 60fps rAF frame batcher     │   │
   │   └──────────────────────────────────────────┬───────────────────────────────────────────┘   │
   │                                              │ Reconciled UI Events                          │
   │                                              ▼                                               │
   │   ┌──────────────────────────────────────────────────────────────────────────────────────┐   │
   │   │                             CLIENT PRESENTATION STORES                               │   │
   │   │     • Projected Backend State (Read-Only)    • Local Presentation State (UI Only)    │   │
   │   └──────────────────────────────────────────┬───────────────────────────────────────────┘   │
   │                                              │ Reactive Selectors                            │
   │                                              ▼                                               │
   │   ┌──────────────────────────────────────────────────────────────────────────────────────┐   │
   │   │                           PRESENTATION & WORKSPACE PANELS                            │   │
   │   │      Terminal • Watchlist • Signals • Positions • Orders • Risk • Health • Lineage   │   │
   │   └──────────────────────────────────────────────────────────────────────────────────────┘   │
   └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. AUTHORITATIVE SYNCHRONIZATION MODEL

To guarantee that the UI never displays divergent or corrupt state across disconnections, restarts, or network jitter, the client follows a formal synchronization lifecycle:

```
   INITIAL SNAPSHOT
        ↓
   EVENT STREAM
        ↓
   SEQUENCE VALIDATION
        ↓
   LOCAL PROJECTION

   If an event gap is detected:

        EVENT GAP
           ↓
        RESYNC
           ↓
   AUTHORITATIVE SNAPSHOT
           ↓
     RESUME EVENTS
```

### 6.1 Synchronization Lifecycle Flow
The canonical synchronization handshake between UI and backend follows a strict sequence to prevent silent event loss:

```
                      ┌──────────────────────────────────────┐
                      │            CLIENT CONNECT            │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │       ESTABLISH STREAM SESSION       │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │        START INBOUND BUFFERING       │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │     REQUEST AUTHORITATIVE SNAPSHOT   │◄─────────────────┐
                      │     (REST /api/v1/state/snapshot)    │                  │
                      └──────────────────┬───────────────────┘                  │
                                         │                                      │
                                         ▼                                      │
                      ┌──────────────────────────────────────┐                  │
                      │       RECEIVE SNAPSHOT + S_snap      │                  │
                      └──────────────────┬───────────────────┘                  │
                                         │                                      │
                                         ▼                                      │
                      ┌──────────────────────────────────────┐                  │
                      │            APPLY SNAPSHOT            │                  │
                      │     (baseline_sequence = S_snap)     │                  │
                      └──────────────────┬───────────────────┘                  │
                                         │                                      │
                                         ▼                                      │
                      ┌──────────────────────────────────────┐                  │
                      │       RECONCILE BUFFERED EVENTS      │                  │
                      │   (Discard seq <= S_snap; check gaps)│                  │
                      └──────────────────┬───────────────────┘                  │
                                         │                                      │
                                         ▼                                      │
                      ┌──────────────────────────────────────┐                  │
                      │           VERIFY S_snap + 1          │                  │
                      │  (Check authoritative delivery head) │                  │
                      └──────────────────┬───────────────────┘                  │
                                         │                                      │
                                         ▼                                      │
           ┌────────────────────────────────────────────────────┐               │
           │              LIVE STREAM ENGAGEMENT                │               │
           │                                                    │               │
           │   Receive Event (seq = S_curr)                     │               │
           │   Expected: seq == S_last + 1                      │               │
           └─────────────┬────────────────────────┬─────────────┘               │
                         │                        │                             │
                   [Seq Valid]              [Event Gap]                         │
                         │                        │                             │
                         ▼                        ▼                             │
           ┌───────────────────────────┐  ┌───────────────────────────┐         │
           │ UPDATE LOCAL PROJECTION   │  │ FLAG STALE UI STATE       │         │
           │ S_last = S_curr           │  │ Display Reconnecting Amber│         │
           │ Render at 60fps rAF frame │  │ Discard Corrupt Stream    │         │
           └───────────────────────────┘  └─────────────┬─────────────┘         │
                                                        │                       │
                                                        └───────────────────────┘
                                                           Trigger Resync
```

### 6.2 Deterministic Snapshot / Stream Handshake
To eliminate race conditions during connection startup or reconnection, the system enforces a deterministic handshake invariant:

> **Deterministic Handoff Invariant:**  
> Snapshot and event-stream activation must provide a deterministic handoff point such that every event after the authoritative snapshot sequence is either delivered to the client or detected as a sequence gap requiring resynchronization. Silent event loss is prohibited.

#### Canonical Handshake Protocol Flow
1. **Client Connect & Establish Stream Session:** Upon initiating connection, the UI client establishes its transport session (e.g. WebSocket connection).
2. **Start Inbound Buffering:** The client immediately begins buffering all incoming enveloped stream events in memory before requesting or receiving the snapshot. The client must begin buffering inbound stream events before or concurrently with snapshot acquisition so that events occurring after the authoritative snapshot sequence cannot be silently lost.
3. **Request Authoritative Snapshot:** Concurrently, the UI client requests the authoritative baseline snapshot via REST (`GET /api/v1/state/snapshot`).
4. **Receive Snapshot + $S_{\text{snap}}$ & Apply Baseline:** Upon receipt of the snapshot, the client records $S_{\text{snap}} = \text{snapshot.authoritative\_sequence}$, applies the complete snapshot payload to local projection stores, and establishes $\text{last\_processed\_sequence} = S_{\text{snap}}$.
5. **Reconcile Buffered Events:** The client reconciles buffered stream events against $S_{\text{snap}}$ according to the sequence processing rules below. Any event where $\text{sequence} \le S_{\text{snap}}$ is discarded as already superseded by the snapshot. The first stream event applied must satisfy $\text{sequence} == S_{\text{snap}} + 1$.
6. **Verify $S_{\text{snap}} + 1$ & Event Delivery Frontier:** The client shall declare a sequence gap only when the stream/session contract provides an authoritative delivery frontier/high-water sequence, or otherwise establishes that the missing sequence cannot still arrive. Temporary network delay or ordinary event reordering must not by itself be interpreted as permanent event loss.
7. **Live Stream Engagement:** Once the buffer is reconciled and contiguous sequencing verified, the client transitions seamlessly to processing live incoming events.

### 6.3 Inbound Sequence Processing Semantics
Every inbound enveloped event delivered to the client stream buffer is evaluated against the authoritative local sequence watermark:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                           INBOUND SEQUENCE PROCESSING RULES                                 │
├──────────────────────────────────────┬────────────────────────┬─────────────────────────────┤
│ Condition                            │ Classification         │ Client Action               │
├──────────────────────────────────────┼────────────────────────┼─────────────────────────────┤
│ 1. incoming_sequence <=              │ Duplicate / Replayed   │ • Discard from projection   │
│    last_processed_sequence           │ Event                  │ • No state mutation         │
│                                      │                        │ • Telemetry debug log       │
├──────────────────────────────────────┼────────────────────────┼─────────────────────────────┤
│ 2. incoming_sequence ==              │ Valid Next Event       │ • Apply to local projection │
│    last_processed_sequence + 1       │ (Strict Causal Order)  │ • Increment sequence counter│
│                                      │                        │ • Batch for 60fps rAF frame │
├──────────────────────────────────────┼────────────────────────┼─────────────────────────────┤
│ 3. incoming_sequence >               │ Event Gap Detected     │ • Quarantine stream updates │
│    last_processed_sequence + 1       │ (Confirmed Gap via     │ • Flag UI as Stale (Amber)  │
│                                      │ Delivery Frontier)     │ • Trigger snapshot resync   │
└──────────────────────────────────────┴────────────────────────┴─────────────────────────────┘
```

> **Design Clarification on Event Delivery Frontier:**  
> The client shall declare a sequence gap only when the stream/session contract provides an authoritative delivery frontier/high-water sequence, or otherwise establishes that the missing sequence cannot still arrive. Temporary network delay or ordinary event reordering must not by itself be interpreted as permanent event loss. Transport-level frontier mechanisms belong to the future UI/API Boundary Architecture.

### 6.4 Sequence Scope Specification (Design Contract Only)
As a strict design contract:
- **Primary Contract Scope:** The authoritative event sequence is specified as a **global, monotonically increasing 64-bit integer** per trading engine instance across all state-mutating events (orders, fills, positions, risk states, lifecycle transitions).
- **Domain-Partitioned Alternative (Subject to Boundary Review):** If market data quote volume (L1/L2 updates) warrants stream isolation from transactional trading events, the future UI/API boundary design may define a **per-stream / per-domain sequence scope** (e.g. separate sequence counters for `market_data:<symbol>` vs `trading_engine:global`).
- **Invariant Guarantee:** Regardless of whether the finalized API design adopts a global sequence or domain-partitioned sequences, each independent sequence scope must independently enforce the deterministic handshake invariant and the three sequence processing cases above.

### 6.5 Snapshot Manifest & Stale State Semantics
1. **Snapshot Manifest:** Every authoritative REST state snapshot contains:
   - `snapshot_id`: UUIDv4 of the generation cycle.
   - `snapshot_timestamp`: Server UTC timestamp of state capture.
   - `authoritative_sequence`: Monotonically increasing 64-bit integer reflecting the exact event sequence counter at snapshot capture.
   - `runtime_state`: Active `RuntimeState` and `GuardState`.
   - `positions`: Complete map of active `PositionSnapshot` records.
   - `orders`: Complete list of open `ExecutionState` records.
   - `account`: Authoritative `AccountRiskState`.
2. **Visual Indication of Stale State & Emergency Action Availability:**
   - Global header badge switches to amber pulsing: `SYNC IN PROGRESS`.
   - Data grid rows dim to 50% opacity with subtle diagonal watermark stripes.
   - **Ordinary Trading Mutations Disabled:** Ordinary discretionary mutations (Manual Paper Entry, Manual Paper Exit, Cancel Order, Replace Order) may be temporarily disabled while sequence alignment is unresolved.
   - **Emergency Flatten Maintained:** Emergency Flatten must NOT be generically suppressed merely because the presentation layer is synchronizing. Emergency risk-abatement availability is determined by the authoritative backend command boundary and `TradingGuard` state, not by a client-side synchronization flag alone.
   - The UI synchronization state must NEVER be interpreted as permission to bypass safety, authorization, Phase 6, or Phase 7. Emergency Flatten remains strictly subject to: `UI → Authenticated Command Boundary → Authorization → TradingGuard.emergency_flatten() → Phase 7 Execution`.

---

## 7. EVENT ENVELOPE & DATA CONTRACT SPECIFICATIONS

### 7.1 TradegoEventEnvelope (Design Contract Only)
All asynchronous push events delivered to the UI presentation layer must be wrapped in a versioned envelope independent of underlying transport:

```typescript
// ============================================================================
// AUTHORITATIVE EVENT ENVELOPE (DESIGN CONTRACT ONLY)
// ============================================================================

export interface TradegoEventEnvelope<T = unknown> {
  event_id: string;                // Unique UUIDv4 per event
  event_type: string;              // e.g. "ORDER_FILLED", "SIGNAL_EMITTED", "GUARD_TRIPPED"
  event_version: string;           // Semantic version of event schema, e.g. "1.0.0"
  sequence: number;                // Monotonically increasing 64-bit sequence counter
  correlation_id: string;          // Traces to originating tick or client action
  server_timestamp: string;        // Server / emitted UTC ISO-8601 timestamp
  exchange_timestamp?: string;     // Authoritative exchange timestamp where applicable
  availability_timestamp?: string; // Point-in-time data availability timestamp where applicable
  instrument_id?: string;          // Canonical instrument ID where applicable (e.g. "NSE:TCS:EQUITY")
  payload: T;                      // Typed DTO payload
}
```

### 7.2 Schema Compatibility & Quarantine Rule
- **Semantic Versioning:** Event versions follow `MAJOR.MINOR.PATCH`.
- **Backward-Compatible (Minor/Patch Bump):** Client gracefully accepts envelopes with unknown optional fields.
- **Breaking (Major Bump):** If `envelope.event_version` contains an unsupported major version:
  - The client **MUST NOT** attempt to guess or parse the payload.
  - The event is routed to an internal quarantine log.
  - A persistent notification is displayed: `UNSUPPORTED PROTOCOL VERSION — UI UPDATE REQUIRED`.

### 7.3 Capability-Aware UI-Facing DTOs
DTOs are designed strictly around active backend capabilities (Indian cash equities). Speculative derivative fields are excluded:

```typescript
// ============================================================================
// UI PRESENTATION DTOs (CASH EQUITIES SCOPE)
// ============================================================================

export interface MarketQuoteDTO {
  dto_version: "1.0";
  instrument_id: string;          // Canonical ID e.g. "NSE:RELIANCE:EQUITY"
  timestamp: string;              // ISO-8601 UTC
  ltp: number;
  bid: number;
  ask: number;
  spread: number;
  volume_cumulative: number;
  vwap?: number;
  is_stale: boolean;
}

export interface OrderBookDepthLevelDTO {
  price: number;
  quantity: number;
  orders: number;
}

export interface OrderBookDTO {
  dto_version: "1.0";
  instrument_id: string;
  timestamp: string;
  bids: OrderBookDepthLevelDTO[]; // Top 5 levels standard
  asks: OrderBookDepthLevelDTO[]; // Top 5 levels standard
}

export interface CandleDTO {
  dto_version: "1.0";
  instrument_id: string;
  timeframe: string;              // "1M", "5M", "15M", "1H", "1D"
  start_time: string;
  end_time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap?: number;
  is_closed: boolean;
}

export interface PositionDTO {
  dto_version: "1.0";
  instrument_id: string;
  strategy_id?: string;
  net_quantity: number;
  average_entry_price: number;
  current_market_price: number;
  unrealized_pnl: number;
  realized_pnl: number;
  trailing_high_price: number;
  trailing_low_price: number;
  opened_timestamp: string;
  is_flat: boolean;
}

export type UIOrderStatus =
  | "LOCAL_REQUESTED"     // Client initiated action locally; awaiting backend transmission
  | "SUBMITTING"          // Transmitted to backend API gateway; awaiting engine registration
  | "ACKNOWLEDGED"        // Engine router registered order; awaiting execution match
  | "PARTIALLY_FILLED"    // One or more partial fills booked; order active
  | "FILLED"              // Completely filled; terminal
  | "CANCEL_REQUESTED"    // Cancellation requested by operator; awaiting engine confirmation
  | "CANCELLED"           // Cancellation confirmed by router; terminal
  | "REJECTED"            // Rejected by router or exchange guard; terminal
  | "EXPIRED"             // Session expired; terminal
  | "FAILED"              // Fatal routing / adapter submission failure; terminal
  | "UNKNOWN";            // Unreconciled state awaiting cold audit; quarantined

#### Detailed UIOrderStatus Semantics & Authoritative Invariant
* **`LOCAL_REQUESTED` (Client-Side Request State):**
  - Represents strictly a client-side presentation/request state indicating that an operator initiated a command in the UI.
  - `LOCAL_REQUESTED` does **NOT** mean:
    - An `OrderRequest` has been accepted by the backend.
    - An order has been submitted.
    - An order has been acknowledged.
    - An order exists at the execution adapter.
* **`SUBMITTING` (Backend-Confirmed Ingestion State):**
  - Represents backend-confirmed processing/submission state according to the future API contract (e.g. gateway acknowledgment that the command is in flight).
  - `SUBMITTING` does **NOT** imply authoritative execution engine registration or broker/adapter receipt.
* **Authoritative Order Invariant (`INVARIANT-UI-03`):**
  - The UI must never represent an order as submitted, acknowledged, or filled until an authoritative backend event confirms that state.
  - Phase 7 `OrderRequest` and `ExecutionState` models remain authoritative, untampered, and unchanged.

export interface OrderDTO {
  dto_version: "1.0";
  client_order_id: string;
  intent_id: string;
  signal_id: string;
  strategy_id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  position_effect: "OPEN" | "CLOSE" | "INCREASE" | "REDUCE";
  order_type: "MARKET" | "LIMIT";
  quantity: number;
  filled_quantity: number;
  remaining_quantity: number;
  limit_price?: number;
  average_fill_price?: number;
  status: UIOrderStatus;
  idempotency_key: string;
  creation_timestamp: string;
}

export interface BackendLineageDTO {
  dto_version: "1.0";
  client_order_id: string;
  intent_id: string;
  signal_id: string;
  signal_fingerprint: string;
  reaffirmation_key: string;
  idempotency_key: string;
  instrument_canonical_id: string;
  strategy_id: string;
  t1_market_receive_ns: number;
  t2_signal_generated_ns: number;
  t3_risk_evaluated_ns: number;
  t4_intent_approved_ns: number;
  t5_order_planned_ns: number;
  t6_order_submitted_ns: number;
  t7_wire_dispatched_ns: number;
  t8_order_acked_ns: number;
  t9_fill_received_ns: number;
  t10_position_updated_ns: number;
  total_pipeline_latency_us: number;
}
```

---

## 8. INFORMATION ARCHITECTURE: MVP vs. FUTURE MODULES

The 21 functional screens of Tradego are organized into 6 clear operational domains, strictly separating the **Core UI MVP** from **Future UI Modules**:

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                                    TRADEGO NAVIGATION ARCHITECTURE
══════════════════════════════════════════════════════════════════════════════════════════════════════

   ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
   │ 1. TRADING DOMAIN                                                                            │
   │    • Trading Terminal ───────────────► [CORE MVP] Institutional execution & monitoring cockpit│
   │    • Market Overview ────────────────► [CORE MVP] Market-wide breadth & sector activity       │
   │    • Watchlist Manager ──────────────► [CORE MVP] Live L1/L2 quote matrix                     │
   │    • Signals Console ────────────────► [CORE MVP] Phase 5 candidate feed & fingerprints       │
   │    • Positions Console ──────────────► [CORE MVP] Open lots, mark prices, trailing watermarks │
   │    • Orders Console ─────────────────► [CORE MVP] Active working limit orders & state machine │
   │    • Executions Tape ────────────────► [CORE MVP] Authoritative fill sequence ledger          │
   ├──────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 2. PORTFOLIO DOMAIN                                                                          │
   │    • Portfolio Ledger ───────────────► [CORE MVP] Balance sheet, cash, equity curve           │
   │    • Risk Center ────────────────────► [CORE MVP] Drawdown meters, daily loss, limits, vetoes │
   ├──────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 3. OPERATIONS DOMAIN                                                                         │
   │    • Runtime System Health ──────────► [CORE MVP] Lifecycle machine, error bursts, heartbeats │
   │    • Market Data Health ─────────────► [CORE MVP] Feed latency, stagnation timers, tick drops │
   │    • Execution Lineage Explorer ─────► [CORE MVP] Display of authoritative backend T1–T10 lineage measurements and timestamps │
   │    • Audit Ledger ───────────────────► [CORE MVP] Cryptographic logs & state transitions      │
   ├──────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 4. RESEARCH DOMAIN                                                                           │
   │    • Historical Replay Controller ───► [FUTURE MODULE - Phase 9 Preview]                      │
   │    • Backtest Studio ────────────────► [FUTURE MODULE - Phase 10 Preview]                     │
   │    • Quantitative Analytics ─────────► [FUTURE MODULE - Deep statistical factor inspection]   │
   │    • Trade Journal ──────────────────► [FUTURE MODULE - Post-trade psychological & tag logs]  │
   ├──────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 5. INTELLIGENCE DOMAIN                                                                       │
   │    • Strategy Manager ───────────────► [CORE MVP] Registry, trigger modes, watermarks         │
   │    • AI Research Assistant ──────────► [FUTURE MODULE - Phase 12 Preview, Read-Only Offline]  │
   ├──────────────────────────────────────────────────────────────────────────────────────────────┤
   │ 6. SYSTEM DOMAIN                                                                             │
   │    • System Settings ────────────────► [CORE MVP] Calendars, connection endpoints, theme     │
   └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 9. CORE UI MVP SPECIFICATION

The initial implementation milestone focuses exclusively on the 8 essential operational consoles:

### 9.1 Trading Terminal (The Cockpit)
- **Central Multi-Pane Layout:**
  - *Header Strip (48px):* System mode (`PAPER`), lifecycle state (`RUNNING`), feed status, net PnL, cash balance, emergency kill switch.
  - *Left Sidebar (260px):* Watchlist with instant symbol switching.
  - *Center Workspace:* Primary chart canvas (TradingView Lightweight Charts) displaying finalized bars, active forming candle, VWAP, EMA overlays, and signal markers.
  - *Right Sidebar (300px):* 5-level bid/ask order book ladder, spread, book pressure, and Phase 5 setup detection summary.
  - *Bottom Dock (260px):* Tabbed data tables for Positions, Working Orders, Fills, Signals, and Lineage.

### 9.2 Market & Watchlist Console
- High-density virtualized quote board.
- Displays: Canonical ID, LTP, Net Change, % Change, Bid, Ask, Spread, Cumulative Volume, VWAP, Stale status.

### 9.3 Signals Console
- Feed of Phase 5 `SignalCandidate` instances.
- Displays: Timestamp, Strategy ID, Symbol, Signal Type, Suggested Price Geometry (Entry/SL/TP), Risk/Reward Ratio, Confidence Score, SHA-256 Fingerprint inspector.

### 9.4 Positions Console
- Authoritative view of open lots projected from `PortfolioRuntimeState`.
- Displays: Symbol, Strategy, Direction, Net Qty, Avg Entry, Current Market Price, Unrealized PnL, Realized PnL, Trailing High/Low Watermarks, Duration.

### 9.5 Orders & Executions Console
- Tabbed view distinguishing Working Orders (`ACKNOWLEDGED`, `PARTIALLY_FILLED`) from Terminal Orders (`FILLED`, `CANCELLED`, `REJECTED`).
- Client Order IDs formatted as `TG-{strat[:4]}-{YYYYMMDD}-{intent[:12]}`.
- Single-click cancellation button issuing authenticated `router.cancel()` request.

### 9.6 Risk Management Center
- Visual gauges: Daily Loss vs Max Daily Loss limit, Drawdown vs Max Peak Drawdown, Concurrent Positions vs Max Limit.
- Detailed Risk Veto Log: Displays rejected trade candidates and binding constraints from Phase 6.

### 9.7 Runtime & System Health Console
- State machine indicator (`RUNNING`, `PAUSED`, `HALTED`).
- Feed stagnation timer with warning alert if $> 5.0\text{s}$ during active market hours.
- Error burst monitor tracking exception rates against the 5 err/s halt threshold.

### 9.8 Execution Lineage Explorer
- Display of authoritative backend T1–T10 lineage measurements and timestamps.
- Interactive inspection of backend $T_1 \dots T_{10}$ latency measurements, cryptographic fingerprints, and state transitions.
- Step-by-step breakdown of pipeline transitions from market tick receive through order planning, submission, fill, and position update.

---

## 10. CLIENT-ONLY PRESENTATION STATE

To protect backend authoritative truth, client state is formally bifurcated. The browser maintains local presentation state that is **never** propagated to the backend as trading truth:

```typescript
// ============================================================================
// CLIENT-ONLY PRESENTATION STATE (Zustand / Local UI Memory)
// ============================================================================

export interface ClientPresentationState {
  // Navigation & Workspace
  selected_instrument_id: string;        // Active symbol in chart/terminal (e.g. "NSE:TCS:EQUITY")
  selected_timeframe: string;            // Active chart bar resolution (e.g. "5M")
  active_bottom_dock_tab: string;        // "POSITIONS" | "ORDERS" | "FILLS" | "SIGNALS" | "LINEAGE"
  panel_collapsed_states: Record<string, boolean>; // Sidebar visibility toggles

  // Chart Viewport State (Local to Canvas)
  chart_visible_range: { from: number; to: number }; // Epoch timestamp bounds
  chart_cursor_price: number | null;     // Crosshair price
  chart_cursor_time: number | null;      // Crosshair timestamp
  active_indicator_overlays: string[];   // ["EMA_20", "VWAP", "RSI_14"]

  // Table Filters & Sorting (Local Presentation)
  order_filter_status: string | null;    // Filter orders grid by status
  position_sort_column: string;          // e.g. "unrealized_pnl"
  position_sort_direction: "asc" | "desc";
  watchlist_search_query: string;        // Symbol filter string

  // Ephemeral Forms
  ticket_form_draft: {
    limit_price_input: string;
    quantity_input: string;
  };

  // UI Preferences
  sound_alerts_enabled: boolean;
  theme_variant: "DARK_OBSIDIAN" | "DARK_SLATE";
}
```

---

## 11. RECONNECTION, GAP DETECTION & RESYNCHRONIZATION

The client enforces resilient recovery across transport interruptions:

```
[WebSocket Disconnect Detected]
       │
       ├─► 1. Set Connection State: RECONNECTING (Amber Pulse)
       ├─► 2. Dim Active Data Panels to 50% Opacity
       ├─► 3. Disable Ordinary Trading Mutations (Manual Entry/Exit, Cancel, Replace)
       │      (Emergency Flatten remains routed through TradingGuard command boundary)
       ▼
[Exponential Backoff Loop] (100ms, 200ms, 500ms, 1000ms... max 5000ms)
       │
       ▼ (Socket Re-established)
[Request Authoritative Snapshot]
       │  GET /api/v1/state/snapshot
       ▼
[Apply Snapshot]
       │  • Overwrite local position map with snapshot.positions
       │  • Overwrite working orders with snapshot.orders
       │  • Overwrite account risk state with snapshot.account
       │  • Set local sequence counter = snapshot.authoritative_sequence
       ▼
[Re-engage Event Stream]
       │  • Buffer stream events
       │  • Drop events where event.sequence <= snapshot.authoritative_sequence
       │  • Apply events where event.sequence == S_last + 1
       ▼
[Restore Full UI Interaction]
          Status: CONNECTED (Green) | Re-enable Action Buttons
```

---

## 12. ADMINISTRATIVE & TRADING COMMAND BOUNDARY (PROPOSED CONCEPTUAL ARCHITECTURE)

All mutating actions initiated by operators within the UI must traverse a strict, audited backend command boundary. Under no circumstances may any client presentation component directly invoke execution routers, brokers, or market data feeds.

### 12.1 Taxonomy of UI Mutating Commands
Mutating actions from the UI are formally segregated into two operational classes:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      TAXONOMY OF UI MUTATING COMMANDS                       │
├──────────────────────────────────────┬──────────────────────────────────────┤
│ A. CONTROL COMMANDS                  │ B. TRADING COMMANDS                  │
│    (Engine & Lifecycle Operations)   │    (Order & Position Operations)     │
├──────────────────────────────────────┼──────────────────────────────────────┤
│ • PAUSE                              │ • MANUAL PAPER ENTRY                 │
│ • RESUME                             │ • MANUAL PAPER EXIT                  │
│ • KILL_SWITCH                        │ • CANCEL ORDER                       │
│ • RECOVERY                           │ • REPLACE ORDER                      │
│ • SHUTDOWN                           │ • EMERGENCY FLATTEN                  │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### 12.2 Universal Mutating Command Flow
Every mutating UI action—without exception—must conceptually conform to the following end-to-end command pipeline:

```
                      ┌──────────────────────────────────────┐
                      │             TRADEGO UI               │
                      │       (Operator Action / Form)       │
                      └──────────────────┬───────────────────┘
                                         │ Mutating Command Request
                                         ▼
                      ┌──────────────────────────────────────┐
                      │    AUTHENTICATED COMMAND BOUNDARY    │
                      │     (Future API Gateway Ingestion)   │
                      └──────────────────┬───────────────────┘
                                         │ Verified Operator Context
                                         ▼
                      ┌──────────────────────────────────────┐
                      │            AUTHORIZATION             │
                      │        (RBAC / Policy Checks)        │
                      └──────────────────┬───────────────────┘
                                         │ Authorized Command
                                         ▼
                      ┌──────────────────────────────────────┐
                      │  APPROPRIATE BACKEND DOMAIN SERVICE  │
                      │  (Lifecycle / Manual Order / Admin)  │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │           TRADING GUARD /            │
                      │       PHASE 6 RISK / PHASE 7         │
                      │      (Authoritative Engines)         │
                      └──────────────────┬───────────────────┘
                                         │ Authoritative Transition / Fill Event
                                         ▼
                      ┌──────────────────────────────────────┐
                      │      AUTHORITATIVE RESULT / EVENT    │
                      │      (Wrapped in EventEnvelope)      │
                      └──────────────────┬───────────────────┘
                                         │ Asynchronous Stream / REST Ack
                                         ▼
                      ┌──────────────────────────────────────┐
                      │            UI PROJECTION             │
                      │     (Updated via State Projection)   │
                      └──────────────────────────────────────┘
```

### 12.3 Explicit Structural Prohibitions
To preserve engine safety and determinism, the following pathways are strictly and permanently prohibited:

```
   [ PROHIBITED ]  UI ───────────────────────► ExecutionRouter directly
   [ PROHIBITED ]  UI ───────────────────────► Broker directly
   [ PROHIBITED ]  UI ───────────────────────► Market Provider directly
```

### 12.4 Future Manual Paper Order Boundary
Manual paper order entry and exit are **proposed future operational capabilities**. Under no circumstances may a manual ticket bypass Phase 6 Risk:

```
                      ┌──────────────────────────────────────┐
                      │             TRADEGO UI               │
                      │        (Manual Ticket Form)          │
                      └──────────────────┬───────────────────┘
                                         │ Authenticated Manual Order Request
                                         ▼
                      ┌──────────────────────────────────────┐
                      │     FUTURE MANUAL ORDER SERVICE      │
                      │  • Proposed backend boundary         │
                      │  • NOT currently implemented (P1–P8) │
                      │  • Synthesizes SignalCandidate       │
                      │  • Sets metadata: SOURCE=MANUAL      │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │     PHASE 6 RISK MANAGEMENT GATE     │◄── MANDATORY GATE
                      │  • Evaluates RiskLimits              │    (CANNOT BE BYPASSED)
                      │  • Sizing & Capital Allocation       │
                      │  • Emits ApprovedTradeIntent         │
                      └──────────────────┬───────────────────┘
                                         │
                                         ▼
                      ┌──────────────────────────────────────┐
                      │       PHASE 7 EXECUTION ROUTER       │◄── MANDATORY ROUTER
                      │  • Session calendar validation       │    (CANNOT BE BYPASSED)
                      │  • Idempotency check                 │
                      │  • Dispatches to Paper Adapter       │
                      └──────────────────────────────────────┘
```

> **Implementation Note on ManualOrderService:**  
> The `ManualOrderService` is a **FUTURE PROPOSED** backend boundary and is **not** currently implemented in Phases 1–8. Its sole purpose when implemented will be translating operator ticket parameters into standard Phase 5 `SignalCandidate` records, ensuring that every operator order is subjected to identical sizing, risk limit verification, and session guards as automated strategies.

### 12.5 Emergency Flatten & Exit Controls
- Operator-initiated "Emergency Flatten" and "Manual Paper Exit" commands are proposed operational controls for risk abatement.
- **The UI must never bypass backend safety or risk authorization.**
- **Emergency Flatten Availability During UI Synchronization:** Emergency Flatten must NOT be generically suppressed merely because the presentation layer is synchronizing or in an amber stale-state. Its availability is governed by the authoritative backend command boundary and `TradingGuard` lifecycle state, not by client presentation synchronization alone. Even when ordinary trading mutations (manual entry, manual exit, cancel, replace) are disabled in the UI during resync, the emergency flatten path remains operational through the authenticated command boundary directly to `TradingGuard.emergency_flatten()`, strictly obeying Phase 7 risk-reducing exit validation.
- Orders are submitted with `PositionEffect.CLOSE` and `OrderPurpose.EMERGENCY_FLATTEN`.
- The engine's Phase 7 router executes risk-reducing exits lawfully while speculative entries remain strictly blocked.

### 12.6 Architectural Boundary & Required Auditability
- **Design Contract Only:** Detailed authentication protocols, custom cryptographic signing algorithms, JWT/session token formats, and identity provider integrations are **not** designed in this specification. They are explicitly deferred to a dedicated Security Architecture Review.
- **Auditability Invariant:** Every mutating command traversing the boundary must produce an immutable backend audit record containing:
  - `operator_id`: Verified operator identifier.
  - `command_type`: Exact enumeration (`PAUSE`, `RESUME`, `KILL_SWITCH`, `MANUAL_PAPER_ENTRY`, etc.).
  - `submission_timestamp`: High-precision UTC timestamp of command ingestion.
  - `correlation_id`: Unique trace ID linking the UI request to resultant backend event envelopes.
  - `payload_summary`: Parameter snapshot (e.g. symbol, requested quantity, order type, reason).

---

## 13. PROPOSED UI TECHNOLOGY STACK (NON-FROZEN)

The following technologies represent the current engineering proposal. **Technology selection requires a separate implementation readiness review before any package installation or code generation:**

| Layer | Proposed Technology | Evaluation & Selection Rationale | Status |
| :--- | :--- | :--- | :--- |
| **Frontend Framework** | React 19 / Next.js (App Router) | Component-driven, strong TypeScript ecosystem, standard institutional frontend tooling. | Proposed |
| **Language** | TypeScript 5.5+ | Strict type contracts mapping to backend DTO interfaces. | Proposed |
| **State Management** | Zustand | Lightweight ($< 2\text{KB}$), unopinionated, high-performance stores with shallow selectors outside the React render loop. | Proposed |
| **Data Fetching** | TanStack Query v5 | Resilient REST query caching, refetching, and pagination. | Proposed |
| **Table Virtualization** | TanStack Virtual v3 | High-density 60fps windowing for high-row grids without DOM bloat. | Proposed |
| **Charting Engine** | TradingView Lightweight Charts v4.2+ | Canvas/WebGL 60fps time-series rendering; zero DOM churn on ticks. | Proposed |
| **Icons & Micro-UI** | Lucide React | Clean, monoline SVG icon set for institutional density. | Proposed |
| **Testing** | Vitest + Testing Library + Playwright | Unit, component, and end-to-end browser integration verification. | Proposed |
| **Build Tooling** | Vite / Turbopack | Fast HMR, optimized tree-shaking, strict bundle sizing. | Proposed |
| **Deployment Target** | Modern Chromium Browser (Chrome / Brave / Edge) | Zero native desktop dependencies in v1.0. | Base Deployment |
| **Desktop Shell** | Tauri (Rust) / Electron | Native multi-window desktop shell deferred as future optional enhancement. | Optional Future |

---

## 14. PROPOSED DESIGN SYSTEM TOKENS

Visual styling tokens are design proposals and do not constitute rigid architectural constraints:

### Visual Token Matrix (Dark Slate & Carbon)
- **Base Surfaces:**
  - `--surface-app`: `#0B0E14` (Deep obsidian base)
  - `--surface-panel`: `#121722` (Dark slate card surface)
  - `--surface-elevated`: `#1A2234` (Modal and dropdown surface)
  - `--border-subtle`: `#232D42` (Structural line divider)
- **Semantic Accents:**
  - `--accent-bullish`: `#00D084` (Emerald green for positive PnL, long orders, buys)
  - `--accent-bearish`: `#FF4D4D` (Crimson red for negative PnL, short orders, sells)
  - `--accent-primary`: `#2F80ED` (Cobalt blue for interactive selection, brand focus)
  - `--accent-warning`: `#F2C94C` (Amber for stale feeds, paused states, rate throttles)
  - `--accent-critical`: `#EB5757` (Scarlet for halted states, kill switch, risk vetoes)
- **Typography Tokens:**
  - `--font-sans`: `Inter, -apple-system, BlinkMacSystemFont, sans-serif`
  - `--font-mono`: `JetBrains Mono, Roboto Mono, monospace` (for all numerical and tabular figures)
- **Baseline Spacing:**
  - 4px modular grid (`--space-1`: 4px, `--space-2`: 8px, `--space-3`: 12px, `--space-4`: 16px, `--space-6`: 24px).
  - Compact table row height pinned to 28px for maximal operational data density.

---

## 15. SECURITY & ACCESS CONTROL REQUIREMENTS

Detailed security implementation will be subject to a dedicated architecture review. The UI design establishes the following mandatory requirements:

1. **Role-Based Access Control (RBAC) Tiers:**
   - **OBSERVER:** Read-only access to quotes, charts, positions, orders, fills, logs. Cannot submit commands.
   - **OPERATOR:** Observer permissions + administrative Pause/Resume and order cancellations.
   - **RISK OFFICER:** Operator permissions + Emergency Kill Switch trip and portfolio flatten.
   - **ADMINISTRATOR:** Full permissions + token-gated manual recovery from HALTED and engine shutdown.
2. **Command Authentication:** Every administrative mutation (pause, resume, kill-switch, recovery) must transmit verified operator identity.
3. **Double-Action Barriers:** Destructive operations (Kill Switch, Recovery, Cancel All) require explicit two-step modal confirmation.

---

## 16. FUTURE ROADMAP COMPATIBILITY

The UI design establishes clean integration boundaries for future roadmap phases without altering the Core UI MVP:

- **Phase 9 (Historical Replay):** Replay Controller hooks into `ReplayCoordinator` via the same event envelopes, enabling tick-by-tick step and seek playback.
- **Phase 10 (Backtesting Engine):** Dedicated Backtest Studio interface for batch historical runs, parameter optimization sweeps, and equity curve analysis.
- **Phase 11 (Advanced Strategies):** Support for multi-timeframe regime models and algorithmic execution schedules.
- **Phase 12 (Machine Learning & AI Assistant):** Read-only, offline LLM/MCP research assistant for post-trade slippage analysis and audit log querying.
- **Phase 13 (Multi-Broker Routing):** Broker connection health matrix and route allocation views.
- **Phase 14 (Futures & Options):** Extension of DTO contracts for strike prices, expiry calendars, implied volatility, Greeks, and option chains.
- **Phase 15 (US & 24/7 Global Markets):** Multi-calendar timezone handling and multi-currency portfolio conversion views.
- **Phase 16 (Live Broker Execution):** Live order dispatch with hardware token confirmation and dual-operator authorization gates.

---

## 17. IMPLEMENTATION READINESS BOUNDARY

The acceptance of this architecture document does **NOT** authorize immediate frontend coding or package installation.

Clearly stated, after this design is accepted:
1. A frontend technology selection review is required.
2. A UI/API boundary design is required.
3. A security architecture review is required.
4. A UI implementation plan is required.
5. Only then may frontend implementation begin.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       IMPLEMENTATION READINESS GATES                        │
├─────────────────────────────────────────────────────────────────────────────┤
│ 1. Senior Architecture Acceptance of this UI Design Specification (v1.1).   │
│ 2. Formal Technology Selection Review (React/Next.js vs alternatives).       │
│ 3. Dedicated Backend UI/API Boundary Architecture Design.                   │
│ 4. Comprehensive Security, Authentication & Session Architecture Review.     │
│ 5. Phased Frontend Implementation Plan with explicit test milestones.       │
├─────────────────────────────────────────────────────────────────────────────┤
│   ONLY UPON COMPLETION OF ALL 5 GATES MAY PACKAGE INSTALLATION COMMENCE.    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 18. OPEN QUESTIONS FOR SENIOR ARCHITECT REVIEW

1. **Gateway Process Boundary:** Should the future API / WebSocket distribution gateway run in-process as an asynchronous thread within the Python runtime, or out-of-process via an inter-process shared memory / IPC ring buffer?
2. **REST Transport Encoding:** For initial browser development, is standard HTTP/1.1 JSON REST sufficient, or should HTTP/2 be mandated from Day 1?
3. **Lineage Historical Retention Depth:** How many historical `RuntimeCorrelationRecord` entries ($T_1 \dots T_{10}$) should the backend retain in memory for UI drill-down (e.g. 500 vs 5,000 completed orders)?

---

## 19. ARCHITECTURE SUGGESTIONS — NOT IMPLEMENTED

The following items are documented as non-binding engineering suggestions for future architectural evaluation:

```
ARCHITECTURE SUGGESTION — NOT IMPLEMENTED

1. ARCHITECTURE SUGGESTION 1 (Binary Transport Evaluation):
   Evaluate binary serialization (such as Protocol Buffers or MessagePack) for L2 depth
   streaming if JSON parsing CPU overhead in Chromium exceeds 5% during 50,000 tick/sec
   replay bursts.

2. ARCHITECTURE SUGGESTION 2 (Desktop Runner Evaluation):
   Evaluate packaging the Chromium frontend inside a Tauri desktop shell (Rust) in Phase 13
   to provide native OS-level multi-window detaching across physical monitors and global
   emergency hotkey trapping.

3. ARCHITECTURE SUGGESTION 3 (Audio Event Signatures):
   Evaluate distinctive, low-latency audio chimes for fill matches, risk vetoes, and
   kill-switch trips to provide eyes-free situational awareness for operators.
```

---

## 20. ACCEPTANCE CRITERIA & REVIEW CHECKLIST

- [x] Conforms strictly to approved and frozen Phases 1–8; zero source code or test modifications.
- [x] Enforces Hot-Path Isolation Invariant (UI operations cannot synchronously block engine hot path).
- [x] Establishes that the backend is the sole authoritative owner of trading truth.
- [x] Defines versioned `TradegoEventEnvelope` and DTO versioning contracts.
- [x] Outlines authoritative state synchronization model (Snapshot $\to$ Stream $\to$ Gap Detection $\to$ Resync).
- [x] Defines deterministic snapshot/stream handshake invariant and 3 explicit inbound sequence processing cases.
- [x] Specifies sequence scope (global baseline vs domain-partitioned alternative) as a design contract.
- [x] Formalizes conceptual administrative command boundary separating Control Commands from Trading Commands.
- [x] Explicitly prohibits `UI → ExecutionRouter directly`, `UI → Broker directly`, and `UI → Market Provider directly`.
- [x] Documents `ManualOrderService` as a future proposed boundary not currently implemented in Phases 1–8.
- [x] Distinguishes client-only presentation state from backend domain truth.
- [x] Reorganizes navigation into 6 operational domains, clearly isolating Core UI MVP from Future Modules.
- [x] Restricts scope to Indian cash equities, removing premature derivative/Greeks assumptions.
- [x] Prohibits optimistic order success; specifies comprehensive `UIOrderStatus` states.
- [x] Defines future Manual Order and Emergency Flatten boundaries routed strictly through Phase 6 Risk and Phase 7 Router.
- [x] Treats frontend technology selections as proposed, subject to separate readiness review.
- [x] Incorporates mandatory "UI ARCHITECTURE INVARIANTS" and "IMPLEMENTATION READINESS BOUNDARY" sections.

---

**END OF REVISED DESIGN SPECIFICATION**  
*Tradego UI Architecture v1.1 — Ready for Senior Architecture Review.*  

*Status: NOT FROZEN | NOT APPROVED*
