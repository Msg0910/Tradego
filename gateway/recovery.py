"""
Tradego Two-Person Operational Recovery Manager (Phase 4).
Enforces authenticated two-person confirmation for clearing HALTED guard state
without modifying the frozen core or bypassing TradingGuard authority.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import secrets
import threading
from typing import Any, Dict, Optional
import uuid

from services.runtime.guards import TradingGuard
from services.runtime.models import GuardState
from .command import CommandGateway
from .contracts import Capability, CommandRequest, CommandResult, CommandStatus, CommandType
from .security import InMemorySessionStore, Tier1AuditLogger


@dataclass(frozen=True)
class RecoveryChallenge:
    """
    Short-lived, single-use recovery challenge created by Operator A.
    Requires distinct confirmation by Operator B within the expiry window.
    """
    challenge_id: str
    operator_a_id: str
    expected_token: str
    created_at: datetime
    expires_at: datetime
    status: str  # "PENDING", "CONSUMED", "EXPIRED"
    correlation_id: str

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    def to_dict(self, include_token: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "challenge_id": self.challenge_id,
            "operator_a_id": self.operator_a_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status,
            "correlation_id": self.correlation_id,
        }
        if include_token:
            d["confirmation_token"] = self.expected_token
            d["token"] = self.expected_token
        return d


class RecoveryManager:
    """
    Stateful boundary manager enforcing authenticated two-person rule for HALTED recovery.
    Coordinates between operators and delegates recovery dispatch to CommandGateway.
    """

    def __init__(
        self,
        trading_guard: TradingGuard,
        command_gateway: CommandGateway,
        audit_logger: Tier1AuditLogger,
        challenge_ttl_seconds: int = 300,
    ) -> None:
        self._guard = trading_guard
        self._gateway = command_gateway
        self._audit = audit_logger
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._lock = threading.RLock()
        self._active_challenge: Optional[RecoveryChallenge] = None
        self._history: Dict[str, RecoveryChallenge] = {}

    def create_challenge(
        self,
        operator_a_id: str,
        correlation_id: str,
        ttl_seconds: Optional[int] = None,
    ) -> RecoveryChallenge:
        """
        Step 1: Operator A requests recovery from HALTED state.
        Creates a short-lived, single-use recovery challenge with expected token.
        """
        with self._lock:
            if self._guard.state != GuardState.HALTED:
                raise ValueError("GUARD_NOT_HALTED: Recovery challenge requires HALTED guard state")

            now = datetime.now(timezone.utc)
            actual_ttl = ttl_seconds if ttl_seconds is not None else self._challenge_ttl_seconds
            token = f"rec-tok-{secrets.token_urlsafe(16)}"
            cid = f"rec-{uuid.uuid4().hex[:12]}"
            challenge = RecoveryChallenge(
                challenge_id=cid,
                operator_a_id=operator_a_id,
                expected_token=token,
                created_at=now,
                expires_at=now + timedelta(seconds=actual_ttl),
                status="PENDING",
                correlation_id=correlation_id,
            )
            self._active_challenge = challenge
            self._history[cid] = challenge

            self._audit.log({
                "action": "RECOVERY_CHALLENGE_REQUESTED",
                "challenge_id": challenge.challenge_id,
                "operator_id": operator_a_id,
                "operator_a_id": operator_a_id,
                "expires_at": challenge.expires_at.isoformat(),
                "correlation_id": correlation_id,
            })
            return challenge

    def get_active_challenge(self) -> Optional[RecoveryChallenge]:
        """Returns currently valid pending challenge, or None if expired/consumed."""
        with self._lock:
            if not self._active_challenge:
                return None
            if self._active_challenge.is_expired and self._active_challenge.status == "PENDING":
                expired = RecoveryChallenge(
                    challenge_id=self._active_challenge.challenge_id,
                    operator_a_id=self._active_challenge.operator_a_id,
                    expected_token=self._active_challenge.expected_token,
                    created_at=self._active_challenge.created_at,
                    expires_at=self._active_challenge.expires_at,
                    status="EXPIRED",
                    correlation_id=self._active_challenge.correlation_id,
                )
                self._history[expired.challenge_id] = expired
                self._active_challenge = None
                return None
            return self._active_challenge if self._active_challenge.status == "PENDING" else None

    def get_challenge(self, challenge_id: str) -> Optional[RecoveryChallenge]:
        """Returns challenge by ID from active or history."""
        with self._lock:
            if self._active_challenge and self._active_challenge.challenge_id == challenge_id:
                return self._active_challenge
            return self._history.get(challenge_id)

    def confirm_recovery(
        self,
        challenge_id: str,
        operator_b_id: str,
        confirmation_token: str,
        correlation_id: str,
        operator_b_token: Optional[str] = None,
    ) -> CommandResult:
        """
        Step 2: Distinct Operator B confirms recovery.
        Enforces two-person uniqueness, single-use challenge consumption, and executes recovery.
        """
        with self._lock:
            challenge = self._active_challenge
            if not challenge or challenge.challenge_id != challenge_id:
                if challenge_id in self._history:
                    h = self._history[challenge_id]
                    if h.status == "CONSUMED":
                        raise ValueError("CHALLENGE_ALREADY_CONSUMED: Challenge single-use constraint violated")
                    if h.is_expired or h.status == "EXPIRED":
                        raise ValueError("CHALLENGE_EXPIRED: Recovery challenge has expired")
                raise ValueError("CHALLENGE_NOT_FOUND: Recovery challenge does not exist")

            if challenge.status == "CONSUMED":
                raise ValueError("CHALLENGE_ALREADY_CONSUMED: Challenge single-use constraint violated")

            if challenge.is_expired or challenge.status == "EXPIRED":
                raise ValueError("CHALLENGE_EXPIRED: Recovery challenge has expired")

            # Two-person distinct identity rule
            if operator_b_id == challenge.operator_a_id:
                self._audit.log({
                    "action": "RECOVERY_SAME_OPERATOR_REJECTED",
                    "challenge_id": challenge_id,
                    "operator_id": operator_b_id,
                    "correlation_id": correlation_id,
                })
                raise ValueError("SAME_OPERATOR_CONFIRMATION_REJECTED: Operator B must be distinct from Operator A")

            if self._guard.state != GuardState.HALTED:
                raise ValueError("GUARD_NOT_HALTED: Guard is not in HALTED state")

            if confirmation_token != challenge.expected_token:
                self._audit.log({
                    "action": "RECOVERY_TOKEN_MISMATCH",
                    "challenge_id": challenge_id,
                    "operator_b_id": operator_b_id,
                    "correlation_id": correlation_id,
                })
                raise ValueError("INVALID_CONFIRMATION_TOKEN: Confirmation token does not match challenge")

            # Single-use: Mark consumed immediately
            consumed = RecoveryChallenge(
                challenge_id=challenge.challenge_id,
                operator_a_id=challenge.operator_a_id,
                expected_token=challenge.expected_token,
                created_at=challenge.created_at,
                expires_at=challenge.expires_at,
                status="CONSUMED",
                correlation_id=challenge.correlation_id,
            )
            self._history[challenge.challenge_id] = consumed
            self._active_challenge = None

            # Delegate to CommandGateway using authoritative CommandType.RECOVER_HALTED
            if operator_b_token:
                req = CommandRequest(
                    command_type=CommandType.RECOVER_HALTED,
                    operator_id=operator_b_id,
                    correlation_id=correlation_id,
                    parameters={
                        "confirmation_token": confirmation_token,
                        "expected_token": challenge.expected_token,
                        "challenge_id": challenge_id,
                        "operator_a_id": challenge.operator_a_id,
                    },
                )
                res = self._gateway.dispatch(token=operator_b_token, request=req)
            else:
                # Direct recovery invocation (e.g. from unit tests without HTTP session token)
                ok = self._guard.recover(
                    confirmation_token=confirmation_token,
                    expected_token=challenge.expected_token,
                )
                res = CommandResult(
                    command_id=f"cmd-rec-{uuid.uuid4().hex[:8]}",
                    status=CommandStatus.ACCEPTED if ok else CommandStatus.REJECTED,
                    guard_state=self._guard.state.value,
                    sequence=0,
                    reason="Recovered from HALTED" if ok else "Failed recovery",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

            if res.status == CommandStatus.ACCEPTED:
                self._audit.log({
                    "action": "RECOVERY_CONFIRMED",
                    "challenge_id": challenge_id,
                    "operator_id": operator_b_id,
                    "operator_a_id": challenge.operator_a_id,
                    "operator_b_id": operator_b_id,
                    "guard_state": self._guard.state.value,
                    "sequence": res.sequence,
                    "correlation_id": correlation_id,
                })
            else:
                self._audit.log({
                    "action": "RECOVERY_FAILED",
                    "challenge_id": challenge_id,
                    "reason": res.reason,
                    "correlation_id": correlation_id,
                })

            return res
