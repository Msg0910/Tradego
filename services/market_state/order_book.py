"""
Order Book State Representation for Tradego Market State Layer.

Maintains a 4-level market depth order book with allocation-conscious
analytics (spread, mid price, weighted mid price, book imbalance).
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple

from services.market_gateway.models import DepthLevel, MarketDepth


@dataclass(slots=True)
class OrderBookState:
    """
    In-memory representation of 4-level order book for an instrument.
    Uses immutable tuples of DepthLevel to allow zero-copy snapshots.
    """
    bids: Tuple[DepthLevel, ...] = ()
    asks: Tuple[DepthLevel, ...] = ()
    total_buy_qty: Optional[int] = None
    total_sell_qty: Optional[int] = None

    @classmethod
    def from_market_depth(cls, depth: Optional[MarketDepth]) -> "OrderBookState":
        """Builds OrderBookState from a normalized MarketDepth object."""
        if not depth:
            return cls()
        return cls(
            bids=tuple(depth.bids[:4]),
            asks=tuple(depth.asks[:4]),
            total_buy_qty=depth.total_buy_qty,
            total_sell_qty=depth.total_sell_qty,
        )

    @property
    def best_bid(self) -> Optional[float]:
        """Level 1 Best Bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_bid_qty(self) -> Optional[int]:
        """Level 1 Best Bid quantity."""
        return self.bids[0].quantity if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Level 1 Best Ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def best_ask_qty(self) -> Optional[int]:
        """Level 1 Best Ask quantity."""
        return self.asks[0].quantity if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        """Spread between best ask and best bid."""
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            return ba - bb
        return None

    @property
    def mid_price(self) -> Optional[float]:
        """Simple mid price between best bid and best ask."""
        bb = self.best_bid
        ba = self.best_ask
        if bb is not None and ba is not None:
            return (bb + ba) / 2.0
        return None

    @property
    def weighted_mid_price(self) -> Optional[float]:
        """
        Volume-weighted mid price at Level 1:
        (best_bid * best_ask_qty + best_ask * best_bid_qty) / (best_bid_qty + best_ask_qty)
        """
        bb = self.best_bid
        ba = self.best_ask
        bq = self.best_bid_qty
        aq = self.best_ask_qty

        if bb is not None and ba is not None and bq is not None and aq is not None:
            total_qty = bq + aq
            if total_qty > 0:
                return (bb * aq + ba * bq) / total_qty
        return self.mid_price

    @property
    def book_imbalance(self) -> Optional[float]:
        """
        Order book quantity imbalance:
        (total_buy_qty - total_sell_qty) / (total_buy_qty + total_sell_qty)
        Range: [-1.0, 1.0].
        Returns 0.0 if balanced, None if totals are unavailable.
        """
        tb = self.total_buy_qty
        ts = self.total_sell_qty

        if tb is not None and ts is not None:
            denom = tb + ts
            if denom > 0:
                return (tb - ts) / denom
            return 0.0
        return None
