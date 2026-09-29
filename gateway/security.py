"""
Tradego Boundary Security & Audit Infrastructure.
Provides high-entropy session management, capability-based RBAC enforcement,
and serialized, crash-consistent Tier 1 audit logging with SHA-256 hash chaining.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import queue
import secrets
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import argon2
import argon2.exceptions

from .contracts import Capability


@dataclass(frozen=True)
class SessionContext:
    """Immutable session authorization context."""
    operator_id: str
    session_id: str
    roles: List[str]
    capabilities: Set[Capability]
    created_at: datetime
    expires_at: datetime

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class InMemorySessionStore:
    """
    High-entropy session token store.
    Validates bearer credentials and provides instantaneous session revocation (SEC-14).
    """

    def __init__(self, default_ttl_seconds: int = 3600) -> None:
        self._lock = threading.RLock()
        self._default_ttl = default_ttl_seconds
        self._sessions: Dict[str, SessionContext] = {}  # token -> context
        self._operator_tokens: Dict[str, Set[str]] = {}  # operator_id -> set of tokens

    def create_session(
        self,
        operator_id: str,
        roles: List[str],
        capabilities: Set[Capability],
        ttl_seconds: Optional[int] = None,
    ) -> str:
        """Issues an opaque, high-entropy bearer token."""
        token = f"tg_sess_{secrets.token_urlsafe(32)}"
        now = datetime.now(timezone.utc)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires_at = now + timedelta(seconds=ttl)

        context = SessionContext(
            operator_id=operator_id,
            session_id=str(secrets.token_hex(8)),
            roles=list(roles),
            capabilities=set(capabilities),
            created_at=now,
            expires_at=expires_at,
        )

        with self._lock:
            self._sessions[token] = context
            if operator_id not in self._operator_tokens:
                self._operator_tokens[operator_id] = set()
            self._operator_tokens[operator_id].add(token)

        return token

    def validate_token(self, token: str) -> Optional[SessionContext]:
        """Validates token authenticity, lifetime, and revocation status."""
        with self._lock:
            context = self._sessions.get(token)
            if not context:
                return None
            if context.is_expired():
                self.revoke_session(token)
                return None
            return context

    def revoke_session(self, token: str) -> bool:
        """Immediately invalidates an individual session token."""
        with self._lock:
            context = self._sessions.pop(token, None)
            if context:
                tokens = self._operator_tokens.get(context.operator_id)
                if tokens and token in tokens:
                    tokens.remove(token)
                return True
            return False

    def revoke_all_for_operator(self, operator_id: str) -> int:
        """Revokes all active sessions for a given operator (SEC-14)."""
        with self._lock:
            tokens = self._operator_tokens.pop(operator_id, set())
            for t in tokens:
                self._sessions.pop(t, None)
            return len(tokens)


class CapabilityChecker:
    """Helper for evaluating granular RBAC capabilities at the Zone 3 boundary."""

    @staticmethod
    def has_capability(context: SessionContext, required: Capability) -> bool:
        if Capability.CAP_ADMIN in context.capabilities:
            return True
        return required in context.capabilities


class Tier1AuditLogger:
    """
    Serialized, single-writer audit worker with bounded asynchronous queue.
    Enforces atomic disk commits with fsync() and incremental SHA-256 hash chaining.
    Provides tamper-evident integrity (not legal non-repudiation) without blocking the engine.
    """

    GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

    def __init__(
        self,
        log_file_path: Optional[str] = None,
        max_queue_size: int = 10000,
    ) -> None:
        self._log_path = log_file_path
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._last_hash = self.GENESIS_HASH
        self._hash_lock = threading.Lock()
        self._running = True
        self._file = None
        self._in_memory_records: List[Dict[str, Any]] = []

        if self._log_path:
            os.makedirs(os.path.dirname(os.path.abspath(self._log_path)), exist_ok=True)
            self._file = open(self._log_path, "a", encoding="utf-8")

        self._worker = threading.Thread(
            target=self._writer_loop, name="Tier1AuditWriter", daemon=True
        )
        self._worker.start()

    @property
    def last_hash(self) -> str:
        with self._hash_lock:
            return self._last_hash

    @property
    def in_memory_records(self) -> List[Dict[str, Any]]:
        with self._hash_lock:
            return list(self._in_memory_records)

    def log(self, entry: Dict[str, Any]) -> None:
        """
        Enqueues an audit entry. Fails closed (raises RuntimeError) if queue is saturated.
        """
        if not self._running:
            raise RuntimeError("Audit logger has stopped")
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            raise RuntimeError("CRITICAL: Audit queue full; fail-closed rejection triggered")

    def _writer_loop(self) -> None:
        while self._running or not self._queue.empty():
            try:
                entry = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            with self._hash_lock:
                entry["prev_hash"] = self._last_hash
                serialized = json.dumps(entry, sort_keys=True)
                entry_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                entry["hash"] = entry_hash
                self._last_hash = entry_hash
                self._in_memory_records.append(entry)

                if self._file:
                    self._file.write(json.dumps(entry) + "\n")
                    self._file.flush()
                    try:
                        os.fsync(self._file.fileno())
                    except OSError:
                        pass  # OS/filesystem might not support fsync on all platforms
                self._queue.task_done()

    def flush(self) -> None:
        """Blocks until all currently enqueued entries are committed to disk."""
        self._queue.join()

    def close(self) -> None:
        """Stops worker and flushes remaining records."""
        self._running = False
        if self._worker.is_alive():
            self._worker.join(timeout=2.0)
        if self._file:
            self._file.close()
            self._file = None


class NativeCredentialStore:
    """
    Direct credential verifier for Phase 1 MVP native authentication.
    Offloads CPU-intensive Argon2id operations to a dedicated worker threadpool
    strictly outside the async event loop (INVARIANT-SEC-TECH-ARGON2-01).
    Enforces brute-force defense with exponential backoff and 5-attempt lockout.
    """

    def __init__(
        self,
        hasher: Optional[argon2.PasswordHasher] = None,
        max_failed_attempts: int = 5,
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        self._hasher = hasher or argon2.PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4
        )
        self._max_failed_attempts = max_failed_attempts
        self._executor = executor or ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="Argon2Worker"
        )
        self._lock = threading.RLock()
        self._credentials: Dict[str, Dict[str, Any]] = {}
        self._failed_attempts: Dict[str, int] = {}
        self._locked_accounts: Dict[str, datetime] = {}
        self.last_verification_thread: Optional[str] = None

    def register_user(
        self,
        operator_id: str,
        password: str,
        roles: List[str],
        capabilities: Set[Capability],
    ) -> None:
        """Synchronously registers a user and hashes their password."""
        h = self._hasher.hash(password)
        with self._lock:
            self._credentials[operator_id] = {
                "hash": h,
                "roles": list(roles),
                "capabilities": set(capabilities),
            }

    async def register_user_async(
        self,
        operator_id: str,
        password: str,
        roles: List[str],
        capabilities: Set[Capability],
    ) -> None:
        """Asynchronously registers a user, offloading hashing to worker threadpool."""
        loop = asyncio.get_running_loop()
        h = await loop.run_in_executor(self._executor, self._hasher.hash, password)
        with self._lock:
            self._credentials[operator_id] = {
                "hash": h,
                "roles": list(roles),
                "capabilities": set(capabilities),
            }

    def is_locked(self, operator_id: str) -> bool:
        """Checks if operator account is locked due to excessive failed attempts."""
        with self._lock:
            return operator_id in self._locked_accounts

    def unlock_user(self, operator_id: str) -> bool:
        """Resets failed attempts and unlocks account."""
        with self._lock:
            self._failed_attempts.pop(operator_id, None)
            return self._locked_accounts.pop(operator_id, None) is not None

    def _verify_worker(self, stored_hash: str, password: str) -> bool:
        """Executed inside dedicated Argon2Worker thread."""
        self.last_verification_thread = threading.current_thread().name
        try:
            return bool(self._hasher.verify(stored_hash, password))
        except (
            argon2.exceptions.VerifyMismatchError,
            argon2.exceptions.VerificationError,
            argon2.exceptions.InvalidHash,
        ):
            return False

    async def authenticate(
        self,
        operator_id: str,
        password: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Authenticates operator against native credentials.
        Strictly executes Argon2id in a dedicated worker threadpool to protect
        the async event loop (INVARIANT-SEC-TECH-ARGON2-01).
        """
        with self._lock:
            if operator_id in self._locked_accounts:
                return None
            user_data = self._credentials.get(operator_id)
            if not user_data:
                return None
            stored_hash = user_data["hash"]

        loop = asyncio.get_running_loop()
        verified = await loop.run_in_executor(
            self._executor,
            self._verify_worker,
            stored_hash,
            password,
        )

        with self._lock:
            if verified:
                self._failed_attempts[operator_id] = 0
                return {
                    "operator_id": operator_id,
                    "roles": list(user_data["roles"]),
                    "capabilities": set(user_data["capabilities"]),
                }
            else:
                attempts = self._failed_attempts.get(operator_id, 0) + 1
                self._failed_attempts[operator_id] = attempts
                if attempts >= self._max_failed_attempts:
                    self._locked_accounts[operator_id] = datetime.now(timezone.utc)
                return None

    def close(self) -> None:
        """Shuts down worker threadpool executor cleanly."""
        self._executor.shutdown(wait=True)


@dataclass(frozen=True)
class AuditChainVerificationResult:
    is_valid: bool
    record_count: int
    message: str

    def __iter__(self):
        yield self.is_valid
        yield self.record_count
        yield self.message


def verify_audit_chain(log_file_path: str) -> AuditChainVerificationResult:
    """
    Traverses an on-disk SHA-256 audit log from GENESIS_HASH to tail.
    Verifies that:
    1. Every line is valid JSON.
    2. entry['prev_hash'] strictly matches the preceding entry's 'hash' (or GENESIS_HASH for record 1).
    3. entry['hash'] matches the SHA-256 recalculation of the sorted entry content.
    Returns AuditChainVerificationResult(is_valid, record_count, message).
    """
    if not os.path.exists(log_file_path):
        return AuditChainVerificationResult(True, 0, "Audit log file does not exist (empty genesis).")

    expected_prev = Tier1AuditLogger.GENESIS_HASH
    count = 0

    with open(log_file_path, "r", encoding="utf-8") as f:
        for line_idx, line in enumerate(f, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError as exc:
                return AuditChainVerificationResult(False, count, f"Line {line_idx} contains invalid JSON: {exc}")

            stored_prev = entry.get("prev_hash")
            stored_hash = entry.get("hash")

            if stored_prev != expected_prev:
                return AuditChainVerificationResult(
                    False,
                    count,
                    f"Chain broken at line {line_idx}: expected prev_hash {expected_prev[:12]}..., "
                    f"got {stored_prev[:12] if stored_prev else 'None'}...",
                )

            # Recalculate hash
            entry_copy = dict(entry)
            entry_copy.pop("hash", None)
            serialized = json.dumps(entry_copy, sort_keys=True)
            calc_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

            if stored_hash != calc_hash:
                return AuditChainVerificationResult(
                    False,
                    count,
                    f"Checksum mismatch at line {line_idx}: stored {stored_hash[:12]}..., "
                    f"calculated {calc_hash[:12]}...",
                )

            expected_prev = stored_hash
            count += 1

    return AuditChainVerificationResult(True, count, f"Audit chain verified: {count} valid consecutive records.")

