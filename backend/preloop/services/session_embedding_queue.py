"""Bounded in-process queue that drives the session embedding worker.

The corpus is written on request paths that must not wait for a provider.
Those paths hand an account id to this queue and return; a single worker
thread drains it with its own database session and embeds one batch per
submission.

Bounded means a full queue drops work rather than blocking a request thread.
Dropping is cheap here in a way it is not for most queues: the backlog is
durable in ``session_search_document`` (the chunks stay ``pending``), so a
dropped submission costs a delay, never a vector. Blocking instead would put
the memory and latency of the embedding backlog on the pod serving agent
traffic, which is the whole thing this design avoids.

Submissions are deduplicated by account: one busy account cannot fill the
queue with its own id and starve every other account's backlog.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any, Optional, Set

from preloop.config import settings
from preloop.models.db.session import get_db_session
from preloop.services.session_embedding import (
    EmbeddingBatchResult,
    embedding_enabled,
    run_account_batch,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 128


def _max_pending() -> int:
    """Accounts allowed to wait before new submissions are dropped."""
    try:
        configured = int(settings.session_embedding_queue_max_pending)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING
    return max(1, configured)


def _worker_enabled() -> bool:
    """Whether a background worker thread may be started.

    Tests drive the queue with :meth:`SessionEmbeddingQueue.drain` so a suite
    never depends on a thread it did not start.
    """
    if os.getenv("TESTING") == "true":
        return False
    return bool(getattr(settings, "session_embedding_queue_worker_enabled", True))


class SessionEmbeddingQueue:
    """A bounded queue of account ids plus the worker that drains it."""

    def __init__(self, max_pending: Optional[int] = None) -> None:
        self._queue: queue.Queue[str] = queue.Queue(
            maxsize=max_pending if max_pending is not None else _max_pending()
        )
        self._queued: Set[str] = set()
        self._queued_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self.submitted = 0
        self.dropped = 0
        self.deduplicated = 0
        self.embedded = 0
        self.failed = 0

    @property
    def pending(self) -> int:
        """Accounts waiting for a batch."""
        return self._queue.qsize()

    def submit(self, account_id: Any) -> bool:
        """Queue one account for a batch. Never blocks, never raises."""
        if account_id is None:
            return False
        account = str(account_id)
        with self._queued_lock:
            if account in self._queued:
                self.deduplicated += 1
                return False
            self._queued.add(account)
        try:
            self._queue.put_nowait(account)
        except queue.Full:
            with self._queued_lock:
                self._queued.discard(account)
            self.dropped += 1
            logger.warning(
                "Session embedding queue full; dropped account %s (dropped=%s). "
                "The chunks stay pending and a later write requeues them.",
                account,
                self.dropped,
            )
            return False
        self.submitted += 1
        self._ensure_worker()
        return True

    def drain(self) -> int:
        """Run a batch for every queued account. Returns chunks embedded.

        Called directly in tests, so the whole write path is exercised
        without depending on a background thread.
        """
        embedded = 0
        while True:
            try:
                account = self._queue.get_nowait()
            except queue.Empty:
                return embedded
            result = None
            try:
                result = self._run(account)
                embedded += result.embedded if result is not None else 0
            finally:
                with self._queued_lock:
                    self._queued.discard(account)
                self._queue.task_done()
            if result is not None and result.embedded and result.pending:
                self.submit(account)

    def _ensure_worker(self) -> None:
        if not _worker_enabled():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._loop,
                name="session-embedding-worker",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _loop(self) -> None:
        while True:
            account = self._queue.get()
            result = None
            try:
                result = self._run(account)
            finally:
                with self._queued_lock:
                    self._queued.discard(account)
                self._queue.task_done()
            if result is not None and result.embedded and result.pending:
                self.submit(account)

    def _run(self, account_id: str) -> Optional[EmbeddingBatchResult]:
        """Embed one batch for one account. Failures are logged, never raised."""
        if not embedding_enabled():
            return None
        try:
            db = next(get_db_session())
        except Exception:  # noqa: BLE001 - a dead pool must not kill the worker
            self.failed += 1
            logger.error(
                "Session embedding could not open a session for account %s", account_id
            )
            return None
        try:
            result = run_account_batch(db, account_id=account_id)
            self.embedded += result.embedded
            return result
        except Exception:  # noqa: BLE001 - one bad account must not stop the rest
            self.failed += 1
            db.rollback()
            # The message carries the account id and nothing else: the chunks
            # are customer session content.
            logger.error("Session embedding batch failed for account %s", account_id)
            return None
        finally:
            db.close()


_queue_instance: Optional[SessionEmbeddingQueue] = None
_queue_instance_lock = threading.Lock()


def get_session_embedding_queue() -> SessionEmbeddingQueue:
    """Return the process-wide embedding queue."""
    global _queue_instance
    if _queue_instance is None:
        with _queue_instance_lock:
            if _queue_instance is None:
                _queue_instance = SessionEmbeddingQueue()
    return _queue_instance


def reset_session_embedding_queue() -> None:
    """Drop the process-wide queue. For tests."""
    global _queue_instance
    with _queue_instance_lock:
        _queue_instance = None


def submit_account_for_embedding(account_id: Any) -> bool:
    """Ask for a batch for one account, swallowing every failure.

    Called from the indexing write path, which must never be slowed or
    broken by the embedding half.
    """
    if not embedding_enabled():
        return False
    try:
        return get_session_embedding_queue().submit(account_id)
    except Exception:  # noqa: BLE001 - indexing never fails over embedding
        logger.warning("Session embedding submission failed for account %s", account_id)
        return False
