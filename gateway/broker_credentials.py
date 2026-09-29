"""
Tradego Phase 9 — Secure Broker Credentials Abstraction.

Provides a protected configuration boundary for venue credentials:
- Zero hardcoded secrets in source code
- Safe redacted representation for logging, audit, UI, and diagnostics
- Custom __repr__ and __str__ preventing secret leakage in stack traces and console output
- Strict validation against empty or malformed keys
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


_SENSITIVE_KEYS = {
    "password",
    "secret",
    "secret_key",
    "api_key",
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "authorization",
}


def _is_sensitive_key(key: str) -> bool:
    """Checks whether a dictionary key represents a sensitive credential parameter."""
    k = str(key).lower().strip()
    if k in _SENSITIVE_KEYS:
        return True
    return any(sens in k for sens in ("password", "secret", "token", "auth", "api_key", "secret_key"))


def _redact_value(val: Any) -> Any:
    """Recursively redacts dictionary values associated with sensitive keys."""
    if isinstance(val, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive_key(k) else _redact_value(v))
            for k, v in val.items()
        }
    elif isinstance(val, list):
        return [_redact_value(item) for item in val]
    return val


@dataclass(frozen=True)
class BrokerCredentialsConfig:
    """
    Authoritative container for broker venue credentials.
    Immutable once initialized, with strict redaction guarantees.
    """
    venue_name: str
    client_id: str
    access_token: str
    api_key: Optional[str] = None
    secret_key: Optional[str] = None
    account_id: Optional[str] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.venue_name or not self.venue_name.strip():
            raise ValueError("INVALID_CREDENTIALS: venue_name must be non-empty string.")
        if not self.client_id or not self.client_id.strip():
            raise ValueError("INVALID_CREDENTIALS: client_id must be non-empty string.")
        if not self.access_token or not self.access_token.strip():
            raise ValueError("INVALID_CREDENTIALS: access_token must be non-empty string.")

    @property
    def is_valid(self) -> bool:
        """Returns True if minimum authentication parameters are non-empty."""
        return bool(
            self.venue_name.strip()
            and self.client_id.strip()
            and self.access_token.strip()
        )

    def _mask_identifier(self, value: Optional[str]) -> str:
        if not value:
            return "N/A"
        clean = value.strip()
        if len(clean) <= 4:
            return "***"
        return f"{clean[:2]}***{clean[-2:]}"

    def redacted_dict(self) -> Dict[str, Any]:
        """
        Returns safe representation with all secrets and tokens masked.
        Safe for API responses, UI inspection, error reporting, and audit logs.
        """
        return {
            "venue_name": self.venue_name,
            "client_id": self._mask_identifier(self.client_id),
            "account_id": self._mask_identifier(self.account_id) if self.account_id else "N/A",
            "has_access_token": bool(self.access_token),
            "access_token": "[REDACTED]",
            "api_key": "[REDACTED]" if self.api_key else None,
            "secret_key": "[REDACTED]" if self.secret_key else None,
            "is_valid": self.is_valid,
            "extra_params": _redact_value(self.extra_params) if self.extra_params else {},
        }

    def __repr__(self) -> str:
        extra_repr = f", extra_params={_redact_value(self.extra_params)!r}" if self.extra_params else ""
        return (
            f"BrokerCredentialsConfig(venue_name={self.venue_name!r}, "
            f"client_id={self._mask_identifier(self.client_id)!r}, "
            f"access_token='[REDACTED]'{extra_repr})"
        )

    def __str__(self) -> str:
        return self.__repr__()
