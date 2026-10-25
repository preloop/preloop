"""Realtime NATS publishes run on the loop that owns the client.

These tests drive a real ``nats.aio.client.Client`` (only the socket is
faked), so nats-py's own pending buffer, flush queue and flusher task decide
whether a publish completes.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Optional
from unittest.mock import patch

import pytest
from nats.aio.client import Client as NATSClient

from preloop.services import account_realtime
from preloop.services.account_realtime import build_account_event
from preloop.sync.services.realtime_publisher import RealtimePublisher

# Larger than the client's forced-flush threshold below, so every publish
# takes nats-py's ``force_flush`` path: the one that waited with no timeout.
_FORCE_FLUSH_PAYLOAD = 4096


class _FakeTransport:
    """Socket stand-in that records which thread wrote each batch."""

    def __init__(self, drain_delay: float = 0.0) -> None:
        self.drain_delay = drain_delay
        self.writes: list[bytes] = []
        self.write_threads: list[int] = []

    def writelines(self, lines: list[bytes]) -> None:
        self.writes.extend(lines)
        self.write_threads.append(threading.get_ident())

    def write(self, data: bytes) -> None:
        self.writelines([data])

    async def drain(self) -> None:
        if self.drain_delay:
            await asyncio.sleep(self.drain_delay)

    def close(self) -> None:
        pass

    def at_eof(self) -> bool:
        return False

    def __bool__(self) -> bool:
        return True


async def _connected_client(drain_delay: float = 0.0) -> NATSClient:
    """A nats-py client in CONNECTED state, owned by the running loop."""
    nc = NATSClient()
    nc._transport = _FakeTransport(drain_delay)  # type: ignore[assignment]
    nc._status = NATSClient.CONNECTED
    nc._max_payload = 1024 * 1024
    nc._max_pending_size = 1024
    nc._flush_timeout = None  # nats-py's default
    nc._flush_queue = asyncio.Queue(maxsize=1024)
    nc._flusher_task = asyncio.get_running_loop().create_task(nc._flusher())

    async def _error_cb(_e: Exception) -> None:
        return None

    nc._error_cb = _error_cb  # type: ignore[assignment]
    return nc


async def _stop_client(nc: NATSClient) -> None:
    nc._status = NATSClient.CLOSED
    if nc._flusher_task is not None:
        nc._flusher_task.cancel()
        try:
            await nc._flusher_task
        except asyncio.CancelledError:
            pass


def _transport(nc: NATSClient) -> _FakeTransport:
    return nc._transport  # type: ignore[return-value]


def _published_events(nc: NATSClient) -> list[dict[str, Any]]:
    """Decode PUB frames written to the fake socket."""
    data = b"".join(_transport(nc).writes)
    events = []
    for chunk in data.split(b"PUB ")[1:]:
        _header, _, rest = chunk.partition(b"\r\n")
        body, _, _ = rest.rpartition(b"\r\n")
        events.append(json.loads(body))
    return events


async def _wait_for(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _run_in_thread(fn: Any, timeout: float = 2.0) -> float:
    """Run ``fn`` on a fresh thread; return its duration or fail if it hangs."""
    errors: list[BaseException] = []
    started = time.monotonic()

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "caller thread blocked on a NATS publish"
    assert not errors, errors
    return time.monotonic() - started


def _event(account_id: str = "acct-1", **payload: Any) -> dict[str, Any]:
    return build_account_event(
        account_id=account_id,
        topic="gateway_activity",
        event_type="model_gateway_call",
        payload={"blob": "x" * _FORCE_FLUSH_PAYLOAD, **payload},
    )


@pytest.fixture
def publisher(monkeypatch: pytest.MonkeyPatch) -> RealtimePublisher:
    instance = RealtimePublisher(publish_timeout=0.5)
    monkeypatch.setattr(
        account_realtime, "publish_realtime", lambda s, p: instance.submit(s, p)
    )
    return instance


@pytest.mark.asyncio
async def test_worker_thread_emit_returns_and_publishes_on_owner_loop(
    publisher: RealtimePublisher,
) -> None:
    """Regression: an AnyIO worker thread used to publish on its own loop.

    The forced flush then awaited a future the owner loop's flusher resolved
    from another thread, without a timeout, so the worker (and the database
    connection its request held) never returned.
    """
    nc = await _connected_client()
    owner_thread = threading.get_ident()
    publisher.start(lambda: nc)
    try:
        duration = await asyncio.to_thread(
            _run_in_thread, lambda: account_realtime.emit_account_event(_event())
        )
        assert duration < 0.5
        await _wait_for(lambda: publisher.stats.published == 1)
        assert _transport(nc).write_threads
        assert set(_transport(nc).write_threads) == {owner_thread}
        assert _published_events(nc)[0]["topic"] == "gateway_activity"
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_foreign_running_loop_emit_is_routed_to_owner_loop(
    publisher: RealtimePublisher,
) -> None:
    """A caller inside another thread's running loop must not use the client."""
    nc = await _connected_client()
    owner_thread = threading.get_ident()
    publisher.start(lambda: nc)

    async def foreign() -> None:
        account_realtime.emit_account_event(_event())

    try:
        await asyncio.to_thread(_run_in_thread, lambda: asyncio.run(foreign()))
        await _wait_for(lambda: publisher.stats.published == 1)
        assert set(_transport(nc).write_threads) == {owner_thread}
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_owner_loop_emit_publishes_without_blocking(
    publisher: RealtimePublisher,
) -> None:
    nc = await _connected_client()
    publisher.start(lambda: nc)
    try:
        account_realtime.emit_account_event(_event())
        await _wait_for(lambda: publisher.stats.published == 1)
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_payload_is_snapshotted_at_emit_time(
    publisher: RealtimePublisher,
) -> None:
    """Mutating the event after emit must not change what is published."""
    nc = await _connected_client()
    publisher.start(lambda: nc)
    try:
        event = _event(status="running")
        account_realtime.emit_account_event(event)
        event["payload"]["status"] = "mutated"
        await _wait_for(lambda: publisher.stats.published == 1)
        assert _published_events(nc)[0]["payload"]["status"] == "running"
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_oversized_payload_is_truncated_before_queueing(
    publisher: RealtimePublisher,
) -> None:
    nc = await _connected_client()
    publisher.start(lambda: nc)
    try:
        account_realtime.emit_account_event(_event(request={"input": "x" * 1_100_000}))
        await _wait_for(lambda: publisher.stats.published == 1)
        published = _published_events(nc)[0]
        assert "request" not in published["payload"]
    finally:
        await publisher.stop()
        await _stop_client(nc)


def test_unserializable_payload_does_not_raise(publisher: RealtimePublisher) -> None:
    account_realtime.emit_account_event(_event(bad=object()))
    assert publisher.stats.submitted == 0


def test_no_owner_loop_drops_with_count(publisher: RealtimePublisher) -> None:
    assert publisher.submit("account-updates.a", b"{}") is False
    assert publisher.stats.dropped == {"no_owner_loop": 1}


@pytest.mark.asyncio
async def test_saturated_backlog_is_bounded_and_publish_waits_time_out() -> None:
    """A stuck client bounds both the backlog and each owner-loop wait."""

    class _StuckClient:
        is_closed = False
        is_connected = True

        async def publish(self, subject: str, payload: bytes) -> None:
            await asyncio.Event().wait()

    publisher = RealtimePublisher(
        max_pending_messages=3, max_pending_bytes=1_000, publish_timeout=0.05
    )
    publisher.start(_StuckClient)
    try:
        results = await asyncio.to_thread(
            lambda: [publisher.submit("s", b"x" * 10) for _ in range(5)]
        )
        assert results == [True, True, True, False, False]
        assert publisher.stats.dropped["backlog_full"] == 2
        assert publisher.submit("s", b"x" * 2_000) is False

        await _wait_for(lambda: publisher.stats.timed_out == 3)
        assert publisher.pending == (0, 0)
        assert publisher.submit("s", b"x") is True
    finally:
        await publisher.stop(timeout=0.01)
    assert not publisher.running
    assert publisher.pending == (0, 0)


@pytest.mark.asyncio
async def test_publish_errors_are_logged_not_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingClient:
        is_closed = False

        async def publish(self, subject: str, payload: bytes) -> None:
            raise RuntimeError("boom")

    publisher = RealtimePublisher()
    publisher.start(_FailingClient)
    try:
        assert publisher.submit("s", b"x")
        await _wait_for(lambda: publisher.stats.failed == 1)
        assert "failed: RuntimeError" in caplog.text
        assert publisher.running
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_disconnected_client_is_counted_and_reconnect_is_picked_up() -> None:
    current: dict[str, Optional[NATSClient]] = {"nc": None}
    publisher = RealtimePublisher()
    publisher.start(lambda: current["nc"])
    nc: Optional[NATSClient] = None
    try:
        assert publisher.submit("s", b"{}")
        await _wait_for(lambda: publisher.stats.dropped.get("not_connected") == 1)

        nc = await _connected_client()
        current["nc"] = nc
        assert publisher.submit("s", b"{}")
        await _wait_for(lambda: publisher.stats.published == 1)
    finally:
        await publisher.stop()
        if nc is not None:
            await _stop_client(nc)


@pytest.mark.asyncio
async def test_stop_flushes_queue_then_refuses_new_messages() -> None:
    nc = await _connected_client(drain_delay=0.01)
    publisher = RealtimePublisher()
    publisher.start(lambda: nc)
    try:
        for _ in range(5):
            assert publisher.submit("s", b"{}")
        await publisher.stop()
        assert publisher.stats.published == 5
        assert publisher.submit("s", b"{}") is False
        assert publisher.stats.dropped == {"no_owner_loop": 1}
    finally:
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_closed_owner_loop_refuses_cross_thread_submit() -> None:
    publisher = RealtimePublisher()
    ready = threading.Event()

    def owner() -> None:
        async def main() -> None:
            publisher.start(lambda: None)
            ready.set()

        loop = asyncio.new_event_loop()
        loop.run_until_complete(main())
        loop.close()

    thread = threading.Thread(target=owner)
    thread.start()
    thread.join(2)
    assert ready.is_set()
    assert publisher.submit("s", b"{}") is False
    assert publisher.stats.dropped == {"no_owner_loop": 1}


@pytest.mark.asyncio
async def test_shared_event_bus_connect_and_close_manage_the_publisher() -> None:
    """Every process that connects the shared bus gets an owner-loop publisher."""
    from preloop.sync.services import event_bus

    with patch.object(event_bus.event_bus_service, "_connect"):
        # Workers and the scheduler call connect directly, without FastAPI's
        # connect_nats wrapper.
        await event_bus.event_bus_service.connect()
        assert event_bus.realtime_publisher.running
        await event_bus.event_bus_service.close()
    assert not event_bus.realtime_publisher.running

    other = event_bus.EventBus()
    with patch.object(other, "_connect"):
        await other.connect()
    assert not event_bus.realtime_publisher.running


@pytest.mark.asyncio
async def test_start_cannot_move_a_live_publisher_to_another_loop() -> None:
    publisher = RealtimePublisher()
    publisher.start(lambda: None)

    async def foreign() -> None:
        with pytest.raises(RuntimeError, match="owner loop"):
            publisher.start(lambda: None)

    try:
        await asyncio.to_thread(lambda: asyncio.run(foreign()))
        assert publisher.running
    finally:
        await publisher.stop()


@pytest.mark.asyncio
async def test_delayed_handoff_from_previous_lifecycle_cannot_publish_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = RealtimePublisher()
    nc = await _connected_client()
    publisher.start(lambda: nc)
    callbacks: list[tuple[Any, tuple[Any, ...]]] = []
    loop = asyncio.get_running_loop()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                loop,
                "call_soon_threadsafe",
                lambda callback, *args: callbacks.append((callback, args)),
            )
            assert publisher.submit("s", b'{"old": true}')
        await publisher.stop(timeout=0)
        publisher.start(lambda: nc)
        assert publisher.submit("s", b'{"new": true}')
        for callback, args in callbacks:
            callback(*args)
        await publisher.stop()
        assert _published_events(nc) == [{"new": True}]
        assert publisher.pending == (0, 0)
        assert publisher.stats.dropped["shutdown"] == 1
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_new_loop_takes_over_after_owner_loop_closed() -> None:
    """A closed owner (per-run asyncio.run, test loops) must not wedge start."""
    publisher = RealtimePublisher()
    stuck = asyncio.Event()

    def owner() -> None:
        async def main() -> None:
            publisher.start(lambda: None)
            # Leave a message accepted but unpublished when the loop dies.
            publisher._loop.call_soon_threadsafe = (  # type: ignore[union-attr]
                lambda *a: stuck.set()
            )
            assert publisher.submit("s", b"{}")

        loop = asyncio.new_event_loop()
        loop.run_until_complete(main())
        loop.close()

    await asyncio.to_thread(owner)
    assert not publisher.running
    assert publisher.pending == (1, 2)

    nc = await _connected_client()
    publisher.start(lambda: nc)
    try:
        assert publisher.running
        assert publisher.pending == (0, 0)
        assert publisher.stats.dropped["owner_loop_closed"] == 1
        assert publisher.submit("s", b'{"after": true}')
        await _wait_for(lambda: publisher.stats.published == 1)
        assert _published_events(nc) == [{"after": True}]
    finally:
        await publisher.stop()
        await _stop_client(nc)


@pytest.mark.asyncio
async def test_shared_bus_connect_from_foreign_loop_keeps_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from preloop.sync.services import event_bus

    with patch.object(event_bus.event_bus_service, "_connect") as connect:
        await event_bus.connect_nats()
        owner_task = event_bus.realtime_publisher._task
        try:
            await asyncio.to_thread(
                lambda: asyncio.run(event_bus.event_bus_service.connect())
            )
            assert event_bus.realtime_publisher._task is owner_task
            assert event_bus.realtime_publisher.running
            # Reject before _connect can replace a disconnected client with
            # one created on the foreign loop.
            assert connect.await_count == 1
            assert "owned by another event loop" in caplog.text
        finally:
            await event_bus.event_bus_service.close()
