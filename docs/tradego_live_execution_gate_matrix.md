# TradeGo Live Execution Gate Matrix & Safety Boundary Specification

**Document Version:** 1.0.0  
**Phase:** 9 / 10 Production Readiness & Forensic Remediation  
**Status:** AUTHORITATIVE & FROZEN  
**Classification:** Tier-1 Financial Safety Specification  

---

## 1. Executive Summary & Core Invariant

The TradeGo automated and semi-automated trading system enforces a defense-in-depth, fail-closed gate matrix prior to any live broker connectivity activation, order instruction construction, or order dispatch.

### The Canonical Execution Chain
Under no circumstances may an order reach any execution venue without strictly traversing the complete, non-bypassable sequential lifecycle:

$$\begin{aligned}
\text{ExecutionIntent} &\longrightarrow \text{Operational Approval (Two-Person Rule)} \\
&\longrightarrow \text{Pre-Trade Risk Gate Evaluation} \\
&\longrightarrow \text{Canonical OrderInstruction (Validated)} \\
&\longrightarrow \text{Dispatch Authorization Gates} \\
&\longrightarrow \text{BrokerAdapter (Idempotency Enforced)} \\
&\longrightarrow \text{Live Network Transport Safeguard} \\
&\longrightarrow \text{Broker Acknowledgement} \\
&\longrightarrow \text{ExecutionState Tracking} \\
&\longrightarrow \text{Durable Fill Reconciliation (WAL Logged)} \\
&\longrightarrow \text{Post-Trade Broadcast Stream}
\end{aligned}$$

### Strict Prohibitions
1. **No UI to Broker Bypass**: The Operator UI has zero capability or direct route to dispatch orders to a broker adapter.
2. **No Strategy to Broker Bypass**: Algorithmic strategies emit proposals (`ExecutionIntent`), never instructions or direct orders.
3. **No Unapproved Draft Dispatch**: As remediated in H-01, `create_instruction()` creates unvalidated draft instructions (`CREATED`), while only `create_instruction_from_intent()` or instructions transitioning through genuine risk evaluation (`VALIDATED`) can be dispatched.
4. **No Real-Money Transport Activation**: In the current deployment, `LiveBrokerAdapter._execute_live_dispatch()` unconditionally fails closed with `REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED`.

---

## 2. Comprehensive Live Execution Gate Matrix

Every gate in the matrix evaluates independently in sequence. If any single gate fails, execution **fails closed immediately**, leaving system state unmutated and recording a tamper-evident audit event.

| Gate # | Gate Name | Evaluating Component | Input Requirement | Verification Rule | Failure Outcome | State Mutation | Audit Action Emitted |
| :---: | :--- | :--- | :--- | :--- | :--- | :---: | :--- |
| **G-01** | **Authenticated Operator Session** | `InMemorySessionStore` | Bearer Token in `Authorization` header | Session token exists in store, is unexpired, and is not revoked. | `401 Unauthorized` | None | `AUTH_SESSION_REJECTED` |
| **G-02** | **RBAC / Capability Gate** | `gateway/security.py` | Valid session token & requested endpoint | Operator possess required `Capability` (`CAP_ADMIN` for mode changes, `CAP_TRADE_DISPATCH` for order dispatch). | `403 Forbidden` | None | `INSUFFICIENT_CAPABILITY` |
| **G-03** | **Explicit Live Confirmation** | `BrokerConnectivityManager.set_mode()` | `mode=LIVE`, `confirm_live: bool` | `confirm_live` must be explicitly set to `True` by an authorized operator. | `ValueError: LIVE_ACTIVATION_REJECTED` | Remains `PAPER` | `LIVE_ACTIVATION_REJECTED` |
| **G-04** | **Operational Guard State** | `TradingGuard` | `TradingGuard.state` | Guard state must be `GuardState.RUNNING` / `NORMAL`. Blocked if `HALTED` or `PAUSED`. | `RuntimeError: GUARD_HALTED` | None | `DISPATCH_BLOCKED_GUARD_HALTED` |
| **G-05** | **Valid Broker Credentials** | `BrokerCredentialsConfig` | `credentials.is_valid` | Venue name, client ID, and access token must be non-empty strings. | `ValueError: BROKER_AUTH_FAILED` | `AUTH_FAILED` | `BROKER_AUTH_FAILED` |
| **G-06** | **Broker Connectivity State** | `BrokerConnectivityManager` | `connectivity_state` | Must be `BrokerConnectivityState.CONNECTED`. Transitions validated against M-01 graph. | `BrokerDispatchResult: BROKER_NOT_CONNECTED` | None | `DISPATCH_BLOCKED_NOT_CONNECTED` |
| **G-07** | **Heartbeat Freshness** | `BrokerConnectivityManager` | `last_heartbeat` | Elapsed time since last heartbeat $\le$ `HEARTBEAT_TIMEOUT_SECONDS` (30s). Cannot be `DEGRADED`. | `BrokerDispatchResult: BROKER_DEGRADED` | Auto-transitions to `DEGRADED` | `BROKER_HEARTBEAT_TIMEOUT` |
| **G-08** | **Pre-Trade Risk Gate Availability** | `PreTradeRiskEvaluator` | `risk_gate_available: bool` | The risk gate coordinator must report active availability. | `ValueError: RISK_GATE_UNAVAILABLE` | None | `RISK_GATE_UNAVAILABLE_BLOCKED` |
| **G-09** | **Canonical OrderInstruction Object** | `LiveBrokerAdapter._validate_instruction()` | `instruction: Any` | Must be an instantiated `OrderInstruction` object (raw dicts or malformed objects strictly rejected). | `TypeError: INVALID_INSTRUCTION_TYPE` | None | `INVALID_INSTRUCTION_REJECTED` |
| **G-10** | **Instruction Validation & Provenance (H-01)** | `OrderInstructionManager.dispatch_instruction()` | `instruction.state`, `risk_evaluation_reference` | `state == InstructionState.VALIDATED` and `risk_evaluation_reference` must be a genuine non-hardcoded reference. | `ValueError: INSTRUCTION_NOT_VALIDATED` | None | `DISPATCH_BYPASS_ATTEMPT_BLOCKED` |
| **G-11** | **Deterministic Idempotency Hash** | `LiveBrokerAdapter._compute_idempotency_key()` | `instruction_id`, `symbol`, `side`, `quantity`, `correlation_id` | SHA-256 hash must not exist in `_dispatched_idempotency_keys`. | `BrokerDispatchResult: DUPLICATE_DISPATCH_BLOCKED` | None | `DUPLICATE_DISPATCH_BLOCKED` |
| **G-12** | **Production Transport Safeguard (Step 9)** | `LiveBrokerAdapter._execute_live_dispatch()` | `instruction`, `idemp_key` | Real broker transport plugin must be explicitly bound. Default uninitialized handler fails closed. | `BrokerDispatchResult: REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED` | Instruction `FAILED` | `REAL_BROKER_TRANSPORT_UNINITIALIZED` |
| **G-13** | **Durable WAL Event Journaling (M-02)** | `PersistenceManager.append()` | `record_type`, `record_id`, `payload` | Event committed to disk with monotonic sequence, SHA-256 hash chain, and `os.fsync()`. | `IOError / WAL_WRITE_ERROR` | Quarantine / Alert | `WAL_COMMIT_FAILED` |

---

## 3. Secret Redaction & Log Sanitization Boundary (M-03)

In accordance with M-03 remediation, credential parameters are strictly sanitized across all boundary representations:

1. **Top-Level Credentials**:
   - `access_token` $\to$ `[REDACTED]`
   - `api_key` $\to$ `[REDACTED]`
   - `secret_key` $\to$ `[REDACTED]`
   - `client_id` $\to$ Masked identifier (e.g. `OP***99`)
   - `account_id` $\to$ Masked identifier (e.g. `AC***01`)
2. **Dynamic / Extra Venue Parameters (`extra_params`)**:
   - Dynamically inspected and recursively sanitized across arbitrary nested dictionaries and lists.
   - Any key matching `_SENSITIVE_KEYS` (`password`, `secret`, `secret_key`, `api_key`, `access_token`, `refresh_token`, `token`, `client_secret`, `authorization`, or containing substring sensitive tokens) has its value replaced with `[REDACTED]`.
   - Non-sensitive operational metadata (`environment`, `timeout_seconds`, `port`, `use_tls`) is preserved for observability.
3. **Representation Sanitization**:
   - `repr(BrokerCredentialsConfig)` and `str(BrokerCredentialsConfig)` never output raw secrets in terminal logs, diagnostics, or stack traces.
   - `Tier1AuditLogger` records contain only sanitized dictionaries from `redacted_dict()`.

---

## 4. WAL Replay & State Hydration Invariants (M-02 & M-04)

1. **Last-Write-Wins (LWW) for Entity Snapshots**:
   - Sequential log traversal replaces entity references in-memory so the latest durable state prior to crash/restart is rehydrated.
2. **Deduplication of Financial Fills**:
   - Fill records represent atomic trades. Fills are deduplicated by `fill_id` during replay to ensure trade quantities and fees cannot be double-counted.
3. **Fail-Closed Intent State Reconstitution (M-04)**:
   - `IntentState.from_str()` rejects empty or unrecognized state strings with `ValueError`.
   - If an intent record in the WAL contains a corrupted state, it is never defaulted to `PENDING_APPROVAL`. It is immediately evicted from active memory and quarantined into `replayed["quarantines"]`.

---

## 5. Frozen Core Integrity Verification

All gate implementations reside cleanly in `gateway/` and `docs/`. The following core trading engine directories remain byte-for-byte unmodified and verified against the 86-file SHA-256 baseline:
- `services/`
- `strategies/`
- `brokers/`
- `config/`

---

## 6. Verification Status

| Gate Verification Suite | Tests Executed | Status |
| :--- | :--- | :--- |
| H-01 Remediation (Bypass Protection) | `tests/unit/test_h01_remediation.py` | **PASSED (5/5)** |
| H-02 Remediation (Health Tuple Unpacking) | `tests/unit/test_h02_remediation.py` | **PASSED (5/5)** |
| H-03 Remediation (UI Routes & RBAC) | `tests/unit/test_h03_remediation.py` | **PASSED (8/8)** |
| M-01 Remediation (Transition Graph) | `tests/unit/test_m01_remediation.py` | **PASSED (3/3)** |
| M-02 Remediation (WAL Replay & Deduplication) | `tests/unit/test_m02_remediation.py` | **PASSED (4/4)** |
| M-03 Remediation (Credential Redaction) | `tests/unit/test_m03_remediation.py` | **PASSED (4/4)** |
| M-04 Remediation (IntentState Fail-Closed) | `tests/unit/test_m04_remediation.py` | **PASSED (4/4)** |
| Step 9 Verification (Live Boundary Safeguard) | `tests/unit/test_step9_live_boundary.py` | **PASSED (1/1)** |
| Step 11 Verification (Idempotency Stability) | `tests/unit/test_step11_idempotency.py` | **PASSED (5/5)** |
| Full Project Regression Suite | All unit and integration suites | **PASSED (578/579)** (1 skipped) |
| Frozen Core Integrity Verification | 86 baseline files SHA-256 | **VERIFIED (86/86)** |
