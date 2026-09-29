"""
Angel One SmartStream (WebSocket 2.0) Binary Normalizer.

Decodes raw binary tick packets into Tradego canonical MarketEvent instances.
Supports Little-Endian binary streaming format for:
- Mode 1: LTP (Last Traded Price) - 51 bytes
- Mode 2: Quote (OHLCV + B/A Totals) - 123 bytes
- Mode 3: SnapQuote / Depth (Full 5-Level Book + OI) - 347+ bytes

CRITICAL CONSTRAINTS:
1. Zero network calls or external I/O.
2. 100% deterministic parsing: identical binary payload + metadata = identical MarketEvent.
3. Clean rejection of malformed or truncated packets without throwing unhandled exceptions.
4. Preserves provider identifier "SMARTAPI" and vendor token string.
"""

from datetime import datetime, timezone
from enum import IntEnum
import struct
from typing import Any, Dict, List, Optional, Tuple

from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent


class SmartStreamMode(IntEnum):
    """SmartStream subscription / packet modes."""
    LTP = 1
    QUOTE = 2
    SNAP_QUOTE = 3
    DEPTH_20 = 4


class SmartStreamExchange(IntEnum):
    """SmartStream exchange segment identifiers."""
    NSE_CM = 1
    NSE_FO = 2
    BSE_CM = 3
    BSE_FO = 4
    MCX_FO = 5
    NCX_FO = 7
    CDE_FO = 13


EXCHANGE_CODE_MAP: Dict[int, str] = {
    SmartStreamExchange.NSE_CM: "NSE",
    SmartStreamExchange.NSE_FO: "NFO",
    SmartStreamExchange.BSE_CM: "BSE",
    SmartStreamExchange.BSE_FO: "BFO",
    SmartStreamExchange.MCX_FO: "MCX",
    SmartStreamExchange.NCX_FO: "NCX",
    SmartStreamExchange.CDE_FO: "CDS",
}


class SmartAPINormalizer:
    """
    Offline binary packet normalizer for Angel One SmartStream feed.
    """

    PROVIDER_NAME: str = "SMARTAPI"

    # Packet size constants
    HEADER_SIZE: int = 27         # mode (1) + exchange (1) + token (25)
    LTP_PACKET_SIZE: int = 51     # header (27) + seq (8) + ts (8) + ltp (8)
    QUOTE_PACKET_SIZE: int = 123  # ltp (51) + qty (8) + atp (8) + vol (8) + buy_qty (8) + sell_qty (8) + o (8) + h (8) + l (8) + c (8)
    SNAP_QUOTE_MIN_SIZE: int = 347 # quote (123) + last_trade_ts (8) + oi (8) + oi_pct (8) + 10 depth levels (200)

    # Struct format strings (Little-Endian '<')
    # Mode 1: mode(B), exch(B), token(25s), seq(q), exch_ts(q), ltp(q)
    FMT_LTP: str = "<BB25sqqq"

    # Mode 2 additional fields (from byte 51 to 123):
    # last_traded_qty(q), avg_traded_price(q), vol(q), total_buy_qty(d), total_sell_qty(d), open(q), high(q), low(q), close(q)
    FMT_QUOTE_TAIL: str = "<qqqddqqqq"

    # Mode 3 additional metadata (from byte 123 to 147):
    # last_trade_timestamp(q), open_interest(q), oi_change_pct(d)
    FMT_SNAP_QUOTE_META: str = "<qqd"

    # Depth level entry (20 bytes per level):
    # flag(h), qty(q), price(q), orders(h)
    FMT_DEPTH_LEVEL: str = "<hqqh"

    @classmethod
    def normalize(
        cls,
        payload: bytes,
        receive_perf_time: float,
        receive_datetime: Optional[datetime] = None,
        symbol_override: Optional[str] = None,
    ) -> Optional[MarketEvent]:
        """
        Parses a SmartStream binary packet into a canonical MarketEvent.

        Parameters:
            payload: Raw bytes received from the WebSocket.
            receive_perf_time: High-resolution monotonic arrival timestamp (e.g. time.perf_counter()).
            receive_datetime: UTC datetime recorded upon arrival (deterministic injection supported).
            symbol_override: Optional mapped canonical symbol string (e.g. "SBIN").

        Returns:
            MarketEvent instance if valid, or None if packet is truncated, malformed, or invalid.
        """
        if not isinstance(payload, (bytes, bytearray)):
            return None

        packet_len = len(payload)
        if packet_len < cls.LTP_PACKET_SIZE:
            return None

        try:
            # Decode Mode 1 (LTP Header & Core)
            mode_val, exch_val, raw_token, seq_num, exch_ts_ms, ltp_paise = struct.unpack_from(
                cls.FMT_LTP, payload, 0
            )
        except struct.error:
            return None

        # Clean token string: strip null bytes and trailing whitespace
        token_str = raw_token.decode("utf-8", errors="replace").rstrip("\x00").strip()
        if not token_str:
            return None

        # Convert price in paise to rupees
        ltp = float(ltp_paise) / 100.0 if ltp_paise >= 0 else 0.0

        # Convert exchange timestamp (epoch milliseconds)
        exchange_dt: Optional[datetime] = None
        if exch_ts_ms > 0:
            try:
                exchange_dt = datetime.fromtimestamp(exch_ts_ms / 1000.0, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                exchange_dt = None

        # Base attributes
        ltp_qty: Optional[int] = None
        atp: Optional[float] = None
        total_volume: Optional[float] = None
        total_buy_qty: Optional[int] = None
        total_sell_qty: Optional[int] = None
        open_price: Optional[float] = None
        high_price: Optional[float] = None
        low_price: Optional[float] = None
        prev_close: Optional[float] = None
        oi: Optional[int] = None
        depth: Optional[MarketDepth] = None
        bid: Optional[float] = None
        bid_qty: Optional[int] = None
        ask: Optional[float] = None
        ask_qty: Optional[int] = None
        spread: Optional[float] = None

        # Mode 2 & Mode 3 Parsing
        if mode_val in (SmartStreamMode.QUOTE, SmartStreamMode.SNAP_QUOTE, SmartStreamMode.DEPTH_20):
            if packet_len < cls.QUOTE_PACKET_SIZE:
                # Mode specifies QUOTE but packet is truncated
                return None

            try:
                (
                    ltq,
                    atp_paise,
                    vol,
                    tbq,
                    tsq,
                    open_paise,
                    high_paise,
                    low_paise,
                    close_paise,
                ) = struct.unpack_from(cls.FMT_QUOTE_TAIL, payload, cls.LTP_PACKET_SIZE)
            except struct.error:
                return None

            ltp_qty = int(ltq) if ltq >= 0 else None
            atp = float(atp_paise) / 100.0 if atp_paise >= 0 else None
            total_volume = float(vol) if vol >= 0 else None
            total_buy_qty = int(tbq) if tbq >= 0 else None
            total_sell_qty = int(tsq) if tsq >= 0 else None
            open_price = float(open_paise) / 100.0 if open_paise >= 0 else None
            high_price = float(high_paise) / 100.0 if high_paise >= 0 else None
            low_price = float(low_paise) / 100.0 if low_paise >= 0 else None
            prev_close = float(close_paise) / 100.0 if close_paise >= 0 else None

            # Mode 3 (SnapQuote / Depth)
            if mode_val in (SmartStreamMode.SNAP_QUOTE, SmartStreamMode.DEPTH_20):
                if packet_len >= cls.SNAP_QUOTE_MIN_SIZE:
                    try:
                        _last_trade_ts, oi_val, _oi_pct = struct.unpack_from(
                            cls.FMT_SNAP_QUOTE_META, payload, cls.QUOTE_PACKET_SIZE
                        )
                        oi = int(oi_val) if oi_val >= 0 else None
                    except struct.error:
                        return None

                    # Parse 10 depth levels (5 bids, 5 asks)
                    offset = cls.QUOTE_PACKET_SIZE + struct.calcsize(cls.FMT_SNAP_QUOTE_META)
                    bids: List[DepthLevel] = []
                    asks: List[DepthLevel] = []

                    for idx in range(10):
                        if offset + 20 > packet_len:
                            break
                        try:
                            flag, d_qty, d_price_paise, d_orders = struct.unpack_from(
                                cls.FMT_DEPTH_LEVEL, payload, offset
                            )
                            offset += 20
                            d_price = float(d_price_paise) / 100.0 if d_price_paise >= 0 else 0.0
                            level = DepthLevel(
                                price=d_price,
                                quantity=int(d_qty) if d_qty >= 0 else 0,
                                orders=int(d_orders) if d_orders >= 0 else None,
                            )
                            # Flag 1 = Buy (Bid), 0 = Sell (Ask)
                            if flag == 1 or idx < 5:
                                bids.append(level)
                            else:
                                asks.append(level)
                        except struct.error:
                            break

                    if bids or asks:
                        depth = MarketDepth(
                            bids=bids,
                            asks=asks,
                            total_buy_qty=total_buy_qty,
                            total_sell_qty=total_sell_qty,
                        )
                        if bids:
                            bid = bids[0].price
                            bid_qty = bids[0].quantity
                        if asks:
                            ask = asks[0].price
                            ask_qty = asks[0].quantity
                        if bid is not None and ask is not None:
                            spread = round(ask - bid, 4)

        # Raw audit payload
        raw_meta: Dict[str, Any] = {
            "mode": mode_val,
            "exchange_type": exch_val,
            "exchange_code": EXCHANGE_CODE_MAP.get(exch_val, "UNKNOWN"),
            "token": token_str,
            "sequence_number": seq_num,
            "exchange_timestamp_ms": exch_ts_ms,
            "packet_bytes_length": packet_len,
        }

        # Local normalization completion timing
        norm_perf_time = receive_perf_time
        process_ms = 0.0

        return MarketEvent(
            provider=cls.PROVIDER_NAME,
            provider_symbol_id=token_str,
            symbol=symbol_override or token_str,
            exchange_timestamp=exchange_dt,
            provider_timestamp=exchange_dt,
            raw_timestamp=exch_ts_ms,
            local_receive_timestamp=receive_perf_time,
            local_receive_datetime=receive_datetime,
            normalized_timestamp=norm_perf_time,
            ltp=ltp,
            ltp_qty=ltp_qty,
            bid=bid,
            bid_qty=bid_qty,
            ask=ask,
            ask_qty=ask_qty,
            spread=spread,
            depth=depth,
            total_buy_qty=total_buy_qty,
            total_sell_qty=total_sell_qty,
            tick_volume=float(ltp_qty) if ltp_qty is not None else None,
            total_volume=total_volume,
            atp=atp,
            open=open_price,
            high=high_price,
            low=low_price,
            previous_close=prev_close,
            oi=oi,
            latency_gateway_process_ms=process_ms,
            raw=raw_meta,
        )
