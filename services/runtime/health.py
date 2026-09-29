"""
Tradego Health Monitor (Phase 8).

Collects runtime component health telemetry, detects error bursts,
monitors feed stagnation, and automatically trips the TradingGuard.
"""

from collections import deque
import logging
import threading
import time
from typing import Dict, List, Optional

from .guards import TradingGuard
from .models import GuardTripReason

logger = logging.getLogger("runtime.health")


class HealthMonitor:
    """
    Subsystem health monitor and safety watchdog.
    """

    def __init__(
        self,
        guard: TradingGuard,
        max_exception_burst_count: int = 5,
        feed_stagnation_timeout_ms: float = 5000.0,
    ) -> None:
        self._guard = guard
        self._max_burst = max_exception_burst_count
        self._feed_timeout_ms = feed_stagnation_timeout_ms
        self._lock = threading.Lock()
        self._error_counts: Dict[str, int] = {}
        self._error_timestamps: deque[float] = deque(maxlen=100)
        self._last_tick_time_ms: float = time.time() * 1000.0

    def record_error(self, component: str, error: Exception) -> None:
        """
        Records an error in a component. Trips the kill switch if an error burst is detected.
        """
        now = time.time()
        with self._lock:
            self._error_counts[component] = self._error_counts.get(component, 0) + 1
            self._error_timestamps.append(now)

            # Check burst in the last 1.0 second
            one_sec_ago = now - 1.0
            recent_errors = sum(1 for ts in self._error_timestamps if ts >= one_sec_ago)

            if recent_errors >= self._max_burst:
                logger.critical(
                    f"[HealthMonitor] EXCEPTION BURST DETECTED: {recent_errors} errors in past 1.0s. "
                    f"Tripping kill switch to HALTED."
                )
                self._guard.trip(
                    GuardTripReason.EXCEPTION_BURST,
                    f"Error burst in {component}: {recent_errors} errors/sec.",
                )

    def record_tick(self) -> None:
        """Updates the timestamp of the latest observed market tick."""
        with self._lock:
            self._last_tick_time_ms = time.time() * 1000.0

    def check_feed_stagnation(self, current_time_ms: Optional[float] = None) -> bool:
        """
        Checks if the market feed has stalled during active market hours.
        Trips the kill switch if ticks have ceased for longer than timeout.
        """
        now_ms = current_time_ms or (time.time() * 1000.0)
        with self._lock:
            delta_ms = now_ms - self._last_tick_time_ms
            if delta_ms > self._feed_timeout_ms:
                logger.warning(
                    f"[HealthMonitor] FEED STAGNATION: No ticks received for {delta_ms:.1f} ms "
                    f"(timeout {self._feed_timeout_ms} ms). Tripping kill switch to HALTED."
                )
                self._guard.trip(
                    GuardTripReason.FEED_STAGNATION,
                    f"Market data feed stalled for {delta_ms:.1f} ms.",
                )
                return True
            return False

    def check_daily_loss(self, current_daily_loss: float, max_daily_loss: float) -> bool:
        """
        Checks if current daily loss breaches configured safety threshold.
        """
        if current_daily_loss >= max_daily_loss:
            logger.critical(
                f"[HealthMonitor] DAILY LOSS BREACH: {current_daily_loss:.2f} >= {max_daily_loss:.2f}. "
                f"Tripping kill switch to HALTED."
            )
            self._guard.trip(
                GuardTripReason.DAILY_LOSS_EXCEEDED,
                f"Daily loss breach: {current_daily_loss:.2f} >= {max_daily_loss:.2f}.",
            )
            return True
        return False

    def get_error_counts(self) -> Dict[str, int]:
        """Returns snapshot of total errors by component."""
        with self._lock:
            return dict(self._error_counts)
