"""
Unit tests for Tradego Market Data Models.
"""

import unittest
from datetime import datetime, timezone

from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent


class TestMarketModels(unittest.TestCase):

    def test_depth_level_creation(self):
        dl = DepthLevel(price=100.5, quantity=50, orders=2)
        self.assertEqual(dl.price, 100.5)
        self.assertEqual(dl.quantity, 50)
        self.assertEqual(dl.orders, 2)

    def test_market_depth_creation(self):
        bids = [DepthLevel(price=100.0, quantity=10)]
        asks = [DepthLevel(price=101.0, quantity=20)]
        depth = MarketDepth(bids=bids, asks=asks, total_buy_qty=10, total_sell_qty=20)
        self.assertEqual(len(depth.bids), 1)
        self.assertEqual(len(depth.asks), 1)
        self.assertEqual(depth.total_buy_qty, 10)
        self.assertEqual(depth.total_sell_qty, 20)

    def test_market_event_defaults(self):
        now_dt = datetime.now(timezone.utc)
        event = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="13061",
            local_receive_timestamp=100.0,
            local_receive_datetime=now_dt,
            normalized_timestamp=100.001,
            ltp=150.0,
            tick_volume=12.5,  # float
            previous_open_interest_close=1000.0,
        )

        self.assertEqual(event.provider, "ATMSTOX")
        self.assertEqual(event.provider_symbol_id, "13061")
        self.assertIsNone(event.symbol)
        self.assertEqual(event.ltp, 150.0)
        self.assertEqual(event.tick_volume, 12.5)
        self.assertEqual(event.previous_open_interest_close, 1000.0)
        self.assertIsNone(event.raw)


if __name__ == "__main__":
    unittest.main()
