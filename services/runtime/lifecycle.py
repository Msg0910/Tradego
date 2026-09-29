"""
Tradego Runtime Lifecycle Manager (Phase 8).

Enforces the deterministic, formal system state machine transitions.
Protects against illegal transitions and enforces manual recovery rules.
"""

import threading
from typing import Set

from .models import RuntimeState


LEGAL_TRANSITIONS = {
    RuntimeState.STARTING: {RuntimeState.READY, RuntimeState.FAILED},
    RuntimeState.READY: {RuntimeState.RUNNING, RuntimeState.SHUTTING_DOWN, RuntimeState.FAILED},
    RuntimeState.RUNNING: {
        RuntimeState.PAUSED,
        RuntimeState.HALTED,
        RuntimeState.DEGRADED,
        RuntimeState.SHUTTING_DOWN,
        RuntimeState.FAILED,
    },
    RuntimeState.PAUSED: {
        RuntimeState.RUNNING,
        RuntimeState.HALTED,
        RuntimeState.SHUTTING_DOWN,
        RuntimeState.FAILED,
    },
    RuntimeState.HALTED: {
        RuntimeState.READY,  # Manual recovery must transition through READY, never directly to RUNNING
        RuntimeState.SHUTTING_DOWN,
        RuntimeState.FAILED,
    },
    RuntimeState.DEGRADED: {
        RuntimeState.RUNNING,
        RuntimeState.HALTED,
        RuntimeState.SHUTTING_DOWN,
        RuntimeState.FAILED,
    },
    RuntimeState.SHUTTING_DOWN: {RuntimeState.STOPPED, RuntimeState.FAILED},
    RuntimeState.STOPPED: set(),
    RuntimeState.FAILED: set(),
}


class RuntimeLifecycleManager:
    """
    Authoritative state machine coordinator for the Tradego runtime.
    """

    def __init__(self, initial_state: RuntimeState = RuntimeState.STARTING) -> None:
        self._lock = threading.RLock()
        self._state: RuntimeState = initial_state

    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    def is_running(self) -> bool:
        with self._lock:
            return self._state == RuntimeState.RUNNING

    def is_active(self) -> bool:
        """True if the engine is in a state capable of processing events (RUNNING or PAUSED)."""
        with self._lock:
            return self._state in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.DEGRADED)

    def is_terminal(self) -> bool:
        with self._lock:
            return self._state in (RuntimeState.STOPPED, RuntimeState.FAILED)

    def transition_to(self, target_state: RuntimeState) -> None:
        """
        Executes a deterministic state transition, strictly enforcing legal state machine rules.
        Fails closed on any illegal transition attempt.
        """
        with self._lock:
            if target_state == self._state:
                return

            allowed_targets = LEGAL_TRANSITIONS.get(self._state, set())
            if target_state not in allowed_targets:
                raise ValueError(
                    f"Illegal lifecycle transition: cannot transition from {self._state} to {target_state}."
                )

            self._state = target_state
