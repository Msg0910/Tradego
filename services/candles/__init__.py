"""
Tradego Candle & Time-Series Aggregation Layer (Phase 3).

High-throughput, real-time multi-timeframe candle engine featuring:
- Session-anchored bucket alignment with IndianMarketCalendar & continuous calendars
- Dual-mode event-driven and coordinator-assisted (finalize_until) candle finalization
- Robust volume accounting (IncrementalVolumePolicy, CumulativeVolumePolicy) with VolumeQuality classification
- Two-path aggregation architecture:
    Path A: 1S -> closed 1M -> higher timeframe cascade (3M..1D)
    Path B: On-demand real-time active higher timeframe previews
- Open Interest tracking (open_oi, high_oi, low_oi, close_oi)
- Bounded fixed-capacity ring buffers per timeframe
- Abstract historical persistence interface (Phase 4+ boundary)
"""

from .builder import CandleBuilder
from .calendar import (
    DefaultContinuousCalendar,
    ExchangeCalendar,
    IndianMarketCalendar,
    MarketSegment,
    SessionSegment,
    SessionSegmentType,
    TradingSession,
)
from .engine import CandleEngine
from .models import ActiveCandleState, Candle, VolumeQuality
from .persistence import CandlePersistenceInterface
from .series import DEFAULT_RING_BUFFER_CAPACITIES, InstrumentCandleSeries
from .timeframe import (
    TF_1D,
    TF_1H,
    TF_1M,
    TF_1S,
    TF_3M,
    TF_5M,
    TF_15M,
    TF_30M,
    TF_TICK,
    TimeFrame,
    TimeFrameType,
)
from .volume import (
    CumulativeVolumePolicy,
    IncrementalVolumePolicy,
    VolumeAccountingPolicy,
)

__all__ = [
    # Models
    "Candle",
    "ActiveCandleState",
    "VolumeQuality",
    # TimeFrame
    "TimeFrame",
    "TimeFrameType",
    "TF_TICK",
    "TF_1S",
    "TF_1M",
    "TF_3M",
    "TF_5M",
    "TF_15M",
    "TF_30M",
    "TF_1H",
    "TF_1D",
    # Calendar
    "ExchangeCalendar",
    "IndianMarketCalendar",
    "DefaultContinuousCalendar",
    "MarketSegment",
    "TradingSession",
    "SessionSegment",
    "SessionSegmentType",
    # Volume
    "VolumeAccountingPolicy",
    "IncrementalVolumePolicy",
    "CumulativeVolumePolicy",
    # Aggregation
    "CandleBuilder",
    "InstrumentCandleSeries",
    "DEFAULT_RING_BUFFER_CAPACITIES",
    "CandleEngine",
    # Persistence
    "CandlePersistenceInterface",
]
