"""
Tradego Feature & Quantitative Analytics Layer (Phase 4).

Public Package Exports:
- Models: FeatureValue, FeatureSnapshot, FeatureQuality
- Base Contracts: BaseIndicator, BaseMicrostructureFeature
- Indicators:
    StreamingEMA, StreamingSMA, StreamingRSI, StreamingMACD,
    StreamingATR, RollingBollingerBands, StreamingVWAP, VolumeZScore,
    OpenInterestQuadrant, QUADRANT_ENCODING
- Microstructure Features:
    BookImbalance, WeightedMidPrice, SpreadBps, TradeFlowImbalance, TickIntensity
- State & Engine:
    InstrumentFeatureStore, FeatureEngine
"""

from .base import BaseIndicator, BaseMicrostructureFeature
from .engine import FeatureEngine
from .indicators import (
    QUADRANT_ENCODING,
    OpenInterestQuadrant,
    RollingBollingerBands,
    StreamingATR,
    StreamingEMA,
    StreamingMACD,
    StreamingRSI,
    StreamingSMA,
    StreamingVWAP,
    VolumeZScore,
)
from .microstructure import (
    BookImbalance,
    SpreadBps,
    TickIntensity,
    TradeFlowImbalance,
    WeightedMidPrice,
)
from .models import FeatureQuality, FeatureSnapshot, FeatureValue
from .store import InstrumentFeatureStore

__all__ = [
    # Models
    "FeatureValue",
    "FeatureSnapshot",
    "FeatureQuality",
    # Base Contracts
    "BaseIndicator",
    "BaseMicrostructureFeature",
    # Streaming Indicators
    "StreamingEMA",
    "StreamingSMA",
    "StreamingRSI",
    "StreamingMACD",
    "StreamingATR",
    "RollingBollingerBands",
    "StreamingVWAP",
    "VolumeZScore",
    "OpenInterestQuadrant",
    "QUADRANT_ENCODING",
    # Microstructure Features
    "BookImbalance",
    "WeightedMidPrice",
    "SpreadBps",
    "TradeFlowImbalance",
    "TickIntensity",
    # Store & Engine
    "InstrumentFeatureStore",
    "FeatureEngine",
]
