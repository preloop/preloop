"""Bounded background writer for per-request API usage rows.

Every authenticated API request writes one ``api_usage`` row. Before this
module those writes were handed to an unbounded ``ThreadPoolExecutor`` queue,
so a database that could not keep up (or a saturated connection pool) grew a
backlog of pending writes, each holding or waiting for a connection, and each
logging its own ``Error logging API usage: QueuePool limit of size 8 overflow
12 reached ...`` line. That is the error string the 2026-09-03 staging
incident surfaced: not the cause, but the loudest symptom.

The recorder here is deliberately boring:

* a bounded queue: when it is full, the record is dropped and counted, never
  queued behind a request or retried;
* a small fixed number of writer threads that batch rows into one session,
  so the writer holds far fewer connections than it did per request;
* counters (and OpenTelemetry counters when export is configured) for
  written, dropped and failed rows, plus rate-limited logging so a database
  problem cannot flood the log with one line per request.

Usage telemetry is the least important write in the system. It is the first
thing that should be shed when the database is under pressure.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 1000
DEFAULT_WORKERS = 2
DEFAULT_BATCH_SIZE = 50

# One log line per problem per minute, however many rows are affected.
LOG_THROTTLE_SECONDS = 60.0

_FALSE_VALUES = {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment variable with a safe fallback."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    return value


def usage_logging_enabled() -> bool:
    """Return whether usage rows should be written (API_USAGE_LOGGING_ENABLED)."""
    return os.getenv("API_USAGE_LOGGING_ENABLED", "true").strip().lower() not in (
        _FALSE_VALUES
    )


@dataclass(frozen=True)
class ApiUsageRecord:
    """One request's usage row, captured before it is queued.

    Holding a plain value object rather than an ORM instance keeps the request
    path free of session state: nothing here is attached to a session until a
    writer thread picks it up.
    """

    user_id: UUID
    endpoint: str
    method: str
    status_code: int
    duration: float
    action_type: Optional[str]
    timestamp: datetime


class _Throttle:
    """Emit at most one message per interval."""

    def __init__(self, interval_seconds: float) -> None:
        """Store the interval and arm the throttle."""
        self._interval = interval_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def should_emit(self) -> bool:
        """Return True when the caller may log now."""
        now = time.monotonic()
        with self._lock:
            if now - self._last < self._interval:
                return False
            self._last = now
            return True


class ApiUsageRecorder:
    """Queue usage rows and write them in batches on background threads."""

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        queue_size: Optional[int] = None,
        workers: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        """Configure the queue and writer pool.

        Args:
            session_factory: Callable returning a new session. Defaults to the
                application session factory, resolved on first use so that
                importing this module never builds an engine.
            queue_size: Maximum pending rows (``API_USAGE_QUEUE_SIZE``).
            workers: Writer threads (``API_USAGE_WORKERS``).
            batch_size: Rows written per session (``API_USAGE_BATCH_SIZE``).
        """
        self._session_factory = session_factory
        self._queue_size = queue_size or _env_int(
            "API_USAGE_QUEUE_SIZE", DEFAULT_QUEUE_SIZE
        )
        self._worker_count = workers or _env_int("API_USAGE_WORKERS", DEFAULT_WORKERS)
        self._batch_size = batch_size or _env_int(
            "API_USAGE_BATCH_SIZE", DEFAULT_BATCH_SIZE
        )
        self._queue: queue.Queue[Optional[ApiUsageRecord]] = queue.Queue(
            maxsize=self._queue_size
        )
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._stopping = False
        self._counts_lock = threading.Lock()
        self._written = 0
        self._dropped = 0
        self._failed = 0
        self._drop_log = _Throttle(LOG_THROTTLE_SECONDS)
        self._error_log = _Throttle(LOG_THROTTLE_SECONDS)
        self._otel_ready = False

    # -- public API ---------------------------------------------------------

    def submit(self, record: ApiUsageRecord) -> bool:
        """Queue one usage row without ever blocking the caller.

        Args:
            record: The row to write.

        Returns:
            True when the row was queued, False when it was dropped because
            the queue was full or the recorder is shutting down.
        """
        if self._stopping:
            return False
        self._ensure_started()
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._count_drop()
            return False
        return True

    def stats(self) -> dict[str, int]:
        """Return counters for health reporting and tests."""
        with self._counts_lock:
            written, dropped, failed = self._written, self._dropped, self._failed
        return {
            "queued": self._queue.qsize(),
            "capacity": self._queue_size,
            "written": written,
            "dropped": dropped,
            "failed": failed,
        }

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the writers, giving queued rows a bounded chance to land."""
        with self._lock:
            if not self._started:
                self._stopping = True
                return
            self._stopping = True
            workers = list(self._workers)
            self._workers = []
            self._started = False
        deadline = time.monotonic() + timeout
        for _ in workers:
            while True:
                try:
                    self._queue.put_nowait(None)
                    break
                except queue.Full:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

    # -- internals ----------------------------------------------------------

    def _ensure_started(self) -> None:
        """Start writer threads on first use."""
        if self._started:
            return
        with self._lock:
            if self._started or self._stopping:
                return
            for index in range(self._worker_count):
                worker = threading.Thread(
                    target=self._run,
                    name=f"api_usage_{index}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
            self._started = True

    def _resolve_session_factory(self) -> Callable[[], Any]:
        """Return the configured session factory, defaulting to the app's."""
        if self._session_factory is not None:
            return self._session_factory
        from preloop.models.db.session import get_session_factory

        return get_session_factory()

    def _run(self) -> None:
        """Drain the queue in batches until asked to stop."""
        while True:
            if self._stopping:
                try:
                    record = self._queue.get(timeout=0.25)
                except queue.Empty:
                    return
            else:
                record = self._queue.get()
            if record is None:
                return
            batch = [record]
            while len(batch) < self._batch_size:
                try:
                    extra = self._queue.get_nowait()
                except queue.Empty:
                    break
                if extra is None:
                    self._write(batch)
                    return
                batch.append(extra)
            self._write(batch)

    def _write(self, batch: list[ApiUsageRecord]) -> None:
        """Write one batch in a single short-lived session."""
        from preloop.models.models.api_usage import ApiUsage

        try:
            session_factory = self._resolve_session_factory()
            session = session_factory()
        except Exception:
            self._count_failure(len(batch), "could not open a usage session")
            return
        try:
            session.add_all(
                [
                    ApiUsage(
                        user_id=record.user_id,
                        endpoint=record.endpoint,
                        method=record.method,
                        status_code=record.status_code,
                        duration=record.duration,
                        action_type=record.action_type,
                        timestamp=record.timestamp,
                    )
                    for record in batch
                ]
            )
            session.commit()
            with self._counts_lock:
                self._written += len(batch)
        except Exception:
            try:
                session.rollback()
            except Exception:
                logger.debug("Usage rollback failed", exc_info=True)
            self._count_failure(len(batch), "could not write usage rows")
        finally:
            try:
                session.close()
            except Exception:
                logger.debug("Usage session close failed", exc_info=True)

    def _count_drop(self) -> None:
        """Count one dropped row, with rate-limited logging and a metric."""
        with self._counts_lock:
            self._dropped += 1
        self._emit_metric("preloop.api_usage.dropped", 1)
        if self._drop_log.should_emit():
            logger.warning(
                "API usage logging is shedding load: %d rows dropped, queue "
                "full at %d. Usage telemetry is lossy by design; investigate "
                "database write latency rather than raising the queue size.",
                self._dropped,
                self._queue_size,
            )

    def _count_failure(self, count: int, reason: str) -> None:
        """Count failed rows, with rate-limited logging and a metric."""
        with self._counts_lock:
            self._failed += count
        self._emit_metric("preloop.api_usage.failed", count)
        if self._error_log.should_emit():
            logger.error(
                "API usage logging %s (%d rows affected, %d total failures). "
                "The request itself was unaffected.",
                reason,
                count,
                self._failed,
                exc_info=True,
            )

    def _emit_metric(self, name: str, value: int) -> None:
        """Add to an OpenTelemetry counter when metrics are configured.

        Mirrors ``services/db_pool_monitor.py``: when OTLP export is off the
        global meter provider is a no-op, so this costs nothing.
        """
        counter = _COUNTERS.get(name)
        if counter is None:
            try:
                from opentelemetry import metrics

                counter = metrics.get_meter("preloop.api_usage").create_counter(
                    name,
                    unit="{record}",
                    description="API usage rows that never reached the database",
                )
            except Exception:  # pragma: no cover - metrics are best effort
                logger.debug("Could not create counter %s", name, exc_info=True)
                return
            _COUNTERS[name] = counter
        try:
            counter.add(value)
        except Exception:  # pragma: no cover - metrics are best effort
            logger.debug("Could not record %s", name, exc_info=True)


_COUNTERS: dict[str, Any] = {}

_recorder: Optional[ApiUsageRecorder] = None
_recorder_lock = threading.Lock()


def get_api_usage_recorder() -> ApiUsageRecorder:
    """Return the process-wide recorder, creating it on first use."""
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = ApiUsageRecorder()
    return _recorder


def record_api_usage(record: ApiUsageRecord) -> bool:
    """Queue one usage row on the process-wide recorder."""
    if not usage_logging_enabled():
        return False
    return get_api_usage_recorder().submit(record)


def shutdown_api_usage_recorder(timeout: float = 5.0) -> None:
    """Stop the process-wide recorder during application shutdown."""
    global _recorder
    with _recorder_lock:
        recorder = _recorder
        _recorder = None
    if recorder is not None:
        recorder.shutdown(timeout=timeout)
