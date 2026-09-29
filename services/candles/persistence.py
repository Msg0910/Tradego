"""
Tradego Historical Candle Persistence Interface Boundary (Phase 4+).

Defines the abstract contract for storing, querying, and retrieving historical
candles across timeframes. Implementations (e.g. Parquet, TimescaleDB, ClickHouse)
will be introduced in future phases without altering the in-memory CandleEngine logic.

RULE:
Do NOT implement database I/O, file persistence, or external network storage in Phase 3.
Only this abstract interface is defined.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from services.market_state.instrument import InstrumentId
from .models import Candle
from .timeframe import TimeFrame


class CandlePersistenceInterface(ABC):
    """
    Abstract storage boundary for historical candle data.
    Enables future historical persistence (Phase 4+) without modifying candle engine logic.
    """

    @abstractmethod
    def save_candle(self, candle: Candle) -> None:
        """
        Persists a single finalized candle to storage.
        """
        ...

    @abstractmethod
    def save_batch(self, candles: List[Candle]) -> None:
        """
        Persists a batch of finalized candles to storage.
        """
        ...

    @abstractmethod
    def load_candles(
        self,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
        start_time: datetime,
        end_time: datetime,
    ) -> List[Candle]:
        """
        Retrieves finalized historical candles for an instrument and timeframe
        within [start_time, end_time).
        """
        ...
