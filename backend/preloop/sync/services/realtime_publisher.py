"""Bounded, loop-owned publisher for best-effort realtime NATS messages.

A ``nats.aio.client.Client`` belongs to the event loop that connected it: its
flush queue, flusher task and futures are bound to that loop. Publishing from
another loop (``run_async`` in an AnyIO worker thread, or a second loop
running in another thread) creates futures the owner loop resolves from the
wrong thread, so a forced flush can wait forever while the caller still holds
a database connection.

Callers therefore never publish directly. ``submit`` takes already-encoded
bytes, so the payload is a snapshot taken by the caller, and hands them to the
owner loop with ``call_soon_threadsafe``. One drain task on the owner loop
publishes them with a per-message timeout. The backlog is bounded by count and
bytes; anything refused is counted and logged rather than queued without
limit. ``submit`` never blocks and never raises.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING_MESSAGES = 1_000
DEFAULT_MAX_PENDING_BYTES = 16 * 1024 * 1024
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 2.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 2.0
_DROP_LOG_INTERVAL_SECONDS = 30.0


@dataclass
class RealtimePublisherStats:
    """Counters exposed for diagnostics and tests."""

    submitted: int = 0
    published: int = 0
    failed: int = 0
    timed_out: int = 0
    dropped: dict[str, int] = field(default_factory=dict)


class RealtimePublisher:
    """Publish realtime messages on the loop that owns the NATS client."""

    def __init__(
        self,
        *,
        max_pending_messages: int = DEFAULT_MAX_PENDING_MESSAGES,
        max_pending_bytes: int = DEFAULT_MAX_PENDING_BYTES,
        publish_timeout: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self.max_pending_messages = max_pending_messages
        self.max_pending_bytes = max_pending_bytes
        self.publish_timeout = publish_timeout
        self.stats = RealtimePublisherStats()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client_getter: Optional[Callable[[], Any]] = None
        self._queue: deque[tuple[str, bytes]] = deque()
        self._wakeup: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._pending_messages = 0
        self._pending_bytes = 0
        self._last_drop_log = 0.0
        self._generation = 0

    @property
    def pending(self) -> tuple[int, int]:
        """Return the backlog as ``(messages, bytes)``."""
        with self._lock:
            return self._pending_messages, self._pending_bytes

    @property
    def running(self) -> bool:
        """Whether a drain task is attached to a live owner loop."""
        task = self._task
        if task is None or task.done():
            return False
        # A task on a closed loop never finishes; its owner is gone.
        return not task.get_loop().is_closed()

    def start(self, client_getter: Callable[[], Any]) -> None:
        """Attach to the running loop. Must be called on the owner loop.

        Args:
            client_getter: Returns the current NATS client, or None. Read on
                every publish so a reconnect or a replaced client is seen.
        """
        loop = asyncio.get_running_loop()
        if self.running:
            assert self._task is not None
            if self._task.get_loop() is not loop:
                raise RuntimeError("Realtime publisher must stay on its owner loop")
            self._client_getter = client_getter
            return
        with self._lock:
            # Anything a dead previous owner left behind is counted, not kept.
            abandoned = self._pending_messages
            self._queue.clear()
            self._pending_messages = 0
            self._pending_bytes = 0
            self._generation += 1
            generation = self._generation
            self._loop = loop
            self._client_getter = client_getter
            self._wakeup = asyncio.Event()
            if self._queue:
                self._wakeup.set()
        self._task = loop.create_task(
            self._drain(generation), name="realtime-publisher"
        )
        if abandoned:
            self._record_drop("owner_loop_closed", count=abandoned)

    async def stop(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS) -> None:
        """Flush what is queued within ``timeout``, then detach.

        Must be called on the owner loop, before the NATS client is drained.
        Messages still queued afterwards are dropped and counted.
        """
        task = self._task
        if task is not None and task.get_loop() is not asyncio.get_running_loop():
            raise RuntimeError("Realtime publisher must stop on its owner loop")
        with self._lock:
            # New submissions are refused from here on.
            self._loop = None
        if task is not None and not task.done():
            if self._wakeup is not None:
                self._wakeup.set()
            deadline = time.monotonic() + timeout
            # The pending count includes hand-offs not yet run on this loop
            # and the message being published right now.
            while self.pending[0] and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        with self._lock:
            left = self._pending_messages
            self._queue.clear()
            self._pending_messages = 0
            self._pending_bytes = 0
            self._task = None
            self._wakeup = None
            self._client_getter = None
            # Callbacks already accepted by call_soon_threadsafe can run after
            # this bounded shutdown. They must not enter the next lifecycle.
            self._generation += 1
        if left:
            self._record_drop("shutdown", count=left)

    def submit(self, subject: str, payload: bytes) -> bool:
        """Queue one encoded message for the owner loop. Never blocks.

        Safe from any thread and from any loop, including the owner loop.

        Args:
            subject: NATS subject.
            payload: Encoded message bytes; the caller's snapshot.

        Returns:
            bool: True when queued, False when refused (and counted).
        """
        size = len(payload)
        with self._lock:
            self.stats.submitted += 1
            loop = self._loop
            generation = self._generation
            if loop is None or loop.is_closed() or not self.running:
                reason = "no_owner_loop"
            elif self._pending_messages >= self.max_pending_messages or (
                self._pending_bytes + size > self.max_pending_bytes
            ):
                reason = "backlog_full"
            else:
                reason = ""
                self._pending_messages += 1
                self._pending_bytes += size
        if reason:
            self._record_drop(reason)
            return False
        try:
            assert loop is not None
            loop.call_soon_threadsafe(self._enqueue, generation, subject, payload)
        except RuntimeError:
            # The owner loop closed between the check and the hand-off.
            self._release(size, generation)
            self._record_drop("no_owner_loop")
            return False
        return True

    def _enqueue(self, generation: int, subject: str, payload: bytes) -> None:
        """Owner-loop side of ``submit``."""
        if generation != self._generation:
            # Shutdown has already counted this abandoned hand-off.
            return
        if self._wakeup is None:
            self._release(len(payload), generation)
            self._record_drop("shutdown")
            return
        self._queue.append((subject, payload))
        self._wakeup.set()

    def _release(self, size: int, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._pending_messages = max(0, self._pending_messages - 1)
            self._pending_bytes = max(0, self._pending_bytes - size)

    async def _drain(self, generation: int) -> None:
        wakeup = self._wakeup
        assert wakeup is not None
        while True:
            if not self._queue:
                wakeup.clear()
                await wakeup.wait()
                continue
            subject, payload = self._queue.popleft()
            try:
                await self._publish_one(subject, payload)
            finally:
                self._release(len(payload), generation)

    async def _publish_one(self, subject: str, payload: bytes) -> None:
        try:
            client = self._client_getter() if self._client_getter else None
            if client is None or getattr(client, "is_closed", False):
                self._record_drop("not_connected")
                return
            # nats-py buffers while reconnecting and raises when that buffer is
            # full; a forced flush with no flush_timeout can otherwise wait
            # indefinitely, so every publish is bounded here as well.
            await asyncio.wait_for(
                client.publish(subject, payload), timeout=self.publish_timeout
            )
        except asyncio.TimeoutError:
            self.stats.timed_out += 1
            logger.warning(
                "Realtime NATS publish to %s timed out after %.1fs",
                subject,
                self.publish_timeout,
            )
        except asyncio.CancelledError:
            self._record_drop("shutdown")
            raise
        except Exception as exc:  # noqa: BLE001 - best effort, but never silent
            self.stats.failed += 1
            logger.warning(
                "Realtime NATS publish to %s failed: %s", subject, type(exc).__name__
            )
        else:
            self.stats.published += 1

    def _record_drop(self, reason: str, count: int = 1) -> None:
        with self._lock:
            self.stats.dropped[reason] = self.stats.dropped.get(reason, 0) + count
            totals = dict(self.stats.dropped)
            now = time.monotonic()
            should_log = now - self._last_drop_log >= _DROP_LOG_INTERVAL_SECONDS
            if should_log:
                self._last_drop_log = now
        if should_log:
            logger.warning(
                "Dropping realtime NATS message (%s); totals by reason: %s",
                reason,
                totals,
            )


realtime_publisher = RealtimePublisher()


def publish_realtime(subject: str, payload: bytes) -> bool:
    """Queue a best-effort realtime message on the NATS owner loop."""
    return realtime_publisher.submit(subject, payload)
