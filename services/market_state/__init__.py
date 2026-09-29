"""
Tradego Real-Time Market State Layer.

Provides fast, in-memory, thread-safe instrument state tracking, 4-level
order book maintenance, and immutable read-only snapshots.
"""

from .candle_design import Candle, TimeFrame
from .instrument import (
    Exchange,
    InstrumentId,
    InstrumentMetadata,
    InstrumentRegistry,
    InstrumentType,
    OptionType,
)
from .order_book import OrderBookState
from .state import InstrumentState, InstrumentStateSnapshot
from .store import InstrumentStateStore, StateChangeListener

__all__ = [
    "Candle",
    "Exchange",
    "InstrumentId",
    "InstrumentMetadata",
    "InstrumentRegistry",
    "InstrumentState",
    "InstrumentStateSnapshot",
    "InstrumentStateStore",
    "InstrumentType",
    "OptionType",
    "OrderBookState",
    "StateChangeListener",
    "TimeFrame",
]
