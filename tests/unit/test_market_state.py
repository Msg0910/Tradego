"""
Unit tests for InstrumentState and InstrumentStateSnapshot.
"""

import unittest
from datetime import datetime, timezone

from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType
from services.market_state.state import InstrumentState, InstrumentStateSnapshot


class TestInstrumentState(unittest.TestCase):

    def setUp(self):
        self.inst_id = InstrumentId(
            symbol="360ONE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.state = InstrumentState(
            provider="ATMSTOX",
            provider_symbol_id="13061",
            instrument_id=self.inst_id,
            is_resolved=True,
        )

    def test_first_tick_initialization(self):
        dt1 = datetime(2026, 9, 12, 9, 15, 0, tzinfo=timezone.utc)
        ev1 = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="13061",
            ltp=100.0,
            ltp_qty=10,
            open=100.0,
            high=100.0,
            low=100.0,
            previous_close=98.0,
            tick_volume=50.5,  # fractional volume
            total_volume=50.5,
            oi=1000,
            previous_open_interest_close=950.5,  # semantic float
            local_receive_timestamp=10.0,
            local_receive_datetime=dt1,
            provider_timestamp=dt1,
            exchange_timestamp=dt1,
        )

        applied = self.state.update_from_event(ev1)
        self.assertTrue(applied)
        self.assertEqual(self.state.update_count, 1)
        self.assertEqual(self.state.ltp, 100.0)
        self.assertIsNone(self.state.prev_ltp)
        self.assertEqual(self.state.tick_direction, 0)
        self.assertEqual(self.state.open, 100.0)
        self.assertEqual(self.state.high, 100.0)
        self.assertEqual(self.state.low, 100.0)
        self.assertEqual(self.state.previous_close, 98.0)
        self.assertEqual(self.state.change, 2.0)
        self.assertAlmostEqual(self.state.change_percent, (2.0 / 98.0) * 100.0, places=3)
        self.assertEqual(self.state.tick_volume, 50.5)
        self.assertEqual(self.state.previous_open_interest_close, 950.5)

        # Timestamps separated
        self.assertEqual(self.state.last_local_receive_timestamp, 10.0)
        self.assertEqual(self.state.last_local_receive_datetime, dt1)
        self.assertEqual(self.state.last_exchange_timestamp, dt1)
        self.assertEqual(self.state.last_provider_timestamp, dt1)

    def test_subsequent_tick_direction_and_high_low(self):
        dt1 = datetime(2026, 9, 12, 9, 15, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 9, 12, 9, 15, 1, tzinfo=timezone.utc)
        dt3 = datetime(2026, 9, 12, 9, 15, 2, tzinfo=timezone.utc)

        # Tick 1: 100.0
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=100.0,
                provider_timestamp=dt1,
                local_receive_timestamp=1.0,
            )
        )

        # Tick 2: 102.5 (Uptick & New High)
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=102.5,
                provider_timestamp=dt2,
                local_receive_timestamp=2.0,
            )
        )
        self.assertEqual(self.state.ltp, 102.5)
        self.assertEqual(self.state.prev_ltp, 100.0)
        self.assertEqual(self.state.tick_direction, 1)  # Uptick
        self.assertEqual(self.state.high, 102.5)

        # Tick 3: 101.0 (Downtick)
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=101.0,
                provider_timestamp=dt3,
                local_receive_timestamp=3.0,
            )
        )
        self.assertEqual(self.state.ltp, 101.0)
        self.assertEqual(self.state.prev_ltp, 102.5)
        self.assertEqual(self.state.tick_direction, -1)  # Downtick
        self.assertEqual(self.state.high, 102.5)  # High preserved

    def test_non_destructive_merge(self):
        """
        CRITICAL RULE: Never silently overwrite valid state with None / missing fields.
        """
        dt1 = datetime(2026, 9, 12, 9, 15, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 9, 12, 9, 15, 1, tzinfo=timezone.utc)

        # Full Tick 1
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=100.0,
                open=100.0,
                high=105.0,
                low=95.0,
                previous_close=99.0,
                upper_circuit=110.0,
                lower_circuit=90.0,
                oi=5000,
                provider_timestamp=dt1,
                local_receive_timestamp=1.0,
            )
        )

        # Sparse Tick 2 (omits open, circuits, previous_close, oi)
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=101.0,
                open=None,
                high=None,
                low=None,
                previous_close=None,
                upper_circuit=None,
                lower_circuit=None,
                oi=None,
                provider_timestamp=dt2,
                local_receive_timestamp=2.0,
            )
        )

        # Verified: Previous valid values were preserved!
        self.assertEqual(self.state.ltp, 101.0)
        self.assertEqual(self.state.open, 100.0)
        self.assertEqual(self.state.high, 105.0)
        self.assertEqual(self.state.low, 95.0)
        self.assertEqual(self.state.previous_close, 99.0)
        self.assertEqual(self.state.upper_circuit, 110.0)
        self.assertEqual(self.state.lower_circuit, 90.0)
        self.assertEqual(self.state.oi, 5000)

    def test_intra_second_burst_vs_out_of_order(self):
        dt1 = datetime(2026, 9, 12, 9, 15, 10, tzinfo=timezone.utc)
        dt_older = datetime(2026, 9, 12, 9, 15, 8, tzinfo=timezone.utc)

        # Baseline tick at 09:15:10
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=100.0,
                provider_timestamp=dt1,
                local_receive_timestamp=1.0,
            )
        )

        # Intra-second tick (identical timestamp 09:15:10, higher local_receive_timestamp)
        applied_burst = self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=100.5,
                provider_timestamp=dt1,
                local_receive_timestamp=1.1,
            )
        )
        self.assertTrue(applied_burst)
        self.assertEqual(self.state.ltp, 100.5)

        # Out-of-order tick (older timestamp 09:15:08 by 2 seconds)
        applied_stale = self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=90.0,  # Old price
                provider_timestamp=dt_older,
                local_receive_timestamp=1.2,
            )
        )
        self.assertFalse(applied_stale)
        self.assertEqual(self.state.out_of_order_count, 1)
        self.assertEqual(self.state.ltp, 100.5)  # Not corrupted by stale tick!

    def test_reset_session(self):
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=150.0,
                open=140.0,
                high=155.0,
                low=138.0,
                previous_close=140.0,
                total_volume=5000.0,
                local_receive_timestamp=1.0,
            )
        )
        self.state.reset_session(initial_price=150.0)

        self.assertEqual(self.state.open, 150.0)
        self.assertEqual(self.state.high, 150.0)
        self.assertEqual(self.state.low, 150.0)
        self.assertEqual(self.state.total_volume, 0.0)
        self.assertEqual(self.state.tick_volume, 0.0)
        self.assertEqual(self.state.tick_direction, 0)
        # Reference data preserved
        self.assertEqual(self.state.previous_close, 140.0)
        self.assertEqual(self.state.instrument_id, self.inst_id)

    def test_snapshot_creation_and_no_market_event_retention(self):
        """
        RULE: Do not retain the complete MarketEvent inside InstrumentState/Snapshot.
        """
        self.state.update_from_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=125.0,
                local_receive_timestamp=1.0,
            )
        )

        snapshot = self.state.create_snapshot()
        self.assertIsInstance(snapshot, InstrumentStateSnapshot)
        self.assertEqual(snapshot.ltp, 125.0)
        self.assertEqual(snapshot.provider, "ATMSTOX")
        self.assertEqual(snapshot.provider_symbol_id, "13061")
        self.assertEqual(snapshot.instrument_id, self.inst_id)
        self.assertTrue(snapshot.is_resolved)

        # Assert last_market_event is NOT an attribute of InstrumentStateSnapshot
        self.assertFalse(hasattr(snapshot, "last_market_event"))
        self.assertFalse(hasattr(self.state, "last_market_event"))


if __name__ == "__main__":
    unittest.main()
