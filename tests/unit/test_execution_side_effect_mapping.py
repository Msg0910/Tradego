"""
Unit tests for Side and PositionEffect derivation mapping in Phase 7 Execution Layer.
Validates exact deterministic mapping from SignalType and position presence.
"""

import unittest

from services.execution.models import OrderPurpose, OrderSide, PositionEffect
from services.execution.planner import derive_order_side_and_position_effect
from services.signals.models import SignalType


class TestSideEffectMapping(unittest.TestCase):
    """Verifies derive_order_side_and_position_effect against the formal mapping matrix."""

    def test_entry_long_mappings(self) -> None:
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.ENTRY_LONG, direction=1, is_position_open=False
        )
        self.assertEqual(side, OrderSide.BUY)
        self.assertEqual(purpose, OrderPurpose.ENTRY)
        self.assertEqual(effect, PositionEffect.OPEN)

        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.ENTRY_LONG, direction=1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.BUY)
        self.assertEqual(purpose, OrderPurpose.ENTRY)
        self.assertEqual(effect, PositionEffect.INCREASE)

    def test_entry_short_mappings(self) -> None:
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.ENTRY_SHORT, direction=-1, is_position_open=False
        )
        self.assertEqual(side, OrderSide.SELL)
        self.assertEqual(purpose, OrderPurpose.ENTRY)
        self.assertEqual(effect, PositionEffect.OPEN)

        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.ENTRY_SHORT, direction=-1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.SELL)
        self.assertEqual(purpose, OrderPurpose.ENTRY)
        self.assertEqual(effect, PositionEffect.INCREASE)

    def test_exit_mappings_never_invert(self) -> None:
        # Exiting long must be SELL CLOSE
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.EXIT_LONG, direction=1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.SELL)
        self.assertEqual(purpose, OrderPurpose.EXIT)
        self.assertEqual(effect, PositionEffect.CLOSE)

        # Exiting short must be BUY CLOSE
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.EXIT_SHORT, direction=-1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.BUY)
        self.assertEqual(purpose, OrderPurpose.EXIT)
        self.assertEqual(effect, PositionEffect.CLOSE)

    def test_scale_in_mappings(self) -> None:
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.SCALE_IN, direction=1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.BUY)
        self.assertEqual(purpose, OrderPurpose.SCALE)
        self.assertEqual(effect, PositionEffect.INCREASE)

        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.SCALE_IN, direction=-1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.SELL)
        self.assertEqual(purpose, OrderPurpose.SCALE)
        self.assertEqual(effect, PositionEffect.INCREASE)

    def test_scale_out_mappings(self) -> None:
        # Trimming long requires SELL REDUCE
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.SCALE_OUT, direction=1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.SELL)
        self.assertEqual(purpose, OrderPurpose.SCALE)
        self.assertEqual(effect, PositionEffect.REDUCE)

        # Trimming short requires BUY REDUCE
        side, purpose, effect = derive_order_side_and_position_effect(
            signal_type=SignalType.SCALE_OUT, direction=-1, is_position_open=True
        )
        self.assertEqual(side, OrderSide.BUY)
        self.assertEqual(purpose, OrderPurpose.SCALE)
        self.assertEqual(effect, PositionEffect.REDUCE)


if __name__ == "__main__":
    unittest.main()
