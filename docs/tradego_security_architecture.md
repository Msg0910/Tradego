# TRADEGO SECURITY ARCHITECTURE SPECIFICATION
## Institutional Access Control, Threat Modeling & Defense-in-Depth Protocol
### Document Version: 1.0.0-DRAFT | Status: Ready for Senior Architecture Review
### Upstream Status: Phases 1–8 Approved and Frozen | UI Arch v1.1 Frozen | UI/API Boundary Arch v1.1 Frozen

---

## 1. PURPOSE

This document establishes the definitive, design-only specification for the **Tradego Security Architecture**. It defines the security perimeters, threat vectors, identity models, authentication and authorization boundaries, command protection pipelines, secrets management protocols, and fail-closed audit standards required to protect the Tradego automated trading platform.

The core mission of the security architecture is to:
1. **Protect Platform Access:** Determine with cryptographic certainty **WHO** may connect, **WHAT** resources they may inspect, **WHICH** commands they may submit, and **WHICH** operational mutations they may execute.
2. **Insulate Core Trading Execution:** Ensure that all security verifications, token validations, cryptographic checks, and audit writes occur strictly at external boundaries, introducing **zero synchronous delay, zero external network I/O, and zero mutex contention** into the Phase 1–8 trading loop.
3. **Preserve Trading Truth Independence:** Strictly enforce the principle that **Security $\neq$ Risk $\neq$ Execution $\neq$ Portfolio Accounting**. The security layer protects access; it never manufactures, alters, or synthesizes trading truth.

```
+-----------------------------------------------------------------------------+
|                     SECURITY BOUNDARY vs. TRADING TRUTH                     |
|                                                                             |
| The Tradego Backend Engine remains the SOLE AUTHORITATIVE SOURCE of:        |
| - market state        - orders                - lifecycle state             |
| - positions           - fills                 - risk parameters             |
| - PnL                 - accounting balances   - T1-T10 telemetry            |
|                                                                             |
| The Security Layer controls ACCESS to the platform. It determines:          |
| WHO connects, WHAT they see, and WHICH commands they can submit.            |
| Security NEVER approves trades, sizes orders, alters positions, or bypasses |
| Phase 6 Risk Management or Phase 8 TradingGuard.                            |
+-----------------------------------------------------------------------------+
```

---

## 2. SCOPE & BOUNDARIES

### 2.1 In-Scope (Design Specifications)
- **Comprehensive Threat Modeling:** Identification and architectural containment of external, insider, transport, session, and operational threats (Threats A through X).
- **Formal Trust Boundaries:** Definition of discrete trust zones (Zone 0 through Zone 6) and inter-zone transit rules.
- **Identity & Session Architecture:** Conceptual modeling of human operators, services, sessions, commands, and correlation tracking.
- **Capability-Based Authorization:** Permission taxonomies separating Read, Control, Trading, Emergency, and Administrative capabilities.
- **Inbound Command Security Pipeline:** Multi-tier validation gate enforcing authentication, authorization, and audit recording before domain routing.
- **Emergency Command Governance:** Specialized controls, dual-confirmation models, and non-bypassable constraints for `EMERGENCY_FLATTEN`, `KILL_SWITCH`, and `RECOVERY`.
- **Replay & Idempotency Boundary:** Strict architectural separation between transport replay protection and Phase 7 execution idempotency.
- **Stream & Snapshot Security:** Protection of WebSocket event streams, subscription authorization, snapshot integrity, and data masking.
- **Data Classification & Secrets Management:** Tiered classification of internal data and non-negotiable rules for credential isolation.
- **Audit & Forensics:** Deterministic logging of security events, completely separated from Phase 8 execution telemetry.
- **Fail-Closed Resilience:** Deterministic failure handling distinguishing presentation access denial from trading engine trip conditions.
- **Future Live Broker Security:** Conceptual requirements for broker credential isolation and adapter protection.

### 2.2 Out-of-Scope (Strict Non-Goals)
- **NO Source Code Changes:** Zero modifications to frozen Phases 1–8 (`services/`, `strategies/`, `brokers/`, `config/`).
- **NO Test Modifications:** Zero changes to automated test suites (`tests/`).
- **NO Upstream Document Edits:** Zero modifications to frozen `docs/tradego_ui_architecture_design.md` or `docs/tradego_ui_api_boundary_architecture.md`.
- **NO Authentication Implementation:** No login forms, password hashers, token generators, or session cookie handlers.
- **NO Premature Technology Locking:** Zero binding selections of JWT, OAuth2, OpenID Connect, mTLS, TLS versions, specific identity providers, or secrets managers.
- **NO API Server Code:** No FastAPI middleware, security filters, or endpoint generation.
- **NO Database or IAM Creation:** No user tables, SQL schemas, Redis session caches, or cloud IAM role deployments.
- **NO Live Broker Integration:** Scope is strictly Indian cash equities (NSE/BSE) paper trading; no live broker API keys, certificates, or order adapters.
- **NO Derivatives or Foreign Markets:** No options, futures, Greeks, or US equities security scopes.
- **NO AI/LLM on the Trading Path:** Artificial Intelligence is strictly prohibited from security decision-making and runtime evaluation.

---

## 3. SECURITY PRINCIPLES

1. **Hot-Path Security Isolation (`SEC-07`):** All authentication, authorization, token parsing, cryptographic decryption, and audit logging MUST execute at external boundaries. Under no circumstances may security checks enter the Phase 1–8 trading loop or introduce blocking I/O into tick processing.
2. **Backend Sole Trading Authority (`SEC-11`):** The security boundary controls access to data and commands. It NEVER creates, modifies, or validates trading truth (market state, signals, risk parameters, order execution, fills, or portfolio balances).
3. **Mandatory Defense-in-Depth (`SEC-04`):** Security approval is a prerequisite for command ingestion, but security NEVER bypasses downstream trading safety. Every mutating trading command must independently pass Phase 8 `TradingGuard`, Phase 6 `RiskEngine`, and Phase 7 `ExecutionRouter`.
4. **Least Privilege & Capability Scoping:** Access to data feeds and operational commands is granted based on the minimum necessary capabilities required for a given operational role.
5. **Separation of Replay Protection and Execution Idempotency (`SEC-10`):** Transport-level request freshness (anti-replay) and business-level order deduplication (execution idempotency) are fundamentally separate responsibilities governed by separate contracts.
6. **Separation of Security Audit and Trading Telemetry (`SEC-09`):** Security audit logs record operator actions and access decisions. Phase 8 telemetry measures nanosecond execution latencies ($T_1 \dots T_{10}$). These channels must remain strictly segregated.
7. **Zero Secret Exposure (`SEC-08`):** Credentials, API secrets, signing keys, and broker tokens must NEVER reach the UI client, browser memory, client-side bundles, or event payloads.
8. **Fail-Closed Security Posture (`SEC-12`):** When authentication, authorization, or integrity verification fails or becomes ambiguous, the boundary MUST fail closed (deny access, reject commands, quarantine streams) without manufacturing synthetic state or crashing the trading core.

---

## 4. CURRENT SYSTEM SECURITY BASELINE

A comprehensive forensic inspection of the Tradego repository confirms the exact current security baseline:

1. **In-Memory Core Reality:** Phases 1 through 8 operate entirely within a single Python process in physical memory.
2. **Zero External Network Listeners:** There are currently **NO** HTTP servers, WebSocket endpoints, IPC sockets, or RPC listeners exposed to the network.
3. **Zero Existing Authentication/Authorization Infrastructure:** The repository currently contains no user management, token verification, session storage, or cryptographic identity modules.
4. **Active Internal Engine Guards:**
   - `TradingGuard` (Phase 8): Deterministic state machine (`NORMAL`, `PAUSED`, `HALTED`, `EMERGENCY_FLATTEN`) providing fail-closed operational tripping.
   - `HealthMonitor` (Phase 8): Monitors feed stagnation and exception bursts, tripping the kill switch if thresholds are breached.
   - `RiskEngine` (Phase 6): Enforces account-level capital limits, max position sizing, daily loss caps, and trade vetoes.
   - `ExecutionPlanner` / `ExecutionRouter` (Phase 7): Validates session calendars and enforces deterministic `IdempotencyKey` checks on order submissions.
   - `TelemetryCollector` (Phase 8): Thread-safe bounded deque recording correlation records ($T_1 \dots T_{10}$) in memory.
5. **Current Security Posture Summary:**
   - **Engine-level safety:** Mature, tested, and frozen (293 tests passing).
   - **Boundary/User security:** **NON-EXISTENT.** All user authentication, session security, transport encryption, and command authorization mechanisms are **FUTURE PROPOSED** specifications.

---

## 5. THREAT MODEL

The Tradego threat model identifies 24 threat vectors (Threats A through X) across the platform perimeters:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                TRADEGO THREAT TAXONOMY                                      │
├────────────────────────────┬─────────────────────────────┬──────────────────────────────────┤
│ Threat Vector              │ Vulnerable Attack Surface   │ Primary Architectural Control    │
├────────────────────────────┼─────────────────────────────┼──────────────────────────────────┤
│ A. External Attacker       │ Public Network / Gateway    │ Transport Encryption, WAF, Deny  │
│ B. Unauthorized Operator   │ API Gateway Ingress         │ Mandatory Identity Verification  │
│ C. Compromised Account     │ Authenticated API Session   │ MFA, Anomaly Detection, Revoke   │
│ D. Stolen Session Token    │ WebSocket / REST Requests   │ Short TTL, IP/Device Binding     │
│ E. Replay Attack           │ Mutating Command Ingress    │ Monotonic Nonce, Time Skew Window│
│ F. Command Tampering       │ Inbound Command Payload     │ Payload Signatures, Strict Schema│
│ G. Man-in-the-Middle       │ External Network Boundary   │ TLS Encryption, Cert Pinning     │
│ H. Malicious UI Client     │ Browser / Client DOM        │ Backend Sole Authority, Zero Trust│
│ I. Malicious API Client    │ Direct REST / WS Endpoints  │ Gateway Schema & Rate Limiting   │
│ J. Insider Misuse          │ Operator Workstation        │ RBAC, Dual-Control, Immutable Log│
│ K. Privilege Escalation    │ Command Gateway Dispatcher  │ Strict Server-Side Capability Map│
│ L. Cross-User Execution    │ Command Ingestion Pipeline  │ Operator-Session Context Binding │
│ M. Duplicate Submission    │ Network Retries / Jitter    │ Client Request ID, Anti-Replay   │
│ N. Ambiguous Delivery      │ Network Drop / Timeout      │ Explicit Queryable Command Ledger│
│ O. Event Stream Injection  │ WebSocket Distribution Tap  │ Server-Only Push, Isolated Egress│
│ P. Snapshot Tampering      │ Snapshot REST Endpoint      │ Cryptographic Digest / Signatures│
│ Q. Sequence Manipulation   │ Stream Sequence Numbering   │ Monotonic Egress Counter Watermark│
│ R. Denial of Service (DoS) │ Public Gateway Endpoints    │ Rate Limiting, Connection Quotas │
│ S. Slow-Client Exhaustion  │ Gateway Outbound Queues     │ Egress Conflation, Drop Policy   │
│ T. Credential Leakage      │ Configuration / Environment │ Externalized Secrets Manager     │
│ U. Secrets in Logs         │ Telemetry / Logging Service │ Redaction Filters, Strict Typing │
│ V. Audit Trail Tampering   │ Security Audit Storage      │ Append-Only, Write-Once Storage  │
│ W. Unauthorized Emergency  │ Kill Switch / Flatten Route │ Role-Gated Emergency Capability  │
│ X. Unauthorized Recovery   │ Recovery from HALTED State  │ Multi-Party Admin Token Gate     │
└────────────────────────────┴─────────────────────────────┴──────────────────────────────────┘
```

### Detailed Threat Specifications

#### Threat A: External Attacker
- **Attack Surface:** Exposed public IP addresses, load balancer ports, open network sockets.
- **Impact:** System penetration, resource exhaustion, unauthorized platform access.
- **Architectural Control:** Perimeter firewall, strict port isolation, transport-layer security, no direct engine port exposure.
- **Detection Requirement:** Network intrusion detection, dropped connection metrics, perimeter access logs.
- **Containment / Fail-Closed:** Firewall blocks unauthorized traffic; gateway drops untrusted packets immediately.

#### Threat B: Unauthorized Operator
- **Attack Surface:** API gateway authentication endpoints and WebSocket handshake.
- **Impact:** Unauthorized viewing of proprietary signals, market state, positions, or command execution.
- **Architectural Control:** Mandatory pre-access identity verification; unauthenticated connections rejected immediately (`SEC-01`).
- **Detection Requirement:** Failed authentication attempt alarms, IP anomaly tracking.
- **Containment / Fail-Closed:** Immediate connection termination (HTTP 401 / WS 1008); client blocked after repeated failures.

#### Threat C: Compromised Operator Account
- **Attack Surface:** Authenticated operator session on a compromised workstation.
- **Impact:** Fraudulent order submission, sabotage of runtime state, malicious position liquidation.
- **Architectural Control:** Capability-scoped authorization, anomaly detection on order sizing, multi-operator confirmation for critical controls.
- **Detection Requirement:** Sudden changes in client footprint, abnormal trading volume or frequency alerts.
- **Containment / Fail-Closed:** Instant session revocation, automated tripping of `TradingGuard` to `HALTED` upon confirmed compromise.

#### Threat D: Stolen Session Credential
- **Attack Surface:** Intercepted session tokens or stolen browser storage credentials.
- **Impact:** Attacker impersonates legitimate operator during the credential validity window.
- **Architectural Control:** Short session lifetimes, mandatory refresh validation, device/client context binding, immediate server-side revocation capability.
- **Detection Requirement:** Concurrent logins from divergent IP ranges, impossible travel velocity detection.
- **Containment / Fail-Closed:** Invalidate all active sessions for the compromised user identity immediately.

#### Threat E: Replay Attack
- **Attack Surface:** Inbound mutating command routes (e.g. `POST /api/v1/commands/order`).
- **Impact:** Duplicate execution of previously valid orders or control commands (e.g. re-executing a completed buy order).
- **Architectural Control:** Transport-level replay protection requiring unique client command IDs, timestamps, and monotonic sequence nonces (`SEC-10`).
- **Detection Requirement:** Duplicate client request ID detection at the security boundary.
- **Containment / Fail-Closed:** Reject command immediately (HTTP 409 Conflict); log security replay alert.

#### Threat F: Command Tampering
- **Attack Surface:** In-transit manipulation of command parameters (altering instrument, price, quantity, or side).
- **Impact:** Erroneous or malicious trade execution, financial losses.
- **Architectural Control:** Transport integrity encryption, cryptographic message signatures, strict server-side schema validation.
- **Detection Requirement:** Schema validation failure alerts, signature mismatch errors.
- **Containment / Fail-Closed:** Command dropped at gateway before reaching domain services; security audit event generated.

#### Threat G: Man-in-the-Middle (MitM)
- **Attack Surface:** External communication transit between client browser and API gateway.
- **Impact:** Eavesdropping on proprietary trading signals, credential theft, payload modification.
- **Architectural Control:** Mandatory strong transport encryption, certificate pinning, strict transport security headers.
- **Detection Requirement:** TLS negotiation error monitoring, invalid certificate handshake logging.
- **Containment / Fail-Closed:** Handshake terminated immediately on certificate or cipher mismatch.

#### Threat H: Malicious / Compromised UI Client
- **Attack Surface:** Tampered browser DOM, modified frontend JavaScript code, malicious browser extensions.
- **Impact:** Injection of fraudulent commands, falsified display to human operators.
- **Architectural Control:** Zero Trust client policy (`SEC-11`, `SEC-13`). The client is never trusted. All state is projected from backend; all commands are re-validated on the server.
- **Detection Requirement:** Client payload schema anomalies, rapid-fire button submission patterns.
- **Containment / Fail-Closed:** Gateway rejects non-conforming requests; client presentation states never alter backend execution.

#### Threat I: Malicious API Client
- **Attack Surface:** Direct REST / WebSocket programmatic callers bypassing the official UI.
- **Impact:** Rapid automated order injection, resource exhaustion, parameter fuzzing.
- **Architectural Control:** Gateway-level schema enforcement, strict type validation, rate limiting, and capability checking.
- **Detection Requirement:** Protocol violation counters, 4xx error burst tracking.
- **Containment / Fail-Closed:** IP/Session throttled and quarantined upon structural payload violation.

#### Threat J: Insider Misuse
- **Attack Surface:** Legitimate operator exceeding operational mandate or executing rogue manual orders.
- **Impact:** Capital losses, regulatory breach, unauthorized portfolio exposure.
- **Architectural Control:** Mandatory Phase 6 Risk Management checks on all orders; strict role capabilities; comprehensive immutable audit logging (`SEC-04`, `SEC-09`).
- **Detection Requirement:** Real-time risk limit alarms, anomalous manual order alerts.
- **Containment / Fail-Closed:** Phase 6 Risk vetoes unauthorized orders; `TradingGuard` halts engine if drawdown breaches occur.

#### Threat K: Privilege Escalation
- **Attack Surface:** Low-privilege user attempting to invoke administrative or risk commands (e.g. Observer attempting `PAUSE`).
- **Impact:** Unauthorized operational interference with live trading execution.
- **Architectural Control:** Strict server-side capability evaluation at the security boundary before routing (`SEC-02`).
- **Detection Requirement:** HTTP 403 Forbidden burst logging, unauthorized command attempt alarms.
- **Containment / Fail-Closed:** Request rejected; operator identity flagged for security review.

#### Threat L: Cross-User Command Execution
- **Attack Surface:** Inbound command request specifying an account or operator context differing from session identity.
- **Impact:** Operator mutating orders or positions belonging to another strategy or account.
- **Architectural Control:** Gateway strictly overrides client-supplied operator identity with the authenticated session identity.
- **Detection Requirement:** Context mismatch logging between token claims and payload headers.
- **Containment / Fail-Closed:** Immediate command rejection; security alert generated.

#### Threat M: Duplicate Command Submission
- **Attack Surface:** Network retries, double-clicking UI buttons, network packet duplication.
- **Impact:** Accidental duplicate order submissions, unintentional position doubling.
- **Architectural Control:** Two-stage deduplication: Security boundary checks client command ID; Phase 7 checks domain `IdempotencyKey` (`SEC-10`).
- **Detection Requirement:** Duplicate request metric tracking.
- **Containment / Fail-Closed:** Second request returns cached or current state of first command; zero duplicate execution.

#### Threat N: Ambiguous Command Delivery
- **Attack Surface:** Client connection drops after submitting command but before receiving HTTP response.
- **Impact:** Operator unaware whether order was submitted, leading to duplicate manual actions.
- **Architectural Control:** Explicit Command Result Model (`ACCEPTED`, `SUBMITTING`, `COMPLETED`, `AMBIGUOUS`); queryable command audit ledger.
- **Detection Requirement:** Command reconciliation queries upon client reconnect.
- **Containment / Fail-Closed:** UI flags command as `AMBIGUOUS` and forces reconciliation query before allowing retry.

#### Threat O: Event Stream Injection
- **Attack Surface:** Compromised API gateway attempting to inject fake market ticks or synthetic fills into client stream.
- **Impact:** Falsification of operator situational awareness, deceptive trading displays.
- **Architectural Control:** Stream dispatcher receives events strictly from internal state export layer; one-way push socket (`SEC-13`).
- **Detection Requirement:** Event sequence validation and cryptographic hash verification at client boundary.
- **Containment / Fail-Closed:** Client detects sequence gap or format violation, flags stream as untrusted, and requests authoritative snapshot.

#### Threat P: Snapshot Tampering
- **Attack Surface:** Interception or corruption of authoritative state snapshot payload during transit.
- **Impact:** Corrupted baseline state loaded into client memory, inaccurate risk or order display.
- **Architectural Control:** Transport encryption, snapshot manifest sequence validation ($S_{\text{snap}}$), integrity digest.
- **Detection Requirement:** Sequence inconsistency during buffered event reconciliation ($S \le S_{\text{snap}}$).
- **Containment / Fail-Closed:** Client discards corrupted snapshot, remains in `SYNCING` state, and re-requests snapshot with backoff.

#### Threat Q: Event Sequence Manipulation
- **Attack Surface:** Malicious network actor reordering or suppressing WebSocket event frames.
- **Impact:** Desynchronization of client order book or position watermarks from backend truth.
- **Architectural Control:** Monotonically increasing 64-bit sequence counter enforced by client stream buffer; delivery frontier gap detection (`UI-13`).
- **Detection Requirement:** Client detects $S_{\text{incoming}} > S_{\text{last}} + 1$ verified against delivery frontier.
- **Containment / Fail-Closed:** Immediate quarantine of stream; UI displays `RESYNC REQUIRED` and fetches authoritative snapshot.

#### Threat R: Denial of Service (DoS)
- **Attack Surface:** Flooding API gateway with SYN packets, HTTP requests, or WebSocket connections.
- **Impact:** Legitimate operators unable to view trading state or submit emergency controls.
- **Architectural Control:** Network rate limiting, connection pooling, isolated administrative ingress path, process boundary isolation (`UI-01`).
- **Detection Requirement:** Gateway ingress traffic rate meters, connection count alarms.
- **Containment / Fail-Closed:** Rate limiting drops offending IPs; core engine continues autonomous risk-managed execution unaffected.

#### Threat S: Slow-Client Resource Exhaustion
- **Attack Surface:** Lagging browser or stalled TCP connection consuming unbounded memory buffers on gateway.
- **Impact:** Gateway memory exhaustion (OOM), impacting other connected operator sessions.
- **Architectural Control:** Bounded egress queues per client; presentation-only quote conflation; drop policy for slow consumers (`UI-09`, `UI-11`).
- **Detection Requirement:** Per-client egress queue depth monitoring.
- **Containment / Fail-Closed:** Gateway forcefully drops lagging connection if buffer exceeds safety threshold; zero impact on engine.

#### Threat T: Credential Leakage
- **Attack Surface:** Accidental commit of API keys, broker secrets, or signing certificates to git repositories.
- **Impact:** External compromise of broker accounts, capital loss, platform takeover.
- **Architectural Control:** Secrets strictly externalized into secure environment stores; zero hardcoded secrets in source (`SEC-08`).
- **Detection Requirement:** Automated static code scanning for credentials, secret manager access logs.
- **Containment / Fail-Closed:** Immediate credential revocation and rotation; broker connection severed if compromised.

#### Threat U: Secrets Exposure Through Logs
- **Attack Surface:** Application logs, debug prints, crash dumps, or error traces displaying raw credentials.
- **Impact:** Secrets leaked to developers, log aggregation systems, or third-party monitoring tools.
- **Architectural Control:** Strict log redaction filters; prohibition of raw token/credential serialization in DTO `__repr__` methods.
- **Detection Requirement:** Automated log scanning for token patterns and credential keywords.
- **Containment / Fail-Closed:** Redaction filters replace sensitive values with `[REDACTED]`; log alerts triggered on format violations.

#### Threat V: Audit Trail Tampering
- **Attack Surface:** Malicious insider attempting to delete or modify command audit records to hide rogue activity.
- **Impact:** Inability to perform post-incident forensics, loss of regulatory traceability.
- **Architectural Control:** Write-once, append-only security audit log store; separation of duties between operators and audit infrastructure.
- **Detection Requirement:** Cryptographic hash chaining of audit entries, log deletion attempt alerts.
- **Containment / Fail-Closed:** Audit writes are synchronous at the security boundary; if audit storage fails, mutating commands are rejected (`DENY`).

#### Threat W: Unauthorized Emergency Control
- **Attack Surface:** Unauthorized party invoking `KILL_SWITCH` or `EMERGENCY_FLATTEN` to disrupt trading.
- **Impact:** Unnecessary position liquidation, transaction fee losses, trading disruption.
- **Architectural Control:** Role-gated capability check (`EMERGENCY_FLATTEN`, `KILL_SWITCH`) restricted to authorized roles; mandatory operator confirmation.
- **Detection Requirement:** High-priority security alert dispatched on any emergency command invocation.
- **Containment / Fail-Closed:** Unauthorized emergency invocations rejected; legitimate emergency commands execute immediately through `TradingGuard`.

#### Threat X: Unauthorized Recovery from HALTED
- **Attack Surface:** Operator attempting to resume trading after engine tripped into `HALTED` state without resolving underlying fault.
- **Impact:** Resuming trading into a broken market feed, bugged strategy, or catastrophic risk failure.
- **Architectural Control:** Strict administrative authorization; multi-factor confirmation token required; explicit reason logging (`SEC-06`).
- **Detection Requirement:** Audit alert on all recovery attempts from `HALTED`.
- **Containment / Fail-Closed:** `TradingGuard` remains in `HALTED` unless recovery command carries verified administrative authorization.

---

## 6. TRUST BOUNDARIES

The Tradego architecture establishes seven discrete trust zones with explicit boundary validation gates:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   TRADEGO TRUST ZONES                                       │
├─────────┬───────────────────────────────────┬───────────────────────────────────────────────┤
│ Zone    │ Trust Level & Domain Description  │ Permitted Interactions & Ingress Controls     │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 0  │ Untrusted External Network        │ Public internet, untrusted browser clients.   │
│         │ (Public Internet, Client Machine) │ All inbound traffic untrusted. Must pass TLS. │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 1  │ Authenticated Client Domain       │ Browser/client holding valid session token.   │
│         │ (Authenticated UI Workspace)      │ Untrusted runtime environment. Zero authority.│
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 2  │ API / Event Distribution Boundary │ Public-facing network gateway. Manages WS/REST│
│         │ (Gateway Network Daemon)          │ connections. Terminates TLS. Schema validator.│
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 3  │ Security / Authorization Boundary │ Policy Enforcement Point (PEP). Validates     │
│         │ (Auth, RBAC, Audit Ledger)        │ capabilities, anti-replay, writes audit log.  │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 4  │ State Export Boundary             │ Non-blocking egress worker. Translates engine │
│         │ (Egress Projection Pipeline)      │ state to versioned envelopes. Zero ingestion. │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 5  │ Core Trading Engine Domain        │ AUTHORITATIVE TRADING TRUTH. Synchronous,     │
│         │ (Phases 1–8 Trading Core)         │ in-memory. Isolated from all network callers. │
├─────────┼───────────────────────────────────┼───────────────────────────────────────────────┤
│ ZONE 6  │ External Broker / Exchange Gateway│ FUTURE live market execution boundary.        │
│         │ (Brokers, Feed Providers - FUTURE)│ Isolated strictly behind Phase 1 and Phase 7. │
└─────────┴───────────────────────────────────┴───────────────────────────────────────────────┘
```

### Trust Boundary Rules
1. **No Zone Traversal Bypass:** No external request may jump directly from Zone 0/1 into Zone 5. Every interaction must cross Zone 2 (Gateway) and Zone 3 (Security).
2. **One-Way State Egress:** Data flow from Zone 5 to Zone 4 to Zone 2 is strictly **one-way egress**. The API distribution gateway cannot call into the engine to alter state.
3. **Engine Domain Integrity:** Zone 5 is completely air-gapped from network I/O. It consumes ticks from Phase 1 and emits state to Zone 4 without knowing about external clients.

---

## 7. IDENTITY ARCHITECTURE

The identity model establishes five distinct identity scopes and their formal structural relationships:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 IDENTITY MODEL STRUCTURE                                    │
├───────────────────────┬──────────────────────┬──────────────────────────────────────────────┤
│ Identity Scope        │ Symbol Representation│ Purpose & Cardinality                        │
├───────────────────────┼──────────────────────┼──────────────────────────────────────────────┤
│ 1. Human Operator     │ U_id (User ID)       │ Natural person authenticated to system.      │
│ 2. Service Identity   │ S_id (Service ID)    │ Internal machine identity (e.g. Gateway).    │
│ 3. Client Session     │ Sigma_id (Session ID)│ Ephemeral authenticated connection session.  │
│ 4. Command Identity   │ C_id (Command ID)    │ Unique UUIDv4 per discrete mutating command. │
│ 5. Correlation ID     │ Corr_id (Trace ID)   │ End-to-end trace propagating T1–T10 lineage. │
└───────────────────────┴──────────────────────┴──────────────────────────────────────────────┘
```

### Identity Hierarchy & Bindings
- **User-to-Session Binding:** A single human operator ($U_{\text{id}}$) may hold one or more active sessions ($\Sigma_{\text{id}}$) across authorized workstations.
- **Session-to-Command Binding:** Every submitted command ($C_{\text{id}}$) is immutably bound to the originating session ($\Sigma_{\text{id}}$) and user ($U_{\text{id}}$). The gateway stamps these identities into the internal command envelope; client-supplied overrides are rejected.
- **Command-to-Correlation Trace:** When an authorized command routes into the trading engine, the system binds $C_{\text{id}}$ to a platform Correlation ID ($\text{Corr}_{\text{id}}$), which flows through Phase 8 telemetry ($T_1 \dots T_{10}$).
- **Technology Independence:** Concrete identity providers (Google OAuth, Microsoft Entra, Auth0, Keycloak, or local identity stores) are **NOT YET DECIDED** and will be evaluated in a later technology review.

---

## 8. AUTHENTICATION ARCHITECTURE

The boundary mandates strict pre-access authentication before any platform interaction:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                              AUTHENTICATION REQUIREMENTS                                    │
├───────────────────────────┬─────────────────────────────────────────────────────────────────┤
│ Lifecycle Requirement     │ Mandatory Architectural Rule                                    │
├───────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Identity Verification     │ Client must prove identity via valid credentials before access. │
│ Session Establishment     │ Gateway issues time-bounded session token on successful auth.   │
│ Session Lifetime          │ Sessions enforce strict absolute TTL and idle timeout policies. │
│ Session Renewal           │ Periodic renewal requires active proof of identity.             │
│ Explicit Logout           │ User logout terminates session and blacklists token immediately.│
│ Immediate Revocation      │ Administrators can revoke any user session instantly.           │
│ Credential Rotation       │ System supports periodic and emergency credential rotation.     │
│ Brute-Force Defense       │ Progressive exponential backoff and account lockout after fails.│
│ Session Visibility        │ Operators can view all active sessions bound to their identity. │
└───────────────────────────┴─────────────────────────────────────────────────────────────────┘
```

*Governance Constraint:* The selection of concrete authentication technologies (e.g. JSON Web Tokens, opaque session cookies, mTLS certificates, or OAuth2 flows) is explicitly deferred to a future technology review.

---

## 9. AUTHORIZATION ARCHITECTURE

Authorization is governed by **capability-based scoping**, categorizing permissions into five functional domains:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               CAPABILITY TAXONOMY MATRIX                                    │
├───────────────────┬───────────────────────────────┬─────────────────────────────────────────┤
│ Domain            │ Capability Identifier         │ Description of Permitted Action         │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 1. READ           │ CAP_READ_MARKET_STATE         │ Inspect real-time L1 quotes and depth.  │
│                   │ CAP_READ_SIGNALS              │ Inspect strategy candidate cards.       │
│                   │ CAP_READ_RISK                 │ Inspect risk limits and drawdown meters.│
│                   │ CAP_READ_ORDERS               │ View working orders and fill ledger.    │
│                   │ CAP_READ_POSITIONS            │ View open positions and watermarks.     │
│                   │ CAP_READ_LINEAGE              │ View T1–T10 execution latency telemetry.│
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 2. CONTROL        │ CAP_CONTROL_PAUSE             │ Pause strategy evaluations via Guard.   │
│                   │ CAP_CONTROL_RESUME            │ Resume strategy evaluations via Guard.  │
│                   │ CAP_CONTROL_CANCEL_ORDER      │ Cancel working order via Router.        │
│                   │ CAP_CONTROL_REPLACE_ORDER     │ Modify price/quantity of working order. │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 3. TRADING        │ CAP_TRADE_MANUAL_ENTRY        │ Submit manual paper limit/market order. │
│                   │ CAP_TRADE_MANUAL_EXIT         │ Submit manual paper position close.     │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 4. EMERGENCY      │ CAP_EMERGENCY_KILL_SWITCH     │ Trip TradingGuard to HALTED immediately.│
│                   │ CAP_EMERGENCY_FLATTEN         │ Unwind all open positions via Guard.    │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ 5. ADMINISTRATIVE │ CAP_ADMIN_RECOVERY            │ Recover TradingGuard from HALTED state. │
│                   │ CAP_ADMIN_SHUTDOWN            │ Orderly platform shutdown sequence.     │
└───────────────────┴───────────────────────────────┴─────────────────────────────────────────┘
```

### Illustrative Role Mappings (Non-Frozen Proposal)
- **OBSERVER:** Assigned all `READ` capabilities. Read-only monitoring.
- **OPERATOR:** Observer capabilities + `CONTROL` capabilities + `TRADING` capabilities.
- **RISK_OFFICER:** Operator capabilities + `EMERGENCY` capabilities (`KILL_SWITCH`, `EMERGENCY_FLATTEN`).
- **ADMINISTRATOR:** Full platform capabilities, including `ADMINISTRATIVE` recovery tokens.

---

## 10. COMMAND SECURITY

Every state-changing command received by the platform must execute through an uncompromising, multi-stage validation pipeline before reaching domain services:

```
               UNTRUSTED CLIENT (ZONE 0/1)
                       │
                       ▼
              AUTHENTICATION GATE (ZONE 2)
              • Verify session token validity
              • Extract U_id and Sigma_id
                       │
                       ▼
             AUTHORIZATION CHECK (ZONE 3)
              • Verify required CAP_* permission
              • Enforce operation scoping
                       │
                       ▼
             COMMAND VALIDATION (ZONE 3)
              • Check schema, timestamp skew
              • Enforce anti-replay nonce
              • Write Security Audit Record
                       │
                       ▼
             TRADING GUARD GATE (ZONE 5)
              • Check Guard state (NORMAL / PAUSED)
              • Block speculative entries if HALTED
                       │
                       ▼
              PHASE 6 RISK GATE (ZONE 5)
              • Capital limit checks
              • Position sizing constraints
              • Daily loss limit verification
                       │
                       ▼
            PHASE 7 EXECUTION ROUTER (ZONE 5)
              • Session calendar validation
              • Execution IdempotencyKey check
              • Match via Paper Execution Adapter
                       │
                       ▼
             AUTHORITATIVE RESULT EVENT (ZONE 5)
```

### Mandatory Structural Prohibitions
To preserve system safety, the following routes are physically prohibited by the boundary design:
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{ExecutionRouter directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Broker Adapter directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Market Provider directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Portfolio Accounting directly}$
- `[PROHIBITED]` $	ext{UI} \longrightarrow 	ext{Direct Risk Limit Bypass}$

---

## 11. EMERGENCY COMMAND SECURITY

Emergency operations represent high-impact actions requiring specialized security protocols:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                              EMERGENCY COMMAND GOVERNANCE                                   │
├────────────────────┬─────────────────────────────┬──────────────────────────────────────────┤
│ Command            │ Required Capability         │ Special Operational Protocol             │
├────────────────────┼─────────────────────────────┼──────────────────────────────────────────┤
│ KILL_SWITCH        │ CAP_EMERGENCY_KILL_SWITCH   │ Instant execution. Trips Guard to HALTED.│
│                    │                             │ Blocks all entries; retains exit path.   │
├────────────────────┼─────────────────────────────┼──────────────────────────────────────────┤
│ EMERGENCY_FLATTEN  │ CAP_EMERGENCY_FLATTEN       │ Requires explicit UI confirmation modal. │
│                    │                             │ Routes via TradingGuard.emergency_flatten│
│                    │                             │ Must NOT bypass Phase 6 Risk or Phase 7. │
├────────────────────┼─────────────────────────────┼──────────────────────────────────────────┤
│ RECOVERY           │ CAP_ADMIN_RECOVERY          │ Dual-confirmation administrative token.  │
│                    │                             │ Requires explicit reason code string.    │
│                    │                             │ Validates that trip cause is resolved.   │
└────────────────────┴─────────────────────────────┴──────────────────────────────────────────┘
```

---

## 12. SESSION SECURITY

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                SESSION SECURITY POLICIES                                    │
├───────────────────────┬─────────────────────────────────────────────────────────────────────┤
│ Policy Dimension      │ Architectural Requirement                                           │
├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
│ Token Entropy         │ Cryptographically random session identifiers (minimum 128-bit).     │
│ Session Lifetime (TTL)│ Time-bounded session duration (exact TTL: TBD pending risk review). │
│ Idle Invalidation     │ Automatic session termination after period of client inactivity.    │
│ Concurrent Sessions   │ Policy-controlled limit on simultaneous sessions per operator.       │
│ Privilege Mutation    │ Privilege modifications immediately invalidate active sessions.     │
│ Reconnect Validation  │ Reconnecting WebSockets must re-validate session token and scope.   │
└───────────────────────┴─────────────────────────────────────────────────────────────────────┘
```

---

## 13. REPLAY & IDEMPOTENCY SECURITY

The architecture enforces a strict conceptual and operational segregation between **Security Replay Protection** and **Phase 7 Execution Idempotency**:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                   SECURITY REPLAY PROTECTION vs. EXECUTION IDEMPOTENCY                      │
├─────────────────────────┬─────────────────────────────────┬─────────────────────────────────┤
│ Architectural Dimension │ Security Replay Protection      │ Phase 7 Execution Idempotency   │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Core Responsibility     │ "Is this command authentic,     │ "Has this approved trade intent │
│                         │  fresh, and submitted once?"    │  already been executed?"        │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Operational Boundary    │ Zone 3 (Security Gateway)       │ Zone 5 (ExecutionPlanner/Router)│
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Evaluation Mechanism    │ Client Request ID, timestamp    │ IdempotencyKey derived from     │
│                         │ skew, monotonic nonce window.   │ Strategy ID, Intent ID, Date.   │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Scope of Protection     │ Prevents network packet replay  │ Prevents duplicate order fills  │
│                         │ and duplicate wire submissions. │ on engine retries / reconnects. │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Invariant Rule          │ SEC-10 (Strict separation)      │ Phase 7 Frozen Contract         │
└─────────────────────────┴─────────────────────────────────┴─────────────────────────────────┘
```

---

## 14. EVENT STREAM SECURITY

WebSocket connections distributing real-time market data and engine events must adhere to five security guarantees:
1. **Connection-Time Authentication:** WebSocket handshake must present valid session credentials. Unauthenticated requests are rejected during HTTP upgrade.
2. **Topic Subscription Authorization:** Clients may only subscribe to symbol quotes and event channels permitted by their capability set.
3. **Per-Client Stream Isolation:** Client subscription state and egress buffers are strictly isolated. A corrupted or slow client cannot inject frames or degrade peers.
4. **Server-to-Client Push Exclusivity:** The WebSocket is strictly a one-way push conduit for engine projections (`SEC-13`). Inbound client frames over the event socket are rejected. All commands flow over audited REST command endpoints.
5. **Session Revocation Ejection:** If an operator's session is revoked, the gateway terminates active WebSocket connections immediately (WS 1008 Policy Violation).

---

## 15. SNAPSHOT SECURITY

Authoritative state snapshots project comprehensive portfolio and order state, requiring stringent access protections:
1. **Authenticated Ingress:** Snapshots are accessible solely over authenticated HTTP GET routes (`GET /api/v1/state/snapshot`).
2. **Field-Level Data Masking:** Accounts with `OBSERVER` roles receive masked account identifiers and aggregate balance metrics, preventing unauthorized exposure of proprietary account numbers.
3. **Snapshot Sequence Frontier ($S_{\text{snap}}$):** Every snapshot carries an immutable sequence watermark ($S_{\text{snap}}$) establishing the exact state boundary.
4. **Integrity Verification:** Clients verify the structural integrity and sequence monotonically before initiating event stream reconciliation.

---

## 16. DATA CLASSIFICATION

Platform data assets are categorized into four formal security tiers:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 DATA CLASSIFICATION TIERS                                   │
├───────────────────┬───────────────────────────────────┬─────────────────────────────────────┤
│ Classification    │ Included Data Assets              │ Access & Protection Requirements    │
├───────────────────┼───────────────────────────────────┼─────────────────────────────────────┤
│ 1. PUBLIC         │ Exchange L1 quotes, trade volume, │ Unrestricted read access within     │
│                   │ market calendar, symbol listings. │ authenticated sessions.             │
├───────────────────┼───────────────────────────────────┼─────────────────────────────────────┤
│ 2. INTERNAL       │ Strategy metadata, runtime status,│ Restricted to authenticated users;  │
│                   │ technical indicators, health data.│ no external public broadcast.       │
├───────────────────┼───────────────────────────────────┼─────────────────────────────────────┤
│ 3. SENSITIVE      │ Working orders, filled positions, │ Restricted to OPERATOR and above;   │
│                   │ PnL, cash balances, T1–T10 latency│ masked for observers; TLS encrypted.│
├───────────────────┼───────────────────────────────────┼─────────────────────────────────────┤
│ 4. HIGHLY         │ Broker credentials, signing keys, │ Stored in secrets manager; NEVER    │
│    SENSITIVE      │ auth secrets, audit logs, recovery│ sent to UI; strict RBAC access.     │
└───────────────────┴───────────────────────────────────┴─────────────────────────────────────┘
```

---

## 17. SECRETS MANAGEMENT

To prevent credential leakage and platform compromise, the following mandatory rules govern secrets management:
1. **Zero Hardcoded Secrets (`SEC-08`):** Credentials, API secrets, signing keys, and certificates must NEVER be committed to source control or hardcoded in configuration files.
2. **Zero Browser Exposure:** Secrets must never be transmitted to client browsers, packaged into frontend bundles, or exposed in HTML/JavaScript.
3. **Zero Payload Exposure:** Event envelopes and state snapshots must never contain credentials, internal tokens, or secret keys.
4. **Zero Log Leakage:** Secrets must never appear in ordinary logs, debug traces, or error dumps. Redaction filters must sanitize all logs.
5. **Externalized Storage:** Secrets must reside in dedicated, access-controlled environment variables or enterprise secret vaults (technology choice: TBD).

---

## 18. SECURITY AUDIT

All security-relevant actions must be immutably recorded in a dedicated **Security Audit Trail**:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                SECURITY AUDIT RECORD SCHEMA                                 │
├───────────────────────┬─────────────────────────────────────────────────────────────────────┤
│ Field                 │ Purpose & Verification Value                                        │
├───────────────────────┼─────────────────────────────────────────────────────────────────────┤
│ audit_id              │ Unique UUIDv4 per audit entry.                                      │
│ timestamp_utc         │ High-precision UTC ISO-8601 timestamp.                              │
│ operator_id           │ Human operator identity (U_id).                                     │
│ session_id            │ Active session identity (Sigma_id).                                 │
│ command_id            │ Associated command identifier (C_id), if applicable.                │
│ correlation_id        │ Platform trace identifier (Corr_id).                                │
│ event_type            │ Action type (e.g. AUTH_LOGIN, CMD_SUBMIT, GUARD_KILL_SWITCH).       │
│ target_resource       │ Affected domain resource (e.g. "order:TG-101", "guard:HALTED").     │
│ authorization_decision│ Decision outcome: "ALLOW" or "DENY".                                │
│ denial_reason         │ Reason string if authorization failed.                              │
│ client_ip             │ Ingress client IP address.                                          │
│ client_user_agent     │ Browser / client user-agent string.                                 │
└───────────────────────┴─────────────────────────────────────────────────────────────────────┘
```

### Audited Security Events
- User authentication success, failure, session renewal, and logout.
- Authorization rejections and privilege escalation attempts.
- All mutating command submissions (`PAUSE`, `RESUME`, `KILL_SWITCH`, `RECOVERY`, `ORDER`, `FLATTEN`).
- Administrative recovery and parameter reconfiguration.

---

## 19. SECURITY VS TRADING TELEMETRY

The platform maintains an absolute separation between security auditing and execution telemetry:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                    SECURITY AUDIT vs. TRADING TELEMETRY vs. LINEAGE                         │
├─────────────────────────┬─────────────────────────────────┬─────────────────────────────────┤
│ Dimension               │ Security Audit Trail            │ Phase 8 Trading Telemetry (T1-10│
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Primary Purpose         │ Forensic accountability, legal  │ Scientific latency tracking,    │
│                         │ auditability, and access trace. │ microstructure bottleneck audit.│
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Managed By              │ Zone 3 Security Gateway         │ Zone 5 Phase 8 TelemetryCollector│
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Measurement Units       │ Millisecond / UTC wall clock    │ Nanosecond monotonic timestamps │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Ingestion Point         │ Security boundary transit       │ Internal engine service hops    │
├─────────────────────────┼─────────────────────────────────┼─────────────────────────────────┤
│ Invariant Rule          │ SEC-09 (Strict separation)      │ Phase 8 Frozen Telemetry Contract│
└─────────────────────────┴─────────────────────────────────┴─────────────────────────────────┘
```

---

## 20. FAILURE & FAIL-CLOSED MODEL

The boundary enforces deterministic failure actions, ensuring that presentation-layer security failures do not inadvertently trip or crash the core trading engine:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 FAIL-CLOSED SECURITY MATRIX                                 │
├───────────────────────────┬───────────────────────────────────┬─────────────────────────────┤
│ Failure Scenario          │ Security Boundary Action          │ Trading Core Engine Action  │
├───────────────────────────┼───────────────────────────────────┼─────────────────────────────┤
│ Invalid Credentials       │ DENY (HTTP 401 / WS 1008)         │ UNAFFECTED (Runs normally)  │
│ Session Token Expired     │ DENY (HTTP 401 / WS 1008)         │ UNAFFECTED (Runs normally)  │
│ Capability Unauthorized   │ DENY (HTTP 403 Forbidden)         │ UNAFFECTED (Runs normally)  │
│ Command Schema Invalid    │ DENY (HTTP 400 Bad Request)       │ UNAFFECTED (Runs normally)  │
│ Replay Nonce Detected     │ DENY (HTTP 409 Conflict)          │ UNAFFECTED (Runs normally)  │
│ Command Signature Mismatch│ DENY & QUARANTINE (HTTP 400)      │ UNAFFECTED (Runs normally)  │
│ Auth Service Offline      │ DENY ALL INBOUND COMMANDS         │ UNAFFECTED (Runs normally)  │
│ Security Audit Log Full   │ DENY ALL MUTATING COMMANDS        │ UNAFFECTED (Autonomous runs)│
│ State Export Queue Full   │ DROP PRESENTATION QUOTES (UI-09)  │ UNAFFECTED (No hot-path wait│
│ Client Ingress Flood (DoS)│ RATE LIMIT & DROP PACKETS         │ UNAFFECTED (Hot path isolated│
│ Exception Burst in Engine │ TRIP KILL SWITCH TO HALTED        │ TRIPS TO HALTED (HealthMon) │
│ Feed Stagnation Detected  │ TRIP KILL SWITCH TO HALTED        │ TRIPS TO HALTED (HealthMon) │
└───────────────────────────┴───────────────────────────────────┴─────────────────────────────┘
```

---

## 21. HOT-PATH SECURITY ISOLATION

The trading hot path operates with absolute independence from security network infrastructure:

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                                HOT-PATH SECURITY ISOLATION
══════════════════════════════════════════════════════════════════════════════════════════════════════

      MARKET DATA FEED (ZONE 0)
                 │
                 ▼
      [PHASE 1: MarketDataGateway] ──── (T1: Ingestion)
                 │
                 ▼
      [PHASE 2: InstrumentStateStore]
                 │
                 ▼
      [PHASE 3: CandleEngine]
                 │
                 ▼
      [PHASE 4: FeatureEngine]
                 │
                 ▼
      [PHASE 5: SignalEngine] ──────── (T2: Strategy Evaluation)
                 │
                 ▼
      [PHASE 6: RiskEngine] ────────── (T3-T4: Sizing & Capital Vetoes)
                 │
                 ▼
      [PHASE 7: ExecutionRouter] ───── (T5-T9: Idempotent Matching & Fills)
                 │
                 ▼
      [PHASE 8: PortfolioRuntime] ──── (T10: Balance & Watermark Commit)
                 │
                 ▼ (Asynchronous non-blocking export)
      ┌─────────────────────────────────────────────────────────┐
      │ SECURITY & PRESENTATION BOUNDARY (ZONES 2 & 3)          │
      │ • Authentication Verification                           │
      │ • Capability Authorization                              │
      │ • Anti-Replay Checking                                  │
      │ • Security Audit Logging                                │
      │ • WebSocket & REST Serialization                        │
      └─────────────────────────────────────────────────────────┘
```

---

## 22. FUTURE LIVE BROKER SECURITY

While the current Phase 1–8 platform scope is restricted to paper trading of Indian cash equities, future live broker integrations must adhere to five mandatory security requirements:
1. **Credential Isolation:** Broker API keys, trading PINs, TOTP seeds, and digital signature certificates must reside exclusively within secure backend vaults, isolated from external access.
2. **Zero Client Broker Exposure:** The browser and UI client must NEVER interact directly with broker endpoints or handle raw broker credentials (`SEC-03`, `SEC-08`).
3. **Adapter Placement:** Broker adapters must reside strictly behind the Phase 7 `ExecutionRouter`, accessible only after passing Phase 6 Risk and Phase 8 `TradingGuard` validation.
4. **Emergency Revocation:** The platform must support immediate programmatic revocation and detachment of broker credentials without requiring engine rebuilds.
5. **Broker Session Auditing:** Every broker interaction, heartbeat, fill callback, and order rejection must be recorded in the security audit trail.

---

## 23. SECURITY THREAT / CONTROL MATRIX

| Threat | Attack Surface | Potential Impact | Architectural Control | Detection Mechanism | Fail-Closed Action |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A. External Attacker** | Public ports | Breach, tampering | Strict firewall, TLS, no public engine ports | Intrusion detection, dropped packet metrics | Block traffic |
| **B. Unauthorized User** | Gateway ingress | Information leakage | Mandatory authentication before access | Failed login rate monitoring | Deny connection |
| **C. Compromised Account**| Active session | Unauthorized trading | MFA, anomaly detection, instant revocation | Activity anomaly alarms | Invalidate session |
| **D. Stolen Token** | Inbound requests| Impersonation | Short TTL, IP/device binding, token revoke | Concurrent IP detection | Revoke token |
| **E. Replay Attack** | Command routes | Duplicate executions | Nonce windows, request ID deduplication | Duplicate nonce alerts | Drop request (409) |
| **F. Command Tampering** | Payload transit | Erroneous execution | TLS encryption, schema validation | Schema error alarms | Drop payload (400) |
| **G. Man-in-the-Middle** | Network transit| Eavesdropping | TLS encryption, certificate pinning | Handshake failure logging | Terminate socket |
| **H. Malicious UI** | Client runtime | False state/orders | Zero Trust client policy, backend sole truth | Payload anomaly alerts | Reject command |
| **I. Malicious API** | Gateway endpoints| System abuse | Strict schemas, rate limiting, RBAC | Rate breach alarms | Throttle & block |
| **J. Insider Misuse** | Operator console| Rogue trading | Phase 6 Risk vetoes, immutable audit log | Risk limit breach alarms | Veto & audit log |
| **K. Privilege Escalation**| Command route | Unauthorized actions| Server-side capability check | 403 Forbidden monitoring | Reject command (403)|
| **L. Cross-User Action** | Inbound headers| Account mutation | Session identity overrides client claims | Context mismatch logging | Reject command |
| **M. Duplicate Submit** | Network retries | Double execution | Two-stage deduplication (Security + Phase 7) | Duplicate ID metrics | Return cached result|
| **N. Ambiguous Delivery** | Dropped response| Operator confusion | Explicit Command Result Model, audit query | Unreconciled command alerts | Mark AMBIGUOUS |
| **O. Stream Injection** | WebSocket feed | False data display | One-way push socket, isolated state exporter | Sequence mismatch alerts | Quarantine stream |
| **P. Snapshot Tamper** | State snapshot | Corrupted state | Transport integrity, sequence validation | Sequence check failure | Re-request snapshot |
| **Q. Sequence Tamper** | Event frames | Desynchronization | Monotonic 64-bit counter, delivery frontier | Gap detection alarms | Resync snapshot |
| **R. Denial of Service** | Public gateway | Service disruption | Gateway rate limiting, connection quotas | Traffic volume alerts | Drop excess packets |
| **S. Slow Client OOM** | Egress buffers | Gateway crash | Egress conflation, drop policy for slow peers| Queue depth monitoring | Disconnect slow peer|
| **T. Credential Leak** | Source / Config | Platform compromise| External secrets manager, static code scan | Secret scanner alarms | Rotate credentials |
| **U. Secrets in Logs** | Logging output | Credential leakage | Strict log redaction filters | Regex log audit alerts | Redact & alert |
| **V. Audit Tampering** | Audit database | Loss of trace | Write-once append-only storage, hash chaining| Integrity breach alarms | Deny mutating cmds |
| **W. Bad Emergency** | Kill switch route| False liquidation | Capability check, confirmation modals | High-priority audit alert | Reject if unauthed |
| **X. Bad Recovery** | Recovery command| Resuming into fault| Multi-factor admin token, reason code | Recovery audit alarms | Remain in HALTED |

---

## 24. SECURITY DECISION STATUS

| Subsystem Component | Candidate Technology | Architectural Status | Governance & Evaluation Notes |
| :--- | :--- | :--- | :--- |
| **Core Trading Safety** | TradingGuard & RiskEngine | **CURRENT / FROZEN** | Verified in Phases 6 & 8. Zero changes authorized. |
| **Execution Idempotency**| IdempotencyKey (Phase 7) | **CURRENT / FROZEN** | Verified in Phase 7. Stable execution deduplication. |
| **Authentication Standard**| OAuth2 / OIDC / JWT / mTLS | **NOT YET DECIDED** | Candidate evaluation deferred to Implementation Review.|
| **Identity Provider (IdP)**| Keycloak / Entra / Local DB | **NOT YET DECIDED** | Enterprise identity integration to be evaluated. |
| **Transport Encryption** | TLS 1.3 / WSS Encryption | **PROPOSED** | Standard production encryption baseline. |
| **Secrets Management** | Vault / Cloud KMS / Env Store| **NOT YET DECIDED** | Dedicated secrets vault selection deferred. |
| **Audit Storage Engine** | Append-Only SQL / OpenSearch | **NOT YET DECIDED** | Write-once compliance storage to be evaluated. |
| **API Gateway Tech** | FastAPI / Envoy / NGINX | **NOT YET DECIDED** | Gateway reverse proxy framework uncommitted. |
| **Rate Limiting Engine** | In-Memory Token Bucket / Redis | **NOT YET DECIDED** | Ingress rate limiting mechanism uncommitted. |

---

## 25. MANDATORY SECURITY INVARIANTS

The Tradego Security Architecture strictly enforces fourteen mandatory security invariants:

- **INVARIANT-SEC-01 (Authentication Precedence):** Authentication precedes privileged access. No client connection or API request may access internal market state, signals, or accounts without verified identity.
- **INVARIANT-SEC-02 (Authorization Precedence):** Authorization precedes state-changing commands. Every command must pass capability verification before routing to domain services.
- **INVARIANT-SEC-03 (No Direct Execution/Broker Access):** Under no circumstances may the UI or API clients directly access `ExecutionRouter`, broker adapters, or market data feeds.
- **INVARIANT-SEC-04 (No Security Bypass of Trading Safety):** Security controls cannot bypass Phase 6 Risk Management or Phase 8 `TradingGuard`. A valid security token does not exempt a trade from capital limits or drawdown vetoes.
- **INVARIANT-SEC-05 (Emergency Controls Authenticated):** Emergency Flatten and Kill Switch operations remain strictly authenticated and authorized, executing through `TradingGuard`.
- **INVARIANT-SEC-06 (Controlled HALTED Recovery):** Recovery from a `HALTED` state is strictly manual and administrator-controlled, requiring verified administrative authorization tokens.
- **INVARIANT-SEC-07 (Hot-Path Security Isolation):** Security checks, authentication validations, token parsers, and audit loggers MUST NOT enter the Phase 1–8 trading loop or introduce blocking I/O into tick processing.
- **INVARIANT-SEC-08 (Secrets Never Reach Client):** Broker credentials, API secrets, signing keys, and private tokens must NEVER reach the browser runtime, client bundles, or event payloads.
- **INVARIANT-SEC-09 (Security Audit Separation):** Security audit logging is structurally separate from Phase 8 execution telemetry ($T_1 \dots T_{10}$).
- **INVARIANT-SEC-10 (Replay Protection Separation):** Security-layer anti-replay verification is structurally separate from Phase 7 execution idempotency.
- **INVARIANT-SEC-11 (Sole Source of Trading Truth):** The authoritative backend engine remains the sole source of trading truth. The security boundary projects state but never manufactures truth.
- **INVARIANT-SEC-12 (Fail-Closed Posture):** Security failures must fail closed (deny access, reject commands) without manufacturing synthetic trading truth or crashing the trading core.
- **INVARIANT-SEC-13 (Zero Client Event Authority):** Client-generated event messages are never authoritative. The UI stream is strictly a server-to-client push channel.
- **INVARIANT-SEC-14 (Session Invalidation on Privilege Change):** Privilege or credential modifications immediately invalidate affected active sessions according to the session security contract.

---

## 26. OPEN SECURITY QUESTIONS FOR SENIOR REVIEW

1. **Session Lifetime & Idle Window:** What are the optimal default values for session absolute TTL (e.g. 8 hours) and idle timeout (e.g. 15 minutes) for active trading operator consoles?
2. **Multi-Operator Emergency Recovery:** Should recovery from `HALTED` require dual-operator quorum (two distinct administrators approving the recovery token) or single-administrator authorization?
3. **Audit Log Persistence Backend:** Should the write-once security audit ledger be implemented via local append-only encrypted files, a dedicated relational database, or an external immutable log service?
4. **WebSocket Session Invalidation Broadcast:** When an operator's session is revoked, what is the preferred gateway-to-client disconnect protocol (immediate TCP reset vs. graceful WebSocket close code 1008)?
5. **Future Broker Credential Isolation:** In future live trading phases, should broker credentials be isolated in a dedicated out-of-process credential sidecar or injected directly via secure OS environment variables?

---

## 27. ACCEPTANCE CRITERIA & REVIEW CHECKLIST

- [x] Defined complete threat model addressing all 24 vectors (Threats A through X).
- [x] Established explicit trust boundaries (Zones 0 through 6).
- [x] Defined conceptual identity model (User, Service, Session, Command, Correlation).
- [x] Formulated technology-neutral authentication architecture.
- [x] Formulated capability-based authorization architecture (READ, CONTROL, TRADING, EMERGENCY, ADMIN).
- [x] Established non-bypassable inbound command security pipeline.
- [x] Specified emergency command governance (`KILL_SWITCH`, `EMERGENCY_FLATTEN`, `RECOVERY`).
- [x] Separated security anti-replay protection from Phase 7 execution idempotency (`SEC-10`).
- [x] Established event stream and snapshot security contracts.
- [x] Formulated 4-tier internal data classification model.
- [x] Formulated secrets management rules (zero client/log exposure).
- [x] Established security audit trail architecture, separated from Phase 8 telemetry (`SEC-09`).
- [x] Defined comprehensive fail-closed failure matrix across 12 scenarios.
- [x] Guaranteed hot-path security isolation (`SEC-07`).
- [x] Formulated future live broker security requirements.
- [x] Provided exhaustive Threat / Control Matrix.
- [x] Categorized security technologies as CURRENT, PROPOSED, and NOT YET DECIDED.
- [x] Preserved all mandatory security invariants (`SEC-01` through `SEC-14`).
- [x] ZERO Phase 1–8 source files or tests modified; ZERO packages installed; ZERO implementation code created.

---

**END OF SECURITY ARCHITECTURE SPECIFICATION**  
*Tradego Security Architecture — Ready for Senior Architecture Review.*  

*Status: NOT FROZEN | NOT APPROVED*
