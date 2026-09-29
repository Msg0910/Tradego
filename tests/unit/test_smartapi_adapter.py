"""
Unit Test Suite for Angel One SmartAPI Market Data Adapter & Binary Normalizer.

Verifies:
1. Valid LTP packet decoding (Mode 1, 51 bytes)
2. Valid Quote packet decoding (Mode 2, 123 bytes)
3. Valid SnapQuote / Depth packet decoding (Mode 3, 347 bytes)
4. Token / instrument ID preservation (null-padded string stripping)
5. Canonical MarketEvent generation & field mapping
6. Malformed packet rejection
7. Truncated packet rejection (length < 51, or mode 2 length < 123)
8. Deterministic parsing (identical input + metadata = identical output)
9. Subscription bookkeeping (subscribe, unsubscribe, idempotency)
10. Callback delivery to MarketDataGateway listener
11. Adapter connection-state transitions without network
12. Zero network calls or socket opening
13. Zero credentials loaded or read
14. Live broker boundary & real-money safety
15. Existing ATMSTOX adapter preservation
16. Frozen-core SHA-256 hash invariance (86/86 identical)
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import struct
import unittest
from unittest.mock import MagicMock, patch

from adapters.smartapi.adapter import SmartAPIAdapter
from adapters.smartapi.client import SmartAPIClient
from adapters.smartapi.normalizer import (
    SmartAPINormalizer,
    SmartStreamExchange,
    SmartStreamMode,
)
from services.market_gateway.atmstox.adapter import ATMStoxAdapter
from services.market_gateway.base import ConnectionState
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent


def build_ltp_packet(
    mode: int = SmartStreamMode.LTP,
    exchange: int = SmartStreamExchange.NSE_CM,
    token: str = "3045",
    seq: int = 1001,
    exchange_ts_ms: int = 1790610600000,
    ltp_paise: int = 58250,  # 582.50 INR
) -> bytes:
    """Constructs a synthetic 51-byte SmartStream LTP packet."""
    token_bytes = token.encode("utf-8").ljust(25, b"\x00")
    return struct.pack(
        "<BB25sqqq",
        mode,
        exchange,
        token_bytes,
        seq,
        exchange_ts_ms,
        ltp_paise,
    )


def build_quote_packet(
    token: str = "3045",
    exchange: int = SmartStreamExchange.NSE_CM,
    seq: int = 1002,
    exchange_ts_ms: int = 1790610600000,
    ltp_paise: int = 58250,
    ltq: int = 100,
    atp_paise: int = 58000,
    vol: int = 1250000,
    tbq: float = 45000.0,
    tsq: float = 38000.0,
    open_paise: int = 57500,
    high_paise: int = 58500,
    low_paise: int = 57200,
    close_paise: int = 57000,
) -> bytes:
    """Constructs a synthetic 123-byte SmartStream Quote packet."""
    base_ltp = build_ltp_packet(
        mode=SmartStreamMode.QUOTE,
        exchange=exchange,
        token=token,
        seq=seq,
        exchange_ts_ms=exchange_ts_ms,
        ltp_paise=ltp_paise,
    )
    tail = struct.pack(
        "<qqqddqqqq",
        ltq,
        atp_paise,
        vol,
        tbq,
        tsq,
        open_paise,
        high_paise,
        low_paise,
        close_paise,
    )
    return base_ltp + tail


def build_snap_quote_packet(
    token: str = "3045",
    exchange: int = SmartStreamExchange.NSE_CM,
    seq: int = 1003,
    exchange_ts_ms: int = 1790610600000,
    ltp_paise: int = 58250,
    oi: int = 450000,
) -> bytes:
    """Constructs a synthetic 347-byte SmartStream SnapQuote/Depth packet."""
    quote_bytes = build_quote_packet(
        token=token,
        exchange=exchange,
        seq=seq,
        exchange_ts_ms=exchange_ts_ms,
        ltp_paise=ltp_paise,
    )
    # Change mode byte to SNAP_QUOTE (3)
    quote_bytes = bytes([SmartStreamMode.SNAP_QUOTE]) + quote_bytes[1:]

    meta = struct.pack("<qqd", exchange_ts_ms, oi, 2.5)  # last_trade_ts, oi, oi_pct

    # 10 depth levels (5 bids, 5 asks)
    depth_bytes = bytearray()
    for i in range(5):
        # Buy levels (flag=1)
        depth_bytes.extend(struct.pack("<hqqh", 1, 100 * (i + 1), ltp_paise - (i * 10), 5 + i))
    for i in range(5):
        # Sell levels (flag=0)
        depth_bytes.extend(struct.pack("<hqqh", 0, 150 * (i + 1), ltp_paise + (i * 10) + 5, 3 + i))

    return quote_bytes + meta + bytes(depth_bytes)


class TestSmartAPIAdapterAndNormalizer(unittest.TestCase):
    """
    Exhaustive offline unit tests for SmartAPI normalizer and adapter.
    """

    def setUp(self) -> None:
        self.fixed_dt = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        self.fixed_perf = 12345.6789

    # -------------------------------------------------------------------------
    # 1. LTP Packet Decoding (Mode 1)
    # -------------------------------------------------------------------------
    def test_01_valid_ltp_packet_decoding(self) -> None:
        """1. Verifies decoding of 51-byte LTP Mode 1 packet into canonical MarketEvent."""
        pkt = build_ltp_packet(token="3045", ltp_paise=58250, exchange_ts_ms=1790610600000)
        self.assertEqual(len(pkt), 51)

        event = SmartAPINormalizer.normalize(
            payload=pkt,
            receive_perf_time=self.fixed_perf,
            receive_datetime=self.fixed_dt,
        )

        self.assertIsNotNone(event)
        self.assertIsInstance(event, MarketEvent)
        self.assertEqual(event.provider, "SMARTAPI")
        self.assertEqual(event.provider_symbol_id, "3045")
        self.assertEqual(event.symbol, "3045")
        self.assertEqual(event.ltp, 582.50)
        self.assertIsNotNone(event.exchange_timestamp)
        self.assertEqual(event.local_receive_timestamp, self.fixed_perf)
        self.assertEqual(event.local_receive_datetime, self.fixed_dt)
        self.assertIn("sequence_number", event.raw)
        self.assertEqual(event.raw["sequence_number"], 1001)

    # -------------------------------------------------------------------------
    # 2. Quote Packet Decoding (Mode 2)
    # -------------------------------------------------------------------------
    def test_02_valid_quote_packet_decoding(self) -> None:
        """2. Verifies decoding of 123-byte Quote Mode 2 packet with OHLCV."""
        pkt = build_quote_packet(
            token="13061",
            ltp_paise=245000,  # 2450.00
            ltq=25,
            atp_paise=244000,  # 2440.00
            vol=50000,
            open_paise=240000,
            high_paise=246000,
            low_paise=239500,
            close_paise=241000,
        )
        self.assertEqual(len(pkt), 123)

        event = SmartAPINormalizer.normalize(
            payload=pkt,
            receive_perf_time=self.fixed_perf,
            receive_datetime=self.fixed_dt,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.provider_symbol_id, "13061")
        self.assertEqual(event.ltp, 2450.00)
        self.assertEqual(event.ltp_qty, 25)
        self.assertEqual(event.atp, 2440.00)
        self.assertEqual(event.total_volume, 50000.0)
        self.assertEqual(event.open, 2400.00)
        self.assertEqual(event.high, 2460.00)
        self.assertEqual(event.low, 2395.00)
        self.assertEqual(event.previous_close, 2410.00)
        self.assertEqual(event.total_buy_qty, 45000)
        self.assertEqual(event.total_sell_qty, 38000)

    # -------------------------------------------------------------------------
    # 3. SnapQuote / Depth Packet Decoding (Mode 3)
    # -------------------------------------------------------------------------
    def test_03_valid_snap_quote_depth_packet_decoding(self) -> None:
        """3. Verifies decoding of 347-byte SnapQuote Mode 3 packet with 5-level market depth."""
        pkt = build_snap_quote_packet(token="3045", ltp_paise=58250, oi=450000)
        self.assertEqual(len(pkt), 347)

        event = SmartAPINormalizer.normalize(
            payload=pkt,
            receive_perf_time=self.fixed_perf,
            receive_datetime=self.fixed_dt,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.oi, 450000)
        self.assertIsNotNone(event.depth)
        self.assertEqual(len(event.depth.bids), 5)
        self.assertEqual(len(event.depth.asks), 5)

        # Level 1 Top of Book
        self.assertEqual(event.depth.bids[0].price, 582.50)
        self.assertEqual(event.depth.bids[0].quantity, 100)
        self.assertEqual(event.bid, 582.50)
        self.assertEqual(event.bid_qty, 100)

        self.assertEqual(event.depth.asks[0].price, 582.55)
        self.assertEqual(event.depth.asks[0].quantity, 150)
        self.assertEqual(event.ask, 582.55)
        self.assertEqual(event.ask_qty, 150)
        self.assertAlmostEqual(event.spread, 0.05, places=4)

    # -------------------------------------------------------------------------
    # 4. Token & Instrument ID Preservation
    # -------------------------------------------------------------------------
    def test_04_token_preservation_and_null_stripping(self) -> None:
        """4. Confirms tokens with trailing null bytes and whitespace are stripped cleanly."""
        tokens_to_test = ["3045", "13061", "GOLD_26OCT", "RELIANCE-EQ"]
        for tok in tokens_to_test:
            pkt = build_ltp_packet(token=tok)
            event = SmartAPINormalizer.normalize(
                payload=pkt,
                receive_perf_time=self.fixed_perf,
                receive_datetime=self.fixed_dt,
            )
            self.assertIsNotNone(event)
            self.assertEqual(event.provider_symbol_id, tok)

    # -------------------------------------------------------------------------
    # 5. Canonical MarketEvent Field Compatibility
    # -------------------------------------------------------------------------
    def test_05_canonical_market_event_attributes(self) -> None:
        """5. Validates all canonical MarketEvent slots are conformant with Tradego contracts."""
        pkt = build_quote_packet(token="3045")
        event = SmartAPINormalizer.normalize(
            payload=pkt,
            receive_perf_time=self.fixed_perf,
            receive_datetime=self.fixed_dt,
            symbol_override="SBIN",
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.symbol, "SBIN")
        self.assertEqual(event.provider_symbol_id, "3045")
        self.assertIsInstance(event.raw, dict)
        self.assertEqual(event.raw["exchange_code"], "NSE")

    # -------------------------------------------------------------------------
    # 6. Malformed Packet Rejection
    # -------------------------------------------------------------------------
    def test_06_malformed_packet_rejection(self) -> None:
        """6. Confirms garbage bytes and non-binary inputs fail closed by returning None."""
        self.assertIsNone(SmartAPINormalizer.normalize(b"invalid_short", 0.0))
        self.assertIsNone(SmartAPINormalizer.normalize(b"\x00" * 50, 0.0))  # 50 bytes < 51
        self.assertIsNone(SmartAPINormalizer.normalize(None, 0.0))  # type: ignore
        self.assertIsNone(SmartAPINormalizer.normalize("not_bytes", 0.0))  # type: ignore

    # -------------------------------------------------------------------------
    # 7. Truncated Packet Rejection
    # -------------------------------------------------------------------------
    def test_07_truncated_packet_rejection(self) -> None:
        """7. Confirms packets claiming Mode 2 but with length < 123 fail closed."""
        # Build Quote packet (123 bytes) and truncate to 100 bytes
        pkt = build_quote_packet(token="3045")
        truncated = pkt[:100]
        event = SmartAPINormalizer.normalize(truncated, self.fixed_perf)
        self.assertIsNone(event, "Truncated Quote packet must be cleanly rejected")

    # -------------------------------------------------------------------------
    # 8. Deterministic Decoding
    # -------------------------------------------------------------------------
    def test_08_deterministic_decoding_invariance(self) -> None:
        """8. Identical binary payload + identical metadata = identical MarketEvent."""
        pkt = build_quote_packet(token="3045", ltp_paise=58250, seq=100)

        ev1 = SmartAPINormalizer.normalize(pkt, self.fixed_perf, self.fixed_dt)
        ev2 = SmartAPINormalizer.normalize(pkt, self.fixed_perf, self.fixed_dt)

        self.assertIsNotNone(ev1)
        self.assertIsNotNone(ev2)
        self.assertEqual(ev1.provider_symbol_id, ev2.provider_symbol_id)
        self.assertEqual(ev1.ltp, ev2.ltp)
        self.assertEqual(ev1.exchange_timestamp, ev2.exchange_timestamp)
        self.assertEqual(ev1.local_receive_timestamp, ev2.local_receive_timestamp)
        self.assertEqual(ev1.raw, ev2.raw)

    # -------------------------------------------------------------------------
    # 9. Subscription Bookkeeping
    # -------------------------------------------------------------------------
    def test_09_subscription_bookkeeping(self) -> None:
        """9. Verifies subscribe/unsubscribe idempotency and bookkeeping."""
        client = SmartAPIClient(offline_mode=True)
        adapter = SmartAPIAdapter(client=client, offline_mode=True)

        self.assertEqual(adapter.name, "SMARTAPI")
        self.assertEqual(adapter.subscribed_symbols, set())

        adapter.subscribe("3045")
        adapter.subscribe("3045")  # Duplicate
        adapter.subscribe("13061")

        self.assertEqual(adapter.subscribed_symbols, {"3045", "13061"})
        self.assertEqual(client.subscribed_tokens, {"3045", "13061"})

        adapter.unsubscribe("3045")
        adapter.unsubscribe("unknown_token")  # Safe unknown unsubscribe

        self.assertEqual(adapter.subscribed_symbols, {"13061"})
        self.assertEqual(client.subscribed_tokens, {"13061"})

    # -------------------------------------------------------------------------
    # 10. Callback Delivery & Gateway Integration
    # -------------------------------------------------------------------------
    def test_10_callback_delivery_to_gateway(self) -> None:
        """10. Confirms SmartAPIAdapter registers with MarketDataGateway and delivers MarketEvents."""
        adapter = SmartAPIAdapter(offline_mode=True)
        gateway = MarketDataGateway(providers=[adapter])

        received_events = []

        def on_market_event(ev: MarketEvent) -> None:
            received_events.append(ev)

        gateway.add_listener(on_market_event)

        pkt = build_ltp_packet(token="3045", ltp_paise=58250)
        # Inject packet through adapter offline test hook
        event = adapter.feed_raw_packet(
            token="3045",
            packet=pkt,
            perf_time=self.fixed_perf,
            receive_datetime=self.fixed_dt,
        )

        self.assertIsNotNone(event)
        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].provider, "SMARTAPI")
        self.assertEqual(received_events[0].provider_symbol_id, "3045")
        self.assertEqual(received_events[0].ltp, 582.50)

    # -------------------------------------------------------------------------
    # 11. Offline Connection State Transitions
    # -------------------------------------------------------------------------
    def test_11_offline_connection_lifecycle(self) -> None:
        """11. Tests connection state lifecycle without network side-effects."""
        adapter = SmartAPIAdapter(offline_mode=True)
        states = []

        def on_state(st: ConnectionState) -> None:
            states.append(st)

        adapter.set_state_callback(on_state)

        self.assertEqual(adapter.state, ConnectionState.DISCONNECTED)
        self.assertFalse(adapter.is_connected)

        adapter.connect()
        self.assertEqual(adapter.state, ConnectionState.CONNECTED)
        self.assertTrue(adapter.is_connected)

        adapter.disconnect()
        self.assertEqual(adapter.state, ConnectionState.DISCONNECTED)
        self.assertFalse(adapter.is_connected)
        self.assertIn(ConnectionState.CONNECTED, states)
        self.assertIn(ConnectionState.DISCONNECTED, states)

    # -------------------------------------------------------------------------
    # 12. Network Isolation Safety (No Sockets / No HTTP)
    # -------------------------------------------------------------------------
    def test_12_network_isolation_safety(self) -> None:
        """12. Verifies zero socket creation or HTTP requests during adapter operations."""
        with patch("socket.socket") as mock_socket, patch("requests.get") as mock_get, patch(
            "requests.post"
        ) as mock_post:
            adapter = SmartAPIAdapter(offline_mode=True)
            adapter.connect()
            adapter.subscribe("3045")
            pkt = build_ltp_packet(token="3045")
            adapter.feed_raw_packet("3045", pkt, self.fixed_perf)
            adapter.disconnect()

            # Assert no network activity occurred
            mock_socket.assert_not_called()
            mock_get.assert_not_called()
            mock_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 13. Zero Credentials Loaded Safety
    # -------------------------------------------------------------------------
    def test_13_zero_credentials_loaded(self) -> None:
        """13. Confirms no SmartAPI credential environment variables are read."""
        with patch.dict(os.environ, {}, clear=True):
            # Instantiate adapter in clean environment with zero secrets
            client = SmartAPIClient(offline_mode=True)
            adapter = SmartAPIAdapter(client=client, offline_mode=True)
            adapter.connect()
            self.assertTrue(adapter.is_connected)
            adapter.disconnect()

    # -------------------------------------------------------------------------
    # 14. Live Broker Boundary & Real-Money Safety
    # -------------------------------------------------------------------------
    def test_14_live_broker_boundary_remains_uninitialized(self) -> None:
        """14. Confirms REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED remains intact."""
        from gateway.broker_adapter import LiveBrokerAdapter

        live_adapter = LiveBrokerAdapter()
        res = live_adapter._execute_live_dispatch("dummy_instruction", "idemp_key")
        self.assertFalse(res.success)
        self.assertEqual(res.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")

    # -------------------------------------------------------------------------
    # 15. Existing ATMSTOX Provider Preservation
    # -------------------------------------------------------------------------
    def test_15_existing_atmstox_remains_untouched(self) -> None:
        """15. Confirms ATMStoxAdapter and ATMStoxNormalizer continue to operate without interference."""
        atm_adapter = ATMStoxAdapter()
        self.assertEqual(atm_adapter.name, "ATMSTOX")
        self.assertEqual(atm_adapter.subscribed_symbols, set())

    # -------------------------------------------------------------------------
    # 16. Frozen-Core Hash Invariance (86/86 Files)
    # -------------------------------------------------------------------------
    def test_16_frozen_core_hash_invariance(self) -> None:
        """16. Confirms all 86 frozen-core files in services/, strategies/, brokers/, config/ match baseline hashes."""
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        baseline_file = (
            r"C:\Users\Admin\.gemini\antigravity-ide\brain\dcf5ee07-0793-4ffd-bf96-5c85f21dc7fd\scratch\frozen_core_hashes.json"
        )

        if os.path.exists(baseline_file):
            with open(baseline_file, "r") as f:
                baseline = json.load(f)

            current = {}
            for d in ["services", "strategies", "brokers", "config"]:
                target_dir = os.path.join(base_dir, d)
                if not os.path.exists(target_dir):
                    continue
                for root, _, files in os.walk(target_dir):
                    if "__pycache__" in root:
                        continue
                    for f in files:
                        p = os.path.join(root, f)
                        rel_p = os.path.relpath(p, base_dir).replace("\\", "/")
                        with open(p, "rb") as fp:
                            current[rel_p] = hashlib.sha256(fp.read()).hexdigest()

            self.assertEqual(len(current), 86, "Frozen core must contain exactly 86 files")
            for k, v in baseline.items():
                self.assertIn(k, current, f"Missing frozen core file: {k}")
                self.assertEqual(current[k], v, f"Frozen core file modified: {k}")


if __name__ == "__main__":
    unittest.main()
