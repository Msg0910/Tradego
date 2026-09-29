"""
Unit tests for Tradego Runtime Lifecycle, Modes, and TradingGuard (Phase 8).
Verifies state machine transitions, LIVE lockout, kill switch, manual recovery,
and emergency exit permissions.
"""

from datetime import datetime, timezone
import unittest

from services.runtime.config import RuntimeConfig
from services.runtime.guards import TradingGuard
from services.runtime.lifecycle import RuntimeLifecycleManager
from services.runtime.models import GuardState, GuardTripReason, RuntimeMode, RuntimeState


class TestRuntimeLifecycleAndGuards(unittest.TestCase):
    """Verifies deterministic lifecycle transitions and trading guard policies."""

    def test_live_mode_fatal_lockout(self) -> None:
        """Confirms configuring LIVE mode raises fatal RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            RuntimeConfig(runtime_mode=RuntimeMode.LIVE)
        self.assertIn("LIVE trading mode is structurally disabled", str(ctx.exception))

    def test_valid_runtime_modes(self) -> None:
        """Confirms DEVELOPMENT, PAPER, and SHADOW modes instantiate successfully."""
        cfg_dev = RuntimeConfig(runtime_mode=RuntimeMode.DEVELOPMENT)
        self.assertEqual(cfg_dev.runtime_mode, RuntimeMode.DEVELOPMENT)

        cfg_paper = RuntimeConfig(runtime_mode=RuntimeMode.PAPER)
        self.assertEqual(cfg_paper.runtime_mode, RuntimeMode.PAPER)

        cfg_shadow = RuntimeConfig(runtime_mode=RuntimeMode.SHADOW)
        self.assertEqual(cfg_shadow.runtime_mode, RuntimeMode.SHADOW)

    def test_lifecycle_legal_transitions(self) -> None:
        """Verifies canonical forward progression: STARTING -> READY -> RUNNING -> PAUSED -> RUNNING."""
        lm = RuntimeLifecycleManager(initial_state=RuntimeState.STARTING)
        self.assertEqual(lm.state, RuntimeState.STARTING)

        lm.transition_to(RuntimeState.READY)
        self.assertEqual(lm.state, RuntimeState.READY)

        lm.transition_to(RuntimeState.RUNNING)
        self.assertTrue(lm.is_running())
        self.assertTrue(lm.is_active())

        lm.transition_to(RuntimeState.PAUSED)
        self.assertEqual(lm.state, RuntimeState.PAUSED)
        self.assertTrue(lm.is_active())

        lm.transition_to(RuntimeState.RUNNING)
        self.assertEqual(lm.state, RuntimeState.RUNNING)

        lm.transition_to(RuntimeState.SHUTTING_DOWN)
        self.assertEqual(lm.state, RuntimeState.SHUTTING_DOWN)

        lm.transition_to(RuntimeState.STOPPED)
        self.assertEqual(lm.state, RuntimeState.STOPPED)
        self.assertTrue(lm.is_terminal())

    def test_lifecycle_illegal_transition_fails_closed(self) -> None:
        """Confirms invalid jumps (e.g. STARTING -> RUNNING) raise ValueError."""
        lm = RuntimeLifecycleManager(initial_state=RuntimeState.STARTING)
        with self.assertRaises(ValueError):
            lm.transition_to(RuntimeState.RUNNING)

        with self.assertRaises(ValueError):
            lm.transition_to(RuntimeState.PAUSED)

    def test_halted_cannot_directly_transition_to_running(self) -> None:
        """Confirms HALTED cannot directly jump to RUNNING without manual recovery to READY."""
        lm = RuntimeLifecycleManager(initial_state=RuntimeState.RUNNING)
        lm.transition_to(RuntimeState.HALTED)
        self.assertEqual(lm.state, RuntimeState.HALTED)

        # Illegal direct recovery
        with self.assertRaises(ValueError):
            lm.transition_to(RuntimeState.RUNNING)

        # Legal manual recovery path
        lm.transition_to(RuntimeState.READY)
        lm.transition_to(RuntimeState.RUNNING)
        self.assertEqual(lm.state, RuntimeState.RUNNING)

    def test_trading_guard_kill_switch_trip(self) -> None:
        """Verifies kill switch trips into HALTED state."""
        guard = TradingGuard()
        self.assertEqual(guard.state, GuardState.NORMAL)
        self.assertTrue(guard.can_submit_speculative_entry())
        self.assertTrue(guard.can_submit_exit())

        guard.trip(GuardTripReason.MANUAL, "Operator emergency halt")
        self.assertEqual(guard.state, GuardState.HALTED)
        self.assertEqual(guard.trip_reason, GuardTripReason.MANUAL)
        self.assertFalse(guard.can_submit_speculative_entry())
        # Emergency exits remain allowed during HALTED state
        self.assertTrue(guard.can_submit_exit())
        self.assertFalse(guard.can_evaluate_strategies())

    def test_trading_guard_manual_recovery(self) -> None:
        """Verifies recovery requires matching confirmation token."""
        guard = TradingGuard()
        guard.trip(GuardTripReason.DAILY_LOSS_EXCEEDED, "Daily loss hit")

        # Wrong token rejected
        res = guard.recover(confirmation_token="WRONG_TOKEN", expected_token="SECRET_KEY")
        self.assertFalse(res)
        self.assertEqual(guard.state, GuardState.HALTED)

        # Correct token recovers
        res = guard.recover(confirmation_token="SECRET_KEY", expected_token="SECRET_KEY")
        self.assertTrue(res)
        self.assertEqual(guard.state, GuardState.NORMAL)
        self.assertIsNone(guard.trip_reason)
        self.assertTrue(guard.can_submit_speculative_entry())

    def test_trading_guard_pause_and_resume(self) -> None:
        """Verifies administrative pause blocks entries but allows exits."""
        guard = TradingGuard()
        guard.pause()
        self.assertEqual(guard.state, GuardState.PAUSED)
        self.assertFalse(guard.can_submit_speculative_entry())
        self.assertTrue(guard.can_submit_exit())
        self.assertFalse(guard.can_evaluate_strategies())

        guard.resume()
        self.assertEqual(guard.state, GuardState.NORMAL)
        self.assertTrue(guard.can_submit_speculative_entry())


if __name__ == "__main__":
    unittest.main()
