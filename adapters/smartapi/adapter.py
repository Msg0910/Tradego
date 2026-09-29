"""
Angel One SmartAPI Market Data Adapter for Tradego.

Implements BaseMarketDataProvider to seamlessly integrate SmartAPI's
SmartStream binary feed into the canonical Tradego MarketDataGateway.

CRITICAL SAFETY & GOVERNANCE RULES:
1. STRICTLY READ-ONLY MARKET DATA NORMALIZATION & DISPATCH.
2. ZERO IMPACT ON ORDER EXECUTION OR LIVE BROKER BOUNDARY.
3. INVARIANT PRESERVED: REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED.
"""

from datetime import datetime, timezone
import logging
import threading
from typing import Any, Callable, Dict, Optional, Set

from services.market_gateway.base import (
    BaseMarketDataProvider,
    ConnectionState,
    StateCallback,
    TickCallback,
)
from services.market_gateway.models import MarketEvent

from .auth import SmartAPIAuthSession, SmartAPICredentials
from .client import SmartAPIClient, SmartAPIState
from .normalizer import SmartAPINormalizer

logger = logging.getLogger("adapters.smartapi.adapter")


class SmartAPIAdapter(BaseMarketDataProvider):
    """
    Tradego Canonical Adapter for Angel One SmartStream feed.

    Implements BaseMarketDataProvider to decouple SmartStream specifics
    from the core Tradego pipeline.
    """

    def __init__(
        self,
        endpoint: str = SmartAPIClient.SMARTSTREAM_ENDPOINT,
        client: Optional[SmartAPIClient] = None,
        credentials: Optional[SmartAPICredentials] = None,
        auth_session: Optional[SmartAPIAuthSession] = None,
        offline_mode: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._subscribed_symbols: Set[str] = set()
        self._tick_callback: Optional[TickCallback] = None
        self._state_callback: Optional[StateCallback] = None
        self._extended_state_callback: Optional[Callable[[SmartAPIState], None]] = None
        self._offline_mode = offline_mode

        if client is not None:
            self._client = client
        else:
            self._client = SmartAPIClient(
                endpoint=endpoint,
                credentials=credentials,
                auth_session=auth_session,
                offline_mode=self._offline_mode,
            )

        # Wire client packet callback to internal normalizer handler
        self._client.add_packet_callback(self._on_client_packet)
        self._client.add_state_callback(self._on_client_state_changed)
        self._client.add_extended_state_callback(self._on_client_extended_state_changed)

    @property
    def name(self) -> str:
        return "SMARTAPI"

    @property
    def client(self) -> SmartAPIClient:
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def state(self) -> ConnectionState:
        """Canonical BaseMarketDataProvider ConnectionState."""
        return self._client.state

    @property
    def extended_state(self) -> SmartAPIState:
        """Fine-grained 11-phase SmartAPI lifecycle state."""
        return self._client.extended_state

    def connect(self, timeout: float = 10.0) -> None:
        """Establish connection to SmartStream."""
        self._client.connect(timeout=timeout)

    def disconnect(self) -> None:
        """Gracefully disconnect from SmartStream."""
        self._client.disconnect()

    def subscribe(self, symbol_id: str) -> None:
        """
        Subscribe to live updates for an instrument token.
        Idempotent: duplicate calls do not create redundant subscriptions.
        """
        str_id = str(symbol_id).strip()
        if not str_id:
            return

        with self._lock:
            self._subscribed_symbols.add(str_id)

        self._client.subscribe(str_id)

    def unsubscribe(self, symbol_id: str) -> None:
        """
        Unsubscribe from live updates for an instrument token.
        Idempotent: unsubscribing unknown symbols does not crash.
        """
        str_id = str(symbol_id).strip()
        with self._lock:
            self._subscribed_symbols.discard(str_id)

        self._client.unsubscribe(str_id)

    @property
    def subscribed_symbols(self) -> Set[str]:
        with self._lock:
            return set(self._subscribed_symbols)

    def set_tick_callback(self, callback: TickCallback) -> None:
        """
        Register the primary tick callback from MarketDataGateway.
        WARNING: The registered callback must NOT perform blocking I/O.
        """
        self._tick_callback = callback

    def set_state_callback(self, callback: StateCallback) -> None:
        """Register a callback for canonical connection lifecycle updates."""
        self._state_callback = callback

    def set_extended_state_callback(self, callback: Callable[[SmartAPIState], None]) -> None:
        """Register a callback for detailed 11-phase lifecycle updates."""
        self._extended_state_callback = callback

    def _on_client_state_changed(self, new_state: ConnectionState) -> None:
        if self._state_callback:
            try:
                self._state_callback(new_state)
            except Exception as e:
                logger.error(f"[SmartAPIAdapter] Error in state callback: {e}", exc_info=True)

    def _on_client_extended_state_changed(self, new_state: SmartAPIState) -> None:
        if self._extended_state_callback:
            try:
                self._extended_state_callback(new_state)
            except Exception as e:
                logger.error(f"[SmartAPIAdapter] Error in extended state callback: {e}", exc_info=True)

    def _on_client_packet(self, token: str, raw_bytes: bytes, perf_time: float) -> None:
        """
        Internal packet receiver:
        1. Normalizes binary packet into canonical MarketEvent.
        2. Dispatches MarketEvent to registered MarketDataGateway tick callback.
        """
        if not self._tick_callback:
            return

        # Normalization
        event = SmartAPINormalizer.normalize(
            payload=raw_bytes,
            receive_perf_time=perf_time,
            receive_datetime=datetime.now(timezone.utc),
        )

        if event is None:
            # Drop malformed or truncated packet without raising
            return

        try:
            # Pass canonical MarketEvent as raw_payload so MarketDataGateway
            # generic fallback ingests it directly via isinstance(raw, MarketEvent)
            self._tick_callback(token, event, perf_time)
        except Exception as e:
            logger.error(f"[SmartAPIAdapter] Error in tick callback for {token}: {e}", exc_info=True)

    def feed_raw_packet(
        self,
        token: str,
        packet: bytes,
        perf_time: float,
        receive_datetime: Optional[datetime] = None,
    ) -> Optional[MarketEvent]:
        """
        Deterministic testing hook:
        Injects a raw packet, normalizes it deterministically, and dispatches to listeners.
        Returns the generated MarketEvent (or None if packet was rejected).
        """
        event = SmartAPINormalizer.normalize(
            payload=packet,
            receive_perf_time=perf_time,
            receive_datetime=receive_datetime,
        )

        if event is not None and self._tick_callback:
            try:
                self._tick_callback(token, event, perf_time)
            except Exception as e:
                logger.error(f"[SmartAPIAdapter] Error in tick callback during feed_raw_packet: {e}", exc_info=True)

        return event
