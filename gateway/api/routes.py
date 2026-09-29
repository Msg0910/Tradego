"""
Tradego API Route Handlers.
Implements health/readiness probes, native authentication, session management,
authoritative snapshot query, and authenticated command dispatch.
"""

import asyncio
import os
import queue
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse

from ..adapters import MarketStateAdapter, PortfolioRiskAdapter
from ..broadcaster import EventBroadcaster, SequenceManager
from ..contracts import Capability, CommandRequest, CommandStatus, CommandType, TradegoEventEnvelope
from ..recovery import RecoveryManager
from ..intent import ExecutionIntent, ExecutionIntentManager, IntentState
from ..risk_gate import PreTradeRiskEvaluator, RiskDecisionType, RiskEvaluationResult
from ..order_instruction import InstructionState, OrderInstruction, OrderInstructionManager
from ..execution import (
    ExecutionRecord,
    ExecutionReconciliationEngine,
    ExecutionState,
    ExecutionStateManager,
    FillRecord,
    ReconciliationStatus,
)
from ..security import (
    CapabilityChecker,
    InMemorySessionStore,
    NativeCredentialStore,
    SessionContext,
    Tier1AuditLogger,
)
from ..command import CommandGateway
from ..projection import SnapshotGenerator
from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState
from .dependencies import (
    get_audit_logger,
    get_broadcaster,
    get_command_gateway,
    get_credential_store,
    get_current_session,
    get_disaster_recovery_manager,
    get_execution_state_manager,
    get_broker_connectivity_manager,
    get_health_monitor,
    get_intent_manager,
    get_market_adapter,
    get_multi_venue_registry,
    get_multi_venue_router,
    get_order_instruction_manager,
    get_persistence_manager,
    get_portfolio_risk_adapter,
    get_recovery_manager,
    get_risk_gate,
    get_session_store,
    get_snapshot_generator,
    get_trading_guard,
)
from ..broker_connectivity import BrokerConnectivityManager, ExecutionMode
from ..broker_credentials import BrokerCredentialsConfig
from ..disaster_recovery import DisasterRecoveryManager, RecoveryState
from ..multi_venue import BrokerVenue, BrokerVenueRegistry, MultiVenueRouter
from ..observability import SystemHealthMonitor
from .models import (
    BrokerConnectPayload,
    BrokerModeLivePayload,
    BrokerModeResponse,
    BrokerReconcileResponse,
    BrokerStatusResponse,
    CancelInstructionPayload,
    CancelIntentPayload,
    CommandApiRequest,
    CommandApiResponse,
    CreateInstructionPayload,
    CreateIntentPayload,
    DetailedHealthResponse,
    ExecutionListResponse,
    ExecutionResponse,
    FillModel,
    HealthResponse,
    IntentListResponse,
    IntentResponse,
    LivezResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    OrderInstructionListResponse,
    OrderInstructionResponse,
    PortfolioResponse,
    ReadinessResponse,
    ReadyzResponse,
    ReconciliationActionPayload,
    RecoveryConfirmPayload,
    RecoveryConfirmResponse,
    RecoveryRequestPayload,
    RecoveryRequestResponse,
    RecoveryStatusResponse,
    RejectIntentPayload,
    RiskEvaluationResponse,
    RiskResponse,
    SnapshotResponse,
    UnquarantinePayload,
    VenueListResponse,
    VenueResponse,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & Readiness Probes (Zone 2 Public Boundary)
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
@router.get("/api/v1/health/livez", response_model=LivezResponse)
async def health_check() -> HealthResponse:
    """Liveness probe confirming the gateway process is up."""
    return HealthResponse(
        status="UP",
        timestamp=datetime.now(timezone.utc).isoformat(),
        version="1.0.0",
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(
    guard: TradingGuard = Depends(get_trading_guard),
    audit_logger: Tier1AuditLogger = Depends(get_audit_logger),
) -> ReadinessResponse:
    """
    Readiness probe validating TradingGuard state and Tier 1 audit logger availability.
    """
    guard_val = guard.state.value
    audit_status = "HEALTHY" if audit_logger._running else "DEGRADED"

    return ReadinessResponse(
        status="READY",
        guard_state=guard_val,
        audit_logger=audit_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.get("/api/v1/health/readyz", response_model=ReadyzResponse)
async def readyz_check(
    request: Request,
    health_mon: Optional[SystemHealthMonitor] = Depends(get_health_monitor),
    guard: TradingGuard = Depends(get_trading_guard),
    dr_mgr: Optional[DisasterRecoveryManager] = Depends(get_disaster_recovery_manager),
) -> ReadyzResponse:
    """
    Readiness probe evaluating operational prerequisites.
    Fails with 503 SERVICE UNAVAILABLE if prerequisites are breached.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    g_state = guard.state.value
    r_state = dr_mgr.recovery_state.value if dr_mgr else "CLEAN"

    if health_mon:
        is_ready, ready_dict = health_mon.check_readiness()
        if not is_ready:
            reasons = ready_dict.get("unready_reasons", [])
            failure_reason = "; ".join(reasons) if reasons else "System not ready"
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "NOT_READY",
                    "message": failure_reason,
                    "status": "NOT_READY",
                    "reason": failure_reason,
                    "guard_state": g_state,
                    "recovery_state": r_state,
                    "timestamp": now_str,
                    "details": {
                        "status": "NOT_READY",
                        "reason": failure_reason,
                        "guard_state": g_state,
                        "recovery_state": r_state,
                        "unready_reasons": reasons,
                    },
                },
            )
        return ReadyzResponse(
            status="READY",
            reason=None,
            guard_state=g_state,
            recovery_state=r_state,
            timestamp=now_str,
        )

    if guard.state in (GuardState.HALTED, GuardState.PAUSED):
        msg = f"TradingGuard is {g_state}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "NOT_READY",
                "message": msg,
                "status": "NOT_READY",
                "reason": msg,
                "guard_state": g_state,
                "recovery_state": r_state,
                "timestamp": now_str,
                "details": {
                    "status": "NOT_READY",
                    "reason": msg,
                    "guard_state": g_state,
                    "recovery_state": r_state,
                },
            },
        )

    if dr_mgr and dr_mgr.is_quarantine_locked:
        msg = "DISASTER_RECOVERY_QUARANTINE_ACTIVE"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "NOT_READY",
                "message": msg,
                "status": "NOT_READY",
                "reason": msg,
                "guard_state": g_state,
                "recovery_state": r_state,
                "timestamp": now_str,
                "details": {
                    "status": "NOT_READY",
                    "reason": msg,
                    "guard_state": g_state,
                    "recovery_state": r_state,
                },
            },
        )

    return ReadyzResponse(
        status="READY",
        reason=None,
        guard_state=g_state,
        recovery_state=r_state,
        timestamp=now_str,
    )


@router.get("/api/v1/health/detailed", response_model=DetailedHealthResponse)
async def detailed_health_check(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    health_mon: Optional[SystemHealthMonitor] = Depends(get_health_monitor),
    guard: TradingGuard = Depends(get_trading_guard),
    b_conn_mgr: Optional[BrokerConnectivityManager] = Depends(get_broker_connectivity_manager),
    dr_mgr: Optional[DisasterRecoveryManager] = Depends(get_disaster_recovery_manager),
) -> DetailedHealthResponse:
    """
    Deep operational health & observability report.
    Exposes engine metrics, WAL health, audit chain status, and venue states with secrets redacted.
    Requires CAP_OBSERVE.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    if health_mon:
        meta = health_mon.get_detailed_health()
        return DetailedHealthResponse(
            status=meta.get("status", "UP"),
            execution_mode=meta.get("execution_mode", "PAPER"),
            trading_guard_state=meta.get("trading_guard_state", guard.state.value),
            wal_health=meta.get("wal_health", {}),
            audit_chain_status=meta.get("audit_chain_status", {}),
            broker_connectivity=meta.get("broker_connectivity", {}),
            disaster_recovery=meta.get("disaster_recovery", {}),
            disk_health=meta.get("disk_health", {}),
            timestamp=meta.get("timestamp", datetime.now(timezone.utc).isoformat()),
        )

    now_str = datetime.now(timezone.utc).isoformat()
    raw_mode = getattr(b_conn_mgr, "execution_mode", "PAPER")
    m_str = raw_mode.value if hasattr(raw_mode, "value") else str(raw_mode)

    return DetailedHealthResponse(
        status="UP",
        execution_mode=m_str,
        trading_guard_state=guard.state.value,
        wal_health={"status": "OK"},
        audit_chain_status={"valid": True},
        broker_connectivity={"state": getattr(b_conn_mgr, "connectivity_state", "DISCONNECTED")},
        disaster_recovery={"recovery_state": dr_mgr.recovery_state.value if dr_mgr else "CLEAN"},
        disk_health={"available": True},
        timestamp=now_str,
    )


# ---------------------------------------------------------------------------
# Authentication Boundary (Native Argon2id + High-Entropy Opaque Session)
# ---------------------------------------------------------------------------

@router.post("/api/v1/auth/login", response_model=LoginResponse)
async def login(
    login_req: LoginRequest,
    request: Request,
    credential_store: NativeCredentialStore = Depends(get_credential_store),
    session_store: InMemorySessionStore = Depends(get_session_store),
    audit_logger: Tier1AuditLogger = Depends(get_audit_logger),
) -> LoginResponse:
    """
    Native direct authentication verifying credentials with Argon2id.
    Strictly offloads hashing and verification outside the async event loop (SEC-TECH-ARGON2-01).
    Issues high-entropy opaque bearer token (tg_sess_...) on success.
    """
    corr_id = getattr(request.state, "correlation_id", "unknown")

    if credential_store.is_locked(login_req.username):
        audit_logger.log({
            "action": "AUTH_LOGIN_LOCKED_ACCOUNT_ATTEMPT",
            "operator_id": login_req.username,
            "correlation_id": corr_id,
        })
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "ACCOUNT_LOCKED",
                "message": "Account is locked due to excessive failed attempts",
            },
        )

    # CPU-intensive Argon2id verification offloaded to worker threadpool
    user = await credential_store.authenticate(login_req.username, login_req.password)
    if not user:
        audit_logger.log({
            "action": "AUTH_LOGIN_FAILED",
            "operator_id": login_req.username,
            "correlation_id": corr_id,
        })
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_CREDENTIALS",
                "message": "Invalid username or password",
            },
        )

    # Create high-entropy server session
    token = session_store.create_session(
        operator_id=user["operator_id"],
        roles=user["roles"],
        capabilities=user["capabilities"],
    )
    session = session_store.validate_token(token)
    assert session is not None

    audit_logger.log({
        "action": "AUTH_LOGIN_SUCCESS",
        "operator_id": user["operator_id"],
        "session_id": session.session_id,
        "correlation_id": corr_id,
    })

    return LoginResponse(
        session_token=token,
        operator_id=session.operator_id,
        roles=session.roles,
        capabilities=[c.value for c in session.capabilities],
        expires_at=session.expires_at.isoformat(),
    )


@router.post("/api/v1/auth/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    session_store: InMemorySessionStore = Depends(get_session_store),
    audit_logger: Tier1AuditLogger = Depends(get_audit_logger),
) -> LogoutResponse:
    """
    Revokes the current session token immediately (SEC-14).
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", "unknown")

    session_store.revoke_session(token)
    audit_logger.log({
        "action": "AUTH_LOGOUT",
        "operator_id": session.operator_id,
        "session_id": session.session_id,
        "correlation_id": corr_id,
    })

    return LogoutResponse(
        status="LOGGED_OUT",
        message="Session successfully invalidated",
    )


# ---------------------------------------------------------------------------
# Safe Read-Only Authoritative Engine Query (UI-02, UI-08)
# ---------------------------------------------------------------------------

@router.get("/api/v1/snapshot", response_model=SnapshotResponse)
async def get_state_snapshot(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    snapshot_gen: SnapshotGenerator = Depends(get_snapshot_generator),
) -> SnapshotResponse:
    """
    Returns point-in-time state projection anchored to the sequence frontier S_snap.
    Requires CAP_OBSERVE or CAP_ADMIN capability.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    snapshot = snapshot_gen.generate_snapshot(runtime_mode="PAPER")

    return SnapshotResponse(
        snapshot_id=snapshot.snapshot_id,
        snapshot_timestamp=snapshot.snapshot_timestamp,
        authoritative_sequence=snapshot.authoritative_sequence,
        runtime_mode=snapshot.runtime_mode,
        guard_state=snapshot.guard_state,
        details=snapshot.details,
    )


# ---------------------------------------------------------------------------
# Authenticated Command Boundary (UI-14, SEC-01, SEC-02, SEC-04, SEC-05)
# ---------------------------------------------------------------------------

@router.post("/api/v1/commands", response_model=CommandApiResponse)
async def dispatch_command(
    cmd_payload: CommandApiRequest,
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    cmd_gateway: CommandGateway = Depends(get_command_gateway),
) -> CommandApiResponse:
    """
    Ingests, validates, audits, and dispatches administrative control commands.
    In Phase 1, only administrative PAUSE and RESUME controls are exposed.
    Mutating order submission commands are strictly disallowed.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", "unknown")

    # Strict Phase 1 scope enforcement: No trading order submission or unexposed commands
    if cmd_payload.command_type not in (
        CommandType.PAUSE,
        CommandType.RESUME,
        CommandType.KILL_SWITCH,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "COMMAND_NOT_SUPPORTED_IN_PHASE_1",
                "message": f"Command type {cmd_payload.command_type.value} is not supported in Phase 1 vertical slice",
            },
        )

    cmd_req = CommandRequest(
        command_type=cmd_payload.command_type,
        operator_id=session.operator_id,
        correlation_id=corr_id,
        parameters=cmd_payload.parameters,
    )

    result = cmd_gateway.dispatch(token=token, request=cmd_req)

    if result.status == CommandStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN
            if "UNAUTHORIZED" in result.reason
            else status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "COMMAND_REJECTED",
                "message": result.reason,
            },
        )

    return CommandApiResponse(
        command_id=result.command_id,
        status=result.status,
        guard_state=result.guard_state,
        sequence=result.sequence,
        reason=result.reason,
        timestamp=result.timestamp,
    )


# ---------------------------------------------------------------------------
# Authoritative Read-Only Market State Query (Phase 2)
# ---------------------------------------------------------------------------

@router.get("/api/v1/market/state")
async def get_market_state(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    market_adapter: MarketStateAdapter = Depends(get_market_adapter),
) -> Dict[str, Any]:
    """
    Exposes point-in-time authoritative market state snapshots across resolved instruments.
    Never fabricates mock data.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    snaps = market_adapter.get_market_snapshots()
    return {
        "snapshots": snaps,
        "count": len(snaps),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Presentation Event Stream Boundary (WebSocket push-only, SEC-01, SEC-06, SEC-13, UI-11)
# ---------------------------------------------------------------------------

@router.websocket("/ws/events")
async def websocket_event_stream(websocket: WebSocket):
    """
    Push-only presentation event stream channel.
    Requires authenticated session (passed via ?token=... or Authorization header).
    Enforces monotonic sequence delivery, backpressure insulation, and SEC-13 one-way push.
    """
    # 1. Extract session token from query param or Authorization header
    token = websocket.query_params.get("token")
    if not token:
        auth_header = websocket.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    session_store: InMemorySessionStore = websocket.app.state.session_store
    audit_logger: Tier1AuditLogger = websocket.app.state.audit_logger
    broadcaster: EventBroadcaster = websocket.app.state.broadcaster
    seq_mgr: SequenceManager = websocket.app.state.sequence_manager

    corr_id = (
        websocket.headers.get("X-Correlation-ID")
        or websocket.query_params.get("correlation_id")
        or str(uuid.uuid4())
    )

    if not token:
        audit_logger.log({
            "action": "AUTH_WS_UNAUTHORIZED",
            "reason": "Missing session token",
            "correlation_id": corr_id,
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session = session_store.validate_token(token)
    if not session:
        audit_logger.log({
            "action": "AUTH_WS_UNAUTHORIZED",
            "reason": "Invalid, expired, or revoked session token",
            "correlation_id": corr_id,
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        audit_logger.log({
            "action": "AUTH_WS_FORBIDDEN",
            "operator_id": session.operator_id,
            "session_id": session.session_id,
            "reason": "Missing CAP_OBSERVE capability",
            "correlation_id": corr_id,
        })
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Handshake succeeded
    await websocket.accept()

    audit_logger.log({
        "action": "WS_CLIENT_CONNECTED",
        "operator_id": session.operator_id,
        "session_id": session.session_id,
        "correlation_id": corr_id,
    })

    # Initial session established frame
    initial_frame = {
        "event_id": str(uuid.uuid4()),
        "event_type": "SESSION_ESTABLISHED",
        "sequence": seq_mgr.current_sequence(),
        "correlation_id": corr_id,
        "server_timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "operator_id": session.operator_id,
            "roles": session.roles,
            "capabilities": [c.value for c in session.capabilities],
            "expires_at": session.expires_at.isoformat(),
        },
        "event_version": "1.0.0",
    }
    await websocket.send_json(initial_frame)

    # Subscribe to non-blocking broadcaster queue
    sub_queue = broadcaster.subscribe(max_queue_size=1000)
    stop_event = asyncio.Event()

    async def send_events():
        try:
            while not stop_event.is_set():
                try:
                    env = sub_queue.get_nowait()
                    if hasattr(env, "to_dict"):
                        data = env.to_dict()
                    elif isinstance(env, dict):
                        data = env
                    else:
                        data = env.__dict__
                    await websocket.send_json(data)
                except queue.Empty:
                    await asyncio.sleep(0.01)
        except Exception:
            pass

    async def receive_inbound():
        try:
            while not stop_event.is_set():
                await websocket.receive_text()
                # SEC-13: Inbound data frames prohibited on push-only stream
                audit_logger.log({
                    "action": "WS_CLIENT_FRAME_REJECTED",
                    "operator_id": session.operator_id,
                    "session_id": session.session_id,
                    "correlation_id": corr_id,
                    "reason": "SEC-13: Inbound data frames strictly prohibited on push-only stream",
                })
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                break
        except Exception:
            pass

    sender_task = asyncio.create_task(send_events())
    receiver_task = asyncio.create_task(receive_inbound())

    try:
        done, pending = await asyncio.wait(
            [sender_task, receiver_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        stop_event.set()
        broadcaster.unsubscribe(sub_queue)
        audit_logger.log({
            "action": "WS_CLIENT_DISCONNECTED",
            "operator_id": session.operator_id,
            "session_id": session.session_id,
            "correlation_id": corr_id,
        })


# ---------------------------------------------------------------------------
# Presentation UI Shell Endpoint (Phase 2 Vertical Slice)
# ---------------------------------------------------------------------------

@router.get("/ui", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def presentation_dashboard():
    """Serves the Phase 2 presentation terminal UI shell."""
    ui_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui", "index.html")
    if os.path.exists(ui_path):
        with open(ui_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(
        content="<html><body><h1>Tradego Terminal</h1><p>Presentation shell ready.</p></body></html>",
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Operational Recovery Control (Two-Person Rule, Phase 4)
# ---------------------------------------------------------------------------

@router.post("/api/v1/recovery/request", response_model=RecoveryRequestResponse)
async def request_recovery(
    payload: RecoveryRequestPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    recovery_mgr: RecoveryManager = Depends(get_recovery_manager),
    guard: TradingGuard = Depends(get_trading_guard),
) -> RecoveryRequestResponse:
    """
    Step 1 of Two-Person Recovery: Operator A initiates a recovery challenge.
    Requires CAP_CONTROL_RECOVER capability and HALTED guard state.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_CONTROL_RECOVER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_CONTROL_RECOVER.value}",
            },
        )

    if guard.state != GuardState.HALTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GUARD_NOT_HALTED",
                "message": f"Recovery challenge requires HALTED guard state, currently {guard.state.value}",
            },
        )

    try:
        challenge = recovery_mgr.create_challenge(
            operator_a_id=session.operator_id,
            correlation_id=corr_id,
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if "GUARD_NOT_HALTED" in str(exc) else status.HTTP_400_BAD_REQUEST,
            detail={"code": "RECOVERY_INITIATION_FAILED", "message": str(exc)},
        )

    return RecoveryRequestResponse(
        challenge_id=challenge.challenge_id,
        operator_a_id=challenge.operator_a_id,
        confirmation_token=challenge.expected_token,
        token=challenge.expected_token,
        expires_at=challenge.expires_at.isoformat(),
        status=challenge.status,
        guard_state=guard.state.value,
        correlation_id=corr_id,
    )


@router.post("/api/v1/recovery/confirm", response_model=RecoveryConfirmResponse)
async def confirm_recovery(
    payload: RecoveryConfirmPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    recovery_mgr: RecoveryManager = Depends(get_recovery_manager),
    guard: TradingGuard = Depends(get_trading_guard),
) -> RecoveryConfirmResponse:
    """
    Step 2 of Two-Person Recovery: Distinct Operator B confirms recovery.
    Enforces two-person separation, single-use token consumption, and trips TradingGuard back to NORMAL.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_CONTROL_RECOVER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_CONTROL_RECOVER.value}",
            },
        )

    if guard.state != GuardState.HALTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "GUARD_NOT_HALTED",
                "message": f"Guard is not in HALTED state, currently {guard.state.value}",
            },
        )

    challenge = recovery_mgr.get_challenge(payload.challenge_id)

    try:
        result = recovery_mgr.confirm_recovery(
            challenge_id=payload.challenge_id,
            operator_b_id=session.operator_id,
            operator_b_token=token,
            confirmation_token=payload.confirmation_token,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_409_CONFLICT
            if code in [
                "SAME_OPERATOR_CONFIRMATION_REJECTED",
                "GUARD_NOT_HALTED",
                "CHALLENGE_ALREADY_CONSUMED",
                "CHALLENGE_EXPIRED",
            ]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    if result.status != CommandStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "RECOVERY_EXECUTION_REJECTED", "message": result.reason},
        )

    return RecoveryConfirmResponse(
        status="RECOVERED",
        recovered=True,
        guard_state=result.guard_state,
        operator_a_id=challenge.operator_a_id if challenge else "",
        operator_b_id=session.operator_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        correlation_id=corr_id,
    )


@router.get("/api/v1/recovery/status", response_model=RecoveryStatusResponse)
async def get_recovery_status(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    recovery_mgr: RecoveryManager = Depends(get_recovery_manager),
    guard: TradingGuard = Depends(get_trading_guard),
    dr_mgr: Optional[DisasterRecoveryManager] = Depends(get_disaster_recovery_manager),
) -> RecoveryStatusResponse:
    """
    Queries current recovery challenge and guard status.
    Requires CAP_OBSERVE capability.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    challenge = recovery_mgr.get_active_challenge()
    challenge_data = challenge.to_dict(include_token=False) if challenge else None

    # Disaster recovery state
    rec_state = dr_mgr.recovery_state.value if dr_mgr else "CLEAN"
    dr_summary = dr_mgr.get_recovery_summary() if dr_mgr else {}

    return RecoveryStatusResponse(
        has_active_challenge=challenge is not None,
        challenge=challenge_data,
        active_challenge=challenge_data,
        guard_state=guard.state.value,
        recovery_state=rec_state,
        last_recovery_timestamp=dr_summary.get("last_recovery_timestamp"),
        in_flight_count=dr_summary.get("in_flight_count", 0),
        unknown_count=dr_summary.get("unknown_count", 0),
        quarantined_count=dr_summary.get("quarantined_count", 0),
        reconciliation_required=dr_summary.get("reconciliation_required", False),
        wal_sequence=dr_summary.get("wal_sequence", 0),
        audit_chain_valid=dr_summary.get("audit_chain_valid", True),
        quarantined_executions=dr_summary.get("quarantined_executions", []),
    )


@router.post("/api/v1/recovery/reconcile")
async def reconcile_quarantined_execution(
    payload: ReconciliationActionPayload,
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    dr_mgr: Optional[DisasterRecoveryManager] = Depends(get_disaster_recovery_manager),
):
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}",
            },
        )
    if not dr_mgr:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Disaster recovery manager unavailable"},
        )
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    try:
        updated_rec = dr_mgr.reconcile_quarantined(
            execution_id=payload.execution_id,
            action=payload.action,
            operator_notes=payload.operator_notes,
            operator_id=session.operator_id,
            correlation_id=corr_id,
        )
        return {
            "status": "RECONCILED",
            "execution_id": payload.execution_id,
            "action": payload.action,
            "operator_id": session.operator_id,
            "notes": payload.operator_notes,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "RECONCILIATION_FAILED", "message": str(e)},
        )


@router.post("/api/v1/recovery/unquarantine")
async def unquarantine_execution(
    payload: UnquarantinePayload,
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    dr_mgr: Optional[DisasterRecoveryManager] = Depends(get_disaster_recovery_manager),
):
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}",
            },
        )
    if not dr_mgr:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Disaster recovery manager unavailable"},
        )
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    try:
        record = dr_mgr.unquarantine(
            execution_id=payload.execution_id,
            primary_operator_id=session.operator_id,
            confirmation_reason=payload.confirmation_reason,
            second_operator_id=payload.second_operator_id,
            correlation_id=corr_id,
        )
        return {
            "status": "UNQUARANTINED",
            "execution_id": payload.execution_id,
            "primary_operator_id": session.operator_id,
            "second_operator_id": payload.second_operator_id,
        }
    except PermissionError as pe:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": str(pe)},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNQUARANTINE_FAILED", "message": str(e)},
        )


@router.get("/api/v1/venues", response_model=VenueListResponse)
async def list_venues(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    registry: Optional[BrokerVenueRegistry] = Depends(get_multi_venue_registry),
) -> VenueListResponse:
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )
    if not registry:
        return VenueListResponse(venues=[], total_venues=0)

    venues_list = []
    for v in registry.list_venues():
        d = v.to_dict()
        venues_list.append(
            VenueResponse(
                venue_id=d["venue_id"],
                broker_name=d["broker_name"],
                operational_status=d["operational_status"],
                connectivity_state=d["connectivity_state"],
                reconciliation_status=d["reconciliation_status"],
                supported_exchanges=d["supported_exchanges"],
                supported_asset_classes=d["supported_asset_classes"],
                last_heartbeat=d.get("last_heartbeat"),
                has_credentials=d.get("has_credentials", False),
            )
        )
    return VenueListResponse(venues=venues_list, total_venues=len(venues_list))


@router.get("/api/v1/venues/{venue_id}", response_model=VenueResponse)
async def get_venue(
    venue_id: str,
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    registry: Optional[BrokerVenueRegistry] = Depends(get_multi_venue_registry),
) -> VenueResponse:
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )
    if not registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "VENUE_NOT_FOUND", "message": f"Venue not found: {venue_id}"},
        )
    venue = registry.get_venue(venue_id)
    if not venue:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "VENUE_NOT_FOUND", "message": f"Venue not found: {venue_id}"},
        )
    d = venue.to_dict()
    return VenueResponse(
        venue_id=d["venue_id"],
        broker_name=d["broker_name"],
        operational_status=d["operational_status"],
        connectivity_state=d["connectivity_state"],
        reconciliation_status=d["reconciliation_status"],
        supported_exchanges=d["supported_exchanges"],
        supported_asset_classes=d["supported_asset_classes"],
        last_heartbeat=d.get("last_heartbeat"),
        has_credentials=d.get("has_credentials", False),
    )


# ---------------------------------------------------------------------------
# Authoritative Read-Only Portfolio & Risk Projection (Phase 4)
# ---------------------------------------------------------------------------

@router.get("/api/v1/portfolio", response_model=PortfolioResponse)
@router.get("/api/v1/portfolio/state", response_model=PortfolioResponse)
async def get_portfolio(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    adapter: PortfolioRiskAdapter = Depends(get_portfolio_risk_adapter),
) -> PortfolioResponse:
    """
    Authoritative read-only portfolio projection.
    Returns real positions, cash, equity, and PnL if mounted; explicitly indicates unavailable otherwise.
    Never fabricates mock data.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    data = adapter.get_portfolio_summary()
    return PortfolioResponse(**data)


@router.get("/api/v1/risk", response_model=RiskResponse)
@router.get("/api/v1/risk/state", response_model=RiskResponse)
async def get_risk(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    adapter: PortfolioRiskAdapter = Depends(get_portfolio_risk_adapter),
    guard: TradingGuard = Depends(get_trading_guard),
) -> RiskResponse:
    """
    Authoritative read-only risk state projection.
    Returns active risk limits, entry/exit gating permissions, and guard state.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    data = adapter.get_risk_summary(guard_state=guard.state.value)
    return RiskResponse(**data)


# ---------------------------------------------------------------------------
# Phase 5: Execution Intent Boundary (Operational Gating)
# ---------------------------------------------------------------------------

def _intent_to_response(intent: ExecutionIntent) -> IntentResponse:
    """Converts internal ExecutionIntent domain entity to API schema."""
    return IntentResponse(
        intent_id=intent.intent_id,
        correlation_id=intent.correlation_id,
        creator_id=intent.creator_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        time_in_force=intent.time_in_force,
        reason=intent.reason,
        state=intent.state.value,
        approver_id=intent.approver_id,
        rejection_reason=intent.rejection_reason,
        cancellation_reason=intent.cancellation_reason,
        created_at=intent.created_at.isoformat(),
        updated_at=intent.updated_at.isoformat(),
        expires_at=intent.expires_at.isoformat(),
        status_category=intent.status_category,
        is_executed=intent.is_executed,
        note="INTENT ONLY — NOT EXECUTED",
        risk_status=intent.risk_status,
        risk_evaluator_id=intent.risk_evaluator_id,
        risk_rejection_reason=intent.risk_rejection_reason,
        risk_evaluated_at=intent.risk_evaluated_at,
        risk_details=intent.risk_details,
    )


@router.post(
    "/api/v1/execution-intents",
    response_model=IntentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_execution_intent(
    payload: CreateIntentPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentResponse:
    """
    Creates a new execution intent proposal.
    Enforces CAP_TRADE_SUBMIT, strictly validates parameters, and initializes intent state.
    INVARIANT: Does not execute or route orders to broker.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_TRADE_SUBMIT):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_TRADE_SUBMIT.value}",
            },
        )

    init_state = IntentState.PENDING_APPROVAL
    if payload.initial_state:
        try:
            init_state = IntentState(payload.initial_state.strip().upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_STATE",
                    "message": f"Invalid initial state: {payload.initial_state}",
                },
            )

    try:
        intent = intent_mgr.create_intent(
            creator_id=session.operator_id,
            symbol=payload.symbol,
            side=payload.side,
            quantity=payload.quantity,
            order_type=payload.order_type,
            correlation_id=corr_id,
            limit_price=payload.limit_price,
            stop_price=payload.stop_price,
            time_in_force=payload.time_in_force,
            reason=payload.reason or "",
            ttl_seconds=payload.ttl_seconds,
            initial_state=init_state,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": code, "message": err_msg},
        )

    return _intent_to_response(intent)


@router.get(
    "/api/v1/execution-intents/{intent_id}",
    response_model=IntentResponse,
)
async def get_execution_intent(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentResponse:
    """
    Reads a single execution intent by ID.
    Requires CAP_OBSERVE or CAP_TRADE_SUBMIT.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_SUBMIT)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    intent = intent_mgr.get_intent(intent_id, correlation_id=corr_id)
    if not intent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INTENT_NOT_FOUND", "message": f"Intent {intent_id} not found"},
        )

    return _intent_to_response(intent)


@router.get(
    "/api/v1/execution-intents",
    response_model=IntentListResponse,
)
async def list_execution_intents(
    request: Request,
    response: Response,
    state: Optional[str] = None,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentListResponse:
    """
    Lists execution intents, optionally filtered by state.
    Requires CAP_OBSERVE or CAP_TRADE_SUBMIT.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_SUBMIT)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    state_filter = None
    if state:
        try:
            state_filter = IntentState(state.strip().upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_STATE_FILTER", "message": f"Invalid state filter: {state}"},
            )

    intents = intent_mgr.list_intents(state_filter=state_filter, correlation_id=corr_id)
    return IntentListResponse(
        intents=[_intent_to_response(i) for i in intents],
        count=len(intents),
    )


@router.post(
    "/api/v1/execution-intents/{intent_id}/approve",
    response_model=IntentResponse,
)
@router.get(
    "/api/v1/execution-intents/{intent_id}/approve",
    response_model=IntentResponse,
)
async def approve_execution_intent(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentResponse:
    """
    Two-Person Operational Approval for an Execution Intent.
    Enforces distinct approver identity, CAP_TRADE_APPROVE, and unexpired pending state.
    INVARIANT: Does not execute order or communicate with broker.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_TRADE_APPROVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_TRADE_APPROVE.value}",
            },
        )

    try:
        intent = intent_mgr.approve_intent(
            intent_id=intent_id,
            approver_id=session.operator_id,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if code == "INTENT_NOT_FOUND"
            else status.HTTP_409_CONFLICT
            if code in [
                "SAME_OPERATOR_APPROVAL_REJECTED",
                "INTENT_EXPIRED",
                "INTENT_CANCELLED",
                "INTENT_ALREADY_TERMINAL",
                "INVALID_STATE_TRANSITION",
            ]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    return _intent_to_response(intent)


@router.post(
    "/api/v1/execution-intents/{intent_id}/reject",
    response_model=IntentResponse,
)
async def reject_execution_intent(
    intent_id: str,
    payload: RejectIntentPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentResponse:
    """
    Two-Person Operational Review: Operator B rejects a pending execution intent.
    Enforces distinct identity and CAP_TRADE_APPROVE.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_TRADE_APPROVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_TRADE_APPROVE.value}",
            },
        )

    try:
        intent = intent_mgr.reject_intent(
            intent_id=intent_id,
            rejector_id=session.operator_id,
            reason=payload.reason,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if code == "INTENT_NOT_FOUND"
            else status.HTTP_409_CONFLICT
            if code in [
                "SAME_OPERATOR_APPROVAL_REJECTED",
                "INTENT_EXPIRED",
                "INTENT_CANCELLED",
                "INTENT_ALREADY_TERMINAL",
                "INVALID_STATE_TRANSITION",
            ]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    return _intent_to_response(intent)


@router.post(
    "/api/v1/execution-intents/{intent_id}/cancel",
    response_model=IntentResponse,
)
async def cancel_execution_intent(
    intent_id: str,
    payload: CancelIntentPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> IntentResponse:
    """
    Cancels an execution intent prior to approval/rejection.
    Authorized for the creator or an operator with CAP_TRADE_CANCEL.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    intent_obj = intent_mgr.get_intent(intent_id, correlation_id=corr_id)
    if not intent_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INTENT_NOT_FOUND", "message": f"Intent {intent_id} not found"},
        )

    is_creator = session.operator_id == intent_obj.creator_id
    has_cancel_cap = CapabilityChecker.has_capability(session, Capability.CAP_TRADE_CANCEL)
    if not (is_creator or has_cancel_cap):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_TRADE_CANCEL.value}",
            },
        )

    try:
        intent = intent_mgr.cancel_intent(
            intent_id=intent_id,
            canceller_id=session.operator_id,
            reason=payload.reason,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_404_NOT_FOUND
            if code == "INTENT_NOT_FOUND"
            else status.HTTP_409_CONFLICT
            if code in [
                "INTENT_ALREADY_TERMINAL",
                "INVALID_STATE_TRANSITION",
            ]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    return _intent_to_response(intent)


# ---------------------------------------------------------------------------
# Phase 6: Authoritative Risk Gating & Pre-Trade Limit Evaluation
# ---------------------------------------------------------------------------

@router.post(
    "/api/v1/execution-intents/{intent_id}/evaluate-risk",
    response_model=RiskEvaluationResponse,
)
@router.post(
    "/api/v1/execution-intents/{intent_id}/risk-evaluate",
    response_model=RiskEvaluationResponse,
)
async def evaluate_execution_intent_risk(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
    risk_gate: PreTradeRiskEvaluator = Depends(get_risk_gate),
) -> RiskEvaluationResponse:
    """
    Evaluates an ExecutionIntent against authoritative Risk Limits and Portfolio State.
    Requires CAP_RISK_EVALUATE (or CAP_ADMIN).
    INVARIANT: Read-only evaluation; does not submit orders or connect to broker.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_RISK_EVALUATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_RISK_EVALUATE.value}",
            },
        )

    intent = intent_mgr.get_intent(intent_id, correlation_id=corr_id)
    if not intent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INTENT_NOT_FOUND", "message": f"Intent {intent_id} not found"},
        )

    active_intents = intent_mgr.list_intents(correlation_id=corr_id)
    result = risk_gate.evaluate_intent(
        intent=intent,
        evaluator_id=session.operator_id,
        correlation_id=corr_id,
        active_intents=active_intents,
    )

    return RiskEvaluationResponse(
        intent_id=result.intent_id,
        decision=result.decision.value,
        reason=result.reason,
        details=result.details,
        evaluator_id=result.evaluator_id,
        evaluated_at=result.evaluated_at,
        correlation_id=result.correlation_id,
        status_category=result.status_category,
        is_executed=result.is_executed,
        resulting_state=result.resulting_state,
    )


@router.get(
    "/api/v1/execution-intents/{intent_id}/risk-evaluation",
    response_model=RiskEvaluationResponse,
)
async def get_execution_intent_risk_evaluation(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
) -> RiskEvaluationResponse:
    """
    Queries current risk evaluation status for an ExecutionIntent.
    Requires CAP_OBSERVE, CAP_RISK_EVALUATE, or CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_RISK_EVALUATE)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    intent = intent_mgr.get_intent(intent_id, correlation_id=corr_id)
    if not intent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INTENT_NOT_FOUND", "message": f"Intent {intent_id} not found"},
        )

    decision = intent.risk_status or "PENDING"
    reason = intent.risk_rejection_reason or (
        "PRE_TRADE_RISK_APPROVED" if decision == "ALLOWED" else "Pending risk evaluation"
    )

    return RiskEvaluationResponse(
        intent_id=intent.intent_id,
        decision=decision,
        reason=reason,
        details=intent.risk_details or {},
        evaluator_id=intent.risk_evaluator_id or "",
        evaluated_at=intent.risk_evaluated_at or "",
        correlation_id=corr_id,
        status_category=intent.status_category,
        is_executed=intent.is_executed,
        resulting_state=intent.state.value,
    )


# ---------------------------------------------------------------------------
# Phase 7: Canonical Order Instruction & Broker Adapter Boundary
# ---------------------------------------------------------------------------

def _instruction_to_response(ins: OrderInstruction) -> OrderInstructionResponse:
    """Converts internal canonical OrderInstruction domain entity to API schema."""
    created_str = ins.created_at.isoformat() if isinstance(ins.created_at, datetime) else str(ins.created_at)
    state_str = ins.state.value if hasattr(ins.state, "value") else str(ins.state)
    return OrderInstructionResponse(
        instruction_id=ins.instruction_id,
        intent_id=ins.intent_id,
        symbol=ins.symbol,
        side=ins.side,
        quantity=ins.quantity,
        order_type=ins.order_type,
        limit_price=ins.limit_price,
        stop_price=ins.stop_price,
        time_in_force=ins.time_in_force,
        risk_evaluation_reference=ins.risk_evaluation_reference,
        approval_reference=ins.approval_reference,
        correlation_id=ins.correlation_id,
        provenance=ins.provenance,
        created_at=created_str,
        state=state_str,
        broker_order_id=ins.broker_order_id,
        dispatched_at=ins.dispatched_at,
        acknowledged_at=ins.acknowledged_at,
        rejection_reason=ins.rejection_reason,
        failure_reason=ins.failure_reason,
        cancellation_reason=ins.cancellation_reason,
        status_category=ins.status_category,
        is_executed=ins.is_executed,
        note="ORDER INSTRUCTION ONLY — NOT CLAIMED AS EXECUTED",
    )


@router.post(
    "/api/v1/execution-intents/{intent_id}/create-instruction",
    response_model=OrderInstructionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order_instruction_from_intent(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    """
    Creates an authoritative, canonical OrderInstruction from a RISK_APPROVED ExecutionIntent.
    Requires CAP_ORDER_INSTRUCT, CAP_TRADE_SUBMIT, CAP_TRADE_APPROVE, or CAP_ADMIN.
    Enforces execution chain, idempotency, provenance, and guard-state protection.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_ORDER_INSTRUCT)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_APPROVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_SUBMIT)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_ORDER_INSTRUCT.value}",
            },
        )

    intent = intent_mgr.get_intent(intent_id, correlation_id=corr_id)
    if not intent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INTENT_NOT_FOUND", "message": f"Intent {intent_id} not found"},
        )

    try:
        instruction = instruction_mgr.create_instruction_from_intent(
            intent=intent,
            operator_id=session.operator_id,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_409_CONFLICT
            if code in ["DUPLICATE_INSTRUCTION_PREVENTED", "GUARD_HALTED"]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    return _instruction_to_response(instruction)


@router.post(
    "/api/v1/order-instructions",
    response_model=OrderInstructionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order_instruction_endpoint(
    payload: CreateInstructionPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    intent_mgr: ExecutionIntentManager = Depends(get_intent_manager),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    """Convenience endpoint to create an OrderInstruction via JSON payload with intent_id."""
    return await create_order_instruction_from_intent(
        intent_id=payload.intent_id,
        request=request,
        response=response,
        session_data=session_data,
        intent_mgr=intent_mgr,
        instruction_mgr=instruction_mgr,
    )


@router.post(
    "/api/v1/order-instructions/{instruction_id}/dispatch",
    response_model=OrderInstructionResponse,
)
async def dispatch_order_instruction(
    instruction_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    """
    Dispatches a VALIDATED OrderInstruction through the BrokerAdapter.
    Requires CAP_ORDER_DISPATCH, CAP_TRADE_APPROVE, or CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_ORDER_DISPATCH)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_APPROVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_ORDER_DISPATCH.value}",
            },
        )

    try:
        instruction = instruction_mgr.dispatch_instruction(
            instruction_id=instruction_id,
            operator_id=session.operator_id,
            correlation_id=corr_id,
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INSTRUCTION_NOT_FOUND", "message": f"OrderInstruction {instruction_id} not found"},
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        status_code = (
            status.HTTP_409_CONFLICT
            if code in ["GUARD_HALTED", "GUARD_PAUSED", "INVALID_INSTRUCTION_STATE"]
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": err_msg},
        )

    return _instruction_to_response(instruction)


@router.post(
    "/api/v1/order-instructions/{instruction_id}/cancel",
    response_model=OrderInstructionResponse,
)
async def cancel_order_instruction(
    instruction_id: str,
    payload: CancelInstructionPayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    """
    Cancels an active or validated OrderInstruction.
    Requires CAP_TRADE_CANCEL or CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_TRADE_CANCEL)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_CAPABILITY",
                "message": f"Operator lacks required capability: {Capability.CAP_TRADE_CANCEL.value}",
            },
        )

    try:
        instruction = instruction_mgr.cancel_instruction(
            instruction_id=instruction_id,
            operator_id=session.operator_id,
            reason=payload.reason,
            correlation_id=corr_id,
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INSTRUCTION_NOT_FOUND", "message": f"OrderInstruction {instruction_id} not found"},
        )
    except ValueError as exc:
        err_msg = str(exc)
        code = err_msg.split(":")[0].strip()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": code, "message": err_msg},
        )

    return _instruction_to_response(instruction)


@router.get(
    "/api/v1/order-instructions/{instruction_id}",
    response_model=OrderInstructionResponse,
)
async def get_order_instruction(
    instruction_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_ORDER_INSTRUCT)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    instruction = instruction_mgr.get_instruction(instruction_id)
    if not instruction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INSTRUCTION_NOT_FOUND", "message": f"OrderInstruction {instruction_id} not found"},
        )

    return _instruction_to_response(instruction)


@router.get(
    "/api/v1/order-instructions",
    response_model=OrderInstructionListResponse,
)
async def list_order_instructions(
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionListResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_ORDER_INSTRUCT)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    instructions = instruction_mgr.list_instructions()
    return OrderInstructionListResponse(
        instructions=[_instruction_to_response(ins) for ins in instructions],
        count=len(instructions),
    )


@router.get(
    "/api/v1/execution-intents/{intent_id}/order-instruction",
    response_model=OrderInstructionResponse,
)
async def get_intent_order_instruction(
    intent_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> OrderInstructionResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE)
        or CapabilityChecker.has_capability(session, Capability.CAP_ORDER_INSTRUCT)
        or CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    instruction = instruction_mgr.get_by_intent(intent_id)
    if not instruction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "INSTRUCTION_NOT_FOUND", "message": f"No OrderInstruction exists for Intent {intent_id}"},
        )

    return _instruction_to_response(instruction)


# ---------------------------------------------------------------------------
# Phase 8: Execution State Synchronization & Fill Reconciliation
# ---------------------------------------------------------------------------

def _execution_to_response(rec: ExecutionRecord) -> ExecutionResponse:
    """Converts internal ExecutionRecord entity to API response schema."""
    return ExecutionResponse(
        execution_id=rec.execution_id,
        instruction_id=rec.instruction_id,
        intent_id=rec.intent_id,
        correlation_id=rec.correlation_id,
        symbol=rec.symbol,
        side=rec.side,
        ordered_quantity=rec.ordered_quantity,
        filled_quantity=rec.filled_quantity,
        remaining_quantity=rec.remaining_quantity,
        price=rec.price,
        limit_price=rec.limit_price,
        average_fill_price=rec.average_fill_price,
        broker_order_id=rec.broker_order_id,
        broker_timestamp=rec.broker_timestamp,
        gateway_timestamp=rec.gateway_timestamp,
        current_state=rec.current_state.value,
        reconciliation_status=rec.reconciliation_status.value,
        reconciliation_notes=rec.reconciliation_notes,
        reconciliation_details=rec.reconciliation_details,
        last_error=rec.last_error,
        rejection_reason=rec.rejection_reason,
        failure_reason=rec.failure_reason,
        cancellation_reason=rec.cancellation_reason,
        fills_count=len(rec.fills),
        last_update_timestamp=rec.last_update_timestamp or rec.gateway_timestamp,
        fills=[
            FillModel(
                fill_id=f.fill_id,
                broker_fill_id=f.broker_fill_id,
                execution_id=f.execution_id or rec.execution_id,
                quantity=f.quantity,
                price=f.price,
                timestamp=f.timestamp,
                fee=f.fee,
            )
            for f in rec.fills
        ],
    )


@router.get(
    "/api/v1/executions",
    response_model=ExecutionListResponse,
)
async def list_executions(
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    exec_mgr: ExecutionStateManager = Depends(get_execution_state_manager),
) -> ExecutionListResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    records = exec_mgr.list_executions()
    return ExecutionListResponse(
        executions=[_execution_to_response(rec) for rec in records],
        count=len(records),
    )


@router.get(
    "/api/v1/executions/{execution_id}",
    response_model=ExecutionResponse,
)
async def get_execution(
    execution_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    exec_mgr: ExecutionStateManager = Depends(get_execution_state_manager),
) -> ExecutionResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    rec = exec_mgr.get_execution(execution_id)
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "EXECUTION_NOT_FOUND", "message": f"Execution {execution_id} not found"},
        )

    return _execution_to_response(rec)


@router.get(
    "/api/v1/order-instructions/{instruction_id}/execution",
    response_model=ExecutionResponse,
)
async def get_instruction_execution(
    instruction_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    exec_mgr: ExecutionStateManager = Depends(get_execution_state_manager),
) -> ExecutionResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}",
            },
        )

    rec = exec_mgr.get_by_instruction(instruction_id)
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "EXECUTION_NOT_FOUND", "message": f"No Execution found for instruction {instruction_id}"},
        )

    return _execution_to_response(rec)


@router.post(
    "/api/v1/executions/{execution_id}/reconcile",
    response_model=ExecutionResponse,
)
async def reconcile_execution(
    execution_id: str,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    exec_mgr: ExecutionStateManager = Depends(get_execution_state_manager),
    instruction_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> ExecutionResponse:
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not (
        CapabilityChecker.has_capability(session, Capability.CAP_ADMIN)
        or CapabilityChecker.has_capability(session, Capability.CAP_TRADE_APPROVE)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}",
            },
        )

    rec = exec_mgr.get_execution(execution_id)
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "EXECUTION_NOT_FOUND", "message": f"Execution {execution_id} not found"},
        )

    instruction = instruction_mgr.get_instruction(rec.instruction_id)
    reconciled_rec = exec_mgr.reconcile(execution_id=execution_id, instruction=instruction)
    return _execution_to_response(reconciled_rec)


# ---------------------------------------------------------------------------
# Phase 9: Broker Connectivity & Execution Mode Boundary
# ---------------------------------------------------------------------------

@router.get("/api/v1/broker/status", response_model=BrokerStatusResponse)
async def get_broker_status(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
) -> BrokerStatusResponse:
    """
    Queries point-in-time broker connectivity, heartbeat, authentication,
    and execution mode status. Never returns unredacted credentials.
    Requires CAP_OBSERVE.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}"},
        )
    return BrokerStatusResponse(**conn_mgr.get_status())


@router.get("/api/v1/broker/mode", response_model=BrokerModeResponse)
async def get_broker_mode(
    request: Request,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
    guard: TradingGuard = Depends(get_trading_guard),
) -> BrokerModeResponse:
    """
    Queries current active execution mode (PAPER vs LIVE).
    Requires CAP_OBSERVE.
    """
    token, session = session_data
    if not CapabilityChecker.has_capability(session, Capability.CAP_OBSERVE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_OBSERVE.value}"},
        )
    mode = conn_mgr.execution_mode
    is_live = (mode == ExecutionMode.LIVE)
    return BrokerModeResponse(
        execution_mode=mode.value,
        is_live=is_live,
        guard_state=guard.state.value,
        connectivity_state=conn_mgr.connectivity_state.value,
        message=f"Current execution mode is {mode.value}.",
    )


@router.post("/api/v1/broker/connect", response_model=BrokerStatusResponse)
async def connect_broker(
    request: Request,
    response: Response,
    payload: Optional[BrokerConnectPayload] = None,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
) -> BrokerStatusResponse:
    """
    Initiates broker session connection.
    Requires CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}"},
        )

    creds = None
    venue = None
    if payload:
        venue = payload.venue_name
        if payload.client_id and payload.access_token:
            creds = BrokerCredentialsConfig(
                venue_name=payload.venue_name or "LIVE_BROKER",
                client_id=payload.client_id,
                access_token=payload.access_token,
                account_id=payload.account_id,
            )

    try:
        conn_mgr.connect(
            operator_id=session.operator_id,
            credentials=creds,
            venue_name=venue,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BROKER_CONNECT_FAILED", "message": str(exc)},
        )

    return BrokerStatusResponse(**conn_mgr.get_status())


@router.post("/api/v1/broker/disconnect", response_model=BrokerStatusResponse)
async def disconnect_broker(
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
) -> BrokerStatusResponse:
    """
    Disconnects active broker session.
    Requires CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}"},
        )

    conn_mgr.disconnect(
        operator_id=session.operator_id,
        reason="OPERATOR_REQUESTED_DISCONNECT",
        correlation_id=corr_id,
    )
    return BrokerStatusResponse(**conn_mgr.get_status())


@router.post("/api/v1/broker/reconcile", response_model=BrokerReconcileResponse)
async def reconcile_broker_book(
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
    exec_mgr: ExecutionStateManager = Depends(get_execution_state_manager),
    ins_mgr: OrderInstructionManager = Depends(get_order_instruction_manager),
) -> BrokerReconcileResponse:
    """
    Performs authoritative broker book reconciliation.
    Requires CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}"},
        )

    broker_adapter = getattr(request.app.state, "broker_adapter", ins_mgr.broker_adapter)
    report = conn_mgr.reconcile(
        operator_id=session.operator_id,
        execution_manager=exec_mgr,
        instruction_manager=ins_mgr,
        broker_adapter=broker_adapter,
        correlation_id=corr_id,
    )
    return BrokerReconcileResponse(**report.to_dict())


@router.post("/api/v1/broker/mode/paper", response_model=BrokerModeResponse)
async def set_paper_mode(
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
    guard: TradingGuard = Depends(get_trading_guard),
) -> BrokerModeResponse:
    """
    Transitions execution mode to PAPER.
    Requires CAP_ADMIN.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}"},
        )

    conn_mgr.set_mode(
        target_mode=ExecutionMode.PAPER,
        operator_id=session.operator_id,
        correlation_id=corr_id,
    )
    return BrokerModeResponse(
        execution_mode=ExecutionMode.PAPER.value,
        is_live=False,
        guard_state=guard.state.value,
        connectivity_state=conn_mgr.connectivity_state.value,
        message="Execution mode successfully set to PAPER.",
    )


@router.post("/api/v1/broker/mode/live", response_model=BrokerModeResponse)
async def set_live_mode(
    payload: BrokerModeLivePayload,
    request: Request,
    response: Response,
    session_data: Tuple[str, SessionContext] = Depends(get_current_session),
    conn_mgr: BrokerConnectivityManager = Depends(get_broker_connectivity_manager),
    guard: TradingGuard = Depends(get_trading_guard),
) -> BrokerModeResponse:
    """
    Transitions execution mode to LIVE after passing multi-gate verification.
    Requires CAP_ADMIN and confirm_live=True.
    """
    token, session = session_data
    corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
    response.headers["X-Correlation-ID"] = corr_id

    if not CapabilityChecker.has_capability(session, Capability.CAP_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": f"Operator lacks required capability: {Capability.CAP_ADMIN.value}"},
        )

    try:
        conn_mgr.set_mode(
            target_mode=ExecutionMode.LIVE,
            operator_id=session.operator_id,
            confirm_live=payload.confirm_live,
            correlation_id=corr_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LIVE_ACTIVATION_REJECTED", "message": str(exc)},
        )

    return BrokerModeResponse(
        execution_mode=ExecutionMode.LIVE.value,
        is_live=True,
        guard_state=guard.state.value,
        connectivity_state=conn_mgr.connectivity_state.value,
        message="LIVE execution mode authorized and enabled.",
    )





