"""
ATMSTOX Provider Adapter for Tradego Market Data Gateway.

Implements BaseMarketDataProvider to decouple ATMSTOX Socket.IO specifics
from the core Tradego market data gateway.
"""

import logging
import threading
from typing import Any, Callable, Optional, Set

from ..base import BaseMarketDataProvider, ConnectionState, StateCallback, TickCallback
from .client import ATMStoxClient

logger = logging.getLogger("market_gateway.atmstox.adapter")


class ATMStoxAdapter(BaseMarketDataProvider):
    """
    Adapter implementing BaseMarketDataProvider for ATMSTOX feed.
    """

    def __init__(
        self,
        endpoint: str = "https://atmstox.com:20100",
        reconnection: bool = True,
        reconnection_attempts: Optional[int] = None,
        logger_enabled: bool = False,
        client: Optional[ATMStoxClient] = None,
    ) -> None:
        self._endpoint = endpoint
        self._lock = threading.Lock()
        self._subscribed_symbols: Set[str] = set()
        self._tick_callback: Optional[TickCallback] = None
        self._state_callback: Optional[StateCallback] = None

        if client is not None:
            self._client = client
        else:
            self._client = ATMStoxClient(
                endpoint=self._endpoint,
                reconnection=reconnection,
                reconnection_attempts=reconnection_attempts,
                logger_enabled=logger_enabled,
            )

        self._client.add_state_callback(self._on_client_state_changed)

    @property
    def name(self) -> str:
        return "ATMSTOX"

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def state(self) -> ConnectionState:
        return self._client.state

    def connect(self, timeout: float = 10.0) -> None:
        """Establish connection to ATMSTOX."""
        self._client.connect(timeout=timeout)

    def disconnect(self) -> None:
        """Gracefully disconnect from ATMSTOX."""
        self._client.disconnect()

    def subscribe(self, symbol_id: str) -> None:
        """
        Subscribe to live tick updates for an instrument.
        Idempotent: duplicate calls do not create redundant handlers.
        """
        str_id = str(symbol_id).strip()
        if not str_id:
            return

        with self._lock:
            already_subscribed = str_id in self._subscribed_symbols
            self._subscribed_symbols.add(str_id)

        if not already_subscribed:
            # Register internal forwarder that passes raw payload + receive timestamp
            def on_tick(raw_data: Any, perf_time: float, *args):
                if self._tick_callback:
                    try:
                        self._tick_callback(str_id, raw_data, perf_time)
                    except Exception as e:
                        logger.error(f"[ATMStoxAdapter] Error in tick callback for {str_id}: {e}", exc_info=True)

            self._client.subscribe(str_id, callback=on_tick)

    def unsubscribe(self, symbol_id: str) -> None:
        """
        Unsubscribe from live tick updates for an instrument.
        Idempotent: unsubscribing an unknown symbol does not crash.
        """
        str_id = str(symbol_id).strip()
        with self._lock:
            if str_id in self._subscribed_symbols:
                self._subscribed_symbols.remove(str_id)
        self._client.unsubscribe(str_id)

    @property
    def subscribed_symbols(self) -> Set[str]:
        with self._lock:
            return set(self._subscribed_symbols)

    def set_tick_callback(self, callback: TickCallback) -> None:
        """
        Register callback for raw ticks.
        WARNING: The registered callback MUST NOT perform blocking I/O.
        """
        self._tick_callback = callback

    def set_state_callback(self, callback: StateCallback) -> None:
        """Register callback for connection state updates."""
        self._state_callback = callback

    def _on_client_state_changed(self, new_state: ConnectionState) -> None:
        if self._state_callback:
            try:
                self._state_callback(new_state)
            except Exception as e:
                logger.error(f"[ATMStoxAdapter] Error in state callback: {e}", exc_info=True)
