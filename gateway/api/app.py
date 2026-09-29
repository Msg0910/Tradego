"""
Tradego FastAPI Application Boundary Factory.
Configures correlation ID propagation middleware, structured JSON exception handlers,
and mounts API routes.
"""

from typing import Optional
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from services.runtime.guards import TradingGuard
from ..adapters import MarketStateAdapter, PortfolioRiskAdapter
from ..broadcaster import EventBroadcaster, SequenceManager
from ..command import CommandGateway
from ..projection import SnapshotGenerator
from ..recovery import RecoveryManager
from ..intent import ExecutionIntentManager
from ..risk_gate import PreTradeRiskEvaluator
from ..broker_adapter import BrokerAdapter, PaperBrokerAdapter
from ..order_instruction import OrderInstructionManager
from ..execution import ExecutionStateManager
from ..broker_connectivity import BrokerConnectivityManager
from ..persistence import PersistenceManager
from ..disaster_recovery import DisasterRecoveryManager
from ..multi_venue import BrokerVenueRegistry, MultiVenueRouter
from ..observability import SystemHealthMonitor
from ..security import (
    InMemorySessionStore,
    NativeCredentialStore,
    Tier1AuditLogger,
)
from .routes import router


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Propagates correlation lineage across all inbound and outbound HTTP requests.
    Inspects X-Correlation-ID header; generates standard UUIDv4 if omitted.
    """

    async def dispatch(self, request: Request, call_next):
        corr_id = request.headers.get("X-Correlation-ID")
        if not corr_id:
            corr_id = str(uuid.uuid4())
        request.state.correlation_id = corr_id

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = corr_id
        return response


def create_app(
    trading_guard: Optional[TradingGuard] = None,
    session_store: Optional[InMemorySessionStore] = None,
    audit_logger: Optional[Tier1AuditLogger] = None,
    sequence_manager: Optional[SequenceManager] = None,
    broadcaster: Optional[EventBroadcaster] = None,
    credential_store: Optional[NativeCredentialStore] = None,
    command_gateway: Optional[CommandGateway] = None,
    snapshot_generator: Optional[SnapshotGenerator] = None,
    market_adapter: Optional[MarketStateAdapter] = None,
    recovery_manager: Optional[RecoveryManager] = None,
    portfolio_risk_adapter: Optional[PortfolioRiskAdapter] = None,
    intent_manager: Optional[ExecutionIntentManager] = None,
    risk_gate: Optional[PreTradeRiskEvaluator] = None,
    order_instruction_manager: Optional[OrderInstructionManager] = None,
    broker_adapter: Optional[BrokerAdapter] = None,
    execution_state_manager: Optional[ExecutionStateManager] = None,
    broker_connectivity_manager: Optional[BrokerConnectivityManager] = None,
    persistence_manager: Optional[PersistenceManager] = None,
    disaster_recovery_manager: Optional[DisasterRecoveryManager] = None,
    multi_venue_registry: Optional[BrokerVenueRegistry] = None,
    multi_venue_router: Optional[MultiVenueRouter] = None,
    health_monitor: Optional[SystemHealthMonitor] = None,
) -> FastAPI:
    """
    Constructs and configures the FastAPI application boundary instance.
    Wires dependencies into application state.
    """
    app = FastAPI(
        title="Tradego API Gateway",
        description="Presentation & Authenticated Command Boundary for Tradego",
        version="1.0.0",
        docs_url=None,  # Disabled by default per security hardening (Zone 2/3)
        redoc_url=None,
    )

    # Wire singletons into app.state
    guard = trading_guard or TradingGuard()
    s_store = session_store or InMemorySessionStore()
    a_logger = audit_logger or Tier1AuditLogger(log_file_path=None)
    seq_mgr = sequence_manager or SequenceManager()
    b_caster = broadcaster or EventBroadcaster(sequence_manager=seq_mgr)
    c_store = credential_store or NativeCredentialStore()
    m_adapter = market_adapter or MarketStateAdapter()
    c_gateway = command_gateway or CommandGateway(
        trading_guard=guard,
        session_store=s_store,
        audit_logger=a_logger,
        broadcaster=b_caster,
    )
    s_gen = snapshot_generator or SnapshotGenerator(
        trading_guard=guard,
        sequence_manager=seq_mgr,
        market_adapter=m_adapter,
    )
    r_mgr = recovery_manager or RecoveryManager(
        trading_guard=guard,
        command_gateway=c_gateway,
        audit_logger=a_logger,
    )
    pr_adapter = portfolio_risk_adapter or PortfolioRiskAdapter(
        trading_guard=guard,
    )
    i_mgr = intent_manager or ExecutionIntentManager(
        audit_logger=a_logger,
    )
    rg = risk_gate or PreTradeRiskEvaluator(
        trading_guard=guard,
        audit_logger=a_logger,
    )
    e_mgr = execution_state_manager or ExecutionStateManager(
        trading_guard=guard,
        audit_logger=a_logger,
        broadcaster=b_caster,
    )
    b_conn_mgr = broker_connectivity_manager or BrokerConnectivityManager(
        trading_guard=guard,
        audit_logger=a_logger,
        broadcaster=b_caster,
    )
    b_adapter = broker_adapter or PaperBrokerAdapter()
    o_mgr = order_instruction_manager or OrderInstructionManager(
        trading_guard=guard,
        audit_logger=a_logger,
        broker_adapter=b_adapter,
        execution_manager=e_mgr,
        connectivity_manager=b_conn_mgr,
    )
    if o_mgr.execution_manager is None:
        o_mgr.set_execution_manager(e_mgr)
    if o_mgr.connectivity_manager is None:
        o_mgr.set_connectivity_manager(b_conn_mgr)
    if e_mgr.connectivity_manager is None:
        e_mgr.set_connectivity_manager(b_conn_mgr)

    # Phase 10 Singletons
    p_mgr = persistence_manager or PersistenceManager(wal_path="data/execution_wal.jsonl")
    dr_mgr = disaster_recovery_manager or DisasterRecoveryManager(
        trading_guard=guard,
        persistence_manager=p_mgr,
        execution_manager=e_mgr,
        instruction_manager=o_mgr,
        intent_manager=i_mgr,
        audit_logger=a_logger,
        broadcaster=b_caster,
        connectivity_manager=b_conn_mgr,
    )
    v_reg = multi_venue_registry or BrokerVenueRegistry(audit_logger=a_logger)
    v_router = multi_venue_router or MultiVenueRouter(registry=v_reg, audit_logger=a_logger)
    h_mon = health_monitor or SystemHealthMonitor(
        trading_guard=guard,
        audit_logger=a_logger,
        persistence_manager=p_mgr,
        connectivity_manager=b_conn_mgr,
        recovery_manager=dr_mgr,
        execution_manager=e_mgr,
    )

    # Wire persistence
    if hasattr(i_mgr, "set_persistence_store"):
        i_mgr.set_persistence_store(p_mgr)
    if hasattr(o_mgr, "set_persistence_manager"):
        o_mgr.set_persistence_manager(p_mgr)
    if hasattr(e_mgr, "set_persistence_manager"):
        e_mgr.set_persistence_manager(p_mgr)

    app.state.trading_guard = guard
    app.state.session_store = s_store
    app.state.audit_logger = a_logger
    app.state.sequence_manager = seq_mgr
    app.state.broadcaster = b_caster
    app.state.credential_store = c_store
    app.state.command_gateway = c_gateway
    app.state.snapshot_generator = s_gen
    app.state.market_adapter = m_adapter
    app.state.recovery_manager = r_mgr
    app.state.portfolio_risk_adapter = pr_adapter
    app.state.intent_manager = i_mgr
    app.state.risk_gate = rg
    app.state.broker_adapter = b_adapter
    app.state.order_instruction_manager = o_mgr
    app.state.execution_state_manager = e_mgr
    app.state.broker_connectivity_manager = b_conn_mgr
    app.state.persistence_manager = p_mgr
    app.state.disaster_recovery_manager = dr_mgr
    app.state.multi_venue_registry = v_reg
    app.state.multi_venue_router = v_router
    app.state.health_monitor = h_mon


    # Middlewares
    app.add_middleware(CorrelationIdMiddleware)

    # Structured Error Handlers (Enforcing Platform Structured Error Contract)
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code", "HTTP_ERROR")
            message = exc.detail.get("message", "An error occurred")
            details = exc.detail.get("details", {})
        else:
            code = "HTTP_ERROR"
            message = str(exc.detail)
            details = {}

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "correlation_id": corr_id,
                    "details": details,
                }
            },
            headers={"X-Correlation-ID": corr_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request schema validation failed",
                    "correlation_id": corr_id,
                    "details": {"validation_errors": exc.errors()},
                }
            },
            headers={"X-Correlation-ID": corr_id},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        corr_id = getattr(request.state, "correlation_id", str(uuid.uuid4()))
        a_logger.log({
            "action": "API_UNHANDLED_EXCEPTION",
            "error": str(exc),
            "correlation_id": corr_id,
        })
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected server error occurred",
                    "correlation_id": corr_id,
                    "details": {},
                }
            },
            headers={"X-Correlation-ID": corr_id},
        )

    # Mount Route Handlers
    app.include_router(router)

    return app
