"""
Angel One SmartAPI Integration Package for Tradego.

Exposes:
- SmartAPIAdapter: BaseMarketDataProvider integration with MarketDataGateway.
- SmartAPIClient: SmartStream WebSocket 2.0 transport client.
- SmartAPIState: 11-phase connection lifecycle state enum.
- SmartAPINormalizer: Binary decoder for Modes 1, 2, 3.
- SmartAPICredentials: Protected credentials container.
- SmartAPIAuthSession: Session token container.
- SmartAPIAuthenticator: REST login helper.
- generate_totp: Standard RFC 6238 TOTP generator.
"""

from .adapter import SmartAPIAdapter
from .auth import (
    SmartAPIAuthError,
    SmartAPIAuthSession,
    SmartAPIAuthenticator,
    SmartAPICredentials,
    generate_totp,
)
from .client import SmartAPIClient, SmartAPIState
from .normalizer import (
    EXCHANGE_CODE_MAP,
    SmartAPINormalizer,
    SmartStreamExchange,
    SmartStreamMode,
)

__all__ = [
    "SmartAPIAdapter",
    "SmartAPIClient",
    "SmartAPIState",
    "SmartAPINormalizer",
    "SmartStreamExchange",
    "SmartStreamMode",
    "EXCHANGE_CODE_MAP",
    "SmartAPICredentials",
    "SmartAPIAuthSession",
    "SmartAPIAuthenticator",
    "SmartAPIAuthError",
    "generate_totp",
]
