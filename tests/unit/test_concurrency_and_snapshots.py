"""
Concurrency and Snapshot Immutability Tests for InstrumentStateStore.
"""

import threading
import time
import unittest
from dataclasses import FrozenInstanceError

from services.market_gateway.models import DepthLevel, MarketDepth, MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)
from services.market_state.store import InstrumentStateStore


class TestConcurrencyAndSnapshots(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.instruments = []
        for i in range(5):
            sym = f"SYM{i}"
            inst = InstrumentId(symbol=sym, exchange=Exchange.NSE, instrument_type=InstrumentType.EQUITY)
            self.registry.register(inst, {"ATMSTOX": f"TOKEN_{i}"})
            self.instruments.append(inst)

        self.store = InstrumentStateStore(registry=self.registry)

    def test_snapshot_immutability(self):
        inst = self.instruments[0]
        # Ingest tick 1
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="TOKEN_0",
                ltp=100.0,
                local_receive_timestamp=1.0,
            )
        )

        snapshot_1 = self.store.get_snapshot(inst)
        self.assertIsNotNone(snapshot_1)
        self.assertEqual(snapshot_1.ltp, 100.0)

        # Ingest tick 2
        self.store.on_market_event(
            MarketEvent(
                provider="ATMSTOX",
                provider_symbol_id="TOKEN_0",
                ltp=105.0,
                local_receive_timestamp=2.0,
            )
        )

        # Snapshot 1 remains completely frozen and unchanged
        self.assertEqual(snapshot_1.ltp, 100.0)

        # New snapshot reflects updated price
        snapshot_2 = self.store.get_snapshot(inst)
        self.assertEqual(snapshot_2.ltp, 105.0)

        # Frozen dataclass rejects attribute assignment
        with self.assertRaises(FrozenInstanceError):
            snapshot_1.ltp = 999.0

    def test_concurrent_producers_and_readers(self):
        """
        Spawns 5 producer threads streaming ticks concurrently across 5 instruments,
        and 3 reader threads continuously taking snapshots.
        Ensures zero deadlocks, zero exceptions, and consistent state.
        """
        num_updates_per_thread = 500
        stop_event = threading.Event()
        errors = []

        def producer_worker(thread_idx: int):
            try:
                for j in range(num_updates_per_thread):
                    token_idx = (thread_idx + j) % 5
                    token_id = f"TOKEN_{token_idx}"
                    ev = MarketEvent(
                        provider="ATMSTOX",
                        provider_symbol_id=token_id,
                        ltp=100.0 + (j % 50),
                        open=100.0,
                        total_volume=float(j),
                        local_receive_timestamp=time.perf_counter(),
                    )
                    self.store.on_market_event(ev)
            except Exception as e:
                errors.append(e)

        def reader_worker():
            try:
                while not stop_event.is_set():
                    # Read single snapshot
                    inst = self.instruments[0]
                    snap = self.store.get_snapshot(inst)
                    if snap:
                        self.assertGreaterEqual(snap.ltp, 100.0)
                    # Read all snapshots
                    all_snaps = self.store.get_all_snapshots()
                    self.assertIsInstance(all_snaps, dict)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        # Start readers
        readers = [threading.Thread(target=reader_worker) for _ in range(3)]
        for r in readers:
            r.start()

        # Start producers
        producers = [threading.Thread(target=producer_worker, args=(i,)) for i in range(5)]
        for p in producers:
            p.start()

        for p in producers:
            p.join()

        # Stop readers
        stop_event.set()
        for r in readers:
            r.join()

        self.assertEqual(len(errors), 0, f"Encountered concurrency errors: {errors}")

        # Verify all instruments received updates
        total_tracked = self.store.tracked_count
        self.assertEqual(total_tracked, 5)
        for inst in self.instruments:
            snap = self.store.get_snapshot(inst)
            self.assertIsNotNone(snap)
            self.assertGreaterEqual(snap.update_count, num_updates_per_thread // 2)


if __name__ == "__main__":
    unittest.main()
