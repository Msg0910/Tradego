"""
Tradego Market Gateway Services.

Provides high-performance, decoupled market data ingestion and normalization.
"""

from .base import BaseMarketDataProvider, ConnectionState, StateCallback, TickCallback
from .gateway import MarketDataGateway, MarketEventListener
from .metrics import FeedHealthMetrics, SymbolMetrics
from .models import DepthLevel, MarketDepth, MarketEvent

__all__ = [
    "BaseMarketDataProvider",
    "ConnectionState",
    "DepthLevel",
    "FeedHealthMetrics",
    "MarketDataGateway",
    "MarketDepth",
    "MarketEvent",
    "MarketEventListener",
    "StateCallback",
    "SymbolMetrics",
    "TickCallback",
]
