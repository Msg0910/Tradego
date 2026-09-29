"""
Candle Engine Design Specification (DESIGN ONLY - Phase 2).

This module defines the architectural contracts and data models for future
multi-timeframe OHLCV candle aggregation (tick, 1s, 1m, 3m, 5m, 15m, 30m, 1h, 1d).

NOTE: Under Phase 2 requirements, active candle aggregation is DESIGN ONLY.
Aggregation logic will be implemented in a dedicated future phase.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .instrument import InstrumentId


class TimeFrame(str, Enum):
    """
    Standard candle aggregation intervals.
    """
    TICK = "TICK"
    S1 = "1S"
    M1 = "1M"
    M3 = "3M"
    M5 = "5M"
    M15 = "15M"
    M30 = "30M"
    H1 = "1H"
    D1 = "1D"

    @property
    def seconds(self) -> int:
        """Duration in seconds for fixed timeframes (0 for TICK)."""
        mapping = {
            TimeFrame.TICK: 0,
            TimeFrame.S1: 1,
            TimeFrame.M1: 60,
            TimeFrame.M3: 180,
            TimeFrame.M5: 300,
            TimeFrame.M15: 900,
            TimeFrame.M30: 1800,
            TimeFrame.H1: 3600,
            TimeFrame.D1: 86400,
        }
        return mapping[self]


@dataclass(frozen=True, slots=True)
class Candle:
    """
    Immutable representation of an aggregated OHLCV candle bar.
    """
    instrument_id: InstrumentId
    timeframe: TimeFrame
    start_time: datetime
    end_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    ticks: int
    oi: Optional[int] = None
    is_closed: bool = False
