"""
Unit tests for Candle data models, ActiveCandleState, and VolumeQuality enum.
"""

import dataclasses
import unittest
from datetime import datetime, timezone

from services.candles.models import ActiveCandleState, Candle, VolumeQuality
from services.candles.timeframe import TF_1M
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestCandleModels(unittest.TestCase):

    def setUp(self):
        self.inst_id = InstrumentId(
            symbol="RELIANCE",
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.EQUITY,
        )
        self.t_start = datetime(2026, 9, 14, 9, 15, 0, tzinfo=timezone.utc)
        self.t_end = datetime(2026, 9, 14, 9, 16, 0, tzinfo=timezone.utc)

    def test_candle_creation_and_immutability(self):
        candle = Candle(
            instrument_id=self.inst_id,
            timeframe=TF_1M,
            start_time=self.t_start,
            end_time=self.t_end,
            open=2500.0,
            high=2510.0,
            low=2495.0,
            close=2505.0,
            volume=1000.0,
            ticks=50,
            volume_quality=VolumeQuality.COMPLETE,
            open_oi=None,
            high_oi=None,
            low_oi=None,
            close_oi=None,
            vwap=2503.5,
            is_closed=True,
            session_id="20260914_REG",
        )

        self.assertEqual(candle.open, 2500.0)
        self.assertEqual(candle.high, 2510.0)
        self.assertEqual(candle.low, 2495.0)
        self.assertEqual(candle.close, 2505.0)
        self.assertEqual(candle.volume, 1000.0)
        self.assertEqual(candle.ticks, 50)
        self.assertEqual(candle.volume_quality, VolumeQuality.COMPLETE)
        self.assertIsNone(candle.open_oi)
        self.assertTrue(candle.is_closed)

        # Immutability check: modifying any field must raise FrozenInstanceError
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candle.close = 2520.0

        with self.assertRaises(dataclasses.FrozenInstanceError):
            candle.volume = 2000.0

    def test_active_candle_state_to_immutable_candle(self):
        state = ActiveCandleState(
            instrument_id=self.inst_id,
            timeframe=TF_1M,
            start_time=self.t_start,
            end_time=self.t_end,
            open=100.0,
            high=105.0,
            low=98.0,
            close=103.0,
            volume=500.0,
            ticks=10,
            volume_quality=VolumeQuality.COMPLETE,
            open_oi=10000,
            high_oi=10500,
            low_oi=9900,
            close_oi=10200,
            sum_pv=50500.0,
            session_id="20260914_REG",
        )

        # Snapshot while active
        active_snap = state.to_immutable_candle(is_closed=False)
        self.assertFalse(active_snap.is_closed)
        self.assertEqual(active_snap.vwap, 101.0)
        self.assertEqual(active_snap.open_oi, 10000)
        self.assertEqual(active_snap.close_oi, 10200)

        # Snapshot when closed
        closed_snap = state.to_immutable_candle(is_closed=True)
        self.assertTrue(closed_snap.is_closed)
        self.assertEqual(closed_snap.close, 103.0)

    def test_vwap_zero_volume(self):
        state = ActiveCandleState(
            instrument_id=self.inst_id,
            timeframe=TF_1M,
            start_time=self.t_start,
            end_time=self.t_end,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0.0,
            ticks=1,
            volume_quality=VolumeQuality.UNAVAILABLE,
            sum_pv=0.0,
        )
        snap = state.to_immutable_candle(is_closed=True)
        # When volume is 0, vwap defaults to close price
        self.assertEqual(snap.vwap, 100.0)

    def test_volume_quality_values(self):
        self.assertEqual(VolumeQuality.COMPLETE.value, "COMPLETE")
        self.assertEqual(VolumeQuality.RECONSTRUCTED.value, "RECONSTRUCTED")
        self.assertEqual(VolumeQuality.PARTIAL.value, "PARTIAL")
        self.assertEqual(VolumeQuality.ESTIMATED.value, "ESTIMATED")
        self.assertEqual(VolumeQuality.UNAVAILABLE.value, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
