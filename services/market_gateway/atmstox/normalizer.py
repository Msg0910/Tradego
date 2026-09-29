"""
ATMSTOX Feed Payload Normalizer.

Transforms raw, provider-specific ATMSTOX Socket.IO payloads into
canonical Tradego MarketEvent objects.

Handles all 28+ confirmed ATMSTOX fields:
- LTP, LTPQty, TickVolume, ATP, TotalVolume, Open, High, Low, Previous_Close
- Today_OI, Previous_Open_Interest_Close, Total_Buy, Total_Sell
- Bid, BidQty, Bid1..Bid4, BidQty1..BidQty4
- Ask, AskQty, Ask1..Ask4, AskQty1..AskQty4
- UC, LC, HIGH52, LOW52, digit, symbol_id
- Timestamp, LastUpdateTime, LastExchangeUpdateTime, Time
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import DepthLevel, MarketDepth, MarketEvent


def _safe_float(val: Any) -> Optional[float]:
    """Safely converts an arbitrary value to float, or None if invalid/empty."""
    if val is None:
        return None
    if isinstance(val, (float, int)):
        return float(val)
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    """Safely converts an arbitrary value to int (handling float strings), or None."""
    if val is None:
        return None
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    if isinstance(val, float):
        try:
            return int(val)
        except (ValueError, OverflowError):
            return None
    s = str(val).strip()
    if not s or s.lower() in ("null", "none", "nan"):
        return None
    try:
        # Some feeds emit ints formatted as float strings (e.g. "100.0")
        return int(float(s))
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_timestamp_field(val: Any) -> Optional[datetime]:
    """
    Parses various timestamp representations used by market feeds:
    - String formatted 'DD-MM-YYYY HH:MM:SS'
    - String formatted 'YYYY-MM-DD HH:MM:SS'
    - Unix epoch integer/float in seconds or milliseconds
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s in ("0", "null", "none"):
        return None

    # Try standard string date formats
    for fmt in (
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            # Default to UTC if naive
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

    # Try numeric epoch timestamp (seconds or milliseconds)
    num = _safe_float(val)
    if num is not None and num > 0:
        try:
            # If > 1e11, assume milliseconds
            if num > 1e11:
                return datetime.fromtimestamp(num / 1000.0, tz=timezone.utc)
            # If > 1e8, assume seconds
            elif num > 1e8:
                return datetime.fromtimestamp(num, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass

    return None


class ATMStoxNormalizer:
    """
    Normalizer for incoming ATMSTOX tick payloads.
    """

    @staticmethod
    def normalize(
        raw: Any,
        symbol_id: str,
        receive_perf_time: float,
        receive_datetime: Optional[datetime] = None,
    ) -> Optional[MarketEvent]:
        """
        Normalizes a raw ATMSTOX dictionary into a canonical MarketEvent.

        Returns None if:
        - raw is None
        - raw is not a dict
        - raw is empty
        - Neither LTP nor Bid/Ask midpoint can be determined
        """
        if not isinstance(raw, dict) or not raw:
            return None

        if receive_datetime is None:
            receive_datetime = datetime.now(timezone.utc)

        # 1. Prices & Best Bid/Ask
        ltp = _safe_float(raw.get("LTP"))
        ltp_qty = _safe_int(raw.get("LTPQty"))

        # Best Bid: try Bid then Bid1
        bid = _safe_float(raw.get("Bid"))
        if bid is None:
            bid = _safe_float(raw.get("Bid1"))

        bid_qty = _safe_int(raw.get("BidQty"))
        if bid_qty is None:
            bid_qty = _safe_int(raw.get("BidQty1"))

        # Best Ask: try Ask then Ask1
        ask = _safe_float(raw.get("Ask"))
        if ask is None:
            ask = _safe_float(raw.get("Ask1"))

        ask_qty = _safe_int(raw.get("AskQty"))
        if ask_qty is None:
            ask_qty = _safe_int(raw.get("AskQty1"))

        # If LTP is missing, fallback to Bid/Ask midpoint if both are present
        if ltp is None and bid is not None and ask is not None:
            ltp = (bid + ask) / 2.0

        # If no price or quote information is present at all, consider tick invalid/null
        if ltp is None and bid is None and ask is None:
            return None

        # Spread
        spread = (ask - bid) if (ask is not None and bid is not None) else None

        # 2. Market Depth Book (Levels 1 to 4)
        bids: List[DepthLevel] = []
        asks: List[DepthLevel] = []

        # Level 1
        b1_price = bid
        b1_qty = bid_qty
        if b1_price is not None and b1_price > 0:
            bids.append(DepthLevel(price=b1_price, quantity=b1_qty or 0))

        a1_price = ask
        a1_qty = ask_qty
        if a1_price is not None and a1_price > 0:
            asks.append(DepthLevel(price=a1_price, quantity=a1_qty or 0))

        # Levels 2 to 4
        for level in range(2, 5):
            b_p = _safe_float(raw.get(f"Bid{level}"))
            b_q = _safe_int(raw.get(f"BidQty{level}"))
            if b_p is not None and b_p > 0:
                bids.append(DepthLevel(price=b_p, quantity=b_q or 0))

            a_p = _safe_float(raw.get(f"Ask{level}"))
            a_q = _safe_int(raw.get(f"AskQty{level}"))
            if a_p is not None and a_p > 0:
                asks.append(DepthLevel(price=a_p, quantity=a_q or 0))

        market_depth = (
            MarketDepth(
                bids=bids,
                asks=asks,
                total_buy_qty=_safe_int(raw.get("Total_Buy")),
                total_sell_qty=_safe_int(raw.get("Total_Sell")),
            )
            if (bids or asks)
            else None
        )

        # 3. Volume & Session Statistics
        # Note: ATMSTOX TickVolume is fractional (float)
        tick_volume = _safe_float(raw.get("TickVolume"))
        total_volume = _safe_float(raw.get("TotalVolume"))
        atp = _safe_float(raw.get("ATP"))
        open_price = _safe_float(raw.get("Open"))
        high_price = _safe_float(raw.get("High"))
        low_price = _safe_float(raw.get("Low"))
        prev_close = _safe_float(raw.get("Previous_Close"))

        # Open interest
        oi = _safe_int(raw.get("Today_OI"))
        # Preserving original semantic name for Previous_Open_Interest_Close
        previous_open_interest_close = _safe_float(raw.get("Previous_Open_Interest_Close"))

        # Circuit limits & 52-week extremes
        upper_circuit = _safe_float(raw.get("UC"))
        lower_circuit = _safe_float(raw.get("LC"))
        high_52 = _safe_float(raw.get("HIGH52"))
        low_52 = _safe_float(raw.get("LOW52"))
        digit = _safe_int(raw.get("digit"))

        # 4. Timestamps - Raw original values preserved without alteration
        raw_last_update_time = raw.get("LastUpdateTime")
        raw_last_exchange_update_time = raw.get("LastExchangeUpdateTime")
        raw_timestamp = raw.get("Timestamp")

        # 5. Timestamps - Parsed values
        # Exchange timestamp
        exchange_timestamp = _parse_timestamp_field(raw_last_exchange_update_time)

        # Provider timestamp: check Timestamp, then fallback to LastUpdateTime
        provider_timestamp = _parse_timestamp_field(raw_timestamp)
        if provider_timestamp is None:
            provider_timestamp = _parse_timestamp_field(raw_last_update_time)

        # 6. Latency Metrics
        # Latency between provider timestamp and local receive timestamp
        # NOTE: This reflects feed provider-to-receive latency, NOT exchange latency.
        latency_provider_to_receive_ms: Optional[float] = None
        if provider_timestamp is not None and receive_datetime is not None:
            p_ts = provider_timestamp.timestamp()
            r_ts = receive_datetime.timestamp()
            # Feed timestamps may not have millisecond resolution or perfect clock sync,
            # but record the delta when within reasonable boundaries (< 86400s)
            delta_ms = (r_ts - p_ts) * 1000.0
            if -86400000.0 < delta_ms < 86400000.0:
                latency_provider_to_receive_ms = round(delta_ms, 2)

        # High-resolution gateway processing latency (receive -> normalization finished)
        norm_perf_time = time.perf_counter()
        gateway_process_ms = round((norm_perf_time - receive_perf_time) * 1000.0, 4)

        return MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id=str(symbol_id),
            symbol=str(symbol_id),
            # Parsed timestamps
            exchange_timestamp=exchange_timestamp,
            provider_timestamp=provider_timestamp,
            # Raw timestamps preserved
            raw_last_update_time=raw_last_update_time,
            raw_last_exchange_update_time=raw_last_exchange_update_time,
            raw_timestamp=raw_timestamp,
            # Local receive & Gateway timing
            local_receive_timestamp=receive_perf_time,
            local_receive_datetime=receive_datetime,
            normalized_timestamp=norm_perf_time,
            # Prices & Depth
            ltp=ltp,
            ltp_qty=ltp_qty,
            bid=bid,
            bid_qty=bid_qty,
            ask=ask,
            ask_qty=ask_qty,
            spread=spread,
            depth=market_depth,
            total_buy_qty=_safe_int(raw.get("Total_Buy")),
            total_sell_qty=_safe_int(raw.get("Total_Sell")),
            # Volume & Stats
            tick_volume=tick_volume,
            total_volume=total_volume,
            atp=atp,
            open=open_price,
            high=high_price,
            low=low_price,
            previous_close=prev_close,
            oi=oi,
            previous_open_interest_close=previous_open_interest_close,
            upper_circuit=upper_circuit,
            lower_circuit=lower_circuit,
            high_52=high_52,
            low_52=low_52,
            digit=digit,
            # Latency measurements
            latency_provider_to_receive_ms=latency_provider_to_receive_ms,
            latency_gateway_process_ms=gateway_process_ms,
            # Complete unmodified raw dictionary
            raw=raw,
        )
