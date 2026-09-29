"""
Unit tests for ATMStoxAdapter.
"""

import unittest
from unittest.mock import MagicMock

from services.market_gateway.atmstox.adapter import ATMStoxAdapter
from services.market_gateway.atmstox.client import ATMStoxClient
from services.market_gateway.base import ConnectionState


class TestATMStoxAdapter(unittest.TestCase):

    def setUp(self):
        # Create a mocked ATMStoxClient
        self.mock_client = MagicMock(spec=ATMStoxClient)
        self.mock_client.is_connected = False
        self.mock_client.state = ConnectionState.DISCONNECTED
        self.mock_client.subscribed_symbols = []

        # Store registered state callbacks
        self._state_callbacks = []
        self.mock_client.add_state_callback.side_effect = lambda cb: self._state_callbacks.append(cb)

        self.adapter = ATMStoxAdapter(client=self.mock_client)

    def test_adapter_properties(self):
        self.assertEqual(self.adapter.name, "ATMSTOX")
        self.assertFalse(self.adapter.is_connected)
        self.assertEqual(self.adapter.state, ConnectionState.DISCONNECTED)

    def test_connect_and_disconnect(self):
        self.adapter.connect(timeout=5.0)
        self.mock_client.connect.assert_called_once_with(timeout=5.0)

        self.adapter.disconnect()
        self.mock_client.disconnect.assert_called_once()

    def test_subscribe_and_unsubscribe(self):
        self.adapter.subscribe("13061")
        self.assertIn("13061", self.adapter.subscribed_symbols)
        self.mock_client.subscribe.assert_called_once()
        args, kwargs = self.mock_client.subscribe.call_args
        self.assertEqual(args[0], "13061")

        # Duplicate subscribe
        self.adapter.subscribe("13061")
        # Should not call client.subscribe a second time
        self.assertEqual(self.mock_client.subscribe.call_count, 1)

        # Unsubscribe
        self.adapter.unsubscribe("13061")
        self.assertNotIn("13061", self.adapter.subscribed_symbols)
        self.mock_client.unsubscribe.assert_called_once_with("13061")

    def test_state_callback_propagation(self):
        received_states = []
        self.adapter.set_state_callback(lambda s: received_states.append(s))

        # Simulate client state change
        for cb in self._state_callbacks:
            cb(ConnectionState.CONNECTED)

        self.assertIn(ConnectionState.CONNECTED, received_states)

    def test_tick_callback_forwarding(self):
        received_ticks = []
        self.adapter.set_tick_callback(
            lambda sym, raw, perf_time: received_ticks.append((sym, raw, perf_time))
        )

        self.adapter.subscribe("13061")
        # Extract the on_tick callback passed to mock_client.subscribe
        args, kwargs = self.mock_client.subscribe.call_args
        tick_cb = kwargs["callback"]

        raw_sample = {"LTP": 100.0}
        tick_cb(raw_sample, 123.456)

        self.assertEqual(len(received_ticks), 1)
        self.assertEqual(received_ticks[0][0], "13061")
        self.assertEqual(received_ticks[0][1], raw_sample)
        self.assertEqual(received_ticks[0][2], 123.456)


if __name__ == "__main__":
    unittest.main()
