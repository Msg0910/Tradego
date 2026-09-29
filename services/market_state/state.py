"""
Instrument State and Snapshot Model for Tradego Market State Layer.

Maintains canonical in-memory state for one instrument with non-destructive
update semantics, explicit timestamp separation, session reset capabilities,
and allocation-conscious hot path design.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple

from services.market_gateway.models import DepthLevel, MarketEvent

from .instrument import InstrumentId
from .order_book import OrderBookState


@dataclass(frozen=True, slots=True)
class InstrumentStateSnapshot:
    """
    Immutable, point-in-time snapshot of an instrument's market state.
    Safe for concurrent read access by strategies, analytics, and UI.
    Does NOT hold raw MarketEvent references.
    """
    instrument_id: Optional[InstrumentId]
    provider: str
    provider_symbol_id: str
    is_resolved: bool

    # Prices & Direction
    ltp: Optional[float]
    ltp_qty: Optional[int]
    prev_ltp: Optional[float]
    tick_direction: int  # +1 uptick, -1 downtick, 0 unchanged

    # Order Book & Depth
    order_book: OrderBookState

    # Session & Volume Statistics
    tick_volume: Optional[float]
    total_volume: Optional[float]
    atp: Optional[float]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    previous_close: Optional[float]
    change: Optional[float]
    change_percent: Optional[float]

    # Open Interest
    oi: Optional[int]
    prev_oi: Optional[int]
    oi_change: Optional[int]
    previous_open_interest_close: Optional[float]

    # Circuits & Extremes
    upper_circuit: Optional[float]
    lower_circuit: Optional[float]
    high_52: Optional[float]
    low_52: Optional[float]
    digit: Optional[int]

    # Explicitly Distinguished Timestamps
    # 1. Local arrival/processing ordering
    last_local_receive_timestamp: float
    last_local_receive_datetime: Optional[datetime]
    # 2. Market-time ordering
    last_exchange_timestamp: Optional[datetime]
    # 3. Provider metadata / diagnostic
    last_provider_timestamp: Optional[datetime]
    # 4. State store update timing
    last_update_timestamp: float

    # Raw provider timestamps
    raw_last_update_time: Optional[Any]
    raw_last_exchange_update_time: Optional[Any]
    raw_timestamp: Optional[Any]

    # Accounting
    update_count: int
    stale_event_count: int
    out_of_order_count: int


@dataclass(slots=True)
class InstrumentState:
    """
    Mutable canonical live market state for an instrument.

    Allocation-conscious: updates modify primitive fields in place.
    Does NOT retain complete MarketEvent objects to avoid unnecessary memory growth.
    """
    provider: str
    provider_symbol_id: str
    instrument_id: Optional[InstrumentId] = None
    is_resolved: bool = False

    # Prices & Direction
    ltp: Optional[float] = None
    ltp_qty: Optional[int] = None
    prev_ltp: Optional[float] = None
    tick_direction: int = 0  # +1 uptick, -1 downtick, 0 unchanged

    # Order Book (immutable tuple internally)
    order_book: OrderBookState = field(default_factory=OrderBookState)

    # Session & Volume Statistics
    tick_volume: Optional[float] = None  # Supports fractional volume
    total_volume: Optional[float] = None
    atp: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None

    # Open Interest
    oi: Optional[int] = None
    prev_oi: Optional[int] = None
    oi_change: Optional[int] = None
    previous_open_interest_close: Optional[float] = None

    # Circuits & Extremes
    upper_circuit: Optional[float] = None
    lower_circuit: Optional[float] = None
    high_52: Optional[float] = None
    low_52: Optional[float] = None
    digit: Optional[int] = None

    # Explicitly Distinguished Timestamps
    # 1. Local arrival/processing ordering: monotonic perf_counter & UTC datetime
    last_local_receive_timestamp: float = 0.0
    last_local_receive_datetime: Optional[datetime] = None
    # 2. Market-time ordering: exchange clock
    last_exchange_timestamp: Optional[datetime] = None
    # 3. Provider metadata / diagnostic: feed broadcast time
    last_provider_timestamp: Optional[datetime] = None
    # 4. State store update timing
    last_update_timestamp: float = 0.0

    # Raw provider timestamps preserved
    raw_last_update_time: Optional[Any] = None
    raw_last_exchange_update_time: Optional[Any] = None
    raw_timestamp: Optional[Any] = None

    # Sequence & Health Accounting
    update_count: int = 0
    stale_event_count: int = 0
    out_of_order_count: int = 0

    def update_from_event(self, event: MarketEvent) -> bool:
        """
        Updates this instrument's state from a normalized MarketEvent.

        Update Semantics:
        1. Non-destructive: Never overwrites a valid existing field with None.
        2. Market-Time Ordering: Detects out-of-order events using exchange/provider timestamps.
           Ticks strictly older by >= 1s do NOT overwrite LTP or session high/low.
        3. Intra-second bursts: Identical timestamps (same second) are accepted as valid.
        4. Allocation-conscious: Modifies primitive fields directly in place.

        Returns True if the update was applied, False if rejected as out-of-order.
        """
        self.update_count += 1
        now_perf = time.perf_counter()
        self.last_update_timestamp = now_perf

        # Track local receive timestamp (monotonically increasing local arrival order)
        self.last_local_receive_timestamp = event.local_receive_timestamp
        if event.local_receive_datetime is not None:
            self.last_local_receive_datetime = event.local_receive_datetime

        # Check market-time ordering
        # Feeds often have 1-second resolution ("DD-MM-YYYY HH:MM:SS").
        # Multiple ticks within the same second have identical timestamps and are valid.
        is_out_of_order = False
        if event.provider_timestamp is not None and self.last_provider_timestamp is not None:
            if event.provider_timestamp < self.last_provider_timestamp:
                delta_sec = (self.last_provider_timestamp - event.provider_timestamp).total_seconds()
                if delta_sec >= 1.0:
                    is_out_of_order = True
                    self.out_of_order_count += 1

        if is_out_of_order:
            # Stale / out-of-order tick: Record diagnostics without corrupting current market prices
            return False

        # Update provider timestamps
        if event.provider_timestamp is not None:
            self.last_provider_timestamp = event.provider_timestamp
        if event.exchange_timestamp is not None:
            self.last_exchange_timestamp = event.exchange_timestamp

        # Preserve raw provider timestamp fields
        if event.raw_last_update_time is not None:
            self.raw_last_update_time = event.raw_last_update_time
        if event.raw_last_exchange_update_time is not None:
            self.raw_last_exchange_update_time = event.raw_last_exchange_update_time
        if event.raw_timestamp is not None:
            self.raw_timestamp = event.raw_timestamp

        # Update LTP and tick direction
        if event.ltp is not None:
            self.prev_ltp = self.ltp
            self.ltp = event.ltp

            if self.prev_ltp is not None:
                if self.ltp > self.prev_ltp:
                    self.tick_direction = 1
                elif self.ltp < self.prev_ltp:
                    self.tick_direction = -1
                else:
                    self.tick_direction = 0

            # Session High/Low ratcheting
            if self.high is None or self.ltp > self.high:
                self.high = self.ltp
            if self.low is None or self.ltp < self.low:
                self.low = self.ltp

        if event.ltp_qty is not None:
            self.ltp_qty = event.ltp_qty

        # Order Book: build OrderBookState if depth or top levels exist
        if event.depth is not None:
            self.order_book = OrderBookState.from_market_depth(event.depth)
        elif event.bid is not None or event.ask is not None:
            # Partial depth: update level 1 while preserving or synthesizing book
            bids_list = []
            asks_list = []
            if event.bid is not None and event.bid > 0:
                bids_list.append(DepthLevel(price=event.bid, quantity=event.bid_qty or 0))
            if event.ask is not None and event.ask > 0:
                asks_list.append(DepthLevel(price=event.ask, quantity=event.ask_qty or 0))
            self.order_book = OrderBookState(
                bids=tuple(bids_list),
                asks=tuple(asks_list),
                total_buy_qty=event.total_buy_qty,
                total_sell_qty=event.total_sell_qty,
            )

        # Volume & Session Stats (Non-destructive)
        if event.tick_volume is not None:
            self.tick_volume = event.tick_volume
        if event.total_volume is not None:
            self.total_volume = event.total_volume
        if event.atp is not None:
            self.atp = event.atp
        if event.open is not None:
            self.open = event.open
        if event.high is not None and (self.high is None or event.high > self.high):
            self.high = event.high
        if event.low is not None and (self.low is None or event.low < self.low):
            self.low = event.low
        if event.previous_close is not None:
            self.previous_close = event.previous_close

        # Compute price change and percentage
        if self.ltp is not None and self.previous_close is not None and self.previous_close > 0:
            self.change = round(self.ltp - self.previous_close, 4)
            self.change_percent = round((self.change / self.previous_close) * 100.0, 4)

        # Open Interest
        if event.oi is not None:
            if self.oi is not None:
                self.prev_oi = self.oi
                self.oi_change = event.oi - self.oi
            self.oi = event.oi

        if event.previous_open_interest_close is not None:
            self.previous_open_interest_close = event.previous_open_interest_close

        # Circuit Limits & 52-Week Statistics
        if event.upper_circuit is not None:
            self.upper_circuit = event.upper_circuit
        if event.lower_circuit is not None:
            self.lower_circuit = event.lower_circuit
        if event.high_52 is not None:
            self.high_52 = event.high_52
        if event.low_52 is not None:
            self.low_52 = event.low_52
        if event.digit is not None:
            self.digit = event.digit

        return True

    def reset_session(self, initial_price: Optional[float] = None) -> None:
        """
        Explicitly resets session-level statistics for a new trading day/session.
        Preserves reference data (previous_close, circuits, 52-week stats, instrument identity).
        Does NOT hardcode any exchange calendar or opening time.
        """
        base_price = initial_price if initial_price is not None else self.ltp
        self.open = base_price
        self.high = base_price
        self.low = base_price
        self.tick_volume = 0.0
        self.total_volume = 0.0
        self.tick_direction = 0
        self.prev_ltp = None
        self.change = 0.0 if (self.previous_close and base_price == self.previous_close) else None
        self.change_percent = 0.0 if (self.previous_close and base_price == self.previous_close) else None
        self.oi_change = 0
        self.order_book = OrderBookState()

    def create_snapshot(self) -> InstrumentStateSnapshot:
        """
        Creates an immutable, point-in-time snapshot of the current state.
        Allocation-conscious: copies primitive attributes directly into a frozen dataclass.
        """
        return InstrumentStateSnapshot(
            instrument_id=self.instrument_id,
            provider=self.provider,
            provider_symbol_id=self.provider_symbol_id,
            is_resolved=self.is_resolved,
            ltp=self.ltp,
            ltp_qty=self.ltp_qty,
            prev_ltp=self.prev_ltp,
            tick_direction=self.tick_direction,
            order_book=self.order_book,
            tick_volume=self.tick_volume,
            total_volume=self.total_volume,
            atp=self.atp,
            open=self.open,
            high=self.high,
            low=self.low,
            previous_close=self.previous_close,
            change=self.change,
            change_percent=self.change_percent,
            oi=self.oi,
            prev_oi=self.prev_oi,
            oi_change=self.oi_change,
            previous_open_interest_close=self.previous_open_interest_close,
            upper_circuit=self.upper_circuit,
            lower_circuit=self.lower_circuit,
            high_52=self.high_52,
            low_52=self.low_52,
            digit=self.digit,
            last_local_receive_timestamp=self.last_local_receive_timestamp,
            last_local_receive_datetime=self.last_local_receive_datetime,
            last_exchange_timestamp=self.last_exchange_timestamp,
            last_provider_timestamp=self.last_provider_timestamp,
            last_update_timestamp=self.last_update_timestamp,
            raw_last_update_time=self.raw_last_update_time,
            raw_last_exchange_update_time=self.raw_last_exchange_update_time,
            raw_timestamp=self.raw_timestamp,
            update_count=self.update_count,
            stale_event_count=self.stale_event_count,
            out_of_order_count=self.out_of_order_count,
        )
