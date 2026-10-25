"""Bounded best-effort audit writes, isolated from the API event loop."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from copy import deepcopy
import logging
from threading import Lock
from typing import Any, Callable

from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Durable audit outbox switch (#94). Read here, not in ``outbox``, so the
#: flag-off path never imports the outbox module or its OSS table.
FLAG_ENV = "PRELOOP_AUDIT_OUTBOX_ENABLED"


def outbox_enabled() -> bool:
    """Whether audit intents go through the durable outbox. Default off."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


# Same phrases as preloop.utils.sentry_filters transient database disconnects.
# Matched only against the DB-API original, never the SQL or parameters.
_DISCONNECTED_CONNECTION_PHRASES = (
    "ssl connection has been closed",
    "connection reset by peer",
    "server closed the connection unexpectedly",
    "connection already closed",
)


def infrastructure_failure_reason(exc: BaseException) -> str | None:
    """Classify expected pool pressure and disconnected connections.

    Args:
        exc: Failure from an audit session open, write, or close.

    Returns:
        ``pool_timeout`` or ``disconnected_connection`` for expected
        infrastructure pressure. ``None`` when the failure must stay an error.
    """
    if isinstance(exc, PoolTimeoutError):
        return "pool_timeout"
    if isinstance(exc, OperationalError) and _is_disconnected_connection(exc):
        return "disconnected_connection"
    return None


def _is_disconnected_connection(exc: OperationalError) -> bool:
    """Return True for an invalidated or known-disconnected operational error."""
    if getattr(exc, "connection_invalidated", False):
        return True
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    try:
        message = str(orig).partition("\n")[0].lower()
    except Exception:
        return False
    return any(phrase in message for phrase in _DISCONNECTED_CONNECTION_PHRASES)


def log_best_effort_failure(
    log: logging.Logger,
    exc: BaseException,
    *,
    summary: str,
    diagnostic: str | None = None,
) -> None:
    """Log an audit write failure without alerting on expected infrastructure errors.

    Pool checkout timeouts and disconnected connections become a warning with
    a fixed summary. Exception text, SQL, and parameters are omitted. Every
    other exception stays at error level with a traceback.

    Args:
        log: Logger for the failing component.
        exc: The caught failure.
        summary: Stable description that does not include exception text.
        diagnostic: Error-level message for unexpected failures. When omitted,
            ``summary`` is logged with the traceback and without appending
            ``str(exc)`` again.
    """
    reason = infrastructure_failure_reason(exc)
    if reason == "pool_timeout":
        _increment_infrastructure_skip_metric(reason)
        log.warning("%s after a database pool timeout", summary)
        return
    if reason == "disconnected_connection":
        _increment_infrastructure_skip_metric(reason)
        log.warning("%s after a disconnected database connection", summary)
        return
    if diagnostic is None:
        log.error(summary, exc_info=exc)
        return
    log.error(diagnostic, exc_info=exc)


origin_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "audit_origin_loop", default=None
)

_drop_counter: Any = None
_drop_counter_failed = False
_infrastructure_skip_counter: Any = None
_infrastructure_skip_counter_failed = False


def _snapshot_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Copy nested containers so callers can mutate after admission.

    Scalars (str, int, UUID, bool, float, None) are immutable and shared.
    """
    snapped: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, (dict, list)):
            snapped[key] = deepcopy(value)
        else:
            snapped[key] = value
    return snapped


def _increment_drop_metric(reason: str) -> None:
    """Count drops on the process meter so overload is alertable."""
    global _drop_counter, _drop_counter_failed
    if _drop_counter_failed:
        return
    try:
        if _drop_counter is None:
            from opentelemetry import metrics

            _drop_counter = metrics.get_meter("preloop.audit").create_counter(
                "preloop.audit.writes.dropped",
                unit="{write}",
                description=(
                    "Best-effort audit writes dropped to protect the API loop"
                ),
            )
        _drop_counter.add(1, {"reason": reason})
    except Exception:
        _drop_counter_failed = True


def _increment_infrastructure_skip_metric(reason: str) -> None:
    """Count a pool timeout or disconnection without changing drop accounting.

    Queue overload, submission failure, and shutdown cancellation stay on
    ``preloop.audit.writes.dropped``. This counter is only the best-effort
    write that ran and then lost its row to database pressure.

    Args:
        reason: ``pool_timeout`` or ``disconnected_connection``.
    """
    global _infrastructure_skip_counter, _infrastructure_skip_counter_failed
    if _infrastructure_skip_counter_failed:
        return
    try:
        if _infrastructure_skip_counter is None:
            from opentelemetry import metrics

            _infrastructure_skip_counter = metrics.get_meter(
                "preloop.audit"
            ).create_counter(
                "preloop.audit.writes.infrastructure_skipped",
                unit="{write}",
                description=(
                    "Best-effort audit writes skipped after a pool timeout "
                    "or a disconnected database connection"
                ),
            )
        _infrastructure_skip_counter.add(1, {"reason": reason})
    except Exception:
        _infrastructure_skip_counter_failed = True


class BoundedAuditWriter:
    """Admit a fixed number of writes before entering the executor queue.

    Audit delivery remains best effort: overload, submission failure and queued
    work cancelled at shutdown are counted and logged, never run on the caller.
    Approval, policy and AI-approval writes may use reserved slots so tool-call
    traffic cannot fill the entire queue.
    """

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 64,
        reserved_priority: int | None = None,
    ) -> None:
        if reserved_priority is None:
            reserved_priority = min(16, max_pending // 4)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="preloop-audit"
        )
        self._capacity = max_pending
        self._reserved_priority = min(max(reserved_priority, 0), max_pending)
        self._lock = Lock()
        self._pending = 0
        self._dropped = 0
        self._closed = False

    @property
    def pending_count(self) -> int:
        """Number of admitted writes, including running database calls."""
        with self._lock:
            return self._pending

    @property
    def dropped_count(self) -> int:
        """Total overload, submission and queued-shutdown drops this process."""
        with self._lock:
            return self._dropped

    def _drop(self, reason: str) -> None:
        with self._lock:
            self._dropped += 1
            count = self._dropped
        _increment_drop_metric(reason)
        # Report exponentially during bursts without replacing a DB stall with
        # an unbounded stream of synchronous logger writes on the event loop.
        if count & (count - 1) == 0:
            logger.error(
                "Async audit write dropped: %s; dropped_total=%s", reason, count
            )

    def _try_admit(self, priority: bool) -> bool:
        with self._lock:
            if self._closed or self._pending >= self._capacity:
                return False
            general_ceiling = self._capacity - self._reserved_priority
            if not priority and self._pending >= general_ceiling:
                return False
            self._pending += 1
            return True

    def submit(
        self,
        loop: asyncio.AbstractEventLoop | None,
        db_factory: Callable[[], Session],
        operation: Callable[..., None],
        fields: dict[str, Any],
        *,
        priority: bool = False,
    ) -> None:
        """Snapshot and enqueue only after nonblocking capacity admission."""
        if not self._try_admit(priority):
            self._drop("capacity exhausted or writer shut down")
            return
        try:
            context = copy_context()
            fields = _snapshot_fields(fields)
            with self._lock:
                if self._closed:
                    raise RuntimeError("Audit writer shut down during admission")
                future = self._executor.submit(
                    context.run, self._write, loop, db_factory, operation, fields
                )
            future.add_done_callback(self._done)
        except Exception:
            with self._lock:
                self._pending -= 1
            self._drop("submission failed")
            logger.exception("Could not submit asynchronous audit write")

    def _done(self, future: Future[None]) -> None:
        with self._lock:
            self._pending -= 1
        if future.cancelled():
            self._drop("queued write cancelled at shutdown")

    @staticmethod
    def _write(
        loop: asyncio.AbstractEventLoop | None,
        db_factory: Callable[[], Session],
        operation: Callable[..., None],
        fields: dict[str, Any],
    ) -> None:
        token = origin_loop.set(loop)
        try:
            db = db_factory()
            try:
                operation(db=db, **fields)
            finally:
                db.close()
        except Exception as exc:
            log_best_effort_failure(
                logger,
                exc,
                summary="Asynchronous audit database write failed",
            )
        finally:
            origin_loop.reset(token)

    def shutdown(self) -> None:
        """Cancel queued writes without waiting for blocked database workers.

        Running calls still own and close their sessions when the database call
        returns. Their admission slots are not released prematurely.
        """
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


writer = BoundedAuditWriter()


def schedule_audit_write(
    db_factory: Callable[[], Session],
    operation: Callable[..., None],
    *,
    priority: bool = False,
    allow_without_loop: bool = False,
    **fields: Any,
) -> None:
    """Retain the existing no-running-loop skip without creating a coroutine.

    Args:
        db_factory: Callable returning a session the worker owns and closes.
        operation: Synchronous audit write to run in the worker.
        priority: Use a reserved admission slot.
        allow_without_loop: Admit the write even when the caller has no
            running event loop. The loop is only ever used to hand the
            realtime broadcast back to the API process, so a write made from
            a threadpool worker (the search endpoint runs its query there)
            still belongs in the log; it broadcasts inline instead.
        **fields: Keyword arguments for ``operation``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        if not allow_without_loop:
            logger.debug("No event loop, skipping async audit log")
            return
        loop = None
    if outbox_enabled():
        from .outbox import intent_operation

        # Durable path (#94): the executor persists an intent and the relay
        # writes the audit row; admission and the loop stay as before.
        durable = intent_operation(operation, priority)
        if durable is not None:
            operation = durable
    writer.submit(loop, db_factory, operation, fields, priority=priority)


def start_audit_writer() -> None:
    """Allow plugin restart after shutdown; workers start lazily on submission."""
    global writer
    if writer._closed:
        writer = BoundedAuditWriter()


def shutdown_audit_writer() -> None:
    """Stop accepting work and cancel the bounded pending queue."""
    writer.shutdown()
