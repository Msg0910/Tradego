"""
Unit tests for Feature & Analytics Data Models (Phase 4).

Tests:
- FeatureQuality enum definitions
- FeatureValue creation, immutability, and properties
- FeatureSnapshot safe reference sharing and query API
"""

import dataclasses
import unittest
from datetime import datetime, timezone

from services.analytics.models import FeatureQuality, FeatureSnapshot, FeatureValue
from services.market_state.instrument import Exchange, InstrumentId, InstrumentType


class TestFeatureModels(unittest.TestCase):

    def setUp(self):
        self.inst_id = InstrumentId("RELIANCE", Exchange.NSE, InstrumentType.EQUITY)
        self.ts = datetime(2026, 9, 14, 9, 20, 0, tzinfo=timezone.utc)

    def test_feature_quality_enum_values(self):
        self.assertEqual(FeatureQuality.VALID.value, "VALID")
        self.assertEqual(FeatureQuality.DEGRADED.value, "DEGRADED")
        self.assertEqual(FeatureQuality.WARMING_UP.value, "WARMING_UP")
        self.assertEqual(FeatureQuality.STALE.value, "STALE")
        self.assertEqual(FeatureQuality.INVALID.value, "INVALID")

    def test_feature_value_creation_and_immutability(self):
        fv = FeatureValue(
            feature_id="EMA_20_5M",
            value=2500.50,
            quality=FeatureQuality.VALID,
            observation_timestamp=self.ts,
            availability_timestamp=self.ts,
            is_confirmed=True,
            metadata={"period": 20},
        )

        self.assertEqual(fv.feature_id, "EMA_20_5M")
        self.assertEqual(fv.value, 2500.50)
        self.assertEqual(fv.quality, FeatureQuality.VALID)
        self.assertTrue(fv.is_valid)
        self.assertTrue(fv.is_usable)
        self.assertTrue(fv.is_confirmed)

        # Immutability check
        with self.assertRaises(dataclasses.FrozenInstanceError):
            fv.value = 2600.0

        with self.assertRaises(dataclasses.FrozenInstanceError):
            fv.quality = FeatureQuality.INVALID

    def test_feature_value_usability(self):
        fv_valid = FeatureValue("F1", 10.0, FeatureQuality.VALID, self.ts, self.ts, True)
        fv_degraded = FeatureValue("F2", 10.0, FeatureQuality.DEGRADED, self.ts, self.ts, True)
        fv_warming = FeatureValue("F3", 10.0, FeatureQuality.WARMING_UP, self.ts, self.ts, True)
        fv_invalid = FeatureValue("F4", None, FeatureQuality.INVALID, self.ts, self.ts, True)

        self.assertTrue(fv_valid.is_valid)
        self.assertTrue(fv_valid.is_usable)

        self.assertFalse(fv_degraded.is_valid)
        self.assertTrue(fv_degraded.is_usable)

        self.assertFalse(fv_warming.is_valid)
        self.assertFalse(fv_warming.is_usable)

        self.assertFalse(fv_invalid.is_valid)
        self.assertFalse(fv_invalid.is_usable)

    def test_feature_snapshot_query_and_immutability(self):
        fv1 = FeatureValue("EMA_20", 2500.0, FeatureQuality.VALID, self.ts, self.ts, True)
        fv2 = FeatureValue("RSI_14", 65.5, FeatureQuality.VALID, self.ts, self.ts, True)

        features_dict = {"EMA_20": fv1, "RSI_14": fv2}
        snapshot = FeatureSnapshot(
            instrument_id=self.inst_id,
            snapshot_timestamp=self.ts,
            _features=features_dict,
        )

        self.assertEqual(len(snapshot), 2)
        self.assertIn("EMA_20", snapshot)
        self.assertIn("RSI_14", snapshot)
        self.assertNotIn("SMA_50", snapshot)

        self.assertEqual(snapshot.get_value("EMA_20"), 2500.0)
        self.assertEqual(snapshot.get_value("RSI_14"), 65.5)
        self.assertIsNone(snapshot.get_value("SMA_50"))
        self.assertEqual(snapshot.get_value("SMA_50", default=0.0), 0.0)

        self.assertTrue(snapshot.is_valid("EMA_20"))
        self.assertFalse(snapshot.is_valid("SMA_50"))

        # Snapshot is frozen
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.snapshot_timestamp = datetime.now()


if __name__ == "__main__":
    unittest.main()
