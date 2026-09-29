"""
Hot-Path Order Book Microstructure Features for Tradego Analytics (Phase 4).

Implements:
- BookImbalance: Distance-weighted Level 1-4 order book imbalance.
- WeightedMidPrice: Instantaneous micro-price weighted by top-level queue balance.
- SpreadBps: Effective bid-ask spread in basis points.

Strictly O(1) arithmetic on InstrumentStateSnapshot. Zero allocations on hot path.
"""

from datetime import datetime
from typing import Optional

from services.market_gateway.models import MarketEvent
from services.market_state.state import InstrumentStateSnapshot
from ..base import BaseMicrostructureFeature
from ..models import FeatureQuality, FeatureValue


class BookImbalance(BaseMicrostructureFeature):
    """
    Computes distance-weighted Level 1-4 Order Book Imbalance (OBI).
    Weights: [1.0, 0.75, 0.50, 0.25] for levels 1 through 4.
    Range: [-1.0, +1.0] where +1.0 = pure buy pressure, -1.0 = pure sell pressure.
    """

    def __init__(self, name: str = "BOOK_IMBALANCE") -> None:
        super().__init__(name)

    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        ob = snapshot.order_book
        ts = snapshot.last_exchange_timestamp or snapshot.last_provider_timestamp or datetime.now()

        # Check for empty depth
        if not ob.bids and not ob.asks:
            return FeatureValue(
                feature_id=self.feature_id,
                value=0.0,
                quality=FeatureQuality.VALID,
                observation_timestamp=ts,
                availability_timestamp=ts,
                is_confirmed=True,
                metadata={"reason": "EMPTY_BOOK"},
            )

        weights = (1.0, 0.75, 0.50, 0.25)
        weighted_buy = 0.0
        weighted_sell = 0.0

        for i, level in enumerate(ob.bids[:4]):
            weighted_buy += level.quantity * weights[i]

        for i, level in enumerate(ob.asks[:4]):
            weighted_sell += level.quantity * weights[i]

        total_weighted = weighted_buy + weighted_sell
        if total_weighted > 0.0:
            imbalance = (weighted_buy - weighted_sell) / total_weighted
            quality = FeatureQuality.VALID
        else:
            imbalance = 0.0
            quality = FeatureQuality.VALID

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(imbalance, 4),
            quality=quality,
            observation_timestamp=ts,
            availability_timestamp=ts,
            is_confirmed=True,
            metadata={
                "weighted_buy_qty": weighted_buy,
                "weighted_sell_qty": weighted_sell,
            },
        )

    def reset(self) -> None:
        pass


class WeightedMidPrice(BaseMicrostructureFeature):
    """
    Computes the Micro-Price (volume-weighted mid price):
    P_micro = (Q_bid * P_ask + Q_ask * P_bid) / (Q_bid + Q_ask)
    Adjusts the standard mid-price toward the side with less liquidity.
    """

    def __init__(self, name: str = "MICRO_PRICE") -> None:
        super().__init__(name)

    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        ob = snapshot.order_book
        ts = snapshot.last_exchange_timestamp or snapshot.last_provider_timestamp or datetime.now()

        if ob.best_bid is None or ob.best_ask is None:
            # Fallback to LTP if one side is missing
            fallback_val = snapshot.ltp or 0.0
            return FeatureValue(
                feature_id=self.feature_id,
                value=fallback_val,
                quality=FeatureQuality.DEGRADED if snapshot.ltp else FeatureQuality.INVALID,
                observation_timestamp=ts,
                availability_timestamp=ts,
                is_confirmed=True,
                metadata={"reason": "ONE_SIDED_BOOK"},
            )

        w_mid = ob.weighted_mid_price
        if w_mid is not None and w_mid > 0.0:
            micro_price = w_mid
            quality = FeatureQuality.VALID
        else:
            micro_price = ob.mid_price or 0.0
            quality = FeatureQuality.VALID if micro_price > 0.0 else FeatureQuality.INVALID

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(micro_price, 4),
            quality=quality,
            observation_timestamp=ts,
            availability_timestamp=ts,
            is_confirmed=True,
            metadata={"best_bid": ob.best_bid, "best_ask": ob.best_ask},
        )

    def reset(self) -> None:
        pass


class SpreadBps(BaseMicrostructureFeature):
    """
    Computes effective bid-ask spread in basis points:
    Spread_bps = (P_ask - P_bid) / ((P_ask + P_bid) / 2) * 10,000
    """

    def __init__(self, name: str = "SPREAD_BPS") -> None:
        super().__init__(name)

    def compute(
        self,
        snapshot: InstrumentStateSnapshot,
        event: Optional[MarketEvent] = None,
    ) -> FeatureValue:
        ob = snapshot.order_book
        ts = snapshot.last_exchange_timestamp or snapshot.last_provider_timestamp or datetime.now()

        if ob.best_bid is None or ob.best_ask is None:
            return FeatureValue(
                feature_id=self.feature_id,
                value=0.0,
                quality=FeatureQuality.INVALID,
                observation_timestamp=ts,
                availability_timestamp=ts,
                is_confirmed=True,
                metadata={"reason": "MISSING_BID_OR_ASK"},
            )

        mid = (ob.best_ask + ob.best_bid) / 2.0
        if mid <= 0.0:
            return FeatureValue(
                feature_id=self.feature_id,
                value=0.0,
                quality=FeatureQuality.INVALID,
                observation_timestamp=ts,
                availability_timestamp=ts,
                is_confirmed=True,
                metadata={"reason": "ZERO_MID_PRICE"},
            )

        spread = ob.best_ask - ob.best_bid
        spread_bps = (spread / mid) * 10000.0

        return FeatureValue(
            feature_id=self.feature_id,
            value=round(spread_bps, 2),
            quality=FeatureQuality.VALID,
            observation_timestamp=ts,
            availability_timestamp=ts,
            is_confirmed=True,
            metadata={"spread": round(spread, 4), "mid_price": round(mid, 4)},
        )

    def reset(self) -> None:
        pass
