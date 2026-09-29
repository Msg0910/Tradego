"""
Unit tests for Hot-Path Market Microstructure Features in Tradego Analytics (Phase 4).

Tests:
- BookImbalance (Level 1-4 distance-weighted depth imbalance)
- WeightedMidPrice (Micro-Price calculation and fallbacks)
- SpreadBps (Effective spread in basis points and edge bounds)
- TradeFlowImbalance (Signed volume delta and VolumeQuality propagation)
- TickIntensity (Rolling trade ticks per second)
"""

import unittest
from datetime import datetime, timezone
from typing import Optional

from services.analytics.microstructure.flow import TickIntensity, TradeFlowImbalance
from services.analytics.microstructure.order_book import (
    BookImbalance,
    SpreadBps,
    WeightedMidPrice,
)
from services.analytics.models import FeatureQuality
from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.order_book import OrderBookState
from services.market_state.state import InstrumentStateSnapshot


def make_snapshot(
    ltp: float = 100.0,
    tick_direction: int = 1,
    order_book: Optional[OrderBookState] = None,
    ts: Optional[datetime] = None,
) -> InstrumentStateSnapshot:
    from services.market_state.state import InstrumentState
    iid = InstrumentId("RELIANCE", Exchange.NSE, InstrumentType.EQUITY)
    dt = ts or datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)
    ob = order_book or OrderBookState()

    state = InstrumentState(
        instrument_id=iid,
        provider="TEST",
        provider_symbol_id="TOKEN_1",
        is_resolved=True,
    )
    state.ltp = ltp
    state.tick_direction = tick_direction
    state.order_book = ob
    state.last_exchange_timestamp = dt
    state.last_provider_timestamp = dt
    state.last_local_receive_timestamp = 1000.0
    return state.create_snapshot()


class TestMicrostructureFeatures(unittest.TestCase):

    def test_book_imbalance_weighted(self):
        # Symmetrical book
        bids = (
            DepthLevel(price=100.0, quantity=100),
            DepthLevel(price=99.95, quantity=100),
        )
        asks = (
            DepthLevel(price=100.05, quantity=100),
            DepthLevel(price=100.10, quantity=100),
        )
        ob_sym = OrderBookState(bids=bids, asks=asks)
        snap_sym = make_snapshot(order_book=ob_sym)

        bi = BookImbalance()
        fv_sym = bi.compute(snap_sym)
        self.assertEqual(fv_sym.value, 0.0)
        self.assertEqual(fv_sym.quality, FeatureQuality.VALID)

        # Asymmetrical book: Heavy bids
        bids_heavy = (
            DepthLevel(price=100.0, quantity=300),  # w=1.0 -> 300
        )
        asks_light = (
            DepthLevel(price=100.05, quantity=100),  # w=1.0 -> 100
        )
        ob_heavy = OrderBookState(bids=bids_heavy, asks=asks_light)
        snap_heavy = make_snapshot(order_book=ob_heavy)

        # Imbalance: (300 - 100) / (300 + 100) = 200 / 400 = 0.50
        fv_heavy = bi.compute(snap_heavy)
        self.assertEqual(fv_heavy.value, 0.50)

    def test_weighted_mid_price(self):
        # Bid: 100 @ 100 qty, Ask: 102 @ 300 qty
        # Micro-price = (100 * 102 + 300 * 100) / (100 + 300) = (10200 + 30000) / 400 = 40200 / 400 = 100.50
        bids = (DepthLevel(price=100.0, quantity=100),)
        asks = (DepthLevel(price=102.0, quantity=300),)
        ob = OrderBookState(bids=bids, asks=asks)
        snap = make_snapshot(order_book=ob)

        w_mid = WeightedMidPrice()
        fv = w_mid.compute(snap)
        self.assertEqual(fv.value, 100.50)
        self.assertEqual(fv.quality, FeatureQuality.VALID)

    def test_spread_bps(self):
        # Bid: 99.95, Ask: 100.05 -> spread = 0.10, mid = 100.00
        # Spread bps = (0.10 / 100.00) * 10000 = 10.00 bps
        bids = (DepthLevel(price=99.95, quantity=100),)
        asks = (DepthLevel(price=100.05, quantity=100),)
        ob = OrderBookState(bids=bids, asks=asks)
        snap = make_snapshot(order_book=ob)

        sp = SpreadBps()
        fv = sp.compute(snap)
        self.assertEqual(fv.value, 10.00)
        self.assertEqual(fv.quality, FeatureQuality.VALID)

    def test_trade_flow_imbalance_volume_quality_propagation(self):
        flow = TradeFlowImbalance(window_ticks=5)
        snap = make_snapshot(tick_direction=1)

        # Tick 1: Complete tick volume = 10.0 (direction = +1) -> sum = 10.0, VALID
        ev1 = MarketEvent(provider="TEST", provider_symbol_id="T1", tick_volume=10.0, local_receive_timestamp=100.0)
        fv1 = flow.compute(snap, ev1)
        self.assertEqual(fv1.value, 10.0)
        self.assertEqual(fv1.quality, FeatureQuality.VALID)

        # Tick 2: Estimated volume via ltp_qty=5.0 (direction = +1) -> sum = 15.0, DEGRADED
        ev2 = MarketEvent(provider="TEST", provider_symbol_id="T1", tick_volume=None, ltp_qty=5, local_receive_timestamp=101.0)
        fv2 = flow.compute(snap, ev2)
        self.assertEqual(fv2.value, 15.0)
        self.assertEqual(fv2.quality, FeatureQuality.DEGRADED)

        # Tick 3: Missing volume -> INVALID
        ev3 = MarketEvent(provider="TEST", provider_symbol_id="T1", tick_volume=None, ltp_qty=None, local_receive_timestamp=102.0)
        fv3 = flow.compute(snap, ev3)
        self.assertEqual(fv3.quality, FeatureQuality.INVALID)

    def test_tick_intensity_window(self):
        intensity = TickIntensity(window_seconds=2.0)
        snap = make_snapshot()

        # Ingest 4 ticks within 1.0 second (t=100.0 to 101.0)
        for t in [100.0, 100.3, 100.6, 100.9]:
            ev = MarketEvent(provider="TEST", provider_symbol_id="T1", local_receive_timestamp=t)
            fv = intensity.compute(snap, ev)

        # 4 ticks over 2.0 second window = 2.0 ticks/sec
        self.assertEqual(fv.value, 2.0)
        self.assertEqual(fv.quality, FeatureQuality.VALID)

        # Next tick arrives at t=103.0 -> evicts ticks older than 103.0 - 2.0 = 101.0
        ev_later = MarketEvent(provider="TEST", provider_symbol_id="T1", local_receive_timestamp=103.0)
        fv_later = intensity.compute(snap, ev_later)
        # Only t=103.0 is in window -> 1 / 2.0 = 0.50 ticks/sec
        self.assertEqual(fv_later.value, 0.50)


if __name__ == "__main__":
    unittest.main()
