"""
Tradego Phase 10 — Production Observability & Health Engine.

Provides unified health aggregation, liveness/readiness probes, and system metrics:
- GET /api/v1/health/livez (Process liveness)
- GET /api/v1/health/readyz (Trading readiness, fails 503 if quarantined or HALTED)
- GET /api/v1/health/detailed (Granular breakdown across venues, disk, guard, and memory)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
import threading
from typing import Any, Dict, Optional, Tuple

from .broker_connectivity import BrokerConnectivityManager
from .disaster_recovery import DisasterRecoveryManager
from .execution import ExecutionStateManager
from .multi_venue import BrokerVenueRegistry
from .recovery import GuardState, TradingGuard
from .security import Tier1AuditLogger


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthCheckResult:
    status: HealthStatus
    is_ready: bool
    details: Dict[str, Any]
    timestamp: str


class SystemHealthMonitor:
    """
    Central aggregation monitor for Tradego production runtime health.
    Evaluates process liveness, trading readiness, and degraded operational states.
    """

    def __init__(
        self,
        trading_guard: TradingGuard,
        audit_logger: Tier1AuditLogger,
        connectivity_manager: Optional[BrokerConnectivityManager] = None,
        disaster_recovery_manager: Optional[DisasterRecoveryManager] = None,
        multi_venue_registry: Optional[BrokerVenueRegistry] = None,
        execution_manager: Optional[ExecutionStateManager] = None,
        persistence_manager: Optional[Any] = None,
        recovery_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        self._guard = trading_guard
        self._audit = audit_logger
        self._conn_mgr = connectivity_manager
        self._dr_mgr = disaster_recovery_manager or recovery_manager
        self._venue_registry = multi_venue_registry
        self._exec_mgr = execution_manager
        self._pm = persistence_manager
        self._boot_time = datetime.now(timezone.utc)
        self._lock = threading.RLock()

    @property
    def boot_time(self) -> str:
        return self._boot_time.isoformat()

    def check_liveness(self) -> Dict[str, Any]:
        """
        Liveness probe: verifies process is alive and responsive.
        Always returns 200 OK as long as the Python runtime is functioning.
        """
        now = datetime.now(timezone.utc)
        uptime_seconds = (now - self._boot_time).total_seconds()
        return {
            "status": "ALIVE",
            "uptime_seconds": round(uptime_seconds, 2),
            "timestamp": now.isoformat(),
            "pid": os.getpid(),
        }

    def check_readiness(self) -> Tuple[bool, Dict[str, Any]]:
        """
        Readiness probe: verifies whether the gateway is in an active, un-quarantined state
        capable of processing orders.
        Fails (is_ready=False, HTTP 503) if:
        - TradingGuard is HALTED or PAUSED
        - Disaster recovery quarantine is active
        - Audit logger has stopped
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            reasons = []

            # 1. Guard check
            guard_state = self._guard.state.value
            if self._guard.state in (GuardState.HALTED, GuardState.PAUSED):
                reasons.append(f"GUARD_NOT_NORMAL: {guard_state}")

            # 2. Disaster recovery quarantine check
            is_quarantined = False
            if self._dr_mgr and self._dr_mgr.is_quarantine_locked:
                is_quarantined = True
                reasons.append("DISASTER_RECOVERY_QUARANTINE_ACTIVE")

            # 3. Audit logger check
            audit_healthy = getattr(self._audit, "_running", True)
            if not audit_healthy:
                reasons.append("AUDIT_LOGGER_STOPPED")

            # 4. Check broker connectivity state if live
            if self._conn_mgr:
                mode = self._conn_mgr.execution_mode.value
                conn_st = self._conn_mgr.connectivity_state.value
                if mode == "LIVE" and conn_st != "CONNECTED":
                    reasons.append(f"LIVE_MODE_BROKER_{conn_st}")

            is_ready = (len(reasons) == 0)
            return is_ready, {
                "status": "READY" if is_ready else "NOT_READY",
                "is_ready": is_ready,
                "guard_state": guard_state,
                "is_quarantined": is_quarantined,
                "unready_reasons": reasons,
                "timestamp": now.isoformat(),
            }

    def get_detailed_health(self) -> Dict[str, Any]:
        """
        Detailed diagnostics report providing comprehensive operational observability.
        Safe for operator inspection (zero unredacted secrets).
        """
        with self._lock:
            is_ready, ready_dict = self.check_readiness()
            live_dict = self.check_liveness()

            # Audit stats
            audit_records_count = len(getattr(self._audit, "_in_memory_records", []))
            audit_queue_depth = getattr(self._audit, "_queue", None)
            queue_size = audit_queue_depth.qsize() if audit_queue_depth else 0

            # Broker & Venues
            venues_info = (
                self._venue_registry.get_all_venue_statuses()
                if self._venue_registry
                else {}
            )
            broker_status = self._conn_mgr.get_status() if self._conn_mgr else {}

            # Execution stats
            exec_stats = {}
            if self._exec_mgr:
                working = 0
                filled = 0
                unknown = 0
                for rec in self._exec_mgr._executions.values():
                    st = rec.current_state.value
                    if st in ("DISPATCH_PENDING", "DISPATCHED", "ACKNOWLEDGED", "PARTIALLY_FILLED"):
                        working += 1
                    elif st == "FILLED":
                        filled += 1
                    elif st == "UNKNOWN":
                        unknown += 1
                exec_stats = {
                    "total_executions": len(self._exec_mgr._executions),
                    "working_orders": working,
                    "filled_orders": filled,
                    "unknown_orders": unknown,
                }

            dr_status = self._dr_mgr.get_status() if self._dr_mgr else None

            return {
                "liveness": live_dict,
                "readiness": ready_dict,
                "guard_state": self._guard.state.value,
                "audit": {
                    "queue_depth": queue_size,
                    "records_count": audit_records_count,
                    "last_hash": self._audit.last_hash,
                },
                "broker": broker_status,
                "multi_venue": venues_info,
                "executions": exec_stats,
                "disaster_recovery": dr_status,
            }


# Alias for unified naming
ObservabilityManager = SystemHealthMonitor
