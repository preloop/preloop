"""User-requested tracker sync bypasses queued polls without more concurrency."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from preloop.sync import tasks
from preloop.sync.services.nats_worker import PreloopSyncNatsWorker


class _QueueSubscription:
    def __init__(self, subject: str) -> None:
        self.subject = subject
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.fetched = asyncio.Event()

    async def fetch(self, batch: int = 1, timeout: int = 60) -> list[Any]:
        assert batch == 1
        message = await self.messages.get()
        self.fetched.set()
        return [message]


def _message(name: str, tracker: str = "tracker-1") -> Any:
    return SimpleNamespace(name=name, tracker=tracker, nak=AsyncMock())


@pytest.mark.asyncio
async def test_manual_sync_bypasses_backlog_without_starvation_or_parallel_scans() -> (
    None
):
    """One active poll finishes; manual and scheduled backlogs then share the slot."""
    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222", "polling", tasks_allowlist=["poll_tracker"]
    )
    assert worker.handler_concurrency() == 1
    worker._run_slots = asyncio.Semaphore(worker.handler_concurrency())
    scheduled = _QueueSubscription("preloop.sync.tasks.poll_tracker")
    manual = _QueueSubscription("preloop.sync.tasks.poll_tracker_on_demand")
    for number in range(3):
        scheduled.messages.put_nowait(_message(f"scheduled-{number}"))
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    finished = asyncio.Event()
    order: list[str] = []
    active = 0
    peak = 0

    async def handler(message: Any) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(message.name)
        if message.name == "scheduled-0":
            active_started.set()
            await release_active.wait()
        await asyncio.sleep(0)
        active -= 1
        if len(order) == 6:
            finished.set()

    pulls = [
        asyncio.create_task(worker._process_pull_messages(sub, handler))
        for sub in (scheduled, manual)
    ]
    try:
        await asyncio.wait_for(active_started.wait(), timeout=2)
        # Repeated requests for the same tracker must still stay serial.
        for number in range(3):
            manual.messages.put_nowait(_message(f"manual-{number}"))
        await asyncio.wait_for(manual.fetched.wait(), timeout=2)
        assert order == ["scheduled-0"]
        release_active.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        assert order == [
            "scheduled-0",
            "manual-0",
            "scheduled-1",
            "manual-1",
            "scheduled-2",
            "manual-2",
        ]
        assert peak == 1
    finally:
        worker._accepting = False
        for pull in pulls:
            pull.cancel()
        await asyncio.gather(*pulls, return_exceptions=True)


@pytest.mark.asyncio
async def test_on_demand_task_reuses_existing_scan_with_unchanged_options() -> None:
    result = {"projects": 1}
    with patch.object(tasks, "poll_tracker", AsyncMock(return_value=result)) as poll:
        actual = await tasks.poll_tracker_on_demand("tracker-1", force_update=True)
    assert actual is result
    poll.assert_awaited_once_with("tracker-1", None, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_on_demand_failure_logs_task_id(
    caplog: pytest.LogCaptureFixture, raises: bool
) -> None:
    """Handled scan failures and raised errors retain the queue correlation."""
    poll = AsyncMock(return_value=None)
    if raises:
        poll.side_effect = RuntimeError("Example scan failure")
    with patch.object(tasks, "poll_tracker", poll), caplog.at_level("INFO"):
        if raises:
            with pytest.raises(RuntimeError, match="Example scan failure"):
                await tasks.poll_tracker_on_demand("tracker-1", task_id="example-task")
        else:
            assert (
                await tasks.poll_tracker_on_demand("tracker-1", task_id="example-task")
                is None
            )
    assert (
        "Tracker sync failed: task_id=example-task tracker_id=tracker-1" in caplog.text
    )


@pytest.mark.asyncio
async def test_scheduler_keeps_periodic_polls_in_scheduled_lane() -> None:
    from preloop.sync.services import manager

    with patch.object(
        manager.event_bus_service, "publish_task", AsyncMock()
    ) as publish:
        await manager.poll_tracker("tracker-1")
    publish.assert_awaited_once_with("poll_tracker", "tracker-1")


@pytest.mark.asyncio
async def test_waiting_message_renews_lease_and_stops_after_slot_acquired(
    monkeypatch,
) -> None:
    from preloop.sync.services import nats_worker

    monkeypatch.setattr(nats_worker, "SLOT_WAIT_PROGRESS_INTERVAL_SECONDS", 0.001)
    renewed = asyncio.Event()
    msg = SimpleNamespace(in_progress=AsyncMock(side_effect=renewed.set))
    slots = asyncio.Semaphore(0)
    waiting = asyncio.create_task(nats_worker._acquire_slot_with_progress(msg, slots))
    await asyncio.wait_for(renewed.wait(), timeout=2)
    assert not waiting.done()
    slots.release()
    await asyncio.wait_for(waiting, timeout=2)
    renewals = msg.in_progress.await_count
    await asyncio.sleep(0.01)
    assert msg.in_progress.await_count == renewals
    assert slots.locked()


@pytest.mark.asyncio
async def test_cancelled_slot_wait_stops_renewing_without_consuming_slot(
    monkeypatch,
) -> None:
    from preloop.sync.services import nats_worker

    monkeypatch.setattr(nats_worker, "SLOT_WAIT_PROGRESS_INTERVAL_SECONDS", 0.001)
    renewed = asyncio.Event()
    msg = SimpleNamespace(in_progress=AsyncMock(side_effect=renewed.set))
    slots = asyncio.Semaphore(0)
    waiting = asyncio.create_task(nats_worker._acquire_slot_with_progress(msg, slots))
    await asyncio.wait_for(renewed.wait(), timeout=2)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    renewals = msg.in_progress.await_count
    await asyncio.sleep(0.01)
    assert msg.in_progress.await_count == renewals
    slots.release()
    await asyncio.wait_for(slots.acquire(), timeout=2)
