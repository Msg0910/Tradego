"""
Unit tests for VolumeAccountingPolicy, IncrementalVolumePolicy, CumulativeVolumePolicy,
and VolumeQuality classification.
"""

import unittest
from datetime import datetime, timezone

from services.candles.models import ActiveCandleState, VolumeQuality
from services.candles.timeframe import TF_1M
from services.candles.volume import (
    CumulativeVolumePolicy,
    IncrementalVolumePolicy,
)
from services.market_gateway.models import MarketEvent
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


def make_event(
    tick_vol=None,
    total_vol=None,
    ltp=100.0,
    ltp_qty=1,
) -> MarketEvent:
    return MarketEvent(
        provider="TEST",
        provider_symbol_id="TOKEN_1",
        ltp=ltp,
        ltp_qty=ltp_qty,
        tick_volume=tick_vol,
        total_volume=total_vol,
        local_receive_timestamp=1000.0,
    )


class TestVolumeAccounting(unittest.TestCase):

    def setUp(self):
        self.inst_id = InstrumentId("TCS", Exchange.NSE, InstrumentType.EQUITY)
        self.t_start = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)
        self.t_end = datetime(2026, 9, 14, 9, 16, 0, tzinfo=timezone.utc)

    def _make_state(self) -> ActiveCandleState:
        return ActiveCandleState(
            instrument_id=self.inst_id,
            timeframe=TF_1M,
            start_time=self.t_start,
            end_time=self.t_end,
        )

    def test_incremental_volume_normal(self):
        policy = IncrementalVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=None)

        ev1 = make_event(tick_vol=150.0)
        delta, q = policy.compute_tick_volume(ev1, state)
        self.assertEqual(delta, 150.0)
        self.assertEqual(q, VolumeQuality.COMPLETE)

    def test_incremental_volume_fallback_ltp_qty(self):
        policy = IncrementalVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=None)

        ev = make_event(tick_vol=None, ltp_qty=25)
        delta, q = policy.compute_tick_volume(ev, state)
        self.assertEqual(delta, 25.0)
        self.assertEqual(q, VolumeQuality.ESTIMATED)

    def test_incremental_volume_unavailable(self):
        policy = IncrementalVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=None)

        ev = make_event(tick_vol=None, ltp_qty=None)
        delta, q = policy.compute_tick_volume(ev, state)
        self.assertEqual(delta, 0.0)
        self.assertEqual(q, VolumeQuality.UNAVAILABLE)

    def test_cumulative_volume_normal_session(self):
        policy = CumulativeVolumePolicy()
        state = self._make_state()

        # Bucket opens with an established baseline of total_volume = 10,000
        policy.on_bucket_opened(state, current_total_volume=10000.0)
        self.assertEqual(state.volume_quality, VolumeQuality.COMPLETE)

        # Tick 1: total_volume moves to 10,250
        ev1 = make_event(total_vol=10250.0)
        delta1, q1 = policy.compute_tick_volume(ev1, state)
        self.assertEqual(delta1, 250.0)
        self.assertEqual(q1, VolumeQuality.COMPLETE)

        # Tick 2: total_volume moves to 10,500
        ev2 = make_event(total_vol=10500.0)
        delta2, q2 = policy.compute_tick_volume(ev2, state)
        self.assertEqual(delta2, 250.0)
        self.assertEqual(q2, VolumeQuality.COMPLETE)

    def test_cumulative_volume_mid_bucket_startup(self):
        policy = CumulativeVolumePolicy()
        state = self._make_state()

        # Mid-bucket startup: baseline is NOT known when bucket opened
        policy.on_bucket_opened(state, current_total_volume=None)
        self.assertEqual(state.volume_quality, VolumeQuality.PARTIAL)

        # First tick establishes baseline: delta is 0, quality remains PARTIAL
        ev1 = make_event(total_vol=50000.0)
        delta1, q1 = policy.compute_tick_volume(ev1, state)
        self.assertEqual(delta1, 0.0)
        self.assertEqual(q1, VolumeQuality.PARTIAL)

        # Second tick observes 300 shares within the remaining portion of the bucket
        ev2 = make_event(total_vol=50300.0)
        delta2, q2 = policy.compute_tick_volume(ev2, state)
        self.assertEqual(delta2, 300.0)
        self.assertEqual(q2, VolumeQuality.PARTIAL)

    def test_cumulative_volume_reconnect(self):
        policy = CumulativeVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=1000.0)

        ev1 = make_event(total_vol=1200.0)
        delta1, q1 = policy.compute_tick_volume(ev1, state)
        self.assertEqual(delta1, 200.0)
        self.assertEqual(q1, VolumeQuality.COMPLETE)

        # Network disconnect/reconnect occurs
        policy.on_reconnect()

        # Next tick arrives after reconnect: volume is reconstructed from delta
        ev2 = make_event(total_vol=1800.0)
        delta2, q2 = policy.compute_tick_volume(ev2, state)
        self.assertEqual(delta2, 600.0)
        self.assertEqual(q2, VolumeQuality.RECONSTRUCTED)

    def test_cumulative_volume_feed_reset(self):
        policy = CumulativeVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=99000.0)

        ev1 = make_event(total_vol=100000.0)
        policy.compute_tick_volume(ev1, state)

        # Feed reset: total_volume drops from 100,000 to 50
        ev_reset = make_event(total_vol=50.0)
        delta, q = policy.compute_tick_volume(ev_reset, state)
        self.assertEqual(delta, 50.0)
        self.assertEqual(q, VolumeQuality.RECONSTRUCTED)

    def test_cumulative_volume_missing_field(self):
        policy = CumulativeVolumePolicy()
        state = self._make_state()
        policy.on_bucket_opened(state, current_total_volume=1000.0)

        # Total volume missing and no tick_volume
        ev_missing = make_event(total_vol=None, tick_vol=None)
        delta, q = policy.compute_tick_volume(ev_missing, state)
        self.assertEqual(delta, 0.0)
        self.assertEqual(q, VolumeQuality.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
