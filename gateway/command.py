"""
Tradego Command Gateway & Domain Service Adapter.
Enforces authentication, authorization, audit logging, and routes control commands
strictly through TradingGuard (UI-14, SEC-01, SEC-02, SEC-04, SEC-05).
"""

from typing import Dict, Optional

from services.runtime.guards import TradingGuard
from services.runtime.models import GuardTripReason

from .broadcaster import EventBroadcaster
from .contracts import (
    Capability,
    CommandRequest,
    CommandResult,
    CommandStatus,
    CommandType,
)
from .security import (
    CapabilityChecker,
    InMemorySessionStore,
    Tier1AuditLogger,
)

COMMAND_CAPABILITY_MAP: Dict[CommandType, Capability] = {
    CommandType.PAUSE: Capability.CAP_CONTROL_PAUSE,
    CommandType.RESUME: Capability.CAP_CONTROL_RESUME,
    CommandType.EMERGENCY_FLATTEN: Capability.CAP_CONTROL_FLATTEN,
    CommandType.KILL_SWITCH: Capability.CAP_CONTROL_KILL,
    CommandType.RECOVER_HALTED: Capability.CAP_CONTROL_RECOVER,
}


class CommandGateway:
    """
    Authenticated, audited command gateway.
    Insulates the trading core and guarantees that zero mutating commands bypass
    security verification or TradingGuard authority.
    """

    def __init__(
        self,
        trading_guard: TradingGuard,
        session_store: InMemorySessionStore,
        audit_logger: Tier1AuditLogger,
        broadcaster: EventBroadcaster,
    ) -> None:
        self._guard = trading_guard
        self._session_store = session_store
        self._audit = audit_logger
        self._broadcaster = broadcaster

    def dispatch(self, token: str, request: CommandRequest) -> CommandResult:
        """
        Processes an inbound command through the strict four-stage pipeline:
        1. Authentication Verification (SEC-01)
        2. Authorization / Capability Check (SEC-02)
        3. Audit Logging (SEC-09)
        4. Authoritative Core Execution & Event Emission
        """
        # 1. Authentication (SEC-01)
        session = self._session_store.validate_token(token)
        if not session:
            self._audit.log({
                "action": "COMMAND_AUTHENTICATION_FAILED",
                "command_id": request.command_id,
                "command_type": request.command_type.value,
                "correlation_id": request.correlation_id,
            })
            return CommandResult(
                command_id=request.command_id,
                status=CommandStatus.REJECTED,
                guard_state=self._guard.state.value,
                reason="UNAUTHENTICATED: Invalid or expired session token",
            )

        # 2. Authorization (SEC-02)
        required_cap = COMMAND_CAPABILITY_MAP.get(request.command_type)
        if not required_cap or not CapabilityChecker.has_capability(session, required_cap):
            self._audit.log({
                "action": "COMMAND_AUTHORIZATION_DENIED",
                "operator_id": session.operator_id,
                "command_id": request.command_id,
                "command_type": request.command_type.value,
                "correlation_id": request.correlation_id,
                "required_capability": required_cap.value if required_cap else "UNKNOWN",
            })
            return CommandResult(
                command_id=request.command_id,
                status=CommandStatus.REJECTED,
                guard_state=self._guard.state.value,
                reason=f"UNAUTHORIZED: Missing required capability {required_cap.value if required_cap else ''}",
            )

        # 3. Audit Receipt (SEC-09)
        self._audit.log({
            "action": "COMMAND_INGESTED",
            "operator_id": session.operator_id,
            "session_id": session.session_id,
            "command_id": request.command_id,
            "command_type": request.command_type.value,
            "correlation_id": request.correlation_id,
            "parameters": request.parameters,
        })

        # 4. Authoritative Domain Service Execution (TradingGuard)
        try:
            if request.command_type == CommandType.PAUSE:
                self._guard.pause()
            elif request.command_type == CommandType.RESUME:
                self._guard.resume()
            elif request.command_type == CommandType.EMERGENCY_FLATTEN:
                self._guard.emergency_flatten()
            elif request.command_type == CommandType.KILL_SWITCH:
                details = request.parameters.get("details", "Manual kill switch triggered")
                self._guard.trip(GuardTripReason.MANUAL, details=details)
            elif request.command_type == CommandType.RECOVER_HALTED:
                conf_token = request.parameters.get("confirmation_token", "")
                exp_token = request.parameters.get("expected_token", "")
                recovered = self._guard.recover(conf_token, exp_token)
                if not recovered:
                    return CommandResult(
                        command_id=request.command_id,
                        status=CommandStatus.REJECTED,
                        guard_state=self._guard.state.value,
                        reason="RECOVERY_FAILED: Invalid confirmation token or guard not HALTED",
                    )
            else:
                return CommandResult(
                    command_id=request.command_id,
                    status=CommandStatus.REJECTED,
                    guard_state=self._guard.state.value,
                    reason=f"UNSUPPORTED_COMMAND: {request.command_type.value}",
                )
        except Exception as exc:
            self._audit.log({
                "action": "COMMAND_EXECUTION_EXCEPTION",
                "command_id": request.command_id,
                "error": str(exc),
            })
            return CommandResult(
                command_id=request.command_id,
                status=CommandStatus.FAILED,
                guard_state=self._guard.state.value,
                reason=f"INTERNAL_ERROR: {str(exc)}",
            )

        # 5. Authoritative State Event Emission
        event = self._broadcaster.publish(
            event_type="GUARD_STATE_CHANGED",
            payload={
                "command_id": request.command_id,
                "guard_state": self._guard.state.value,
                "operator_id": session.operator_id,
            },
            correlation_id=request.correlation_id,
        )

        # 6. Audit Execution Confirmation
        self._audit.log({
            "action": "COMMAND_COMPLETED",
            "command_id": request.command_id,
            "guard_state": self._guard.state.value,
            "sequence": event.sequence,
        })

        return CommandResult(
            command_id=request.command_id,
            status=CommandStatus.ACCEPTED,
            guard_state=self._guard.state.value,
            sequence=event.sequence,
        )
