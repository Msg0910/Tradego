"""
Tradego Trading Guard & Kill Switch (Phase 8).

Provides centralized risk gating, administrative pause/resume,
fail-closed kill-switch halting, and emergency de-risking controls.
"""

import threading
from typing import Optional

from .models import GuardState, GuardTripReason


class TradingGuard:
    """
    Centralized runtime trading guard.
    Controls order submission permissions across normal, paused, and halted states.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: GuardState = GuardState.NORMAL
        self._trip_reason: Optional[GuardTripReason] = None
        self._trip_details: str = ""

    @property
    def state(self) -> GuardState:
        with self._lock:
            return self._state

    @property
    def trip_reason(self) -> Optional[GuardTripReason]:
        with self._lock:
            return self._trip_reason

    @property
    def trip_details(self) -> str:
        with self._lock:
            return self._trip_details

    def trip(self, reason: GuardTripReason, details: str = "") -> None:
        """
        Immediately trips the kill switch, entering HALTED state.
        Fails closed: blocks speculative entries while retaining emergency exit access.
        """
        with self._lock:
            self._state = GuardState.HALTED
            self._trip_reason = reason
            self._trip_details = details

    def pause(self) -> None:
        """Administratively pauses trading. Only valid from NORMAL state."""
        with self._lock:
            if self._state == GuardState.NORMAL:
                self._state = GuardState.PAUSED

    def resume(self) -> None:
        """Resumes trading from PAUSED state. Cannot resume from HALTED."""
        with self._lock:
            if self._state == GuardState.PAUSED:
                self._state = GuardState.NORMAL

    def recover(self, confirmation_token: str, expected_token: str) -> bool:
        """
        Explicit operator-mediated recovery from HALTED state.
        Requires matching confirmation token. Does NOT allow silent recovery.
        """
        with self._lock:
            if self._state != GuardState.HALTED:
                return False
            if confirmation_token != expected_token:
                return False
            self._state = GuardState.NORMAL
            self._trip_reason = None
            self._trip_details = ""
            return True

    def emergency_flatten(self) -> None:
        """Enters EMERGENCY_FLATTEN mode for orderly portfolio unwinding."""
        with self._lock:
            self._state = GuardState.EMERGENCY_FLATTEN

    def can_submit_speculative_entry(self) -> bool:
        """Speculative entries permitted strictly when guard is NORMAL."""
        with self._lock:
            return self._state == GuardState.NORMAL

    def can_submit_exit(self) -> bool:
        """Exits remain permitted in all states to allow capital de-risking."""
        with self._lock:
            return True

    def can_evaluate_strategies(self) -> bool:
        """Strategy evaluations permitted strictly when guard is NORMAL."""
        with self._lock:
            return self._state == GuardState.NORMAL
