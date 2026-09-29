"""
Unit tests for CandleEngine: Central Coordinator, Registry Resolution,
Listener Error Isolation, and Session Finalization Hooks.
"""

import unittest
from datetime import datetime, timedelta

from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.models import Candle
from services.candles.timeframe import TF_1M, TF_5M
from services.candles.volume import IncrementalVolumePolicy
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)


def make_event(
    provider: str,
    token: str,
    dt: datetime,
    ltp: float,
    vol: float = 10.0,
) -> MarketEvent:
    return MarketEvent(
        provider=provider,
        provider_symbol_id=token,
        exchange_timestamp=dt,
        ltp=ltp,
        tick_volume=vol,
        local_receive_timestamp=100.0,
    )


class TestCandleEngine(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.cal = IndianMarketCalendar()
        self.engine = CandleEngine(
            registry=self.registry,
            calendar=self.cal,
            volume_policy_factory=lambda iid: IncrementalVolumePolicy(),
        )

        # Register RELIANCE
        self.reliance_id = InstrumentId("RELIANCE", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(
            instrument_id=self.reliance_id,
            provider_tokens={"ATMSTOX": "TOKEN_RELIANCE"},
        )

        # Register INFOSYS
        self.infy_id = InstrumentId("INFY", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(
            instrument_id=self.infy_id,
            provider_tokens={"ATMSTOX": "TOKEN_INFY"},
        )

    def test_registered_instrument_ingestion(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        ev = make_event("ATMSTOX", "TOKEN_RELIANCE", t0, ltp=2500.0, vol=50.0)

        closed = self.engine.on_market_event(ev)
        self.assertEqual(len(closed), 0)
        self.assertEqual(self.engine.active_instruments_count, 1)

        act = self.engine.get_active_candle(self.reliance_id, TF_1M)
        self.assertIsNotNone(act)
        self.assertEqual(act.open, 2500.0)
        self.assertEqual(act.volume, 50.0)

    def test_unresolved_token_graceful_handling(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        ev_unknown = make_event("ATMSTOX", "TOKEN_UNKNOWN_999", t0, ltp=100.0)

        closed = self.engine.on_market_event(ev_unknown)
        self.assertEqual(len(closed), 0)
        self.assertEqual(self.engine.unresolved_ticks_count, 1)
        self.assertEqual(self.engine.active_instruments_count, 0)

    def test_listener_dispatch_and_error_isolation(self):
        received_candles = []
        error_called = []

        def good_listener(candle: Candle):
            received_candles.append(candle)

        def failing_listener(candle: Candle):
            error_called.append(True)
            raise RuntimeError("Intentional downstream failure")

        self.engine.add_candle_listener(good_listener)
        self.engine.add_candle_listener(failing_listener)

        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        t1 = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)

        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t0, ltp=2500.0))
        # This tick closes both the 1S bar and the 1M bar!
        closed = self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t1, ltp=2510.0))

        self.assertEqual(len(closed), 2)  # 1S and 1M
        self.assertEqual(len(received_candles), 2)
        self.assertEqual(len(error_called), 2)
        self.assertEqual(self.engine.listener_error_count, 2)

        # Unregister listener
        self.engine.remove_candle_listener(good_listener)
        self.engine.remove_candle_listener(failing_listener)

    def test_multi_instrument_management(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t0, ltp=2500.0))
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_INFY", t0, ltp=1500.0))

        self.assertEqual(self.engine.active_instruments_count, 2)

        rel_act = self.engine.get_active_candle(self.reliance_id, TF_1M)
        infy_act = self.engine.get_active_candle(self.infy_id, TF_1M)
        self.assertEqual(rel_act.open, 2500.0)
        self.assertEqual(infy_act.open, 1500.0)

    def test_finalize_until_and_force_finalize_all(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t0, ltp=2500.0))
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_INFY", t0, ltp=1500.0))

        # Explicit finalization at 09:16:00
        t_due = datetime(2026, 9, 14, 9, 16, 0, tzinfo=INDIA_TZ)
        closed = self.engine.finalize_until(t_due)
        # Both RELIANCE and INFY 1M bars should be finalized
        rel_closed = [c for c in closed if c.instrument_id == self.reliance_id and c.timeframe == TF_1M]
        infy_closed = [c for c in closed if c.instrument_id == self.infy_id and c.timeframe == TF_1M]
        self.assertEqual(len(rel_closed), 1)
        self.assertEqual(len(infy_closed), 1)

        # Force finalize all
        forced = self.engine.force_finalize_all()
        self.assertIsInstance(forced, list)

    def test_history_query(self):
        t0 = datetime(2026, 9, 14, 9, 15, 10, tzinfo=INDIA_TZ)
        t1 = datetime(2026, 9, 14, 9, 16, 10, tzinfo=INDIA_TZ)
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t0, ltp=2500.0))
        self.engine.on_market_event(make_event("ATMSTOX", "TOKEN_RELIANCE", t1, ltp=2505.0))

        hist = self.engine.get_history(self.reliance_id, TF_1M)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].open, 2500.0)

        # Non-existent instrument
        unknown_id = InstrumentId("UNKNOWN", Exchange.NSE, InstrumentType.EQUITY)
        self.assertEqual(self.engine.get_history(unknown_id, TF_1M), [])
        self.assertIsNone(self.engine.get_active_candle(unknown_id, TF_1M))


if __name__ == "__main__":
    unittest.main()
