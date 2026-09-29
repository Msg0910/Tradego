"""
Integration test for ATMSTOX live market data feed.

SAFETY & ISOLATION RULES:
1. This test does NOT place any orders or perform any trading.
2. It is strictly read-only market data streaming.
3. It does NOT run automatically during normal unit test execution.
   It is skipped unless the environment variable TRADEGO_LIVE_TEST=1 is set.
4. No credentials or secrets are used or hardcoded.
"""

import os
import time
import unittest
from typing import List

from services.market_gateway.atmstox.adapter import ATMStoxAdapter
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent


@unittest.skipUnless(
    os.getenv("TRADEGO_LIVE_TEST") in ("1", "true", "TRUE"),
    "Live ATMSTOX integration tests skipped by default. Set TRADEGO_LIVE_TEST=1 to run.",
)
class TestATMStoxLiveIntegration(unittest.TestCase):

    def setUp(self):
        self.adapter = ATMStoxAdapter(endpoint="https://atmstox.com:20100", logger_enabled=False)
        self.gateway = MarketDataGateway(providers=[self.adapter])

    def tearDown(self):
        self.gateway.stop()

    def test_live_stream_normalization(self):
        received_events: List[MarketEvent] = []

        def on_event(ev: MarketEvent):
            received_events.append(ev)

        self.gateway.add_listener(on_event)

        # Connect to live feed
        self.gateway.start(timeout=10.0)
        self.assertTrue(self.gateway.is_connected())

        # Subscribe to known active token: 13061 (360ONE) or 466583 (GOLD MCX)
        test_token = "13061"
        self.gateway.subscribe(test_token)

        # Wait up to 15 seconds for a live market event
        deadline = time.time() + 15.0
        while time.time() < deadline and not received_events:
            time.sleep(0.5)

        # If market is open or quote is returned, verify normalized structure
        if received_events:
            ev = received_events[0]
            self.assertEqual(ev.provider, "ATMSTOX")
            self.assertEqual(ev.provider_symbol_id, test_token)
            self.assertIsNotNone(ev.local_receive_datetime)
            self.assertIsNotNone(ev.raw)
            # If LTP is populated, check that it's a non-negative float (may be 0.0 outside market hours)
            if ev.ltp is not None:
                self.assertGreaterEqual(ev.ltp, 0.0)
            # If tick_volume is populated, check that it's float
            if ev.tick_volume is not None:
                self.assertIsInstance(ev.tick_volume, float)

        # Unsubscribe and clean up
        self.gateway.unsubscribe(test_token)
        self.assertNotIn(test_token, self.gateway.subscribed_symbols)


if __name__ == "__main__":
    # Force run when executed directly as a script
    os.environ["TRADEGO_LIVE_TEST"] = "1"
    unittest.main()
