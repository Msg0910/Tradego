"""
Base interfaces, protocols, and connection states for Tradego Market Data Gateway.

CRITICAL PERFORMANCE & ARCHITECTURE RULE:
Tick callbacks are invoked directly in the feed receiving/processing path.
Registered callbacks and downstream handlers MUST NOT perform blocking I/O
(such as database queries/writes, Kafka/Redis operations, HTTP network calls,
LLM or MCP tool calls, synchronous file writes, or heavy CPU computations).
Doing so introduces latency spikes and drops network buffers.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Set


class ConnectionState(str, Enum):
    """
    Standard connection lifecycle states for market data providers.
    """
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


# Tick callback signature:
# callback(symbol_id: str, raw_payload: Any, receive_perf_time: float) -> None
# Note: receive_perf_time is obtained via time.perf_counter() immediately upon arrival.
TickCallback = Callable[[str, Any, float], None]

# State callback signature:
# callback(state: ConnectionState) -> None
StateCallback = Callable[[ConnectionState], None]


class BaseMarketDataProvider(ABC):
    """
    Abstract contract that all market data providers (ATMSTOX, etc.) must implement.

    Decouples Tradego's internal gateway from any specific broker, protocol,
    or external market data vendor.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique human-readable identifier for this provider (e.g. 'ATMSTOX')."""
        pass

    @abstractmethod
    def connect(self, timeout: float = 10.0) -> None:
        """Establish connection to the market data feed."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Gracefully disconnect from the market data feed."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if currently connected to the market data feed."""
        pass

    @property
    @abstractmethod
    def state(self) -> ConnectionState:
        """Current ConnectionState of the provider."""
        pass

    @abstractmethod
    def subscribe(self, symbol_id: str) -> None:
        """
        Subscribe to live tick updates for the given instrument/token ID.
        Must be idempotent and manage subscription state.
        """
        pass

    @abstractmethod
    def unsubscribe(self, symbol_id: str) -> None:
        """
        Unsubscribe from live tick updates for the given instrument/token ID.
        Must be idempotent.
        """
        pass

    @property
    @abstractmethod
    def subscribed_symbols(self) -> Set[str]:
        """Set of currently subscribed symbol IDs."""
        pass

    @abstractmethod
    def set_tick_callback(self, callback: TickCallback) -> None:
        """
        Register the primary tick callback for raw incoming payloads.

        WARNING: The registered callback MUST NOT perform blocking I/O
        (database, Kafka, Redis, HTTP, LLM, MCP calls, or disk writes).
        """
        pass

    @abstractmethod
    def set_state_callback(self, callback: StateCallback) -> None:
        """Register a callback for connection state changes."""
        pass
