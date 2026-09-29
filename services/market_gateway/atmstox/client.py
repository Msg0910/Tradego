"""
ATMSTOX Socket.IO Client for Tradego Market Gateway.

Based on the protocol discovered from the Chart system:
- Endpoint: https://atmstox.com:20100
- Transport: websocket only
- Protocol: Socket.IO v4
- Subscription: socket.emit("giverate", str(symbol_id))
- Tick event: trade{symbol_id}
- Unsubscription: socket.emit("stoprate", str(symbol_id))
- Authentication: None

CRITICAL PERFORMANCE & ARCHITECTURE RULE:
No blocking I/O (database, Kafka, Redis, HTTP, LLM, MCP calls, or print statements)
is permitted on the critical tick-handling path.
"""

import inspect
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from ..base import ConnectionState

logger = logging.getLogger("market_gateway.atmstox")

try:
    import socketio
    SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None  # type: ignore
    SOCKETIO_AVAILABLE = False


def normalize_tick(raw: Any, symbol_id: str) -> Optional[Dict[str, Any]]:
    """
    Backwards-compatible lightweight dictionary normalizer for legacy tests and scripts.
    Maintains compatibility with scripts/test_atmstox_feed.py.
    """
    if not isinstance(raw, dict) or not raw:
        return None

    # Price determination: LTP with Bid/Ask midpoint fallback
    ltp_raw = raw.get("LTP")
    bid_raw = raw.get("Bid")
    ask_raw = raw.get("Ask")

    ltp = float(ltp_raw) if ltp_raw is not None and str(ltp_raw).strip() != "" else None
    bid = float(bid_raw) if bid_raw is not None and str(bid_raw).strip() != "" else None
    ask = float(ask_raw) if ask_raw is not None and str(ask_raw).strip() != "" else None

    price = ltp
    if price is None and bid is not None and ask is not None:
        price = (bid + ask) / 2.0

    if price is None:
        return None

    spread = (ask - bid) if (ask is not None and bid is not None) else None

    ltp_qty = None
    if raw.get("LTPQty") is not None:
        try:
            ltp_qty = int(float(raw.get("LTPQty", 0)))
        except (ValueError, TypeError):
            pass

    total_volume = None
    if raw.get("TotalVolume") is not None:
        try:
            total_volume = float(raw.get("TotalVolume", 0))
        except (ValueError, TypeError):
            pass

    today_oi = None
    if raw.get("Today_OI") is not None:
        try:
            today_oi = int(float(raw.get("Today_OI", 0)))
        except (ValueError, TypeError):
            pass

    feed_timestamp = raw.get("Timestamp")
    parsed_time = None
    if feed_timestamp and str(feed_timestamp).strip() not in ("0", ""):
        try:
            parsed_time = datetime.strptime(str(feed_timestamp).strip(), "%d-%m-%Y %H:%M:%S")
        except ValueError:
            pass

    local_receive_time = datetime.now(timezone.utc)

    return {
        "symbol_id": str(symbol_id),
        "local_receive_time": local_receive_time,
        "feed_timestamp_str": feed_timestamp,
        "feed_time": parsed_time,
        "price": price,
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "ltp_qty": ltp_qty,
        "total_volume": total_volume,
        "today_oi": today_oi,
        "raw": raw,
    }


class ATMStoxClient:
    """
    Socket.IO transport client for ATMSTOX feed.
    """

    def __init__(
        self,
        endpoint: str = "https://atmstox.com:20100",
        reconnection: bool = True,
        reconnection_attempts: Optional[int] = None,
        logger_enabled: bool = False,
    ):
        if not SOCKETIO_AVAILABLE:
            raise RuntimeError(
                "The 'python-socketio' package is not installed. "
                "Please install it using: pip install \"python-socketio[client]\""
            )

        self.endpoint = endpoint
        self.reconnection = reconnection
        self.reconnection_attempts = reconnection_attempts
        self.logger_enabled = logger_enabled

        self._lock = threading.Lock()
        self._subscribed_symbols: Set[str] = set()
        self._callbacks: Dict[str, List[Callable[..., None]]] = {}
        self._state_callbacks: List[Callable[[ConnectionState], None]] = []
        self._state = ConnectionState.DISCONNECTED

        self.sio = socketio.Client(
            reconnection=self.reconnection,
            reconnection_attempts=self.reconnection_attempts or 0,
            reconnection_delay=1,
            reconnection_delay_max=5,
            logger=self.logger_enabled,
            engineio_logger=self.logger_enabled,
        )

        self._setup_event_handlers()

    @property
    def state(self) -> ConnectionState:
        return self._state

    def _set_state(self, new_state: ConnectionState) -> None:
        self._state = new_state
        with self._lock:
            state_cbs = list(self._state_callbacks)
        for cb in state_cbs:
            try:
                cb(new_state)
            except Exception as e:
                logger.error(f"[ATMStoxClient] Error in state callback: {e}")

    def add_state_callback(self, callback: Callable[[ConnectionState], None]) -> None:
        with self._lock:
            if callback not in self._state_callbacks:
                self._state_callbacks.append(callback)

    def _setup_event_handlers(self) -> None:
        @self.sio.event
        def connect():
            logger.info(f"[ATMStoxClient] Connected to {self.endpoint}")
            self._set_state(ConnectionState.CONNECTED)
            # Re-subscribe to all active symbols on reconnect
            with self._lock:
                symbols = list(self._subscribed_symbols)
            if symbols:
                logger.info(f"[ATMStoxClient] Re-subscribing to {len(symbols)} active symbols...")
                for symbol in symbols:
                    self._emit_giverate(symbol)

        @self.sio.event
        def disconnect():
            logger.warning(f"[ATMStoxClient] Disconnected from {self.endpoint}")
            if self.reconnection:
                self._set_state(ConnectionState.RECONNECTING)
            else:
                self._set_state(ConnectionState.DISCONNECTED)

        @self.sio.event
        def connect_error(data):
            logger.error(f"[ATMStoxClient] Connection Error: {data}")
            self._set_state(ConnectionState.FAILED)

    def connect(self, timeout: float = 10.0) -> None:
        """
        Connects to the ATMSTOX Socket.IO feed using WebSocket transport.
        """
        logger.info(f"[ATMStoxClient] Connecting to {self.endpoint} via websocket...")
        self._set_state(ConnectionState.CONNECTING)
        try:
            self.sio.connect(
                self.endpoint,
                transports=["websocket"],
                wait_timeout=int(timeout),
            )
        except Exception as e:
            self._set_state(ConnectionState.FAILED)
            raise e

    def _emit_giverate(self, symbol_id: str) -> None:
        """Internal helper to emit giverate."""
        if self.sio.connected:
            self.sio.emit("giverate", str(symbol_id))
            logger.debug(f"[ATMStoxClient] Emitted giverate({symbol_id})")

    def _emit_stoprate(self, symbol_id: str) -> None:
        """Internal helper to emit stoprate."""
        if self.sio.connected:
            self.sio.emit("stoprate", str(symbol_id))
            logger.debug(f"[ATMStoxClient] Emitted stoprate({symbol_id})")

    def subscribe(
        self,
        symbol_id: Any,
        callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        Subscribes to an instrument token on ATMSTOX.
        Registers an event listener for: trade{symbol_id}
        Supports both legacy callback(raw_data) and callback(raw_data, perf_time, dt).
        """
        str_id = str(symbol_id).strip()
        event_name = f"trade{str_id}"

        with self._lock:
            already_subscribed = str_id in self._subscribed_symbols
            self._subscribed_symbols.add(str_id)

            if callback:
                if str_id not in self._callbacks:
                    self._callbacks[str_id] = []
                if callback not in self._callbacks[str_id]:
                    self._callbacks[str_id].append(callback)

            if not already_subscribed:
                # Bind dynamic event handler for this symbol
                def make_handler(sym: str):
                    def handler(raw_data: Any):
                        perf_now = time.perf_counter()
                        dt_now = datetime.now(timezone.utc)
                        self._handle_incoming_tick(sym, raw_data, perf_now, dt_now)
                    return handler

                self.sio.on(event_name, make_handler(str_id))

        if self.sio.connected:
            self._emit_giverate(str_id)

    def _handle_incoming_tick(
        self, symbol_id: str, raw_data: Any, perf_time: float, dt_now: datetime
    ) -> None:
        """
        Dispatches incoming raw tick to registered callbacks immediately.
        """
        with self._lock:
            callbacks = list(self._callbacks.get(symbol_id, []))

        for cb in callbacks:
            try:
                # Inspect callback signature to support both 1-arg and 3-arg formats
                sig = getattr(cb, "_param_count", None)
                if sig is None:
                    try:
                        sig = len(inspect.signature(cb).parameters)
                    except (ValueError, TypeError):
                        sig = 1
                    setattr(cb, "_param_count", sig)

                if sig == 1:
                    cb(raw_data)
                elif sig == 2:
                    cb(raw_data, perf_time)
                else:
                    cb(raw_data, perf_time, dt_now)
            except Exception as e:
                logger.error(f"[ATMStoxClient] Error in tick callback for {symbol_id}: {e}", exc_info=True)

    def unsubscribe(self, symbol_id: Any) -> None:
        """
        Unsubscribes from an instrument token using stoprate.
        """
        str_id = str(symbol_id).strip()
        event_name = f"trade{str_id}"

        with self._lock:
            if str_id in self._subscribed_symbols:
                self._subscribed_symbols.remove(str_id)
            self._callbacks.pop(str_id, None)

            try:
                if "/" in self.sio.handlers and event_name in self.sio.handlers["/"]:
                    del self.sio.handlers["/"][event_name]
            except Exception:
                pass

        if self.sio.connected:
            self._emit_stoprate(str_id)

    def disconnect(self) -> None:
        """
        Gracefully stops subscriptions and closes the socket.
        """
        with self._lock:
            symbols = list(self._subscribed_symbols)

        if self.sio.connected:
            for s in symbols:
                try:
                    self._emit_stoprate(s)
                except Exception:
                    pass

        try:
            self.sio.disconnect()
        except Exception:
            pass

        self._set_state(ConnectionState.DISCONNECTED)

    @property
    def is_connected(self) -> bool:
        return bool(self.sio.connected)

    @property
    def subscribed_symbols(self) -> List[str]:
        with self._lock:
            return list(self._subscribed_symbols)
