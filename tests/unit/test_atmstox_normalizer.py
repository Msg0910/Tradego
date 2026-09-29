"""
Unit tests for ATMStoxNormalizer.
"""

import time
import unittest
from datetime import datetime, timezone

from services.market_gateway.atmstox.normalizer import (
    ATMStoxNormalizer,
    _safe_float,
    _safe_int,
)
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent


class TestATMStoxNormalizer(unittest.TestCase):

    def setUp(self):
        # Sample realistic ATMSTOX payload containing all 28+ confirmed fields
        self.sample_full_payload = {
            "symbol_id": "13061",
            "LTP": "1250.50",
            "LTPQty": "25",
            "TickVolume": "8026.99",  # Fractional volume as confirmed in ATMSTOX
            "ATP": "1248.20",
            "TotalVolume": "542310.0",
            "Open": "1240.00",
            "High": "1255.00",
            "Low": "1238.50",
            "Previous_Close": "1242.00",
            "Today_OI": "154000",
            "Previous_Open_Interest_Close": "150000.5",  # Semantic float
            "Total_Buy": "125400",
            "Total_Sell": "98200",
            "Bid": "1250.25",
            "BidQty": "100",
            "Bid1": "1250.25",
            "BidQty1": "100",
            "Bid2": "1250.00",
            "BidQty2": "250",
            "Bid3": "1249.75",
            "BidQty3": "500",
            "Bid4": "1249.50",
            "BidQty4": "750",
            "Ask": "1250.75",
            "AskQty": "150",
            "Ask1": "1250.75",
            "AskQty1": "150",
            "Ask2": "1251.00",
            "AskQty2": "300",
            "Ask3": "1251.25",
            "AskQty3": "450",
            "Ask4": "1251.50",
            "AskQty4": "600",
            "UC": "1366.20",
            "LC": "1117.80",
            "HIGH52": "1420.00",
            "LOW52": "950.00",
            "digit": "2",
            "LastUpdateTime": "12-09-2026 13:45:00",
            "LastExchangeUpdateTime": "12-09-2026 13:44:59",
            "Timestamp": "12-09-2026 13:45:00",
            "Time": "13:45:00",
        }

    def test_full_payload_normalization(self):
        recv_perf = time.perf_counter()
        recv_dt = datetime(2026, 9, 12, 13, 45, 1, tzinfo=timezone.utc)

        event = ATMStoxNormalizer.normalize(
            raw=self.sample_full_payload,
            symbol_id="13061",
            receive_perf_time=recv_perf,
            receive_datetime=recv_dt,
        )

        self.assertIsNotNone(event)
        assert event is not None

        # Provider identification
        self.assertEqual(event.provider, "ATMSTOX")
        self.assertEqual(event.provider_symbol_id, "13061")

        # Prices & Quantities
        self.assertEqual(event.ltp, 1250.50)
        self.assertEqual(event.ltp_qty, 25)
        self.assertEqual(event.bid, 1250.25)
        self.assertEqual(event.bid_qty, 100)
        self.assertEqual(event.ask, 1250.75)
        self.assertEqual(event.ask_qty, 150)
        self.assertAlmostEqual(event.spread, 0.50)

        # Fractional volume verification
        self.assertIsInstance(event.tick_volume, float)
        self.assertEqual(event.tick_volume, 8026.99)
        self.assertEqual(event.total_volume, 542310.0)

        # Session Stats & Circuits
        self.assertEqual(event.atp, 1248.20)
        self.assertEqual(event.open, 1240.00)
        self.assertEqual(event.high, 1255.00)
        self.assertEqual(event.low, 1238.50)
        self.assertEqual(event.previous_close, 1242.00)
        self.assertEqual(event.upper_circuit, 1366.20)
        self.assertEqual(event.lower_circuit, 1117.80)
        self.assertEqual(event.high_52, 1420.00)
        self.assertEqual(event.low_52, 950.00)
        self.assertEqual(event.digit, 2)

        # OI and Semantic previous_open_interest_close
        self.assertEqual(event.oi, 154000)
        self.assertIsInstance(event.previous_open_interest_close, float)
        self.assertEqual(event.previous_open_interest_close, 150000.5)

        # Raw provider timestamps preserved untouched
        self.assertEqual(event.raw_last_update_time, "12-09-2026 13:45:00")
        self.assertEqual(event.raw_last_exchange_update_time, "12-09-2026 13:44:59")
        self.assertEqual(event.raw_timestamp, "12-09-2026 13:45:00")

        # Parsed timestamps distinct
        self.assertIsNotNone(event.exchange_timestamp)
        self.assertIsNotNone(event.provider_timestamp)
        self.assertEqual(event.exchange_timestamp.second, 59)
        self.assertEqual(event.provider_timestamp.second, 0)
        self.assertEqual(event.local_receive_datetime, recv_dt)

        # Latencies
        self.assertIsNotNone(event.latency_provider_to_receive_ms)
        self.assertIsNotNone(event.latency_gateway_process_ms)
        self.assertGreaterEqual(event.latency_gateway_process_ms, 0.0)

        # Raw payload preserved without alteration
        self.assertIs(event.raw, self.sample_full_payload)

    def test_market_depth_levels(self):
        recv_perf = time.perf_counter()
        event = ATMStoxNormalizer.normalize(
            raw=self.sample_full_payload,
            symbol_id="13061",
            receive_perf_time=recv_perf,
        )

        self.assertIsNotNone(event)
        self.assertIsNotNone(event.depth)
        depth = event.depth

        # Check 4 bid levels
        self.assertEqual(len(depth.bids), 4)
        self.assertEqual(depth.bids[0], DepthLevel(price=1250.25, quantity=100))
        self.assertEqual(depth.bids[1], DepthLevel(price=1250.00, quantity=250))
        self.assertEqual(depth.bids[2], DepthLevel(price=1249.75, quantity=500))
        self.assertEqual(depth.bids[3], DepthLevel(price=1249.50, quantity=750))

        # Check 4 ask levels
        self.assertEqual(len(depth.asks), 4)
        self.assertEqual(depth.asks[0], DepthLevel(price=1250.75, quantity=150))
        self.assertEqual(depth.asks[1], DepthLevel(price=1251.00, quantity=300))
        self.assertEqual(depth.asks[2], DepthLevel(price=1251.25, quantity=450))
        self.assertEqual(depth.asks[3], DepthLevel(price=1251.50, quantity=600))

        # Check depth totals
        self.assertEqual(depth.total_buy_qty, 125400)
        self.assertEqual(depth.total_sell_qty, 98200)

    def test_null_and_malformed_payloads(self):
        recv_perf = time.perf_counter()

        # None payload
        self.assertIsNone(ATMStoxNormalizer.normalize(None, "13061", recv_perf))

        # Empty dictionary
        self.assertIsNone(ATMStoxNormalizer.normalize({}, "13061", recv_perf))

        # String payload
        self.assertIsNone(ATMStoxNormalizer.normalize("invalid", "13061", recv_perf))

        # List payload
        self.assertIsNone(ATMStoxNormalizer.normalize([1, 2, 3], "13061", recv_perf))

        # Dict with only empty / null prices
        self.assertIsNone(
            ATMStoxNormalizer.normalize({"LTP": None, "Bid": None, "Ask": None}, "13061", recv_perf)
        )

    def test_missing_fields_defaults(self):
        recv_perf = time.perf_counter()
        minimal_payload = {"LTP": 500.0}

        event = ATMStoxNormalizer.normalize(minimal_payload, "9999", recv_perf)
        self.assertIsNotNone(event)
        self.assertEqual(event.ltp, 500.0)
        self.assertIsNone(event.bid)
        self.assertIsNone(event.ask)
        self.assertIsNone(event.depth)
        self.assertIsNone(event.tick_volume)
        self.assertIsNone(event.previous_open_interest_close)
        self.assertIsNone(event.raw_last_update_time)

    def test_midpoint_price_fallback(self):
        recv_perf = time.perf_counter()
        payload_no_ltp = {
            "Bid": "100.0",
            "Ask": "102.0",
        }
        event = ATMStoxNormalizer.normalize(payload_no_ltp, "123", recv_perf)
        self.assertIsNotNone(event)
        self.assertEqual(event.ltp, 101.0)
        self.assertEqual(event.spread, 2.0)

    def test_safe_converters(self):
        self.assertEqual(_safe_float("123.45"), 123.45)
        self.assertEqual(_safe_float("  50  "), 50.0)
        self.assertIsNone(_safe_float(""))
        self.assertIsNone(_safe_float("null"))
        self.assertIsNone(_safe_float(None))
        self.assertIsNone(_safe_float("not-a-number"))

        self.assertEqual(_safe_int("123"), 123)
        self.assertEqual(_safe_int("123.0"), 123)
        self.assertEqual(_safe_int(456.7), 456)
        self.assertIsNone(_safe_int(""))
        self.assertIsNone(_safe_int("null"))
        self.assertIsNone(_safe_int(None))
        self.assertIsNone(_safe_int("abc"))


if __name__ == "__main__":
    unittest.main()
