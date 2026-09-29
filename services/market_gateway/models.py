"""
Canonical Market Event and Market Depth Data Models for Tradego Market Data Gateway.

These models represent the normalized, provider-agnostic market data within Tradego.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class DepthLevel:
    """
    Represents a single price level in the market depth book.
    """
    price: float
    quantity: int
    orders: Optional[int] = None


@dataclass(slots=True)
class MarketDepth:
    """
    Represents multi-level market depth (order book snapshot).
    """
    bids: List[DepthLevel] = field(default_factory=list)
    asks: List[DepthLevel] = field(default_factory=list)
    total_buy_qty: Optional[int] = None
    total_sell_qty: Optional[int] = None


@dataclass(slots=True)
class MarketEvent:
    """
    Canonical Tradego Normalized Market Event.

    Attributes:
        provider: Name of the originating data provider (e.g. "ATMSTOX").
        provider_symbol_id: Instrument / token ID used by the provider.
        symbol: Tradego canonical symbol if mapped (optional, defaults to provider_symbol_id).

        Timestamps:
            exchange_timestamp: Parsed exchange timestamp if provided by feed.
            provider_timestamp: Parsed provider timestamp if provided by feed.
            raw_last_update_time: Original unmodified LastUpdateTime string/value from provider.
            raw_last_exchange_update_time: Original unmodified LastExchangeUpdateTime string/value from provider.
            raw_timestamp: Original unmodified Timestamp string/value from provider.
            local_receive_timestamp: High-resolution monotonic timestamp (perf_counter) upon network arrival.
            local_receive_datetime: UTC datetime recorded at local receive time.
            normalized_timestamp: High-resolution monotonic timestamp (perf_counter) upon normalization completion.

        Market Prices & Quantities:
            ltp: Last Traded Price.
            ltp_qty: Last Traded Quantity.
            bid: Best Bid (Level 1).
            bid_qty: Best Bid Quantity (Level 1).
            ask: Best Ask (Level 1).
            ask_qty: Best Ask Quantity (Level 1).
            spread: ask - bid if both are available.

        Depth & Aggregates:
            depth: Multi-level order book (levels 1-4 for bids and asks).
            total_buy_qty: Total buy quantity across the order book.
            total_sell_qty: Total sell quantity across the order book.

        Volume & Session Statistics:
            tick_volume: Volume of current tick / interval (float, supports fractional volume).
            total_volume: Cumulative session volume.
            atp: Average Traded Price.
            open: Session open price.
            high: Session high price.
            low: Session low price.
            previous_close: Previous day closing price.
            oi: Current Open Interest (from Today_OI).
            previous_open_interest_close: Previous day OI close reference (float).
            upper_circuit: Upper circuit limit (UC).
            lower_circuit: Lower circuit limit (LC).
            high_52: 52-week high price.
            low_52: 52-week low price.
            digit: Decimal precision digit count.

        Latency Metrics:
            latency_provider_to_receive_ms: Milliseconds elapsed between provider timestamp
                and local receive time (NOTE: This is feed provider-to-receiver latency,
                NOT true exchange-to-system latency).
            latency_gateway_process_ms: High-resolution duration (ms) from local receive
                to normalization completion.

        Raw Payload:
            raw: Complete, unmodified original provider payload for audit and lossless access.
    """
    provider: str
    provider_symbol_id: str
    symbol: Optional[str] = None

    # Timestamps (Parsed)
    exchange_timestamp: Optional[datetime] = None
    provider_timestamp: Optional[datetime] = None

    # Timestamps (Original raw provider values)
    raw_last_update_time: Optional[Any] = None
    raw_last_exchange_update_time: Optional[Any] = None
    raw_timestamp: Optional[Any] = None

    # Local Receive & Gateway Timing
    local_receive_timestamp: float = 0.0
    local_receive_datetime: Optional[datetime] = None
    normalized_timestamp: float = 0.0

    # Market Prices & Quantities
    ltp: Optional[float] = None
    ltp_qty: Optional[int] = None
    bid: Optional[float] = None
    bid_qty: Optional[int] = None
    ask: Optional[float] = None
    ask_qty: Optional[int] = None
    spread: Optional[float] = None

    # Depth & Aggregates
    depth: Optional[MarketDepth] = None
    total_buy_qty: Optional[int] = None
    total_sell_qty: Optional[int] = None

    # Volume & Session Statistics
    tick_volume: Optional[float] = None
    total_volume: Optional[float] = None
    atp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None
    oi: Optional[int] = None
    previous_open_interest_close: Optional[float] = None
    upper_circuit: Optional[float] = None
    lower_circuit: Optional[float] = None
    high_52: Optional[float] = None
    low_52: Optional[float] = None
    digit: Optional[int] = None

    # Latency Metrics
    latency_provider_to_receive_ms: Optional[float] = None
    latency_gateway_process_ms: Optional[float] = None

    # Raw Payload (Preserved without mutation)
    raw: Optional[Dict[str, Any]] = None
