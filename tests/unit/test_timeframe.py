"""
Unit tests for TimeFrame abstraction and string parser.
"""

import unittest

from services.candles.timeframe import (
    TF_1D,
    TF_1H,
    TF_1M,
    TF_1S,
    TF_3M,
    TF_5M,
    TF_15M,
    TF_30M,
    TF_TICK,
    TimeFrame,
    TimeFrameType,
)


class TestTimeFrame(unittest.TestCase):

    def test_standard_constants(self):
        self.assertEqual(TF_TICK.tf_type, TimeFrameType.TICK)
        self.assertEqual(TF_TICK.seconds, 0)
        self.assertEqual(TF_TICK.label, "TICK")

        self.assertEqual(TF_1S.seconds, 1)
        self.assertEqual(TF_1S.label, "1S")

        self.assertEqual(TF_1M.seconds, 60)
        self.assertEqual(TF_1M.label, "1M")

        self.assertEqual(TF_3M.seconds, 180)
        self.assertEqual(TF_3M.label, "3M")

        self.assertEqual(TF_5M.seconds, 300)
        self.assertEqual(TF_5M.label, "5M")

        self.assertEqual(TF_15M.seconds, 900)
        self.assertEqual(TF_15M.label, "15M")

        self.assertEqual(TF_30M.seconds, 1800)
        self.assertEqual(TF_30M.label, "30M")

        self.assertEqual(TF_1H.seconds, 3600)
        self.assertEqual(TF_1H.label, "1H")

        self.assertEqual(TF_1D.seconds, 86400)
        self.assertEqual(TF_1D.label, "1D")

    def test_string_parsing(self):
        self.assertEqual(TimeFrame.from_string("1s"), TF_1S)
        self.assertEqual(TimeFrame.from_string("1S"), TF_1S)
        self.assertEqual(TimeFrame.from_string("1m"), TF_1M)
        self.assertEqual(TimeFrame.from_string("5M"), TF_5M)
        self.assertEqual(TimeFrame.from_string("15m"), TF_15M)
        self.assertEqual(TimeFrame.from_string("1H"), TF_1H)
        self.assertEqual(TimeFrame.from_string("1d"), TF_1D)
        self.assertEqual(TimeFrame.from_string("tick"), TF_TICK)
        self.assertEqual(TimeFrame.from_string("TICK"), TF_TICK)

    def test_invalid_parsing_and_multipliers(self):
        with self.assertRaises(ValueError):
            TimeFrame.from_string("invalid")

        with self.assertRaises(ValueError):
            TimeFrame.from_string("5X")

        with self.assertRaises(ValueError):
            TimeFrame(TimeFrameType.MINUTE, 0)

        with self.assertRaises(ValueError):
            TimeFrame(TimeFrameType.SECOND, -5)

    def test_immutability_and_hashable(self):
        tf_dict = {TF_1M: "one_minute", TF_5M: "five_minute"}
        self.assertEqual(tf_dict[TF_1M], "one_minute")
        self.assertEqual(tf_dict[TimeFrame(TimeFrameType.MINUTE, 1)], "one_minute")


if __name__ == "__main__":
    unittest.main()
