"""
Unit tests for Candle Engine Concurrency: Multi-Instrument Isolation,
Reader-Writer Snapshot Consistency, and Multi-Timeframe Concurrency.
"""

import threading
import time
import unittest
from datetime import datetime, timedelta

from services.candles.calendar import INDIA_TZ, IndianMarketCalendar
from services.candles.engine import CandleEngine
from services.candles.timeframe import TF_1M, TF_5M
from services.candles.volume import IncrementalVolumePolicy
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import (
    Exchange,
    InstrumentId,
    InstrumentRegistry,
    InstrumentType,
)


class TestCandleConcurrency(unittest.TestCase):

    def setUp(self):
        self.registry = InstrumentRegistry()
        self.cal = IndianMarketCalendar()
        self.engine = CandleEngine(
            registry=self.registry,
            calendar=self.cal,
            volume_policy_factory=lambda iid: IncrementalVolumePolicy(),
        )

    def test_multi_instrument_concurrent_ingestion(self):
        """
        Multiple instruments receiving ticks concurrently on separate threads.
        Verifies fine-grained per-instrument isolation with zero deadlock or state corruption.
        """
        num_instruments = 5
        ticks_per_instrument = 200
        instruments = []

        for i in range(num_instruments):
            iid = InstrumentId(f"SYM_{i}", Exchange.NSE, InstrumentType.EQUITY)
            self.registry.register(iid, {"ATMSTOX": f"TOKEN_{i}"})
            instruments.append((iid, f"TOKEN_{i}"))

        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)
        errors = []

        def worker(token: str, symbol_idx: int):
            try:
                for k in range(ticks_per_instrument):
                    # Progress time every 50 ticks to trigger candle transitions
                    tick_time = base_time + timedelta(seconds=k)
                    event = MarketEvent(
                        provider="ATMSTOX",
                        provider_symbol_id=token,
                        exchange_timestamp=tick_time,
                        ltp=100.0 + (symbol_idx * 10) + (k % 5),
                        tick_volume=10.0,
                        local_receive_timestamp=float(k),
                    )
                    self.engine.on_market_event(event)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(token, idx))
            for idx, (iid, token) in enumerate(instruments)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Worker encountered errors: {errors}")
        self.assertEqual(self.engine.active_instruments_count, num_instruments)

        # Check all instruments accumulated history
        for iid, _ in instruments:
            hist = self.engine.get_history(iid, TF_1M)
            self.assertGreater(len(hist), 0)

    def test_reader_writer_concurrency(self):
        """
        Writer thread streaming ticks while concurrent reader threads
        repeatedly query active candle previews and history snapshots.
        """
        iid = InstrumentId("CONCUR_TEST", Exchange.NSE, InstrumentType.EQUITY)
        self.registry.register(iid, {"ATMSTOX": "TOKEN_CONCUR"})

        base_time = datetime(2026, 9, 14, 9, 15, 0, tzinfo=INDIA_TZ)
        stop_event = threading.Event()
        errors = []
        read_counts = [0, 0]

        def writer():
            try:
                for k in range(500):
                    tick_time = base_time + timedelta(milliseconds=k * 200)
                    event = MarketEvent(
                        provider="ATMSTOX",
                        provider_symbol_id="TOKEN_CONCUR",
                        exchange_timestamp=tick_time,
                        ltp=1000.0 + (k % 20),
                        tick_volume=5.0,
                        local_receive_timestamp=float(k),
                    )
                    self.engine.on_market_event(event)
                    if k % 50 == 0:
                        time.sleep(0.001)
            except Exception as e:
                errors.append(e)
            finally:
                stop_event.set()

        def reader_active(idx: int):
            try:
                while not stop_event.is_set():
                    act_1m = self.engine.get_active_candle(iid, TF_1M)
                    act_5m = self.engine.get_active_candle(iid, TF_5M)
                    if act_1m is not None:
                        self.assertFalse(act_1m.is_closed)
                        self.assertGreater(act_1m.high, 0.0)
                    if act_5m is not None:
                        self.assertFalse(act_5m.is_closed)
                    read_counts[idx] += 1
            except Exception as e:
                errors.append(e)

        def reader_history(idx: int):
            try:
                while not stop_event.is_set():
                    hist = self.engine.get_history(iid, TF_1M)
                    for bar in hist:
                        self.assertTrue(bar.is_closed)
                        self.assertGreater(bar.volume, 0.0)
                    read_counts[idx] += 1
            except Exception as e:
                errors.append(e)

        t_writer = threading.Thread(target=writer)
        t_reader1 = threading.Thread(target=reader_active, args=(0,))
        t_reader2 = threading.Thread(target=reader_history, args=(1,))

        t_writer.start()
        t_reader1.start()
        t_reader2.start()

        t_writer.join()
        t_reader1.join()
        t_reader2.join()

        self.assertEqual(len(errors), 0, f"Concurrency errors encountered: {errors}")
        self.assertGreater(read_counts[0], 100)
        self.assertGreater(read_counts[1], 100)


if __name__ == "__main__":
    unittest.main()
