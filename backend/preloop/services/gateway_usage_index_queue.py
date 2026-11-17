"""Write gateway search-corpus rows off the response path.

The gateway used to build and write a search document while it still held
the request and response bodies of the call it had just served. That put a
text pass and a database round trip between the upstream response and the
point where the payloads became garbage, on the pod with the tightest
memory budget in the deployment (issue #670).

What runs on the response path now is only
``GatewayUsageSearchService.build_index_document``, which reads the payloads
once and returns a bounded document: an id, at most ``MAX_TEXT_CHARS`` of
text and a small metadata dict. That document is handed to this queue and
the payloads are free. A single worker thread drains the queue with its own
session, so the write cannot hold the request session, cannot slow the
response, and cannot turn a successful model call into an error.

The queue is bounded on purpose. If indexing falls behind, dropping
documents is the correct failure: the corpus is an opt-in convenience, and
memory pressure is the exact problem this exists to avoid. Drops are
counted and logged.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Optional

from preloop.config import settings
from preloop.models.crud import crud_gateway_usage_search_document
from preloop.models.db.session import get_db_session
from preloop.services.gateway_usage_search import GatewayUsageIndexDocument

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 256


def _max_pending() -> int:
    """Pending documents allowed before new ones are dropped."""
    try:
        configured = int(settings.gateway_usage_index_queue_max_pending)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING
    return max(1, configured)


def _worker_enabled() -> bool:
    """Whether a background worker thread may be started.

    Tests drive the queue explicitly with :meth:`GatewayUsageIndexQueue.drain`
    so a suite never depends on a thread it did not start.
    """
    if os.getenv("TESTING") == "true":
        return False
    return bool(settings.gateway_usage_index_queue_enabled)


class GatewayUsageIndexQueue:
    """A bounded queue of prepared corpus rows plus the worker that writes them."""

    def __init__(self, max_pending: Optional[int] = None) -> None:
        self._queue: queue.Queue[GatewayUsageIndexDocument] = queue.Queue(
            maxsize=max_pending if max_pending is not None else _max_pending()
        )
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self.submitted = 0
        self.dropped = 0
        self.written = 0
        self.failed = 0

    @property
    def pending(self) -> int:
        """Documents waiting to be written."""
        return self._queue.qsize()

    def submit(self, document: GatewayUsageIndexDocument) -> bool:
        """Queue one prepared document. Never blocks, never raises."""
        try:
            self._queue.put_nowait(document)
        except queue.Full:
            self.dropped += 1
            logger.warning(
                "Gateway interaction indexing queue full; dropped document for "
                "usage %s (dropped=%s)",
                document.api_usage_id,
                self.dropped,
            )
            return False
        self.submitted += 1
        self._ensure_worker()
        return True

    def drain(self) -> int:
        """Write every currently queued document. Returns how many were written.

        In tests this is called directly, so the write path is exercised
        without depending on a background thread.
        """
        written = 0
        while True:
            try:
                document = self._queue.get_nowait()
            except queue.Empty:
                return written
            try:
                if self._write(document):
                    written += 1
            finally:
                self._queue.task_done()

    def _ensure_worker(self) -> None:
        if not _worker_enabled():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._run,
                name="gateway-usage-indexer",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _run(self) -> None:
        while True:
            document = self._queue.get()
            try:
                self._write(document)
            finally:
                self._queue.task_done()

    def _write(self, document: GatewayUsageIndexDocument) -> bool:
        """Persist one document. Failures are logged, never raised."""
        try:
            db = next(get_db_session())
        except Exception:
            self.failed += 1
            logger.error(
                "Gateway interaction indexing could not open a session for usage %s",
                document.api_usage_id,
            )
            return False
        try:
            crud_gateway_usage_search_document.upsert_for_api_usage_id(
                db,
                api_usage_id=document.api_usage_id,
                searchable_text=document.searchable_text,
                meta_data=document.meta_data,
            )
            self.written += 1
            return True
        except Exception:
            self.failed += 1
            db.rollback()
            # The message carries the usage id and nothing else: the
            # document is built from customer content.
            logger.error(
                "Gateway interaction indexing failed for usage %s",
                document.api_usage_id,
            )
            return False
        finally:
            db.close()


_queue_instance: Optional[GatewayUsageIndexQueue] = None
_queue_instance_lock = threading.Lock()


def get_gateway_usage_index_queue() -> GatewayUsageIndexQueue:
    """Return the process-wide index queue."""
    global _queue_instance
    if _queue_instance is None:
        with _queue_instance_lock:
            if _queue_instance is None:
                _queue_instance = GatewayUsageIndexQueue()
    return _queue_instance


def reset_gateway_usage_index_queue() -> None:
    """Drop the process-wide queue. For tests."""
    global _queue_instance
    with _queue_instance_lock:
        _queue_instance = None
