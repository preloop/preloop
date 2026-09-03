"""Tests for the bounded API usage recorder.

Usage logging is the write that shouted during the 2026-09-03 staging
incident (``Error logging API usage: QueuePool limit of size 8 overflow 12
reached``). These tests pin the behaviour that keeps it quiet and harmless:
it never blocks a request, it sheds load with a counter when the queue fills,
and a database failure costs rows rather than requests.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from preloop.services.api_usage_recorder import ApiUsageRecord, ApiUsageRecorder

POOL_TIMEOUT_MESSAGE = (
    "QueuePool limit of size 8 overflow 12 reached, connection timed out, timeout 5.00"
)


def _record(endpoint: str = "/api/v1/ai-models") -> ApiUsageRecord:
    """Build one usage record."""
    return ApiUsageRecord(
        user_id=uuid.uuid4(),
        endpoint=endpoint,
        method="GET",
        status_code=200,
        duration=0.01,
        action_type=None,
        timestamp=datetime.now(timezone.utc),
    )


class _FakeSession:
    """Collects rows instead of writing them."""

    def __init__(self, sink: list[Any], batches: list[int]) -> None:
        """Store the shared sink for written rows and batch sizes."""
        self._sink = sink
        self._batches = batches
        self._pending: list[Any] = []

    def add_all(self, rows: list[Any]) -> None:
        """Stage rows for the next commit."""
        self._pending.extend(rows)

    def commit(self) -> None:
        """Record the staged rows and the batch size."""
        self._sink.extend(self._pending)
        self._batches.append(len(self._pending))
        self._pending = []

    def rollback(self) -> None:
        """Drop staged rows."""
        self._pending = []

    def close(self) -> None:
        """No-op close."""


def _drain(recorder: ApiUsageRecorder, expected: int, timeout: float = 5.0) -> None:
    """Wait until the recorder has accounted for the expected rows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = recorder.stats()
        if stats["written"] + stats["dropped"] + stats["failed"] >= expected:
            return
        time.sleep(0.01)
    pytest.fail(f"recorder did not settle: {recorder.stats()}")


def test_records_are_written_in_batches() -> None:
    """Rows land in the database grouped into one session per batch."""
    written: list[Any] = []
    batches: list[int] = []
    recorder = ApiUsageRecorder(
        session_factory=lambda: _FakeSession(written, batches),
        workers=1,
        batch_size=50,
    )
    try:
        for _ in range(20):
            assert recorder.submit(_record()) is True
        _drain(recorder, 20)
    finally:
        recorder.shutdown()

    assert len(written) == 20
    # One session per batch rather than one per request: the whole point is
    # holding fewer connections than there are requests.
    assert len(batches) < 20


def test_full_queue_drops_and_counts_instead_of_blocking() -> None:
    """A full queue sheds rows immediately; requests are never held up."""
    release = threading.Event()

    def _blocked_session_factory() -> Any:
        """Stand in for a writer stuck on a saturated pool."""
        release.wait(timeout=10)
        raise SQLAlchemyTimeoutError(POOL_TIMEOUT_MESSAGE)

    recorder = ApiUsageRecorder(
        session_factory=_blocked_session_factory,
        queue_size=2,
        workers=1,
        batch_size=1,
    )
    try:
        accepted = 0
        started = time.perf_counter()
        for _ in range(50):
            if recorder.submit(_record()):
                accepted += 1
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0, "submitting usage rows must never block a request"
        assert accepted <= 3, "the bounded queue must stop accepting rows"
        assert recorder.stats()["dropped"] >= 47
    finally:
        release.set()
        recorder.shutdown()


def test_database_failure_costs_rows_not_requests() -> None:
    """A pool timeout in the writer is counted, not raised at the caller."""

    def _failing_session_factory() -> Any:
        """Fail the way a saturated pool fails."""
        raise SQLAlchemyTimeoutError(POOL_TIMEOUT_MESSAGE)

    recorder = ApiUsageRecorder(
        session_factory=_failing_session_factory,
        workers=1,
        batch_size=1,
    )
    try:
        for _ in range(5):
            assert recorder.submit(_record()) is True
        _drain(recorder, 5)
    finally:
        recorder.shutdown()

    stats = recorder.stats()
    assert stats["failed"] == 5
    assert stats["written"] == 0


def test_shutdown_stops_accepting_records() -> None:
    """After shutdown the recorder drops rather than starting new threads."""
    written: list[Any] = []
    batches: list[int] = []
    recorder = ApiUsageRecorder(
        session_factory=lambda: _FakeSession(written, batches),
        workers=1,
    )
    recorder.submit(_record())
    _drain(recorder, 1)
    recorder.shutdown()

    assert recorder.submit(_record()) is False


def test_shutdown_retries_stop_sentinel_when_queue_is_full() -> None:
    """A full queue must not leave workers blocked on get() after shutdown."""
    release = threading.Event()

    def _blocked_session_factory() -> Any:
        release.wait(timeout=10)
        raise SQLAlchemyTimeoutError(POOL_TIMEOUT_MESSAGE)

    recorder = ApiUsageRecorder(
        session_factory=_blocked_session_factory,
        queue_size=2,
        workers=2,
        batch_size=1,
    )
    try:
        for _ in range(2):
            assert recorder.submit(_record()) is True
        started = time.perf_counter()
        recorder.shutdown(timeout=2.0)
        elapsed = time.perf_counter() - started
    finally:
        release.set()

    assert elapsed < 2.5, "shutdown should not rely on daemon threads alone"
