"""
Angel One SmartStream (WebSocket 2.0) Client Abstraction.

Implements the transport layer for Angel One's binary streaming feed:
- 11-phase connection lifecycle (DISCONNECTED -> AUTHENTICATING -> ... -> STREAM_CONNECTED)
- Dual state model: canonical ConnectionState and extended SmartAPIState
- Subscription / unsubscription JSON message serialization
- Heartbeat / ping-pong handling (30s interval)
- Bounded exponential backoff reconnection
- Offline packet injection harness for deterministic testing
- Live websocket.WebSocketApp integration

CRITICAL SAFETY & GOVERNANCE RULES:
1. Zero live order execution capabilities.
2. Market-data ingestion ONLY.
3. No hardcoded credentials.
4. Clean exception handling and bounded reconnects.
"""

from enum import Enum
import json
import logging
import math
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import websocket

from services.market_gateway.base import ConnectionState
from .auth import (
    SmartAPIAuthError,
    SmartAPIAuthSession,
    SmartAPIAuthenticator,
    SmartAPICredentials,
)

logger = logging.getLogger("adapters.smartapi.client")

# Callback signatures
PacketCallback = Callable[[str, bytes, float], None]
ClientStateCallback = Callable[[ConnectionState], None]
ExtendedStateCallback = Callable[["SmartAPIState"], None]


class SmartAPIState(str, Enum):
    """Fine-grained 11-phase connection lifecycle state."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    STREAM_CONNECTING = "STREAM_CONNECTING"
    STREAM_CONNECTED = "STREAM_CONNECTED"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


_CANONICAL_STATE_MAP: Dict[SmartAPIState, ConnectionState] = {
    SmartAPIState.DISCONNECTED: ConnectionState.DISCONNECTED,
    SmartAPIState.CONNECTING: ConnectionState.CONNECTING,
    SmartAPIState.AUTHENTICATING: ConnectionState.CONNECTING,
    SmartAPIState.AUTHENTICATED: ConnectionState.CONNECTING,
    SmartAPIState.STREAM_CONNECTING: ConnectionState.CONNECTING,
    SmartAPIState.STREAM_CONNECTED: ConnectionState.CONNECTED,
    SmartAPIState.SUBSCRIBED: ConnectionState.CONNECTED,
    SmartAPIState.RECONNECTING: ConnectionState.RECONNECTING,
    SmartAPIState.STOPPING: ConnectionState.DISCONNECTED,
    SmartAPIState.STOPPED: ConnectionState.DISCONNECTED,
    SmartAPIState.ERROR: ConnectionState.FAILED,
}


class SmartAPIClient:
    """
    Client transport abstraction for Angel One SmartStream (WebSocket 2.0).

    Supports both offline simulation (Step 1) and live WebSocket streaming (Step 2).
    """

    SMARTSTREAM_ENDPOINT: str = "wss://smartapisocket.angelone.in/smart-stream"
    HEARTBEAT_INTERVAL_SEC: float = 30.0
    MAX_RECONNECT_ATTEMPTS: int = 5
    INITIAL_RECONNECT_DELAY_SEC: float = 1.0
    MAX_RECONNECT_DELAY_SEC: float = 16.0

    def __init__(
        self,
        endpoint: str = SMARTSTREAM_ENDPOINT,
        credentials: Optional[SmartAPICredentials] = None,
        auth_session: Optional[SmartAPIAuthSession] = None,
        authenticator: Optional[SmartAPIAuthenticator] = None,
        reconnection: bool = True,
        max_reconnect_attempts: int = MAX_RECONNECT_ATTEMPTS,
        offline_mode: bool = True,
        default_mode: int = 1,          # 1 = LTP, 2 = Quote, 3 = SnapQuote
        default_exchange_type: int = 1, # 1 = NSE_CM
    ) -> None:
        self._endpoint = endpoint
        self._credentials = credentials
        self._auth_session = auth_session
        self._authenticator = authenticator or SmartAPIAuthenticator()
        self._reconnection = reconnection
        self._max_reconnect_attempts = max_reconnect_attempts
        self._offline_mode = offline_mode
        self._default_mode = default_mode
        self._default_exchange_type = default_exchange_type

        self._lock = threading.Lock()
        self._state = SmartAPIState.DISCONNECTED
        self._subscribed_tokens: Set[str] = set()
        self._packet_callbacks: List[PacketCallback] = []
        self._state_callbacks: List[ClientStateCallback] = []
        self._extended_state_callbacks: List[ExtendedStateCallback] = []

        # Transport internals
        self._ws: Optional[websocket.WebSocketApp] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconnect_count = 0
        self._last_pong_time: float = 0.0
        self._last_ping_time: float = 0.0

    # -------------------------------------------------------------------------
    # Public Properties
    # -------------------------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def credentials(self) -> Optional[SmartAPICredentials]:
        return self._credentials

    @property
    def auth_session(self) -> Optional[SmartAPIAuthSession]:
        with self._lock:
            return self._auth_session

    @property
    def extended_state(self) -> SmartAPIState:
        with self._lock:
            return self._state

    @property
    def state(self) -> ConnectionState:
        """Returns canonical ConnectionState mapped from extended SmartAPIState."""
        with self._lock:
            return _CANONICAL_STATE_MAP[self._state]

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._state in (SmartAPIState.STREAM_CONNECTED, SmartAPIState.SUBSCRIBED)

    @property
    def subscribed_tokens(self) -> Set[str]:
        with self._lock:
            return set(self._subscribed_tokens)

    @property
    def reconnect_count(self) -> int:
        with self._lock:
            return self._reconnect_count

    @property
    def last_pong_time(self) -> float:
        with self._lock:
            return self._last_pong_time

    # -------------------------------------------------------------------------
    # State Management & Callbacks
    # -------------------------------------------------------------------------

    def _set_state(self, new_state: SmartAPIState) -> None:
        with self._lock:
            old_ext_state = self._state
            self._state = new_state
            canonical_state = _CANONICAL_STATE_MAP[new_state]
            old_canonical_state = _CANONICAL_STATE_MAP[old_ext_state]
            state_cbs = list(self._state_callbacks)
            ext_cbs = list(self._extended_state_callbacks)

        logger.info(f"[SmartAPIClient] State changed: {old_ext_state.value} -> {new_state.value}")

        # Trigger extended state callbacks
        for ecb in ext_cbs:
            try:
                ecb(new_state)
            except Exception as e:
                logger.error(f"[SmartAPIClient] Error in extended state callback: {e}", exc_info=True)

        # Trigger canonical state callbacks if canonical state shifted
        if canonical_state != old_canonical_state:
            for scb in state_cbs:
                try:
                    scb(canonical_state)
                except Exception as e:
                    logger.error(f"[SmartAPIClient] Error in state callback: {e}", exc_info=True)

    def add_packet_callback(self, callback: PacketCallback) -> None:
        with self._lock:
            if callback not in self._packet_callbacks:
                self._packet_callbacks.append(callback)

    def remove_packet_callback(self, callback: PacketCallback) -> None:
        with self._lock:
            if callback in self._packet_callbacks:
                self._packet_callbacks.remove(callback)

    def add_state_callback(self, callback: ClientStateCallback) -> None:
        with self._lock:
            if callback not in self._state_callbacks:
                self._state_callbacks.append(callback)

    def add_extended_state_callback(self, callback: ExtendedStateCallback) -> None:
        with self._lock:
            if callback not in self._extended_state_callbacks:
                self._extended_state_callbacks.append(callback)

    # -------------------------------------------------------------------------
    # Subscription Framing (JSON Action Packets)
    # -------------------------------------------------------------------------

    @classmethod
    def build_subscription_payload(
        cls,
        tokens: List[str],
        mode: int = 1,
        exchange_type: int = 1,
        action: int = 1,
    ) -> str:
        """
        Constructs SmartStream JSON action packet:
        action: 1 = subscribe, 0 = unsubscribe
        mode: 1 = LTP, 2 = Quote, 3 = SnapQuote
        exchangeType: 1 = NSE_CM, 2 = NSE_FO, etc.
        """
        clean_tokens = [str(t).strip() for t in tokens if str(t).strip()]
        payload = {
            "action": action,
            "params": {
                "mode": mode,
                "tokenList": [
                    {
                        "exchangeType": exchange_type,
                        "tokens": clean_tokens,
                    }
                ],
            },
        }
        return json.dumps(payload)

    # -------------------------------------------------------------------------
    # Connection Lifecycle: Connect / Disconnect / Subscribe
    # -------------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """
        Establishes connection to Angel One SmartStream.
        - In offline_mode=True: Transitions deterministically without opening sockets.
        - In offline_mode=False: Authenticates if needed and opens WebSocket.
        """
        self._stop_event.clear()
        self._set_state(SmartAPIState.CONNECTING)

        if self._offline_mode:
            # Deterministic offline activation for Step 1 safety
            self._set_state(SmartAPIState.AUTHENTICATING)
            self._set_state(SmartAPIState.AUTHENTICATED)
            self._set_state(SmartAPIState.STREAM_CONNECTING)
            self._set_state(SmartAPIState.STREAM_CONNECTED)
            if self._subscribed_tokens:
                self._set_state(SmartAPIState.SUBSCRIBED)
            logger.info("[SmartAPIClient] Connected in offline simulation mode.")
            return

        # Live Transport Pathway
        self._start_live_connection()

    def disconnect(self) -> None:
        """Gracefully disconnects the client and stops all worker threads."""
        self._set_state(SmartAPIState.STOPPING)
        self._stop_event.set()

        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception as e:
                logger.debug(f"[SmartAPIClient] Error closing websocket: {e}")

        # Wait for threads to cleanly finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)

        self._set_state(SmartAPIState.STOPPED)
        self._set_state(SmartAPIState.DISCONNECTED)
        logger.info("[SmartAPIClient] Gracefully disconnected.")

    def subscribe(
        self,
        token: str,
        mode: Optional[int] = None,
        exchange_type: Optional[int] = None,
    ) -> None:
        """
        Subscribes to an instrument token.
        Idempotent: duplicate tokens do not generate redundant frames.
        """
        token_clean = str(token).strip()
        if not token_clean:
            return

        with self._lock:
            already_subscribed = token_clean in self._subscribed_tokens
            self._subscribed_tokens.add(token_clean)
            is_active = self._state in (SmartAPIState.STREAM_CONNECTED, SmartAPIState.SUBSCRIBED)
            ws = self._ws

        if already_subscribed:
            logger.debug(f"[SmartAPIClient] Duplicate subscription for token {token_clean} ignored.")
            return

        logger.debug(f"[SmartAPIClient] Subscribed to token: {token_clean}")

        if is_active:
            self._set_state(SmartAPIState.SUBSCRIBED)

        # In live mode with open socket, transmit subscription frame
        if not self._offline_mode and ws and is_active:
            sub_frame = self.build_subscription_payload(
                tokens=[token_clean],
                mode=mode or self._default_mode,
                exchange_type=exchange_type or self._default_exchange_type,
                action=1,
            )
            try:
                ws.send(sub_frame)
                logger.debug(f"[SmartAPIClient] Transmitted subscription frame: {sub_frame}")
            except Exception as e:
                logger.error(f"[SmartAPIClient] Failed to send subscription frame: {e}")

    def unsubscribe(
        self,
        token: str,
        mode: Optional[int] = None,
        exchange_type: Optional[int] = None,
    ) -> None:
        """
        Unsubscribes from an instrument token.
        Idempotent: unsubscribing unknown tokens does not error.
        """
        token_clean = str(token).strip()
        with self._lock:
            was_subscribed = token_clean in self._subscribed_tokens
            self._subscribed_tokens.discard(token_clean)
            ws = self._ws
            is_active = self._state in (SmartAPIState.STREAM_CONNECTED, SmartAPIState.SUBSCRIBED)
            remaining = len(self._subscribed_tokens)

        if not was_subscribed:
            return

        logger.debug(f"[SmartAPIClient] Unsubscribed from token: {token_clean}")

        if is_active and remaining == 0:
            self._set_state(SmartAPIState.STREAM_CONNECTED)

        # In live mode with open socket, transmit unsubscription frame
        if not self._offline_mode and ws and is_active:
            unsub_frame = self.build_subscription_payload(
                tokens=[token_clean],
                mode=mode or self._default_mode,
                exchange_type=exchange_type or self._default_exchange_type,
                action=0,
            )
            try:
                ws.send(unsub_frame)
                logger.debug(f"[SmartAPIClient] Transmitted unsubscription frame: {unsub_frame}")
            except Exception as e:
                logger.error(f"[SmartAPIClient] Failed to send unsubscription frame: {e}")

    # -------------------------------------------------------------------------
    # Offline Testing & Packet Injection
    # -------------------------------------------------------------------------

    def feed_packet(self, token: str, packet: bytes, perf_time: float) -> None:
        """
        Offline packet injection hook for tests and simulations.
        Dispatches incoming binary packet to all registered packet callbacks.
        """
        with self._lock:
            cbs = list(self._packet_callbacks)

        for cb in cbs:
            try:
                cb(token, packet, perf_time)
            except Exception as e:
                logger.error(f"[SmartAPIClient] Error in packet callback for {token}: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Live Transport Implementation
    # -------------------------------------------------------------------------

    def _start_live_connection(self) -> None:
        """Authenticates session and launches the background WebSocket worker."""
        # 1. Authenticate if no valid session
        if self._auth_session is None or self._auth_session.is_expired():
            if self._credentials is None:
                self._set_state(SmartAPIState.ERROR)
                raise SmartAPIAuthError("Credentials missing; cannot authenticate live SmartStream.", error_code="NO_CREDENTIALS")

            self._set_state(SmartAPIState.AUTHENTICATING)
            try:
                session = self._authenticator.authenticate(self._credentials)
                with self._lock:
                    self._auth_session = session
                self._set_state(SmartAPIState.AUTHENTICATED)
            except Exception as exc:
                self._set_state(SmartAPIState.ERROR)
                raise

        # 2. Launch background WebSocket thread
        self._set_state(SmartAPIState.STREAM_CONNECTING)
        self._worker_thread = threading.Thread(
            target=self._ws_run_loop,
            name="SmartStream-WSWorker",
            daemon=True,
        )
        self._worker_thread.start()

    def _ws_run_loop(self) -> None:
        """Worker thread executing websocket run_forever with bounded backoff."""
        while not self._stop_event.is_set():
            with self._lock:
                session = self._auth_session
                creds = self._credentials

            if not session or not creds:
                self._set_state(SmartAPIState.ERROR)
                break

            headers = session.ws_headers(creds.api_key, creds.client_code)

            self._ws = websocket.WebSocketApp(
                self._endpoint,
                header=headers,
                on_open=self._on_ws_open,
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )

            try:
                self._ws.run_forever(ping_interval=0)  # We handle explicit SmartStream heartbeat
            except Exception as exc:
                logger.error(f"[SmartAPIClient] WebSocket worker exception: {exc}")

            if self._stop_event.is_set():
                break

            # Handle reconnection with exponential backoff
            if not self._reconnection or self._reconnect_count >= self._max_reconnect_attempts:
                logger.warning(
                    f"[SmartAPIClient] Max reconnect attempts ({self._max_reconnect_attempts}) reached or reconnection disabled."
                )
                self._set_state(SmartAPIState.ERROR)
                break

            with self._lock:
                self._reconnect_count += 1
                attempts = self._reconnect_count

            self._set_state(SmartAPIState.RECONNECTING)
            delay = min(
                self.INITIAL_RECONNECT_DELAY_SEC * (2 ** (attempts - 1)),
                self.MAX_RECONNECT_DELAY_SEC,
            )
            logger.info(f"[SmartAPIClient] Reconnecting in {delay:.1f}s (attempt {attempts}/{self._max_reconnect_attempts})...")

            # Interruptible sleep
            if self._stop_event.wait(delay):
                break

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        """Handles socket open event."""
        with self._lock:
            self._reconnect_count = 0
            has_subscriptions = bool(self._subscribed_tokens)
            tokens_to_subscribe = list(self._subscribed_tokens)

        if has_subscriptions:
            self._set_state(SmartAPIState.SUBSCRIBED)
            # Re-subscribe tokens on reconnect
            frame = self.build_subscription_payload(
                tokens=tokens_to_subscribe,
                mode=self._default_mode,
                exchange_type=self._default_exchange_type,
                action=1,
            )
            try:
                ws.send(frame)
                logger.info(f"[SmartAPIClient] Resubscribed {len(tokens_to_subscribe)} tokens on connect.")
            except Exception as e:
                logger.error(f"[SmartAPIClient] Error sending resubscription frame: {e}")
        else:
            self._set_state(SmartAPIState.STREAM_CONNECTED)

        logger.info("[SmartAPIClient] SmartStream WebSocket connection established.")

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="SmartStream-Heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _on_ws_message(self, ws: websocket.WebSocketApp, message: Any) -> None:
        """Processes inbound binary packets or text frames."""
        perf_now = time.perf_counter()

        # Binary packet: SmartStream tick
        if isinstance(message, bytes):
            # Extract token for routing (token is at bytes 2..27 in Little-Endian header)
            token = ""
            if len(message) >= 27:
                token_raw = message[2:27]
                token = token_raw.rstrip(b"\x00").decode("ascii", errors="replace")

            with self._lock:
                cbs = list(self._packet_callbacks)

            for cb in cbs:
                try:
                    cb(token, message, perf_now)
                except Exception as e:
                    logger.error(f"[SmartAPIClient] Error in packet callback: {e}", exc_info=True)

        # Text message: heartbeat or json response
        elif isinstance(message, str):
            clean_msg = message.strip()
            if clean_msg.lower() == "pong":
                with self._lock:
                    self._last_pong_time = time.time()
                logger.debug("[SmartAPIClient] Heartbeat pong received.")
            else:
                logger.debug(f"[SmartAPIClient] Received text message: {clean_msg}")

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        logger.error(f"[SmartAPIClient] WebSocket error: {error}")

    def _on_ws_close(self, ws: websocket.WebSocketApp, close_status_code: Any, close_msg: Any) -> None:
        logger.info(f"[SmartAPIClient] WebSocket closed: status={close_status_code}, msg={close_msg}")
        if not self._stop_event.is_set():
            self._set_state(SmartAPIState.DISCONNECTED)

    def _heartbeat_loop(self) -> None:
        """Sends 'ping' text frame every HEARTBEAT_INTERVAL_SEC seconds."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.HEARTBEAT_INTERVAL_SEC):
                break

            ws = self._ws
            with self._lock:
                is_active = self._state in (SmartAPIState.STREAM_CONNECTED, SmartAPIState.SUBSCRIBED)

            if is_active and ws:
                try:
                    ws.send("ping")
                    with self._lock:
                        self._last_ping_time = time.time()
                    logger.debug("[SmartAPIClient] Heartbeat ping sent.")
                except Exception as e:
                    logger.warning(f"[SmartAPIClient] Failed to send heartbeat ping: {e}")
                    break
