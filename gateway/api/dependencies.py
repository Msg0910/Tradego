"""
Tradego API FastAPI Route Dependencies.
Handles bearer token extraction, session validation, RBAC capability gating,
and application dependency resolution.
"""

from typing import Callable, Optional, Tuple
from fastapi import Header, HTTPException, Request, status

from ..contracts import Capability
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


def get_session_store(request: Request) -> InMemorySessionStore:
    return request.app.state.session_store


def get_audit_logger(request: Request) -> Tier1AuditLogger:
    return request.app.state.audit_logger


def get_credential_store(request: Request) -> NativeCredentialStore:
    return request.app.state.credential_store


def get_command_gateway(request: Request) -> CommandGateway:
    return request.app.state.command_gateway


def get_snapshot_generator(request: Request) -> SnapshotGenerator:
    return request.app.state.snapshot_generator


def get_trading_guard(request: Request) -> TradingGuard:
    return request.app.state.trading_guard


def get_broadcaster(request: Request):
    return request.app.state.broadcaster


def get_market_adapter(request: Request):
    return request.app.state.market_adapter


def get_recovery_manager(request: Request):
    return request.app.state.recovery_manager


def get_portfolio_risk_adapter(request: Request):
    return request.app.state.portfolio_risk_adapter


def get_intent_manager(request: Request):
    return request.app.state.intent_manager


def get_risk_gate(request: Request):
    return request.app.state.risk_gate


def get_order_instruction_manager(request: Request):
    return request.app.state.order_instruction_manager


def get_execution_state_manager(request: Request):
    return request.app.state.execution_state_manager


def get_broker_connectivity_manager(request: Request):
    return request.app.state.broker_connectivity_manager


def get_persistence_manager(request: Request):
    return getattr(request.app.state, "persistence_manager", None)


def get_disaster_recovery_manager(request: Request):
    return getattr(request.app.state, "disaster_recovery_manager", None)


def get_multi_venue_registry(request: Request):
    return getattr(request.app.state, "multi_venue_registry", None)


def get_multi_venue_router(request: Request):
    return getattr(request.app.state, "multi_venue_router", None)


def get_health_monitor(request: Request):
    return getattr(request.app.state, "health_monitor", None)


def get_current_session(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> Tuple[str, SessionContext]:
    """
    Extracts and validates opaque bearer token from the Authorization header.
    Fails closed with 401 UNAUTHENTICATED on missing, malformed, or revoked token.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHENTICATED",
                "message": "Authorization header missing",
            },
        )

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "MALFORMED_AUTHORIZATION",
                "message": "Authorization header must use 'Bearer <token>' format",
            },
        )

    token = parts[1].strip()
    session_store: InMemorySessionStore = request.app.state.session_store
    session = session_store.validate_token(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_TOKEN",
                "message": "Session token is invalid, expired, or revoked",
            },
        )

    return token, session


def require_capability(required_cap: Capability) -> Callable:
    """Dependency factory checking that the authenticated operator possesses required capability."""
    def _checker(
        session_tuple: Tuple[str, SessionContext] = None,
    ) -> Tuple[str, SessionContext]:
        token, session = session_tuple
        if not CapabilityChecker.has_capability(session, required_cap):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "FORBIDDEN",
                    "message": f"Operator lacks required capability: {required_cap.value}",
                },
            )
        return token, session

    return _checker
