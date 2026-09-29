"""
Unit tests for OrderBookState.
"""

import unittest

from services.market_gateway.models import DepthLevel, MarketDepth
from services.market_state.order_book import OrderBookState


class TestOrderBookState(unittest.TestCase):

    def test_order_book_from_market_depth(self):
        bids = [
            DepthLevel(price=100.0, quantity=50),
            DepthLevel(price=99.5, quantity=100),
            DepthLevel(price=99.0, quantity=150),
            DepthLevel(price=98.5, quantity=200),
        ]
        asks = [
            DepthLevel(price=100.5, quantity=60),
            DepthLevel(price=101.0, quantity=120),
            DepthLevel(price=101.5, quantity=180),
            DepthLevel(price=102.0, quantity=240),
        ]
        depth = MarketDepth(bids=bids, asks=asks, total_buy_qty=500, total_sell_qty=600)

        ob = OrderBookState.from_market_depth(depth)

        self.assertEqual(len(ob.bids), 4)
        self.assertEqual(len(ob.asks), 4)
        self.assertEqual(ob.best_bid, 100.0)
        self.assertEqual(ob.best_bid_qty, 50)
        self.assertEqual(ob.best_ask, 100.5)
        self.assertEqual(ob.best_ask_qty, 60)
        self.assertAlmostEqual(ob.spread, 0.5)
        self.assertAlmostEqual(ob.mid_price, 100.25)

        # Weighted mid: (100.0 * 60 + 100.5 * 50) / 110 = (6000 + 5025) / 110 = 11025 / 110 = 100.22727...
        self.assertAlmostEqual(ob.weighted_mid_price, (100.0 * 60 + 100.5 * 50) / 110.0)

        # Imbalance: (500 - 600) / (500 + 600) = -100 / 1100 = -0.090909...
        self.assertAlmostEqual(ob.book_imbalance, (500 - 600) / 1100.0)

    def test_empty_and_one_sided_books(self):
        empty_ob = OrderBookState()
        self.assertIsNone(empty_ob.best_bid)
        self.assertIsNone(empty_ob.best_ask)
        self.assertIsNone(empty_ob.spread)
        self.assertIsNone(empty_ob.mid_price)
        self.assertIsNone(empty_ob.book_imbalance)

        bids_only = OrderBookState(bids=(DepthLevel(price=100.0, quantity=10),))
        self.assertEqual(bids_only.best_bid, 100.0)
        self.assertIsNone(bids_only.best_ask)
        self.assertIsNone(bids_only.spread)
        self.assertIsNone(bids_only.mid_price)


if __name__ == "__main__":
    unittest.main()
