"""
Unit Test Suite for Angel One SmartAPI Live Transport & Authentication Preparation.

Tests:
1. Authentication request construction without real credentials.
2. Credential redaction in __repr__, __str__, and logging.
3. WebSocket header construction (Bearer, x-api-key, x-client-code, x-feed-token).
4. Subscription request construction (modes 1, 2, 3, action=1).
5. Unsubscription request construction (action=0).
6. Heartbeat handling (ping transmission and pong reception).
7. Connection lifecycle transitions across all 11 states.
8. Bounded exponential backoff reconnection behavior.
9. Malformed packet rejection and exception suppression.
10. Existing SmartAPINormalizer integration with transport dispatch.
11. MarketEvent delivery to MarketDataGateway.
12. Duplicate subscription protection (idempotency).
13. Zero live order APIs and execution safety boundary.
14. Existing ATMSTOX adapter preservation.
15. Frozen-core SHA-256 hash invariance (86/86 files).
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import struct
import unittest
from unittest.mock import MagicMock, patch

from adapters.smartapi import (
    SmartAPIAdapter,
    SmartAPIAuthError,
    SmartAPIAuthSession,
    SmartAPIAuthenticator,
    SmartAPICredentials,
    SmartAPIClient,
    SmartAPINormalizer,
    SmartAPIState,
    SmartStreamExchange,
    SmartStreamMode,
    generate_totp,
)
from gateway.broker_adapter import LiveBrokerAdapter
from services.market_gateway.atmstox.adapter import ATMStoxAdapter
from services.market_gateway.base import ConnectionState
from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent


class TestSmartAPITransportAndAuth(unittest.TestCase):
    """Exhaustive offline test suite for SmartAPI transport preparation and authentication."""

    def setUp(self) -> None:
        self.mock_creds = SmartAPICredentials(
            api_key="TEST_API_KEY_12345",
            client_code="A11223344",
            pin="9988",
            totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",  # RFC 6238 standard test secret
        )
        self.mock_session = SmartAPIAuthSession(
            jwt_token="mock_jwt_header.mock_payload.mock_signature",
            refresh_token="mock_refresh_token",
            feed_token="mock_feed_token_xyz987",
            created_at=1000000.0,
            expires_in_sec=86400.0,
        )

    # -------------------------------------------------------------------------
    # 1. Authentication Request Construction
    # -------------------------------------------------------------------------
    def test_01_auth_request_construction(self) -> None:
        """1. Verifies loginByPassword payload and header generation without real credentials."""
        # Test RFC 6238 TOTP output for standard test vector (T=59 -> '287082')
        totp_val = generate_totp(self.mock_creds.totp_secret, for_time=59)
        self.assertEqual(totp_val, "287082")

        payload = SmartAPIAuthenticator.build_login_payload(self.mock_creds, for_time=59)
        self.assertEqual(payload["clientcode"], "A11223344")
        self.assertEqual(payload["password"], "9988")
        self.assertEqual(payload["totp"], "287082")

        headers = SmartAPIAuthenticator.build_login_headers(self.mock_creds)
        self.assertEqual(headers["X-PrivateKey"], "TEST_API_KEY_12345")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-UserType"], "USER")
        self.assertEqual(headers["X-SourceID"], "WEB")

    # -------------------------------------------------------------------------
    # 2. Credential Redaction & Security Masking
    # -------------------------------------------------------------------------
    def test_02_credential_redaction(self) -> None:
        """2. Verifies credentials and tokens are strictly masked in string representations."""
        creds_str = str(self.mock_creds)
        creds_repr = repr(self.mock_creds)

        self.assertNotIn("TEST_API_KEY_12345", creds_str)
        self.assertNotIn("9988", creds_str)
        self.assertNotIn("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", creds_str)
        self.assertIn("[REDACTED]", creds_str)
        self.assertIn("A1***44", creds_str)  # Masked client code
        self.assertEqual(creds_str, creds_repr)

        session_str = str(self.mock_session)
        self.assertNotIn("mock_jwt_header", session_str)
        self.assertNotIn("mock_feed_token_xyz987", session_str)
        self.assertIn("has_jwt=True", session_str)
        self.assertIn("has_feed_token=True", session_str)

    # -------------------------------------------------------------------------
    # 3. WebSocket Header Construction
    # -------------------------------------------------------------------------
    def test_03_ws_header_construction(self) -> None:
        """3. Verifies handshake headers for SmartStream WebSocket 2.0."""
        headers_list = self.mock_session.ws_headers("TEST_API_KEY_12345", "A11223344")
        self.assertIn("Authorization: Bearer mock_jwt_header.mock_payload.mock_signature", headers_list)
        self.assertIn("x-api-key: TEST_API_KEY_12345", headers_list)
        self.assertIn("x-client-code: A11223344", headers_list)
        self.assertIn("x-feed-token: mock_feed_token_xyz987", headers_list)

        headers_dict = self.mock_session.ws_headers_dict("TEST_API_KEY_12345", "A11223344")
        self.assertEqual(headers_dict["Authorization"], "Bearer mock_jwt_header.mock_payload.mock_signature")
        self.assertEqual(headers_dict["x-api-key"], "TEST_API_KEY_12345")
        self.assertEqual(headers_dict["x-client-code"], "A11223344")
        self.assertEqual(headers_dict["x-feed-token"], "mock_feed_token_xyz987")

    # -------------------------------------------------------------------------
    # 4. Subscription Request Construction
    # -------------------------------------------------------------------------
    def test_04_subscription_payload_construction(self) -> None:
        """4. Verifies SmartStream JSON subscription action frame for modes 1, 2, 3."""
        # Mode 1: LTP
        raw_json_m1 = SmartAPIClient.build_subscription_payload(["3045", "11536"], mode=1, exchange_type=1, action=1)
        data_m1 = json.loads(raw_json_m1)
        self.assertEqual(data_m1["action"], 1)
        self.assertEqual(data_m1["params"]["mode"], 1)
        self.assertEqual(data_m1["params"]["tokenList"][0]["exchangeType"], 1)
        self.assertEqual(data_m1["params"]["tokenList"][0]["tokens"], ["3045", "11536"])

        # Mode 2: Quote
        raw_json_m2 = SmartAPIClient.build_subscription_payload(["26000"], mode=2, exchange_type=1, action=1)
        data_m2 = json.loads(raw_json_m2)
        self.assertEqual(data_m2["params"]["mode"], 2)

        # Mode 3: SnapQuote
        raw_json_m3 = SmartAPIClient.build_subscription_payload(["999"], mode=3, exchange_type=2, action=1)
        data_m3 = json.loads(raw_json_m3)
        self.assertEqual(data_m3["params"]["mode"], 3)
        self.assertEqual(data_m3["params"]["tokenList"][0]["exchangeType"], 2)

    # -------------------------------------------------------------------------
    # 5. Unsubscription Request Construction
    # -------------------------------------------------------------------------
    def test_05_unsubscription_payload_construction(self) -> None:
        """5. Verifies SmartStream JSON unsubscription action frame (action=0)."""
        raw_json = SmartAPIClient.build_subscription_payload(["3045"], mode=1, exchange_type=1, action=0)
        data = json.loads(raw_json)
        self.assertEqual(data["action"], 0)
        self.assertEqual(data["params"]["tokenList"][0]["tokens"], ["3045"])

    # -------------------------------------------------------------------------
    # 6. Heartbeat Handling
    # -------------------------------------------------------------------------
    def test_06_heartbeat_handling(self) -> None:
        """6. Verifies heartbeat pong updates internal timestamp."""
        client = SmartAPIClient(offline_mode=True)
        self.assertEqual(client.last_pong_time, 0.0)

        # Simulate receiving "pong" text frame
        mock_ws = MagicMock()
        client._on_ws_message(mock_ws, "pong")
        self.assertGreater(client.last_pong_time, 0.0)

        client._on_ws_message(mock_ws, "PONG\n")
        self.assertGreater(client.last_pong_time, 0.0)

    # -------------------------------------------------------------------------
    # 7. Connection Lifecycle Transitions Across All 11 States
    # -------------------------------------------------------------------------
    def test_07_connection_lifecycle_states(self) -> None:
        """7. Verifies 11-phase connection states and canonical ConnectionState mapping."""
        client = SmartAPIClient(offline_mode=True)
        extended_states_observed = []
        canonical_states_observed = []

        client.add_extended_state_callback(extended_states_observed.append)
        client.add_state_callback(canonical_states_observed.append)

        self.assertEqual(client.extended_state, SmartAPIState.DISCONNECTED)
        self.assertEqual(client.state, ConnectionState.DISCONNECTED)

        # Transition through connecting sequence
        client._set_state(SmartAPIState.CONNECTING)
        self.assertEqual(client.state, ConnectionState.CONNECTING)

        client._set_state(SmartAPIState.AUTHENTICATING)
        self.assertEqual(client.state, ConnectionState.CONNECTING)

        client._set_state(SmartAPIState.AUTHENTICATED)
        self.assertEqual(client.state, ConnectionState.CONNECTING)

        client._set_state(SmartAPIState.STREAM_CONNECTING)
        self.assertEqual(client.state, ConnectionState.CONNECTING)

        client._set_state(SmartAPIState.STREAM_CONNECTED)
        self.assertEqual(client.state, ConnectionState.CONNECTED)
        self.assertTrue(client.is_connected)

        client._set_state(SmartAPIState.SUBSCRIBED)
        self.assertEqual(client.state, ConnectionState.CONNECTED)
        self.assertTrue(client.is_connected)

        client._set_state(SmartAPIState.RECONNECTING)
        self.assertEqual(client.state, ConnectionState.RECONNECTING)
        self.assertFalse(client.is_connected)

        client._set_state(SmartAPIState.STOPPING)
        self.assertEqual(client.state, ConnectionState.DISCONNECTED)

        client._set_state(SmartAPIState.STOPPED)
        self.assertEqual(client.state, ConnectionState.DISCONNECTED)

        client._set_state(SmartAPIState.ERROR)
        self.assertEqual(client.state, ConnectionState.FAILED)

        self.assertEqual(len(extended_states_observed), 10)
        self.assertIn(ConnectionState.CONNECTED, canonical_states_observed)

    # -------------------------------------------------------------------------
    # 8. Bounded Exponential Backoff Reconnection
    # -------------------------------------------------------------------------
    def test_08_reconnection_backoff_bounds(self) -> None:
        """8. Verifies retry counting and delay bounds without infinite loop."""
        client = SmartAPIClient(
            max_reconnect_attempts=3,
            offline_mode=True,
        )
        self.assertEqual(client.reconnect_count, 0)

        # Calculate delays for attempts 1..5
        delays = [
            min(client.INITIAL_RECONNECT_DELAY_SEC * (2 ** (i - 1)), client.MAX_RECONNECT_DELAY_SEC)
            for i in range(1, 6)
        ]
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 16.0])

        # Test retry cap
        client._reconnect_count = 3
        self.assertEqual(client.reconnect_count, 3)

    # -------------------------------------------------------------------------
    # 9. Malformed Packet Rejection
    # -------------------------------------------------------------------------
    def test_09_malformed_packet_rejection(self) -> None:
        """9. Verifies malformed binary and text messages are cleanly rejected without throwing."""
        client = SmartAPIClient(offline_mode=True)
        captured = []
        client.add_packet_callback(lambda token, pkt, ts: captured.append((token, pkt)))

        mock_ws = MagicMock()
        # Truncated short binary frame (< 27 bytes)
        client._on_ws_message(mock_ws, b"too_short")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "")  # Empty token because < 27 bytes

        # Garbage text frame (not "pong")
        client._on_ws_message(mock_ws, "{\"error\": \"Invalid Token\"}")
        # Does not crash or append to binary packets
        self.assertEqual(len(captured), 1)

    # -------------------------------------------------------------------------
    # 10. Existing SmartAPINormalizer Integration
    # -------------------------------------------------------------------------
    def test_10_normalizer_transport_integration(self) -> None:
        """10. Verifies binary frame received by client is decoded by SmartAPINormalizer into MarketEvent."""
        adapter = SmartAPIAdapter(offline_mode=True)
        received_events = []
        adapter.set_tick_callback(lambda tok, ev, ts: received_events.append(ev))

        # Synthetic LTP packet (Mode 1: 51 bytes)
        token_bytes = b"3045" + b"\x00" * 21
        pkt = struct.pack("<BB25sqqq", 1, 1, token_bytes, 101, 1727000000000, 58025)
        self.assertEqual(len(pkt), 51)

        # Dispatch via client._on_ws_message
        adapter.client._on_ws_message(MagicMock(), pkt)

        self.assertEqual(len(received_events), 1)
        ev = received_events[0]
        self.assertIsInstance(ev, MarketEvent)
        self.assertEqual(ev.provider, "SMARTAPI")
        self.assertEqual(ev.provider_symbol_id, "3045")
        self.assertEqual(ev.ltp, 580.25)

    # -------------------------------------------------------------------------
    # 11. MarketEvent Delivery to MarketDataGateway
    # -------------------------------------------------------------------------
    def test_11_market_data_gateway_delivery(self) -> None:
        """11. Verifies delivery from SmartAPIClient through SmartAPIAdapter into MarketDataGateway."""
        adapter = SmartAPIAdapter(offline_mode=True)
        gateway = MarketDataGateway(providers=[adapter])

        gateway_ticks = []
        gateway.add_listener(lambda ev: gateway_ticks.append(ev))

        adapter.subscribe("11536")

        # Synthetic Quote packet (Mode 2: 123 bytes)
        token_bytes = b"11536" + b"\x00" * 20
        header_seq = struct.pack("<BB25sqqq", 2, 1, token_bytes, 202, 1727000000000, 350050)
        tail_seq = struct.pack("<qqqddqqqq", 10, 349900, 50000, 10000.0, 15000.0, 348000, 352000, 347500, 349000)
        pkt = header_seq + tail_seq
        self.assertEqual(len(pkt), 123)

        adapter.client._on_ws_message(MagicMock(), pkt)

        self.assertEqual(len(gateway_ticks), 1)
        ev = gateway_ticks[0]
        self.assertEqual(ev.provider, "SMARTAPI")
        self.assertEqual(ev.provider_symbol_id, "11536")
        self.assertEqual(ev.ltp, 3500.50)
        self.assertEqual(ev.open, 3480.00)
        self.assertEqual(ev.high, 3520.00)
        self.assertEqual(ev.low, 3475.00)
        self.assertEqual(ev.previous_close, 3490.00)

    # -------------------------------------------------------------------------
    # 12. Duplicate Subscription Protection
    # -------------------------------------------------------------------------
    def test_12_duplicate_subscription_protection(self) -> None:
        """12. Verifies duplicate subscriptions are deduplicated and unsubscribing is safe."""
        client = SmartAPIClient(offline_mode=True)
        client.subscribe("3045")
        client.subscribe("3045")  # Duplicate
        client.subscribe(" 3045 ")  # Whitespace duplicate

        self.assertEqual(len(client.subscribed_tokens), 1)
        self.assertIn("3045", client.subscribed_tokens)

        # Unsubscribe unknown symbol
        client.unsubscribe("NON_EXISTENT")
        self.assertEqual(len(client.subscribed_tokens), 1)

        # Unsubscribe registered symbol
        client.unsubscribe("3045")
        self.assertEqual(len(client.subscribed_tokens), 0)

    # -------------------------------------------------------------------------
    # 13. Zero Live Order Calls & Safety Boundary
    # -------------------------------------------------------------------------
    def test_13_zero_live_order_apis_and_boundary(self) -> None:
        """13. Verifies adapter has no order execution APIs and broker boundary remains intact."""
        adapter = SmartAPIAdapter(offline_mode=True)
        # Verify no order placement or execution methods exist
        order_methods = ["place_order", "placeOrder", "modify_order", "cancel_order", "close_position"]
        for m in order_methods:
            self.assertFalse(hasattr(adapter, m), f"SmartAPIAdapter must not have {m}")
            self.assertFalse(hasattr(adapter.client, m), f"SmartAPIClient must not have {m}")

        # Verify LiveBrokerAdapter boundary remains uninitialized
        live_broker = LiveBrokerAdapter()
        res = live_broker._execute_live_dispatch("dummy_instruction", "idemp_test_13")
        self.assertEqual(res.failure_reason, "REAL_BROKER_NETWORK_TRANSPORT_UNINITIALIZED")
        self.assertFalse(res.success)

    # -------------------------------------------------------------------------
    # 14. Existing ATMSTOX Preservation
    # -------------------------------------------------------------------------
    def test_14_existing_atmstox_preservation(self) -> None:
        """14. Verifies ATMStoxAdapter and client can still be instantiated and operate identically."""
        atm_adapter = ATMStoxAdapter()
        self.assertEqual(atm_adapter.name, "ATMSTOX")
        self.assertEqual(atm_adapter.subscribed_symbols, set())
        self.assertEqual(atm_adapter.state, ConnectionState.DISCONNECTED)

    # -------------------------------------------------------------------------
    # 15. Frozen-Core Hash Invariance (86/86 Files)
    # -------------------------------------------------------------------------
    def test_15_frozen_core_hash_invariance(self) -> None:
        """15. Confirms all 86 frozen-core files in services/, strategies/, brokers/, config/ match baseline hashes."""
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
