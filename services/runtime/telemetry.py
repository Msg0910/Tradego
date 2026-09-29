"""
Tradego Telemetry Collector (Phase 8).

Maintains a strictly bounded in-memory ring buffer for end-to-end latency records
and lineage tracking. Operates with zero disk/database I/O on the hot path.
"""

from collections import deque
import threading
from typing import Any, Dict, List, Optional

from .models import RuntimeCorrelationRecord


class TelemetryCollector:
    """
    Non-blocking, bounded telemetry ring buffer for Phase 8 runtime profiling.
    """

    def __init__(self, capacity: int = 65536) -> None:
        self._capacity = max(1, capacity)
        self._lock = threading.Lock()
        self._records: deque[RuntimeCorrelationRecord] = deque(maxlen=self._capacity)
        self._dropped_count: int = 0
        self._total_recorded: int = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped_telemetry_count(self) -> int:
        with self._lock:
            return self._dropped_count

    @property
    def total_recorded_count(self) -> int:
        with self._lock:
            return self._total_recorded

    def record_lineage(self, record: RuntimeCorrelationRecord) -> None:
        """
        Appends a complete T1->T10 correlation record to the bounded ring buffer.
        If capacity is reached, increments dropped counter and evicts oldest record without blocking.
        """
        with self._lock:
            if len(self._records) == self._capacity:
                self._dropped_count += 1
            self._records.append(record)
            self._total_recorded += 1

    def get_records(self) -> List[RuntimeCorrelationRecord]:
        """Returns a snapshot copy of all active records in the ring buffer."""
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        """Clears all records in the buffer."""
        with self._lock:
            self._records.clear()
            self._dropped_count = 0
            self._total_recorded = 0

    def get_latency_stats(self) -> Dict[str, float]:
        """
        Computes percentile end-to-end latencies (T1 -> T10) in microseconds.
        """
        with self._lock:
            if not self._records:
                return {
                    "count": 0.0,
                    "p50_us": 0.0,
                    "p95_us": 0.0,
                    "p99_us": 0.0,
                    "max_us": 0.0,
                }

            latencies = [
                (r.t10_position_updated_ns - r.t1_market_receive_ns) / 1000.0
                for r in self._records
                if r.t10_position_updated_ns > r.t1_market_receive_ns
            ]

        if not latencies:
            return {
                "count": 0.0,
                "p50_us": 0.0,
                "p95_us": 0.0,
                "p99_us": 0.0,
                "max_us": 0.0,
            }

        latencies.sort()
        n = len(latencies)
        p50 = latencies[int(n * 0.50)]
        p95 = latencies[int(n * 0.95)] if n > 1 else latencies[0]
        p99 = latencies[int(n * 0.99)] if n > 1 else latencies[0]
        max_lat = latencies[-1]

        return {
            "count": float(n),
            "p50_us": p50,
            "p95_us": p95,
            "p99_us": p99,
            "max_us": max_lat,
        }
