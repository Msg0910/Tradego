# TRADEGO SECURITY TECHNOLOGY SELECTION SPECIFICATION
## Concrete Technology Evaluation, Session Governance & Operational Authorization Policy
### Document Version: 1.1.0-DRAFT | Status: Ready for Final Senior Architecture Review
### Upstream Status: Phases 1–8 Approved and Frozen | UI Arch v1.1 Frozen | UI/API Boundary Arch v1.1 Frozen | Security Arch v1.0 Approved with Conditions

---

## 1. PURPOSE & SCOPE

This document establishes the definitive technology evaluation, security policy determinations, and infrastructure selections for the **Tradego Platform Security Boundary**. It formally resolves the four conditions and open questions established during the Senior Architecture Review of `docs/tradego_security_technology_selection.md`:

1. **OIDC Role Clarification:** Explicit architectural delineation establishing that Tradego operates as an **OIDC Relying Party (Client / Resource Server)** when integrated with external enterprise identity providers, while utilizing direct native authentication (Argon2id + TOTP) for Phase 1 MVP without implementing an in-house OIDC Provider.
2. **Argon2id Worker Isolation (`SEC-TECH-ARGON2-01`):** Mandatory architectural offloading of CPU-intensive password hashing and verification outside the async event loop to protect real-time WebSocket event streaming.
3. **Audit Concurrency & Serialization:** Formalization of the Tier 1 audit queue, serialized single-writer pipeline, crash-consistency mechanics, and the explicit distinction between hash-chain tamper-evidence (integrity) and cryptographic authenticity (non-repudiation).
4. **Development Transport Relaxation:** Formal policy for loopback (`127.0.0.1`) unencrypted HTTP/WS development while enforcing strict TLS 1.3 / WSS for all non-loopback and production deployments.

```
+-----------------------------------------------------------------------------+
|                     TECHNOLOGY SELECTION MANDATE                            |
|                                                                             |
| 1. Technology must serve the immutable security invariants (SEC-01 to 14).  |
| 2. Security mechanisms exist strictly at external boundaries (Zones 2 & 3). |
| 3. The Phase 1–8 trading hot path remains 100% in-memory with ZERO network, |
|    ZERO database, ZERO authentication, and ZERO external blocking I/O.      |
| 4. Security controls ACCESS; it NEVER manufactures or alters trading truth. |
+-----------------------------------------------------------------------------+
```

### Scope & Non-Goals
- **In-Scope (Architectural Decisions):** Formal trade-off analyses, candidate evaluations, concrete architectural recommendations, security policy definitions, and an Implementation Readiness Checklist.
- **Strict Non-Goals (Zero Implementation):** Zero production code generation, zero package installations (`pip`, `npm`), zero database schema creation, zero token/certificate issuance, zero API gateway implementation, and zero modifications to existing Phase 1–8 source files or frozen UI/API specifications.

---

## 2. DECISION PRINCIPLES & EVALUATION FRAMEWORK

Every candidate technology and policy model is evaluated against thirteen institutional engineering criteria:

1. **Hot-Path Isolation:** Under no circumstances may a security component introduce synchronous network calls, serialization overhead, or mutex locks into tick ingestion, strategy evaluation, risk gating, or order execution.
2. **Security & Cryptographic Rigor:** Resistance to credential theft, replay attacks, session hijacking, tampering, and privilege escalation under a Zero Trust threat model.
3. **Operational Complexity:** Cognitive and maintenance overhead imposed on operators; avoidance of fragile distributed systems for single-broker/paper setups.
4. **Local Development Feasibility:** Ergonomics on developer workstations (Windows 11 / PowerShell) without requiring heavyweight cloud infrastructure or enterprise orchestration.
5. **Production Deployment Suitability:** Robustness, daemonization, and security hardening on dedicated production host environments (Linux / container appliances).
6. **Failure Blast Radius & Isolation:** Process, memory, and network decoupling ensuring that a crash or saturation of the security layer cannot crash the core trading engine.
7. **End-to-End Latency:** Minimizing transport and boundary ingress delays ($<5\text{ms}$ boundary overhead target for interactive operator commands).
8. **Maintainability & Auditability:** Verifiable code paths, clear debugging interfaces, transparent telemetry, and tamper-evident audit logging.
9. **Credential Rotation Ergonomics:** Support for scheduled and emergency rotation of secrets without requiring trading engine restarts or code redeployments.
10. **Deterministic Recovery:** Clear, deterministic fail-closed recovery procedures that prevent accidental resumption into broken states.
11. **Future Multi-User Scalability:** Seamless expansion from a single quantitative developer to concurrent institutional trading desks (Traders, Risk Officers, Observers).
12. **Future Live Broker Extensibility:** Preparedness for external broker credentials and session lifecycles without leaking secrets to the presentation layer.
13. **Platform Portability:** Technology compatibility across local Windows environments and headless Linux trading hosts.

---

## 3. AUTHENTICATION & IDENTITY ARCHITECTURE COMPARISON

To avoid architectural ambiguity, authentication concerns are strictly decoupled into five functional layers:
- **Identity Provider (IdP):** System of record for user identities, password credentials, and MFA enrollments.
- **Authentication Protocol:** Handshake mechanism verifying identity and issuing credentials (e.g. OAuth 2.0 / OIDC).
- **Access Token / Credential:** Compact proof of authentication presented with client requests.
- **Session Mechanism:** State model tracking active operator connections, activity timestamps, and revocation state.
- **Authorization Mechanism:** Enforcement point evaluating capability permissions (`CAP_*`) against requested actions.

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                            AUTHENTICATION CANDIDATE EVALUATION                              │
├───────────────────┬───────────────────────────────┬─────────────────────────────────────────┤
│ Architecture      │ Architectural Description     │ Primary Trade-Offs & Fit for Tradego    │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ A. OAuth 2.0 /    │ Industry-standard token-based │ + Clean separation of IdP and Gateway.  │
│    OIDC           │ federation with authorization │ - High operational complexity; requires │
│                   │ server (JWT / opaque tokens). │   external identity server deployment.  │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ B. Pure Stateless │ Signed JSON Web Tokens with   │ + Stateless gateway validation; simple. │
│    JWT Sessions   │ embedded claims and exp.      │ - Irrevocable until expiry; vulnerable   │
│                   │                               │   to stolen token replay; poor for WS.  │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ C. Opaque Server- │ Cryptographically random token│ + Instant server-side revocation; simple│
│    Side Sessions  │ mapped to memory session store│   token format; zero claim tampering.   │
│                   │                               │ - Requires session lookup per request.  │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ D. Mutual TLS     │ Client and server verify X.509│ + Maximum cryptographic security.       │
│    (mTLS)         │ certificates on TLS handshake.│ - Complex cert lifecycle & rotation;    │
│                   │                               │   poor UX in standard web browsers.     │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ E. API Keys /     │ Static or rotating secret keys│ + Simple machine-to-machine integration.│
│    Basic Auth     │ passed in HTTP headers.       │ - Inadequate for human sessions, MFA, or│
│                   │                               │   granular user capability tracking.    │
├───────────────────┼───────────────────────────────┼─────────────────────────────────────────┤
│ F. Hybrid Model   │ OpenID Connect authentication │ + COMBINES BEST OF ALL: Instant server  │
│    (Opaque Token  │ with Opaque Server-Side       │   revocation, centralized IdP audit,    │
│    + OIDC IdP)    │ Session handle at Gateway.    │   clean browser integration, short TTL. │
└───────────────────┴───────────────────────────────┴─────────────────────────────────────────┘
```

### Detailed Candidate Comparative Analysis

| Evaluation Dimension | A. OAuth 2.0 / OIDC | B. Pure Stateless JWT | C. Opaque Server Sessions | D. Mutual TLS (mTLS) | F. Hybrid Architecture (Recommended) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Security Posture** | High (Industry standard) | Moderate (Revocation gap) | High (Instant revocation) | Maximum (Cryptographic) | **High (Defense-in-depth)** |
| **Operational Overhead** | High (Separate IdP cluster)| Low (Key pair only) | Low-Moderate (State store)| High (PKI / Cert authority)| **Moderate (Local IdP / Gateway)** |
| **Revocation Capability** | Complex (Token blacklists) | **Fails SEC-14** (Cannot revoke)| **Optimal:** Instant deletion | Complex (CRL / OCSP) | **Optimal:** Instant session drop |
| **Browser Ergonomics** | Good (Standard redirects) | Good (Authorization header)| **Optimal:** Secure cookies/hdr | Poor (OS cert dialogs) | **Optimal:** Bearer / Secure token |
| **WebSocket Stream Fit** | Moderate (Ticket auth) | Moderate (Payload auth) | **Optimal:** Validated at upgrade| Good (Connection-level) | **Optimal:** Verified at upgrade |
| **Audit Traceability** | High (Federated claims) | Moderate (Claims in token)| High (Session ledger) | Moderate (Cert Subject) | **Maximum:** User + Session + Trace |
| **Failure Behavior** | IdP outage blocks login | Gateway validates offline | Session store must be up | Gateway validates offline | Gateway caches active sessions |
| **Suitability for Tradego**| Strong candidate | **REJECTED** (Revocation gap)| Strong candidate | Niche (S2S internal only)| **RECOMMENDED SELECTION** |

### Architectural Recommendation: The Hybrid Session Architecture
The recommended authentication architecture for Tradego is a **Hybrid Architecture (Candidate F)**:
1. **Upstream Identity & Federation Standard:** OpenID Connect (OIDC) standard used for external enterprise operator authentication and multi-factor verification.
2. **Gateway Session Representation:** Upon successful authentication, the Tradego API Gateway issues an **Opaque High-Entropy Session Token** (256-bit cryptographically secure random string).
3. **Session Storage:** Active sessions are tracked in an internal, fast, thread-safe memory store at the Gateway (Zone 2/3), containing `user_id`, `assigned_capabilities`, `created_at`, and `last_active_at`.
4. **Why Pure JWT Was Rejected:** Pure stateless JWT violates **`INVARIANT-SEC-14`** (Session Invalidation on Privilege Change) and **`INVARIANT-SEC-06`** because a compromised or demoted operator's JWT remains valid until physical token expiration, creating an unacceptable window of operational vulnerability during live market trading.

---

## 4. IDENTITY PROVIDER (IdP) & OIDC ARCHITECTURE

### 4.1 Explicit OIDC Role Clarification (Correction 1)
To eliminate architectural ambiguity between Identity Providers and Relying Parties:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                           PHASE 1 MVP: NATIVE DIRECT AUTHENTICATION                         │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  UI Client ──(Username / Password / TOTP)──► [Native Tradego Identity Module]               │
│                                              • Direct Argon2id password verification        │
│                                              • RFC 6238 TOTP validation                     │
│                                              • Issues 256-bit Opaque Session Token          │
│                                                                                             │
│  NOTE: The Native Identity Module is a direct internal credential verifier.                 │
│        It is NOT an OIDC Identity Provider and implements zero OIDC endpoints.              │
└─────────────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                      FUTURE PHASE 2: EXTERNAL OIDC FEDERATION PATTERN                       │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  Operator ──► [External OIDC Provider (Keycloak / Microsoft Entra ID / Okta)]               │
│                     │                                                                       │
│                     ▼ (Standard OIDC Authorization Code + PKCE)                             │
│               [Tradego API Gateway (Acts as OIDC Relying Party / Resource Server)]          │
│                     ├─► 1. Validates ID Token from External IdP                             │
│                     ├─► 2. Maps OIDC Claims / Groups to Tradego CAP_* Capabilities         │
│                     ├─► 3. Issues Opaque Session Token for UI & WebSocket Sessions          │
│                     └─► 4. Routes Audited Commands into Core Trading Engine                 │
│                                                                                             │
│  PROHIBITION: Tradego must NOT implement an in-house OIDC Identity Provider (e.g.           │
│               .well-known/openid-configuration, JWKS provider endpoints, OIDC token         │
│               issuer, or dynamic client registration). It acts strictly as an OIDC client.  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Phase 1 MVP Scope (Native Direct Authentication):**  
   Tradego uses native direct authentication:
   $$	ext{Native Tradego Identity Module} \longrightarrow 	ext{Argon2id} \longrightarrow 	ext{TOTP} \longrightarrow 	ext{Opaque Server-Side Session}$$
   The Native Identity Module is **NOT** an OIDC Identity Provider. It provides local credential verification without external dependencies.
2. **Phase 2 Institutional Scope (External OIDC Relying Party):**  
   When integrating Keycloak, Microsoft Entra ID, or Okta, Tradego operates strictly as an **OIDC Relying Party (RP) / Resource Server**. Tradego will **NEVER** implement an in-house OIDC Identity Provider.

---

## 5. ARGON2ID WORKER ISOLATION SPECIFICATION (Correction 2)

### Invariant SEC-TECH-ARGON2-01 (Password Hashing Hot-Loop Isolation)
> **INVARIANT-SEC-TECH-ARGON2-01:** CPU-intensive password hashing and verification (Argon2id) MUST execute strictly outside the API distribution gateway's asynchronous event loop.

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                            ASYNC EVENT LOOP THREADPOOL ISOLATION
══════════════════════════════════════════════════════════════════════════════════════════════════════

      HTTP Ingress: POST /api/v1/auth/login
              │
              ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ FastAPI / Uvicorn Main Event Loop (Asyncio Worker Thread)              │
   │                                                                        │
   │ • Manages non-blocking WebSocket connections                           │
   │ • Dispatches real-time TradegoEventEnvelope streams to UI clients      │
   │ • Enforces zero blocking I/O on the main loop                          │
   │                                                                        │
   │ [OFFLOAD CALL]                                                         │
   │   await run_in_threadpool(verify_argon2id, password, hash)             │
   └────────────────────────────────────┬───────────────────────────────────┘
                                        │ (Non-blocking threadpool handoff)
                                        ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ Dedicated Background Worker Threadpool (Bounded Threadpool Executor)   │
   │                                                                        │
   │ • Executes Argon2id verification (m=64MiB, t=3, p=4: ~100-250ms CPU)  │
   │ • Zero impact on concurrent WebSocket packet distribution              │
   │ • Returns boolean verification result to async handler                 │
   └────────────────────────────────────────────────────────────────────────┘
```

### Architectural Rationale & Enforcement
1. **Event Loop Protection:** Argon2id is mathematically designed to be CPU- and memory-intensive to defeat hardware brute-forcing. Executing Argon2id synchronously on the main asyncio event loop would freeze the gateway process for 100–300ms per attempt, causing dropped WebSocket frames, delayed quote streaming, and rendering lag on all connected operator workstations.
2. **Future Implementation Mechanism:** The API gateway implementation must route all Argon2id calls through `starlette.concurrency.run_in_threadpool` or an equivalent bounded Python `ThreadPoolExecutor`.

---

## 6. SESSION SECURITY ARCHITECTURE & POLICY SPECIFICATION

To prevent session hijacking, stale credential abuse, and unauthorized command injection, the security architecture establishes explicit, non-negotiable **Session Policy Thresholds**:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                SESSION GOVERNANCE POLICIES                                  │
├───────────────────────────┬─────────────────────────┬───────────────────────────────────────┤
│ Session Tier              │ Absolute Lifetime (TTL) │ Inactivity Idle Timeout               │
├───────────────────────────┼─────────────────────────┼───────────────────────────────────────┤
│ Tier 1: Observer Session  │ 12 Hours (Trading Day)  │ 60 Minutes                            │
│ Tier 2: Operator Session  │ 8 Hours (Trading Shift) │ 15 Minutes                            │
│ Tier 3: Admin / Emergency │ 2 Hours (Restricted)    │ 5 Minutes                             │
└───────────────────────────┴─────────────────────────┴───────────────────────────────────────┘
```

### Explicit Session Governance Rules
1. **Concurrent Session Quota:**
   - Observers: Maximum 3 concurrent active sessions.
   - Operators: Maximum 2 concurrent active sessions (e.g. primary multi-monitor desk + secondary monitoring tablet).
   - Administrators: Strictly 1 active concurrent session. Initiating a new admin login automatically invalidates prior active sessions.
2. **Immediate Invalidation on Privilege Modification (`SEC-14`):**
   - If an operator's role or capability set is altered in the user store, the Gateway's Session Manager must immediately revoke all active session tokens associated with that user ID and forcefully terminate active WebSocket streams with close code `1008 (Policy Violation)`.
3. **Sensitive Command Re-Authentication Challenge:**
   - Executing critical administrative actions (e.g. `CAP_ADMIN_RECOVERY` to exit `HALTED` state, or modifying core risk limit parameters) requires **step-up re-authentication** (re-entering password or TOTP one-time code within 60 seconds prior to command dispatch).
4. **WebSocket Reconnect Session Handshake:**
   - Reconnecting WebSocket clients must present the active session token during the connection handshake. The gateway validates that the token remains unrevoked and that idle timeout has not elapsed before accepting subscription traffic.

---

## 7. EMERGENCY AUTHORIZATION & HALTED RECOVERY POLICY

### Resolution of Senior Review Condition: HALTED Recovery Policy
The Senior Security Review raised the critical operational governance question:  
*Should recovery from `HALTED` require (A) Single Administrator Authorization or (B) Two-Person Authorization?*

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                             HALTED RECOVERY POLICY TRADE-OFFS                               │
├───────────────────────┬──────────────────────────────────┬──────────────────────────────────┤
│ Dimension             │ Option A: Single Administrator   │ Option B: Two-Person (Dual) Rule │
├───────────────────────┼──────────────────────────────────┼──────────────────────────────────┤
│ Incident Response Time│ Immediate (<30 seconds).         │ Slower (Requires quorum).        │
│ Single-Operator Feas. │ 100% compatible with standalone. │ Fails in single-operator setups. │
│ Rogue Insider Defense │ Relies entirely on audit trail.  │ Maximum defense against insider. │
│ Operational Deadlock  │ Zero risk of deadlock.           │ Risk of deadlock if peer absent. │
└───────────────────────┴──────────────────────────────────┴──────────────────────────────────┘
```

### Institutional Determination: The Phased Quorum Model
- **Default Tradego Policy (Current Scope — Phase 8 Paper & Dev):**  
  Adopt **Option A+ (Single Authorized Administrator with Mandatory Multi-Factor Step-Up and Immutable Forensic Reason Logging)**.  
  *Operational Rationale:* Tradego is currently operating in paper trading mode and standalone development. Requiring two physical operators would paralyze local operational workflows and testing. Security is preserved by requiring:
  1. Administrative capability (`CAP_ADMIN_RECOVERY`).
  2. Cryptographic MFA step-up verification at the moment of command submission.
  3. Mandatory, non-empty structural diagnostic reason string (`recovery_justification >= 20 characters`).
  4. Automatic broadcast of an `EMERGENCY_RECOVERY_EXECUTED` security audit alert to all active sessions.
- **Institutional Multi-User Policy (Future Live Execution):**  
  Provide an architectural policy toggle (`ENABLE_DUAL_OPERATOR_RECOVERY=TRUE`) in the configuration schema, allowing institutional firms to require dual-signature cryptographic tokens from two distinct administrator IDs before un-halting live execution.

### Operational Command Governance Matrix

| Command | Capability Required | Confirmation Level | Re-Auth / MFA | Downstream Gate |
| :--- | :--- | :--- | :--- | :--- |
| **`KILL_SWITCH`** | `CAP_EMERGENCY_KILL_SWITCH` | Single click / Hotkey | None (Instant halt) | `TradingGuard.trip_kill_switch()` |
| **`EMERGENCY_FLATTEN`** | `CAP_EMERGENCY_FLATTEN` | Explicit Modal Dialog | None (Emergency exit)| `TradingGuard.emergency_flatten()` |
| **`HALTED RECOVERY`** | `CAP_ADMIN_RECOVERY` | Dual-Confirmation Form| Mandatory MFA Step-Up| `TradingGuard.recover_from_halted()`|
| **`SHUTDOWN`** | `CAP_ADMIN_SHUTDOWN` | Verification Dialog | Mandatory Password | PipelineCoordinator orderly drain |

---

## 8. SECRETS MANAGEMENT ARCHITECTURE

To satisfy **`INVARIANT-SEC-08`** (Secrets Never Reach Browser or Client Bundles), secrets management technologies were evaluated:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               SECRETS MANAGEMENT EVALUATION                                 │
├───────────────────┬─────────────────────────────────────────────────────────────────────────┤
│ Candidate         │ Operational Evaluation & Environment Suitability                        │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ A. HashiCorp Vault│ Enterprise standard; dynamic secrets, strict audit, automated rotation. │
│                   │ - High operational overhead; requires external daemon and unseal keys.  │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ B. Cloud KMS /    │ AWS Secrets Manager / Azure Key Vault / GCP Secret Manager.             │
│    Secret Manager │ + Zero local maintenance; native IAM integration; automated rotation.   │
│                   │ - Cloud vendor lock-in; requires internet egress from trading engine.   │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ C. OS-Protected   │ Windows DPAPI / Linux Secret Service / Keyring.                         │
│    Keyring        │ + Native host OS security; hardware-backed encryption (TPM).            │
│                   │ - Inconsistent cross-platform APIs; complex headless automation.        │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ D. Encrypted File │ Local file encrypted with master key injected via environment variable  │
│    Store (SOPS)   │ (e.g. Mozilla SOPS or AES-256-GCM encrypted vault file).                │
│                   │ + Highly portable; works seamlessly on Windows dev and Linux prod;      │
│                   │   zero external network dependency; version-controlled ciphertexts.     │
│                   │ - Manual key distribution.                                              │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ E. Raw Environment│ Secrets stored directly in plain process environment variables.         │
│    Variables      │ + Simple; zero code dependencies.                                       │
│                   │ - Risk of accidental exposure via crash dumps, debug prints, or /proc.  │
└───────────────────┴─────────────────────────────────────────────────────────────────────────┘
```

### Architectural Recommendation: Tiered Secrets Architecture
- **Development & Staging Baseline (Recommended):** Adopt **Candidate D (AES-256-GCM Encrypted Local Vault)** combined with scoped process environment variables. Secrets on disk are strictly encrypted; the master decryption key is injected at process startup via a single ephemeral environment variable (`TRADEGO_MASTER_KEY`).
- **Production Key Management Hygiene:** In shared host or production deployments, environment variables risk exposure through `/proc` or process dumps. Production deployment requires migrating master key injection to a file-permissioned key file (`0400`) or native secret manager.
- **Production Staging & Multi-Broker Live (Future):** Architecture supports **Candidate A (Vault)** or **Candidate B (Cloud KMS)** via an abstract `SecretsProvider` interface.
- **Absolute Rule:** Secrets are ingested strictly by backend server processes in Zone 3 and Phase 7 adapters; secrets are NEVER exposed across WebSocket event envelopes, REST state snapshots, or client browser code.

---

## 9. SECURITY AUDIT STORAGE ARCHITECTURE (Correction 3)

To satisfy **`INVARIANT-SEC-09`** (Security Audit Separation from Trading Telemetry) and resolve concurrency and crash consistency requirements:

```
══════════════════════════════════════════════════════════════════════════════════════════════════════
                        SERIALIZED TIER-1 SECURITY AUDIT PIPELINE
══════════════════════════════════════════════════════════════════════════════════════════════════════

      Concurrent Gateway Ingress Workers (Async HTTP/WS Handlers)
            │                  │                  │
            ├──────────────────┼──────────────────┤
            ▼                  ▼                  ▼
      ┌─────────────────────────────────────────────────────────┐
      │ Bounded Asynchronous Audit Queue (Zone 3 Memory)        │
      │ • Thread-safe, non-blocking enqueue                     │
      │ • Preserves deterministic ingress sequencing            │
      │ • Bounded capacity: 16,384 records                      │
      └────────────────────────────┬────────────────────────────┘
                                   │
                                   ▼ (Single-consumer serialized dispatch)
      ┌─────────────────────────────────────────────────────────┐
      │ Single Serialized Audit Writer (Background Worker)   │
      │                                                         │
      │ 1. Dequeues next audit event                            │
      │ 2. Reads previous hash watermark (H_{i-1})              │
      │ 3. Computes monotonic hash:                             │
      │    H_i = SHA-256(H_{i-1} || Record_i)                   │
      │ 4. Formats canonical JSON Lines entry                   │
      │ 5. Appends record to disk + synchronous fsync()         │
      │ 6. Updates in-memory hash watermark                     │
      └────────────────────────────┬────────────────────────────┘
                                   │
                                   ▼ (Append-only write)
      ┌─────────────────────────────────────────────────────────┐
      │ Immutable Audit Ledger File: audit_YYYYMMDD.jsonl       │
      │ (Local encrypted disk partition; restricted 0400)       │
      └─────────────────────────────────────────────────────────┘
```

### Formal Concurrency & Integrity Mechanics
1. **Serialized Single-Writer Invariant:** Concurrent gateway requests enqueue audit records into a thread-safe, bounded asynchronous queue. A single, dedicated audit writer worker deserializes and processes records sequentially, guaranteeing deterministic ordering and eliminating hash-chain race conditions.
2. **Crash Consistency & Partial Write Protection:** Every audit entry is written as a discrete, atomic line followed by an explicit `fsync()` flush before acknowledging state-mutating commands. If a process crash occurs mid-write, partial lines are detected during hash validation and quarantined.
3. **Queue Saturation & Backpressure Policy:** If the bounded audit queue fills to capacity (e.g. during catastrophic disk stalls), the security gateway **FAILS CLOSED**: it rejects all inbound state-mutating commands (`DENY`) to guarantee that no un-audited command can ever reach the trading engine.
4. **Integrity vs. Authenticity Delineation:**
   - **Tamper-Evident Integrity (Tier 1):** SHA-256 hash chaining mathematically proves that no interior lines have been altered, inserted, or deleted without invalidating subsequent hashes.
   - **Cryptographic Authenticity & Non-Repudiation (Tier 2 Future):** Hash chaining alone does not prevent an attacker with root file-overwrite access from regenerating an entire chain. Full institutional compliance requires an **asymmetric digital signature anchor** or periodic notarization of $H_i$ to an external append-only log or KMS.

---

## 10. API GATEWAY TECHNOLOGY SELECTION

The API Gateway is the public-facing guardian residing in Zones 2 and 3. Three candidate technologies were evaluated:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 API GATEWAY CANDIDATES                                      │
├───────────────────┬─────────────────────────────────────────────────────────────────────────┤
│ Technology        │ Evaluation Profile & Trade-Off Analysis                                 │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 1. FastAPI /      │ High-performance Python async framework (Uvicorn / Starlette).          │
│    Uvicorn        │ + Native Python data model integration; seamless DTO serialization;     │
│                   │   excellent WebSocket and REST support; zero multi-language friction;   │
│                   │   runs natively on Windows and Linux; trivial testability with pytest.  │
│                   │ - Single-threaded asyncio event loop requires careful offloading of     │
│                   │   CPU-heavy cryptography (Argon2id) to worker thread pools.             │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 2. Envoy Proxy    │ High-performance C++ cloud-native service proxy.                        │
│                   │ + Unmatched throughput, concurrency, and advanced filter chaining.      │
│                   │ - Steep configuration complexity (YAML/xDS); requires external auth     │
│                   │   gRPC service (ext_authz); poor ergonomics for local Windows dev.      │
├───────────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 3. NGINX Reverse  │ Battle-tested C reverse proxy and load balancer.                        │
│    Proxy          │ + Rock-solid TLS termination and HTTP connection pooling.               │
│                   │ - Limited native WebSocket application-level routing; requires separate │
│                   │   backend application service for session logic and command validation. │
└───────────────────┴─────────────────────────────────────────────────────────────────────────┘
```

### Architectural Recommendation: FastAPI / Uvicorn Gateway Service
- **Selected Proposed Framework:** **FastAPI (ASGI) on Uvicorn**.
- **Architectural Rationale:**
  1. Unified implementation of both HTTP REST endpoints (for snapshot delivery and command ingestion) and persistent WebSocket connections (for streaming `TradegoEventEnvelope` broadcasts).
  2. Shared Python dataclass and Pydantic schema contracts without multi-language code generation friction.
  3. Seamless cross-platform execution across developer Windows environments and Linux production containers.

---

## 11. TRANSPORT SECURITY & DEVELOPMENT RELAXATION (Correction 4)

To resolve local developer ergonomics without compromising production security:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                             TRANSPORT SECURITY SPECIFICATION                                │
├───────────────────────────┬─────────────────────────────────────────────────────────────────┤
│ Environment Scope         │ Transport Security Policy & Architectural Rule                  │
├───────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Production & Staging      │ Mandatory TLS 1.3 / WSS Encryption.                             │
│ (All non-loopback network)│ • All traffic over external WAN/LAN requires strong TLS 1.3.    │
│                           │ • Plain HTTP or unencrypted WS connections are rejected.        │
│                           │ • Strict Transport Security (HSTS) headers enforced.           │
├───────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Local Development         │ Loopback Exception Permitted (127.0.0.1 / localhost only).      │
│ (Developer Workstation)   │ • Unencrypted HTTP and WS are permitted strictly on loopback.   │
│                           │ • Eliminates browser self-signed certificate warnings during    │
│                           │   local development on Windows and macOS.                       │
│                           │ • PROHIBITION: The loopback exception must never be exposed on  │
│                           │   0.0.0.0, LAN, or public network interfaces.                   │
└───────────────────────────┴─────────────────────────────────────────────────────────────────┘
```

---

## 12. EVENT STREAM & SNAPSHOT SECURITY SPECIFICATION

The security gateway strictly enforces the frozen sequence and synchronization invariants:

```
      CLIENT (ZONE 1)                 GATEWAY SECURITY (ZONE 2/3)           STATE EXPORT (ZONE 4)
             │                                     │                                  │
             ├───── Authenticated WS Connect ─────►│                                  │
             │      (Presents Session Token)       ├── 1. Validate Session Token     │
             │                                     ├── 2. Verify Channel Scope        │
             │◄──── Handshake Accepted (WS 101) ───┤                                  │
             │                                     ├───── Subscribe Authorized Feeds ─►│
             │                                     │                                  │
             │ [Inbound Stream Buffer Engaged]     │                                  │
             │                                     │                                  │
             ├───── GET /api/v1/state/snapshot ───►│                                  │
             │      (Authenticated REST Request)   ├── 1. Verify CAP_READ_*           │
             │                                     ├── 2. Mask Sensitive Fields       │
             │                                     ├───── Pull Authoritative State ───►│
             │◄──── Return Snapshot (S_snap) ──────┤                                  │
             │                                     │                                  │
             │ [Reconcile: Drop seq <= S_snap]     │                                  │
             │ [Verify contiguous: S_snap + 1]     │                                  │
             │                                     │                                  │
             │◄──── Push Event Envelope ───────────┼───── Envelopes (seq > S_snap) ───┤
```

### Stream Security Invariants Preserved
1. **One-Way Push Protection (`SEC-13`):** WebSocket frames from client to server are strictly prohibited. Inbound data frames trigger immediate socket termination (`WS 1003 Unsupported Data`).
2. **Snapshot Integrity & Masking:** Snapshots delivered to clients with `OBSERVER` roles have account cash balances and account numbers masked or aggregated.
3. **Delivery Frontier Integrity:** Sequence numbers are monotonically assigned at the State Export boundary (Zone 4) before gateway distribution, guaranteeing that sequence tampering is instantly detected by client stream reconcilers.

---

## 13. SECURITY NETWORK TOPOLOGY & DEPLOYMENT OPTIONS

Three physical deployment topologies were evaluated:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               SECURITY TOPOLOGY OPTIONS                                     │
├─────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                             │
│  [OPTION A: MONOLITHIC IN-PROCESS]                                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐               │
│  │ Single Host Process (Python VM)                                         │               │
│  │  ┌──────────────────────┐   Queue    ┌──────────────────────────────┐   │               │
│  │  │ Trading Engine (P1-8)│ ─────────► │ Security & Gateway Service   │   │ ──► [Browser] │
│  │  └──────────────────────┘            └──────────────────────────────┘   │               │
│  └─────────────────────────────────────────────────────────────────────────┘               │
│                                                                                             │
│  [OPTION B: SEPARATE PROCESS ON SAME HOST (RECOMMENDED)]                                    │
│  ┌───────────────────────────┐          ┌──────────────────────────────┐                   │
│  │ Trading Engine Process    │ ───────► │ Security & Gateway Process   │ ──► [Browser]     │
│  │ (High-Priority Core)      │ IPC/SHM  │ (Public Network Ingress)     │                   │
│  └───────────────────────────┘          └──────────────────────────────┘                   │
│                                                                                             │
│  [OPTION C: DISTRIBUTED THREE-TIER DEPLOYMENT]                                              │
│  ┌───────────────────┐      ┌────────────────────────┐      ┌──────────────────────────┐   │
│  │ Trading Host      │ ───► │ Security Gateway Host  │ ───► │ API Edge / CDN           │   │
│  │ (Air-Gapped Core) │ LAN  │ (Auth & Policy Service)│ WAN  │ (TLS Termination)        │   │
│  └───────────────────┘      └────────────────────────┘      └──────────────────────────┘   │
│                                                                                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Comparative Topology Trade-Off Matrix

| Evaluation Dimension | Option A: In-Process Dual-Thread | Option B: Separate Process (Same Host) | Option C: Distributed Three-Tier |
| :--- | :--- | :--- | :--- |
| **Hot-Path Fault Isolation** | Poor (Gateway crash/OOM impacts engine) | **High (Engine completely insulated)** | Maximum (Physical machine boundary) |
| **Inter-Process Latency** | Sub-microsecond (Queue pointer) | **Ultra-Low (10–50 $\mu	ext{s}$ via IPC/SHM)**| Moderate (100–500 $\mu	ext{s}$ over LAN) |
| **Python GIL Contention** | High risk under heavy socket loads | **Zero GIL contention (Separate VM)** | Zero GIL contention (Separate VM) |
| **Network Attack Surface** | Engine process bound to public ports | **Engine has ZERO open network ports** | Tiered DMZ perimeters |
| **Operational Simplicity** | Maximum (Single process) | **Moderate (Two supervised processes)**| Complex (Distributed orchestration)|
| **Windows Dev Ergonomics** | Excellent | **Excellent (Local background process)**| Poor (Requires complex local network)|
| **Architectural Decision** | Candidate for unit tests only | **SELECTED ARCHITECTURAL BASELINE** | Deferred to Multi-Broker Live Phase |

---

## 14. HOT-PATH REQUIREMENT & GUARANTEE

The Tradego Security Architecture affirms the absolute, uncompromised hot-path requirement:

$$	ext{NO authentication calls} \quad ig| \quad 	ext{NO authorization lookups} \quad ig| \quad 	ext{NO database queries} \quad ig| \quad 	ext{NO external security services}$$

$$	ext{inside the synchronous execution loop:}$$

$$	ext{Market Data Ingestion } (T_1) \longrightarrow 	ext{State Store} \longrightarrow 	ext{Candles} \longrightarrow 	ext{Features} \longrightarrow 	ext{Signals } (T_2) \longrightarrow 	ext{Risk } (T_3 \dots T_4) \longrightarrow 	ext{Execution } (T_5 \dots T_9) \longrightarrow 	ext{Portfolio } (T_{10})$$

**Architectural Enforcement:** Security logic is physically quarantined within Zones 2 and 3 at the API Gateway. The trading engine in Zone 5 receives pre-authenticated, pre-authorized, and pre-validated command structs via internal IPC/queue mechanisms and emits state projections via non-blocking egress memory buffers.

---

## 15. TECHNOLOGY DECISION MATRIX

| Domain | Candidate Evaluated | Selected / Proposed | Architectural Reason | Future Gate |
| :--- | :--- | :--- | :--- | :--- |
| **Identity Standard** | OpenID Connect (OIDC) | **SELECTED (Federation)** | Standard for external identity; Tradego acts strictly as Relying Party. | Phase 2 Staging |
| **Identity Provider** | Native Tradego IdP (Argon2id+TOTP) | **SELECTED (MVP)** | Direct credential verifier; zero external daemons; air-gapped. | Phase 1 Baseline |
| **Authentication Token**| High-Entropy Opaque Session Token | **SELECTED** | Instant server revocation (`SEC-14`), zero client token parsing. | Impl. Readiness |
| **Stateless JWT** | Pure Self-Contained JWT | **NOT SUITABLE** | Violates `SEC-14`; cannot revoke on privilege changes or compromise.| Rejected |
| **Capability Engine** | Internal Capability RBAC | **SELECTED** | Granular scoping (`CAP_*`); evaluated cleanly at Zone 3 boundary. | Impl. Readiness |
| **Secrets Management** | AES-256-GCM Encrypted Vault File | **SELECTED (MVP)** | Portable cross-platform (Win/Linux); no external daemon required. | Production Vault |
| **Audit Storage (Tier 1)**| Append-Only JSONL with SHA-256 Chain | **SELECTED** | Serialized single-writer worker; crash-consistent; tamper-evident. | Impl. Readiness |
| **Audit Storage (Tier 2)**| PostgreSQL / SIEM Shipper | **PROPOSED** | Long-term institutional query indexing and compliance archive. | Enterprise Staging |
| **API Gateway Framework**| FastAPI (ASGI) on Uvicorn | **SELECTED** | High-speed native async Python, unified REST+WebSocket support. | Impl. Readiness |
| **Transport Encryption**| TLS 1.3 / WSS Encryption | **SELECTED (Production)** | Mandatory for production/LAN; loopback exception on 127.0.0.1 for dev. | Infrastructure Gate |
| **Rate Limiting Engine** | In-Memory Sliding Window Bucket | **SELECTED (MVP)** | Local memory rate limiter at gateway; protects against ingress DoS. | Distributed Redis |
| **Emergency Authorization**| Single-Admin + Mandatory Step-Up MFA| **SELECTED (MVP)** | Eliminates operational deadlock in paper/dev; audit-verified. | Institutional Quorum|
| **Deployment Topology** | Option B (Separate Process on Host) | **SELECTED** | Complete engine GIL isolation; zero open ports on engine core. | Impl. Readiness |

---

## 16. FORMAL SECURITY POLICY DECISIONS

The following security policy parameters are formally enacted for platform implementation:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                               FORMAL SECURITY POLICY RULES                                  │
├───────────────────────────┬─────────────────────────────────────────────────────────────────┤
│ Policy Parameter          │ Enacted Policy Threshold & Invariant Rule                       │
├───────────────────────────┼─────────────────────────────────────────────────────────────────┤
│ Operator Session TTL      │ 8 Hours absolute maximum lifetime (requires fresh re-login).    │
│ Operator Idle Timeout     │ 15 Minutes inactivity timeout (forces session lock).            │
│ Admin / Emergency TTL     │ 2 Hours absolute lifetime; 5 minutes idle timeout.              │
│ Privilege Change Rule     │ Immediate session revocation; active WebSockets closed (1008).  │
│ Emergency Recovery Gate   │ Single administrator + mandatory MFA step-up + reason string.   │
│ Emergency Flatten Gate    │ Authorized role + explicit client-side confirmation dialog.     │
│ Credential Rotation Cycle │ 90 Days scheduled rotation; immediate emergency rotation.       │
│ Audit Retention Period    │ 7 Years immutable retention for compliance and trade lineage.   │
│ Brute-Force Defense       │ Exponential backoff after 3 fails; account locked after 5 fails.│
│ Inbound Replay Window     │ Monotonic nonce tracking; timestamp skew rejected if > 5,000ms. │
└───────────────────────────┴─────────────────────────────────────────────────────────────────┘
```

---

## 17. IMPLEMENTATION READINESS CHECKLIST

Before any security implementation code is authored, the following architectural milestones must be completed:

- [x] Comprehensive Threat Model approved (Threats A through X).
- [x] Formal Trust Boundaries defined (Zones 0 through 6).
- [x] Security Invariants formalized (`SEC-01` through `SEC-14`).
- [x] OIDC role clarified: Tradego is an OIDC Relying Party, not an OIDC Identity Provider (Correction 1).
- [x] Argon2id worker isolation invariant formalized (`SEC-TECH-ARGON2-01`) (Correction 2).
- [x] Tier 1 audit queue, single-writer serialization, and crash consistency specified (Correction 3).
- [x] Transport security formalizes loopback development exception vs. mandatory production TLS 1.3 (Correction 4).
- [x] Identity architecture standard selected (OIDC claims model for federation).
- [x] Authentication mechanism selected (Opaque Server-Side Session Token).
- [x] Stateless pure JWT rejected due to revocation constraints (`SEC-14`).
- [x] Capability authorization taxonomy established (`CAP_*` matrix).
- [x] Session lifetime and idle timeout policies formalized.
- [x] Emergency HALTED recovery governance resolved (Single Admin + MFA step-up).
- [x] Secrets management architecture defined (Encrypted Vault + Env Master Key).
- [x] Process deployment topology selected (Option B: Separate Process).
- [x] Hot-path security isolation mathematically guaranteed.

---

## 18. STRICT NON-GOALS

The following activities are explicitly prohibited during this architecture gate:
- **No Implementation Code Authoring:** Do not write Python authentication middleware, FastAPI route handlers, or encryption algorithms.
- **No Frontend Login Development:** Do not create React login pages, HTML forms, or client-side cookie handlers.
- **No Dependency Installation:** Do not install `pydantic-settings`, `passlib`, `bcrypt`, `cryptography`, or `authlib`.
- **No Database Deployment:** Do not execute database migrations, write SQL tables, or stand up Redis daemons.
- **No Production Secret Issuance:** Do not generate production API keys, production certificates, or live broker credentials.
- **No Engine Modifications:** Do not alter Phase 1–8 core execution modules or existing test fixtures.

---

## 19. FINAL ARCHITECTURAL STATUS

```
TRADEGO SECURITY TECHNOLOGY SELECTION
CORRECTED DECISION DRAFT
CONDITIONS ADDRESSED
READY FOR FINAL SENIOR ARCHITECTURE REVIEW
NOT IMPLEMENTED
NOT FROZEN
```
