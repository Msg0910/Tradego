"""
Deterministic Simulation Clock for Tradego Backtesting & Replay.

Provides a virtual simulation clock that advances exclusively upon receipt
of historical event exchange timestamps. Decouples simulation time entirely
from system wall-clock time (time.time(), datetime.now()).
"""

from datetime import datetime
from typing import Optional


class SimulationClock:
    """
    Monotonic deterministic simulation clock.
    Advances strictly when driven by historical market events.
    """

    def __init__(self) -> None:
        self._current_time: Optional[datetime] = None

    @property
    def current_time(self) -> Optional[datetime]:
        """Returns the current simulation timestamp, or None if uninitialized."""
        return self._current_time

    def advance_to(self, timestamp: datetime) -> None:
        """
        Advances the simulation clock to the target timestamp.
        Enforces monotonic non-decreasing timestamp progression.
        """
        if timestamp is None:
            raise ValueError("SimulationClock cannot advance to a null timestamp.")

        if self._current_time is not None and timestamp < self._current_time:
            raise ValueError(
                f"Clock monotonicity violation: Cannot advance backwards from "
                f"{self._current_time.isoformat()} to {timestamp.isoformat()}."
            )

        self._current_time = timestamp

    def now(self) -> datetime:
        """
        Returns the current simulation timestamp.
        Raises RuntimeError if the clock has not yet observed an event.
        """
        if self._current_time is None:
            raise RuntimeError("Simulation clock has not been initialized with any market event.")
        return self._current_time

    def reset(self) -> None:
        """Resets the simulation clock to uninitialized state."""
        self._current_time = None
