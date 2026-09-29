"""
Unit tests for MarketDataGateway.
"""

import time
import unittest
from typing import Any, Callable, List, Optional, Set

from services.market_gateway.base import BaseMarketDataProvider, ConnectionState, StateCallback, TickCallback
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent


class MockMarketDataProvider(BaseMarketDataProvider):
    """
    In-memory mock market data provider for unit testing the gateway.
    """

    def __init__(self, name: str = "ATMSTOX"):
        self._name = name
        self._connected = False
        self._state = ConnectionState.DISCONNECTED
        self._subscribed: Set[str] = set()
        self._tick_cb: Optional[TickCallback] = None
        self._state_cb: Optional[StateCallback] = None

    @property
    def name(self) -> str:
        return self._name

    def connect(self, timeout: float = 10.0) -> None:
        self._connected = True
        self._state = ConnectionState.CONNECTED
        if self._state_cb:
            self._state_cb(ConnectionState.CONNECTED)

    def disconnect(self) -> None:
        self._connected = False
        self._state = ConnectionState.DISCONNECTED
        if self._state_cb:
            self._state_cb(ConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def state(self) -> ConnectionState:
        return self._state

    def subscribe(self, symbol_id: str) -> None:
        self._subscribed.add(symbol_id)

    def unsubscribe(self, symbol_id: str) -> None:
        self._subscribed.discard(symbol_id)

    @property
    def subscribed_symbols(self) -> Set[str]:
        return set(self._subscribed)

    def set_tick_callback(self, callback: TickCallback) -> None:
        self._tick_cb = callback

    def set_state_callback(self, callback: StateCallback) -> None:
        self._state_cb = callback

    # Helper to simulate receiving raw ticks from provider wire
    def emit_mock_tick(self, symbol_id: str, raw_payload: Any) -> None:
        if self._tick_cb:
            perf_now = time.perf_counter()
            self._tick_cb(symbol_id, raw_payload, perf_now)


class TestMarketDataGateway(unittest.TestCase):

    def setUp(self):
        self.mock_provider = MockMarketDataProvider(name="ATMSTOX")
        self.gateway = MarketDataGateway(providers=[self.mock_provider])

    def test_provider_registration_and_lifecycle(self):
        self.assertFalse(self.gateway.is_connected())
        self.gateway.start()
        self.assertTrue(self.gateway.is_connected())
        self.assertEqual(self.mock_provider.state, ConnectionState.CONNECTED)

        self.gateway.stop()
        self.assertFalse(self.gateway.is_connected())
        self.assertEqual(self.mock_provider.state, ConnectionState.DISCONNECTED)

    def test_subscription_management(self):
        self.gateway.subscribe("13061")
        self.assertIn("13061", self.gateway.subscribed_symbols)
        self.assertIn("13061", self.mock_provider.subscribed_symbols)

        # Duplicate subscription should be idempotent
        self.gateway.subscribe("13061")
        self.assertEqual(len(self.gateway.subscribed_symbols), 1)

        # Unsubscribe
        self.gateway.unsubscribe("13061")
        self.assertNotIn("13061", self.gateway.subscribed_symbols)
        self.assertNotIn("13061", self.mock_provider.subscribed_symbols)

        # Unsubscribing an unknown symbol should not raise an error
        try:
            self.gateway.unsubscribe("unknown_token_999")
        except Exception as e:
            self.fail(f"Unsubscribe of unknown symbol raised exception: {e}")

    def test_event_dispatch_to_listeners(self):
        received_events: List[MarketEvent] = []

        def on_event(ev: MarketEvent):
            received_events.append(ev)

        self.gateway.add_listener(on_event)

        raw_tick = {
            "LTP": "1250.0",
            "Bid": "1249.5",
            "Ask": "1250.5",
            "TickVolume": "100.5",
            "Previous_Open_Interest_Close": "5000.0",
        }

        self.mock_provider.emit_mock_tick("13061", raw_tick)

        self.assertEqual(len(received_events), 1)
        event = received_events[0]
        self.assertEqual(event.provider, "ATMSTOX")
        self.assertEqual(event.provider_symbol_id, "13061")
        self.assertEqual(event.ltp, 1250.0)
        self.assertEqual(event.tick_volume, 100.5)
        self.assertEqual(event.previous_open_interest_close, 5000.0)

        # Remove listener
        self.gateway.remove_listener(on_event)
        self.mock_provider.emit_mock_tick("13061", raw_tick)
        self.assertEqual(len(received_events), 1)

    def test_null_tick_dropped_metrics(self):
        received_events = []
        self.gateway.add_listener(lambda ev: received_events.append(ev))

        # Emit null tick
        self.mock_provider.emit_mock_tick("13061", None)

        self.assertEqual(len(received_events), 0)
        metrics = self.gateway.metrics.get_summary()
        self.assertEqual(metrics["total_ticks_received"], 1)
        self.assertEqual(metrics["total_ticks_dropped"], 1)
        self.assertEqual(metrics["total_ticks_normalized"], 0)

    def test_listener_error_isolation(self):
        received_in_second_listener: List[MarketEvent] = []

        def failing_listener(ev: MarketEvent):
            raise ValueError("Intentional failure in faulty listener")

        def working_listener(ev: MarketEvent):
            received_in_second_listener.append(ev)

        self.gateway.add_listener(failing_listener)
        self.gateway.add_listener(working_listener)

        raw_tick = {"LTP": "100.0"}
        self.mock_provider.emit_mock_tick("13061", raw_tick)

        # Faulty listener should not prevent working listener from receiving event
        self.assertEqual(len(received_in_second_listener), 1)
        self.assertEqual(self.gateway.metrics.total_errors, 1)


if __name__ == "__main__":
    unittest.main()
