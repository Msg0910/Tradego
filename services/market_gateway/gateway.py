"""
Core Market Data Gateway for Tradego.

Coordinates one or more market data providers, manages subscription lifecycle,
normalizes incoming raw ticks into canonical MarketEvent instances, collects
lightweight health metrics, and dispatches events to registered in-memory listeners.

CRITICAL ARCHITECTURAL CONSTRAINTS:
1. The critical tick path is:
   network tick -> parse -> normalize -> publish internal market event -> market state
2. ZERO blocking operations:
   Listeners MUST NOT execute database queries/writes, Kafka/Redis operations,
   HTTP calls, LLM/MCP tool calls, synchronous file writes, or print() statements.
3. Decoupled design:
   The gateway is provider-agnostic and broker-agnostic.
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .atmstox.normalizer import ATMStoxNormalizer
from .base import BaseMarketDataProvider, ConnectionState
from .metrics import FeedHealthMetrics
from .models import MarketEvent

logger = logging.getLogger("market_gateway")

MarketEventListener = Callable[[MarketEvent], None]


class MarketDataGateway:
    """
    Tradego Canonical Market Data Gateway.
    """

    def __init__(
        self,
        providers: Optional[List[BaseMarketDataProvider]] = None,
        default_provider_name: Optional[str] = None,
    ) -> None:
        self._providers: Dict[str, BaseMarketDataProvider] = {}
        self._default_provider_name = default_provider_name
        self._listeners: List[MarketEventListener] = []
        self._symbol_to_provider: Dict[str, str] = {}
        self._metrics = FeedHealthMetrics()

        if providers:
            for p in providers:
                self.register_provider(p)

    @property
    def metrics(self) -> FeedHealthMetrics:
        """Access the gateway's lightweight feed health & latency metrics."""
        return self._metrics

    def register_provider(self, provider: BaseMarketDataProvider) -> None:
        """
        Register a market data provider adapter with the gateway.
        """
        name = provider.name
        self._providers[name] = provider
        if not self._default_provider_name:
            self._default_provider_name = name

        # Wire the raw tick callback from the provider to the gateway's tick ingestor
        provider.set_tick_callback(
            lambda sym, raw, perf_time: self._on_provider_tick(name, sym, raw, perf_time)
        )

    def get_provider(self, name: Optional[str] = None) -> Optional[BaseMarketDataProvider]:
        """Get a registered provider by name, or the default provider."""
        p_name = name or self._default_provider_name
        if p_name:
            return self._providers.get(p_name)
        return None

    def add_listener(self, listener: MarketEventListener) -> None:
        """
        Register an in-memory listener to receive normalized MarketEvent instances.

        CRITICAL NOTICE:
        Registered listeners MUST NOT perform blocking I/O (database, Kafka,
        Redis, HTTP, LLM, MCP calls, or disk writes) or heavy computations.
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: MarketEventListener) -> None:
        """Unregister an in-memory listener."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def start(self, timeout: float = 10.0) -> None:
        """Connect all registered providers."""
        for name, provider in self._providers.items():
            logger.info(f"[MarketDataGateway] Connecting provider: {name}")
            try:
                provider.connect(timeout=timeout)
            except Exception as e:
                logger.error(f"[MarketDataGateway] Failed to connect provider {name}: {e}")

    def stop(self) -> None:
        """Disconnect all registered providers."""
        for name, provider in self._providers.items():
            logger.info(f"[MarketDataGateway] Disconnecting provider: {name}")
            try:
                provider.disconnect()
            except Exception as e:
                logger.error(f"[MarketDataGateway] Failed to disconnect provider {name}: {e}")

    def is_connected(self, provider_name: Optional[str] = None) -> bool:
        """Check if provider(s) are connected."""
        if provider_name:
            p = self._providers.get(provider_name)
            return p.is_connected if p else False
        return any(p.is_connected for p in self._providers.values())

    def subscribe(self, symbol_id: str, provider_name: Optional[str] = None) -> None:
        """
        Subscribe to an instrument symbol on the specified provider (or default provider).
        Manages subscription mapping and avoids duplicate subscriptions.
        """
        str_id = str(symbol_id).strip()
        if not str_id:
            return

        target_provider_name = provider_name or self._default_provider_name
        if not target_provider_name or target_provider_name not in self._providers:
            raise RuntimeError(
                f"Cannot subscribe to '{str_id}': No valid provider available."
            )

        provider = self._providers[target_provider_name]
        self._symbol_to_provider[str_id] = target_provider_name
        provider.subscribe(str_id)

    def unsubscribe(self, symbol_id: str) -> None:
        """
        Unsubscribe from an instrument symbol across providers.
        Idempotent: unknown symbols are handled gracefully without errors.
        """
        str_id = str(symbol_id).strip()
        provider_name = self._symbol_to_provider.pop(str_id, None)

        if provider_name and provider_name in self._providers:
            self._providers[provider_name].unsubscribe(str_id)
        else:
            # Fallback: unsubscribe from all providers if symbol mapping wasn't found
            for p in self._providers.values():
                p.unsubscribe(str_id)

    @property
    def subscribed_symbols(self) -> Set[str]:
        """Set of all currently subscribed symbols across registered providers."""
        symbols: Set[str] = set()
        for p in self._providers.values():
            symbols.update(p.subscribed_symbols)
        return symbols

    def _on_provider_tick(
        self, provider_name: str, symbol_id: str, raw_data: Any, receive_perf_time: float
    ) -> None:
        """
        Critical tick ingest handler.
        Executes:
        1. Metrics recording of raw arrival
        2. Normalization via provider-specific normalizer
        3. Metrics recording of normalization latency
        4. In-memory dispatch to registered listeners
        """
        wall_now = time.time()
        self._metrics.record_tick_received(symbol_id, receive_perf_time, wall_now)

        # Provider-specific normalization
        event: Optional[MarketEvent] = None
        if provider_name == "ATMSTOX":
            event = ATMStoxNormalizer.normalize(
                raw=raw_data,
                symbol_id=symbol_id,
                receive_perf_time=receive_perf_time,
            )
        else:
            # Generic fallback: if raw is already a MarketEvent
            if isinstance(raw_data, MarketEvent):
                event = raw_data

        if event is None:
            # Null tick, malformed tick, or unpopulated rate
            self._metrics.record_tick_dropped(symbol_id)
            return

        # Record gateway normalization processing latency
        process_ms = event.latency_gateway_process_ms or 0.0
        self._metrics.record_tick_normalized(symbol_id, process_ms)

        # Dispatch to in-memory listeners
        # Note: An isolated try/except prevents a single faulty listener from
        # killing the tick loop for other listeners.
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                self._metrics.record_error(symbol_id)
                logger.error(
                    f"[MarketDataGateway] Error in listener callback for {symbol_id}: {e}",
                    exc_info=True,
                )
