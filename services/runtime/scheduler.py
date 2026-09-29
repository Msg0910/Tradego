"""
Tradego Strategy Scheduler (Phase 8).

Implements the authoritative monotonic evaluation watermark for BAR_CLOSE
and the frequency-limiting cadence governor for INTRABAR_PREVIEW.
"""

from datetime import datetime
import threading
from typing import Dict, Optional, Tuple

from services.candles.timeframe import TimeFrame
from services.market_state.instrument import InstrumentId


class StrategyScheduler:
    """
    Authoritative evaluation timing and cadence coordinator for Phase 5 strategies.
    Enforces monotonic bar watermarks and intrabar evaluation rate limits.
    """

    def __init__(self, min_intrabar_interval_ms: float = 100.0) -> None:
        self._lock = threading.RLock()
        self._min_intrabar_interval_ns = int(min_intrabar_interval_ms * 1_000_000)
        # Authoritative monotonic watermark: (strategy_id, instrument_id, timeframe) -> last_evaluated_candle_end_time
        self._watermarks: Dict[Tuple[str, InstrumentId, TimeFrame], datetime] = {}
        # Intrabar frequency throttle: (strategy_id, instrument_id) -> last_eval_time_ns
        self._last_intrabar_eval_ns: Dict[Tuple[str, InstrumentId], int] = {}

    def should_evaluate_bar_close(
        self,
        strategy_id: str,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
        candle_end_time: datetime,
    ) -> bool:
        """
        Evaluates whether a closed candle boundary is eligible for strategy evaluation.
        Enforces: evaluation_boundary > last_evaluated_boundary.
        Suppresses duplicate or out-of-order historical candles.
        """
        key = (strategy_id, instrument_id, timeframe)
        with self._lock:
            last_boundary = self._watermarks.get(key)
            if last_boundary is not None and candle_end_time <= last_boundary:
                return False
            return True

    def advance_watermark(
        self,
        strategy_id: str,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
        candle_end_time: datetime,
    ) -> None:
        """
        Advances the monotonic evaluation watermark strictly after evaluation execution.
        Guarantees that the watermark never regresses.
        """
        key = (strategy_id, instrument_id, timeframe)
        with self._lock:
            current = self._watermarks.get(key)
            if current is None or candle_end_time > current:
                self._watermarks[key] = candle_end_time

    def get_watermark(
        self,
        strategy_id: str,
        instrument_id: InstrumentId,
        timeframe: TimeFrame,
    ) -> Optional[datetime]:
        """Returns current monotonic watermark for diagnostic inspection."""
        with self._lock:
            return self._watermarks.get((strategy_id, instrument_id, timeframe))

    def should_evaluate_intrabar(
        self,
        strategy_id: str,
        instrument_id: InstrumentId,
        current_time_ns: int,
    ) -> bool:
        """
        Evaluates whether an intrabar tick meets the evaluation-frequency rate limit.
        Note: Controls frequency only; does NOT perform signal/order deduplication.
        """
        key = (strategy_id, instrument_id)
        with self._lock:
            last_eval = self._last_intrabar_eval_ns.get(key)
            if last_eval is not None and (current_time_ns - last_eval) < self._min_intrabar_interval_ns:
                return False
            return True

    def record_intrabar_eval(
        self,
        strategy_id: str,
        instrument_id: InstrumentId,
        current_time_ns: int,
    ) -> None:
        """Records an intrabar evaluation timestamp for frequency rate limiting."""
        key = (strategy_id, instrument_id)
        with self._lock:
            self._last_intrabar_eval_ns[key] = current_time_ns
