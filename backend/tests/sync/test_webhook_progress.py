"""Long embedding work keeps its lease until the worker has drained."""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.sync import tasks
from preloop.sync.services import nats_worker


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_webhook_progress_survives_provider_wait_and_cancellation_drain(
    monkeypatch: Any,
    cancel: bool,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    ticked = asyncio.Event()
    worker_closed = threading.Event()
    msg = MagicMock(ack=AsyncMock(), nak=AsyncMock())

    async def progress() -> None:
        ticked.set()

    msg.in_progress = AsyncMock(side_effect=progress)
    monkeypatch.setattr(nats_worker, "WEBHOOK_PROGRESS_INTERVAL_SECONDS", 0.005)

    def generate(db: Session, request: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(3)

    def owned(operation: Any) -> Any:
        try:
            with Session() as db:
                return operation(db)
        finally:
            worker_closed.set()

    monkeypatch.setattr(tasks, "_generate_webhook_embeddings", generate)
    monkeypatch.setattr(tasks, "run_db_sync", owned)
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([MagicMock()]))
    monkeypatch.setattr(tasks.crud_tracker, "get", MagicMock(return_value=None))

    async def consume() -> None:
        try:
            async with nats_worker._webhook_progress(msg):
                await tasks.process_webhook_event(
                    "tracker",
                    "Job Hook",
                    {},
                    embedding_requests=[{"issue_id": "issue"}],
                )
            assert worker_closed.is_set()
            await msg.ack()
        except asyncio.CancelledError:
            assert worker_closed.is_set()
            await msg.nak()
            raise

    task = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(ticked.wait(), 1)
        ticked.clear()
        if cancel:
            task.cancel()
        await asyncio.wait_for(ticked.wait(), 1)
        assert not task.done()
        assert not worker_closed.is_set()
        msg.ack.assert_not_awaited()
        msg.nak.assert_not_awaited()
    finally:
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    assert msg.ack.await_count == int(not cancel)
    assert msg.nak.await_count == int(cancel)
    ticks = msg.in_progress.await_count
    await asyncio.sleep(0.02)
    assert msg.in_progress.await_count == ticks, "heartbeat survived completed work"
