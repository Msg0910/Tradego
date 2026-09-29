"""
Canonical Candle Data Models and Volume Quality Indicators for Tradego.

Provides the immutable Candle contract, mutable ActiveCandleState for
allocation-conscious hot-path processing, and explicit volume semantics.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from services.market_state.instrument import InstrumentId
from .timeframe import TimeFrame


class VolumeQuality(str, Enum):
    """
    Explicit quality and completeness classification for candle volume.
    Guarantees that partial/observed volume is never silently presented
    as complete authoritative exchange volume.
    """
    COMPLETE = "COMPLETE"          # Fully observed from bucket start
    RECONSTRUCTED = "RECONSTRUCTED"# Reconstructed from cumulative delta with validated baseline
    PARTIAL = "PARTIAL"            # Incomplete: observed after mid-bucket startup or reconnect
    ESTIMATED = "ESTIMATED"        # Estimated/interpolated (e.g. from tick quantities)
    UNAVAILABLE = "UNAVAILABLE"    # Feed provides no volume information


@dataclass(frozen=True, slots=True)
class Candle:
    """
    Immutable representation of an OHLCV/OI candle bar.
    Safe for multi-threaded consumer access and historical storage.
    """
    instrument_id: InstrumentId
    timeframe: TimeFrame
    start_time: datetime             # Bucket start timestamp (inclusive), in market timezone
    end_time: datetime               # Bucket end timestamp (exclusive), in market timezone
    open: float
    high: float
    low: float
    close: float
    volume: float                    # Total volume aggregated in this bucket
    ticks: int                       # Total trade ticks aggregated
    volume_quality: VolumeQuality    # Explicit volume completeness indicator
    open_oi: Optional[int] = None    # Open interest at first tick of bucket
    high_oi: Optional[int] = None    # Maximum open interest during bucket
    low_oi: Optional[int] = None     # Minimum open interest during bucket
    close_oi: Optional[int] = None   # Open interest at last tick of bucket
    vwap: Optional[float] = None     # Volume-weighted average price in bucket
    is_closed: bool = False          # True = finalized historical bar, False = forming active bar
    session_id: Optional[str] = None # Unique trading session identifier

    @property
    def is_volume_complete(self) -> bool:
        """True if the volume is authoritative (fully observed or validly reconstructed)."""
        return self.volume_quality in (VolumeQuality.COMPLETE, VolumeQuality.RECONSTRUCTED)


@dataclass(slots=True)
class ActiveCandleState:
    """
    Mutable state structure for an active, in-progress candle bar.
    Allocated once per bucket and updated in-place on the critical tick path.
    """
    instrument_id: InstrumentId
    timeframe: TimeFrame
    start_time: datetime
    end_time: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    ticks: int = 0
    volume_quality: VolumeQuality = VolumeQuality.COMPLETE
    open_oi: Optional[int] = None
    high_oi: Optional[int] = None
    low_oi: Optional[int] = None
    close_oi: Optional[int] = None
    sum_pv: float = 0.0              # Sum of (price * volume) for calculating VWAP
    is_initialized: bool = False
    session_id: Optional[str] = None

    def to_candle(self, is_closed: bool = False) -> Candle:
        """Produces an immutable point-in-time Candle object."""
        vwap = (self.sum_pv / self.volume) if self.volume > 0.0 else self.close
        return Candle(
            instrument_id=self.instrument_id,
            timeframe=self.timeframe,
            start_time=self.start_time,
            end_time=self.end_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            ticks=self.ticks,
            volume_quality=self.volume_quality,
            open_oi=self.open_oi,
            high_oi=self.high_oi,
            low_oi=self.low_oi,
            close_oi=self.close_oi,
            vwap=round(vwap, 4) if vwap is not None else None,
            is_closed=is_closed,
            session_id=self.session_id,
        )

    to_immutable_candle = to_candle


