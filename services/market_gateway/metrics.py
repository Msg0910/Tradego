"""
Lightweight, allocation-conscious feed health and latency metrics.

Designed to have minimal overhead on the critical tick-processing path.
Avoids heavy monitoring frameworks, locking contention, and excessive object allocations.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(slots=True)
class SymbolMetrics:
    """
    Per-symbol tick counters and latency tracking.
    """
    symbol_id: str
    ticks_received: int = 0
    ticks_normalized: int = 0
    ticks_dropped: int = 0
    errors_count: int = 0
    last_tick_perf_time: float = 0.0
    last_tick_wall_time: float = 0.0

    # Gateway processing latency (receive -> normalized, in ms)
    min_gateway_latency_ms: float = float("inf")
    max_gateway_latency_ms: float = 0.0
    total_gateway_latency_ms: float = 0.0

    def record_received(self, perf_now: float, wall_now: float) -> None:
        self.ticks_received += 1
        self.last_tick_perf_time = perf_now
        self.last_tick_wall_time = wall_now

    def record_normalized(self, process_latency_ms: float) -> None:
        self.ticks_normalized += 1
        self.total_gateway_latency_ms += process_latency_ms
        if process_latency_ms < self.min_gateway_latency_ms:
            self.min_gateway_latency_ms = process_latency_ms
        if process_latency_ms > self.max_gateway_latency_ms:
            self.max_gateway_latency_ms = process_latency_ms

    def record_dropped(self) -> None:
        self.ticks_dropped += 1

    def record_error(self) -> None:
        self.errors_count += 1

    @property
    def avg_gateway_latency_ms(self) -> float:
        if self.ticks_normalized == 0:
            return 0.0
        return self.total_gateway_latency_ms / self.ticks_normalized

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "ticks_received": self.ticks_received,
            "ticks_normalized": self.ticks_normalized,
            "ticks_dropped": self.ticks_dropped,
            "errors_count": self.errors_count,
            "last_tick_wall_time": self.last_tick_wall_time,
            "min_gateway_latency_ms": (
                round(self.min_gateway_latency_ms, 4)
                if self.min_gateway_latency_ms != float("inf")
                else 0.0
            ),
            "max_gateway_latency_ms": round(self.max_gateway_latency_ms, 4),
            "avg_gateway_latency_ms": round(self.avg_gateway_latency_ms, 4),
        }


class FeedHealthMetrics:
    """
    Lightweight gateway metrics collector across all providers and symbols.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._symbols: Dict[str, SymbolMetrics] = {}
        self.total_ticks_received = 0
        self.total_ticks_normalized = 0
        self.total_ticks_dropped = 0
        self.total_errors = 0
        self.start_time = time.time()

    def _get_or_create(self, symbol_id: str) -> SymbolMetrics:
        sm = self._symbols.get(symbol_id)
        if sm is None:
            sm = SymbolMetrics(symbol_id=symbol_id)
            self._symbols[symbol_id] = sm
        return sm

    def record_tick_received(self, symbol_id: str, perf_now: float, wall_now: float) -> None:
        with self._lock:
            self.total_ticks_received += 1
            sm = self._get_or_create(symbol_id)
            sm.record_received(perf_now, wall_now)

    def record_tick_normalized(self, symbol_id: str, process_latency_ms: float) -> None:
        with self._lock:
            self.total_ticks_normalized += 1
            sm = self._get_or_create(symbol_id)
            sm.record_normalized(process_latency_ms)

    def record_tick_dropped(self, symbol_id: str) -> None:
        with self._lock:
            self.total_ticks_dropped += 1
            sm = self._get_or_create(symbol_id)
            sm.record_dropped()

    def record_error(self, symbol_id: Optional[str] = None) -> None:
        with self._lock:
            self.total_errors += 1
            if symbol_id:
                sm = self._get_or_create(symbol_id)
                sm.record_error()

    def get_symbol_metrics(self, symbol_id: str) -> Optional[SymbolMetrics]:
        with self._lock:
            sm = self._symbols.get(symbol_id)
            if sm is None:
                return None
            # Return a snapshot copy
            return SymbolMetrics(
                symbol_id=sm.symbol_id,
                ticks_received=sm.ticks_received,
                ticks_normalized=sm.ticks_normalized,
                ticks_dropped=sm.ticks_dropped,
                errors_count=sm.errors_count,
                last_tick_perf_time=sm.last_tick_perf_time,
                last_tick_wall_time=sm.last_tick_wall_time,
                min_gateway_latency_ms=sm.min_gateway_latency_ms,
                max_gateway_latency_ms=sm.max_gateway_latency_ms,
                total_gateway_latency_ms=sm.total_gateway_latency_ms,
            )

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = time.time() - self.start_time
            rate = self.total_ticks_received / elapsed if elapsed > 0 else 0.0
            symbols_summary = {sym: sm.to_dict() for sym, sm in self._symbols.items()}
            return {
                "uptime_seconds": round(elapsed, 2),
                "total_ticks_received": self.total_ticks_received,
                "total_ticks_normalized": self.total_ticks_normalized,
                "total_ticks_dropped": self.total_ticks_dropped,
                "total_errors": self.total_errors,
                "tick_rate_per_sec": round(rate, 2),
                "active_symbols_count": len(self._symbols),
                "symbols": symbols_summary,
            }
