"""
Unit tests for InstrumentStateStore and Gateway Integration.
"""

import unittest
from typing import List

from services.market_gateway.gateway import MarketDataGateway
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.state import InstrumentStateSnapshot
from services.market_state.store import InstrumentStateStore


class TestInstrumentStateStore(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.inst_360one = InstrumentId(
            symbol="360ONE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.registry.register(
            self.inst_360one,
            provider_tokens={"ATMSTOX": "13061"},
        )
        self.store = InstrumentStateStore(registry=self.registry)

    def test_registered_token_ingest_and_snapshot(self):
        ev = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="13061",
            ltp=1500.0,
            open=1480.0,
            previous_close=1490.0,
            local_receive_timestamp=1.0,
        )

        self.store.on_market_event(ev)

        # Lookup by InstrumentId
        snapshot = self.store.get_snapshot(self.inst_360one)
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.is_resolved)
        self.assertEqual(snapshot.instrument_id, self.inst_360one)
        self.assertEqual(snapshot.ltp, 1500.0)
        self.assertEqual(snapshot.open, 1480.0)

        # Lookup by provider token
        snap_token = self.store.get_snapshot_by_token("ATMSTOX", "13061")
        self.assertEqual(snap_token, snapshot)

    def test_unresolved_token_handling(self):
        """
        MANDATORY REQUIREMENT:
        NEVER silently fallback an unresolved provider token to Exchange.NSE.
        Unknown instruments must remain explicitly unresolved.
        """
        unknown_ev = MarketEvent(
            provider="ATMSTOX",
            provider_symbol_id="888888",
            ltp=450.0,
            local_receive_timestamp=1.0,
        )

        self.store.on_market_event(unknown_ev)

        # Snapshot by token should exist but be UNRESOLVED
        snap = self.store.get_snapshot_by_token("ATMSTOX", "888888")
        self.assertIsNotNone(snap)
        self.assertFalse(snap.is_resolved)
        self.assertIsNone(snap.instrument_id)  # NOT silently set to NSE!
        self.assertEqual(snap.ltp, 450.0)

        self.assertIn(("ATMSTOX", "888888"), self.store.unresolved_tokens)

        # Lookup by InstrumentId should return None since it was never mapped
        all_snaps = self.store.get_all_snapshots()
        self.assertNotIn("888888", [s.provider_symbol_id for s in all_snaps.values()])

    def test_late_registration_of_unresolved_token(self):
        # 1. Ingest unknown tick
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="777777",
                ltp=200.0,
                local_receive_timestamp=1.0,
            )
        )
        snap_before = self.store.get_snapshot_by_token("ATMSTOX", "777777")
        self.assertFalse(snap_before.is_resolved)

        # 2. Register metadata later
        late_inst = InstrumentId(
            symbol="INFY",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.registry.register(late_inst, {"ATMSTOX": "777777"})

        # 3. Next tick arrives
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="777777",
                ltp=202.0,
                local_receive_timestamp=2.0,
            )
        )

        # 4. Now state is resolved!
        snap_after = self.store.get_snapshot(late_inst)
        self.assertIsNotNone(snap_after)
        self.assertTrue(snap_after.is_resolved)
        self.assertEqual(snap_after.ltp, 202.0)

    def test_gateway_listener_attachment(self):
        gateway = MarketDataGateway()
        self.store.attach_to_gateway(gateway)

        # Verify gateway listeners receive normalized event from provider raw tick
        raw_tick = {"LTP": 1250.0}
        gateway._on_provider_tick("ATMSTOX", "13061", raw_tick, 1.0)

        snap = self.store.get_snapshot(self.inst_360one)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.ltp, 1250.0)

        self.store.detach_from_gateway(gateway)

    def test_state_change_listener_and_error_isolation(self):
        received_snapshots: List[InstrumentStateSnapshot] = []

        def failing_listener(s: InstrumentStateSnapshot):
            raise RuntimeError("Listener exception should be caught")

        def working_listener(s: InstrumentStateSnapshot):
            received_snapshots.append(s)

        self.store.add_listener(failing_listener)
        self.store.add_listener(working_listener)

        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=100.0,
                local_receive_timestamp=1.0,
            )
        )

        # Working listener received snapshot despite failing listener error
        self.assertEqual(len(received_snapshots), 1)
        self.assertEqual(received_snapshots[0].ltp, 100.0)

        # Remove listener
        self.store.remove_listener(working_listener)
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=101.0,
                local_receive_timestamp=2.0,
            )
        )
        self.assertEqual(len(received_snapshots), 1)

    def test_reset_session_across_store(self):
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="13061",
                ltp=150.0,
                open=140.0,
                previous_close=140.0,
                total_volume=1000.0,
                local_receive_timestamp=1.0,
            )
        )

        self.store.reset_session()

        snap = self.store.get_snapshot(self.inst_360one)
        self.assertEqual(snap.open, 150.0)
        self.assertEqual(snap.total_volume, 0.0)
        self.assertEqual(snap.previous_close, 140.0)


if __name__ == "__main__":
    unittest.main()
