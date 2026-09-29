"""
Angel One SmartAPI Authentication & Session Management.

Provides:
- RFC 6238 standard TOTP generation using Python standard library (no external dependency).
- Secure, redacted SmartAPICredentials container.
- SmartAPIAuthSession lifecycle and token management.
- SmartAPIAuthenticator for executing REST loginByPassword requests.

CRITICAL GOVERNANCE & SAFETY RULES:
1. Zero hardcoded secrets in source code.
2. Credentials and tokens are strictly masked in __repr__ and __str__.
3. Exceptions must never leak raw passwords, MPINs, or TOTP secrets.
4. By default, zero outbound network requests without explicit caller invocation.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os
import struct
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("adapters.smartapi.auth")


def generate_totp(
    secret: str,
    for_time: Optional[float] = None,
    interval: int = 30,
    digits: int = 6,
) -> str:
    """
    Generates standard RFC 6238 Time-Based One-Time Password (TOTP).

    Uses HMAC-SHA1 with standard 30-second interval and 6 digits.
    Self-contained using Python standard library (hashlib, hmac, struct, base64).
    """
    clean_secret = secret.strip().replace(" ", "").upper()
    if not clean_secret:
        raise ValueError("TOTP secret cannot be empty.")

    # Fix base32 padding if missing
    pad = len(clean_secret) % 8
    if pad != 0:
        clean_secret += "=" * (8 - pad)

    try:
        key = base64.b32decode(clean_secret, casefold=True)
    except Exception as e:
        raise ValueError(f"Invalid base32 TOTP secret: {e}") from e

    current_time = for_time if for_time is not None else time.time()
    counter = int(current_time // interval)
    msg = struct.pack(">Q", counter)

    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    return str(code % (10**digits)).zfill(digits)


@dataclass(frozen=True)
class SmartAPICredentials:
    """
    Immutable container for Angel One SmartAPI credentials.
    Enforces strict masking on string conversion to prevent secret leakage in logs.
    """
    api_key: str
    client_code: str
    pin: str
    totp_secret: str

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise ValueError("INVALID_CREDENTIALS: api_key must be non-empty string.")
        if not self.client_code or not self.client_code.strip():
            raise ValueError("INVALID_CREDENTIALS: client_code must be non-empty string.")
        if not self.pin or not self.pin.strip():
            raise ValueError("INVALID_CREDENTIALS: pin must be non-empty string.")
        if not self.totp_secret or not self.totp_secret.strip():
            raise ValueError("INVALID_CREDENTIALS: totp_secret must be non-empty string.")

    @property
    def is_valid(self) -> bool:
        return bool(
            self.api_key.strip()
            and self.client_code.strip()
            and self.pin.strip()
            and self.totp_secret.strip()
        )

    def _mask_identifier(self, value: str) -> str:
        clean = value.strip()
        if len(clean) <= 4:
            return "***"
        return f"{clean[:2]}***{clean[-2:]}"

    def __repr__(self) -> str:
        return (
            f"<SmartAPICredentials client_code={self._mask_identifier(self.client_code)} "
            "api_key=[REDACTED] pin=[REDACTED] totp_secret=[REDACTED]>"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def from_env(cls, prefix: str = "SMARTAPI_") -> Optional["SmartAPICredentials"]:
        """
        Loads credentials from environment variables:
        - {prefix}API_KEY
        - {prefix}CLIENT_CODE
        - {prefix}PIN
        - {prefix}TOTP_SECRET

        Returns None if any required credential is missing.
        """
        api_key = os.getenv(f"{prefix}API_KEY")
        client_code = os.getenv(f"{prefix}CLIENT_CODE")
        pin = os.getenv(f"{prefix}PIN")
        totp_secret = os.getenv(f"{prefix}TOTP_SECRET")

        if not (api_key and client_code and pin and totp_secret):
            return None

        try:
            return cls(
                api_key=api_key.strip(),
                client_code=client_code.strip(),
                pin=pin.strip(),
                totp_secret=totp_secret.strip(),
            )
        except ValueError as err:
            logger.warning(f"[SmartAPICredentials] Incomplete or invalid credentials in environment: {err}")
            return None


@dataclass(frozen=True)
class SmartAPIAuthSession:
    """
    Session tokens returned by Angel One loginByPassword.
    """
    jwt_token: str
    refresh_token: str
    feed_token: str
    created_at: float = field(default_factory=time.time)
    expires_in_sec: float = 86400.0  # Daily session lifetime

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        now = current_time if current_time is not None else time.time()
        return (now - self.created_at) >= self.expires_in_sec

    def __repr__(self) -> str:
        return (
            f"<SmartAPIAuthSession created_at={self.created_at} "
            f"has_jwt={bool(self.jwt_token)} has_feed_token={bool(self.feed_token)}>"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def ws_headers(self, api_key: str, client_code: str) -> List[str]:
        """Returns header list formatted for websocket.WebSocketApp."""
        return [
            f"Authorization: Bearer {self.jwt_token}",
            f"x-api-key: {api_key}",
            f"x-client-code: {client_code}",
            f"x-feed-token: {self.feed_token}",
        ]

    def ws_headers_dict(self, api_key: str, client_code: str) -> Dict[str, str]:
        """Returns header dictionary formatted for standard HTTP/WS requests."""
        return {
            "Authorization": f"Bearer {self.jwt_token}",
            "x-api-key": api_key,
            "x-client-code": client_code,
            "x-feed-token": self.feed_token,
        }


class SmartAPIAuthError(Exception):
    """Raised when authentication against Angel One REST API fails."""
    def __init__(self, message: str, error_code: str = "AUTH_FAILED", status_code: Optional[int] = None):
        super().__init__(f"[{error_code}] {message}")
        self.error_code = error_code
        self.status_code = status_code


class SmartAPIAuthenticator:
    """
    Programmatic REST authenticator for Angel One SmartAPI.
    Executes loginByPassword with clientcode, MPIN, and computed TOTP.
    """

    AUTH_ENDPOINT: str = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"

    @classmethod
    def build_login_payload(
        cls,
        credentials: SmartAPICredentials,
        for_time: Optional[float] = None,
    ) -> Dict[str, str]:
        """Constructs the JSON request payload for loginByPassword."""
        totp_code = generate_totp(credentials.totp_secret, for_time=for_time)
        return {
            "clientcode": credentials.client_code,
            "password": credentials.pin,
            "totp": totp_code,
        }

    @classmethod
    def build_login_headers(cls, credentials: SmartAPICredentials) -> Dict[str, str]:
        """Constructs the HTTP request headers for loginByPassword."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "127.0.0.1",
            "X-PrivateKey": credentials.api_key,
        }

    @classmethod
    def authenticate(
        cls,
        credentials: SmartAPICredentials,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
        for_time: Optional[float] = None,
    ) -> SmartAPIAuthSession:
        """
        Performs authentication request to obtain jwtToken and feedToken.
        """
        payload = cls.build_login_payload(credentials, for_time=for_time)
        headers = cls.build_login_headers(credentials)

        requester = session if session is not None else requests

        try:
            response = requester.post(
                cls.AUTH_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:
            logger.error("[SmartAPIAuthenticator] Network connection failure during login: %s", type(exc).__name__)
            raise SmartAPIAuthError(
                f"Network request to SmartAPI login failed: {type(exc).__name__}",
                error_code="NETWORK_ERROR",
            ) from exc

        if response.status_code != 200:
            raise SmartAPIAuthError(
                f"SmartAPI login HTTP {response.status_code}",
                error_code="HTTP_ERROR",
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except Exception as exc:
            raise SmartAPIAuthError("Failed to parse JSON response from SmartAPI login", error_code="INVALID_RESPONSE") from exc

        status = body.get("status")
        if status is not True:
            err_msg = body.get("message", "Authentication rejected by SmartAPI")
            err_code = body.get("errorcode", "REJECTED")
            raise SmartAPIAuthError(f"{err_msg} ({err_code})", error_code=err_code)

        data = body.get("data")
        if not isinstance(data, dict):
            raise SmartAPIAuthError("Malformed response: 'data' object missing", error_code="INVALID_RESPONSE")

        jwt_token = data.get("jwtToken")
        feed_token = data.get("feedToken")
        refresh_token = data.get("refreshToken", "")

        if not jwt_token or not feed_token:
            raise SmartAPIAuthError("SmartAPI response missing jwtToken or feedToken", error_code="MISSING_TOKENS")

        logger.info(
            "[SmartAPIAuthenticator] Authentication successful for client %s",
            credentials.client_code[:2] + "***" if len(credentials.client_code) > 2 else "***",
        )

        return SmartAPIAuthSession(
            jwt_token=jwt_token,
            refresh_token=refresh_token,
            feed_token=feed_token,
            created_at=time.time(),
        )
