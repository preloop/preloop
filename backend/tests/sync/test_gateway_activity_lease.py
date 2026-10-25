"""The hourly gateway activity pass renews its NATS lease while it runs.

A pass over a large backlog can outlast the consumer's 180 second ack wait.
Without renewal JetStream redelivers the message and a second worker starts
the same pass from the oldest row, doubling the rewrite work and WAL.
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.sync import tasks
from preloop.sync.services import nats_worker


async def _handler(monkeypatch: pytest.MonkeyPatch) -> Any:
    worker = nats_worker.PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "maintenance-test-pool",
        tasks_allowlist=["maintain_gateway_activity"],
    )
    worker.nc = MagicMock(is_connected=True)
    worker.js = MagicMock(
        add_consumer=AsyncMock(),
        pull_subscribe=AsyncMock(return_value=MagicMock(subject="maintenance")),
        consumer_info=AsyncMock(side_effect=RuntimeError("absent")),
    )
    handlers: list[Any] = []

    async def capture(self: Any, sub: Any, handler: Any) -> None:
        handlers.append(handler)

    monkeypatch.setattr(
        nats_worker.PreloopSyncNatsWorker, "_process_pull_messages", capture
    )
    monkeypatch.setattr(
        "preloop.services.model_price_catalog.load_catalog", lambda: None
    )
    monkeypatch.setattr(
        "preloop.services.reviewed_model_price_refresh.start_reviewed_price_refresh",
        lambda: None,
    )
    await worker.start_listening()
    assert len(handlers) == 1
    return handlers[0]


@pytest.mark.asyncio
async def test_maintenance_pass_renews_lease_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is renewed during the pass, then acked once, then left alone."""
    handler = await _handler(monkeypatch)
    release = asyncio.Event()
    ticked = asyncio.Event()

    async def maintain() -> dict[str, Any]:
        await release.wait()
        return {"compaction": {"compacted_rows": 1}}

    async def progress() -> None:
        ticked.set()

    msg = MagicMock(
        subject="preloop.sync.tasks.maintain_gateway_activity",
        data=json.dumps({"function": "maintain_gateway_activity"}).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        in_progress=AsyncMock(side_effect=progress),
    )
    monkeypatch.setattr(tasks, "maintain_gateway_activity", maintain)
    monkeypatch.setattr(nats_worker, "WEBHOOK_PROGRESS_INTERVAL_SECONDS", 0.005)
    consuming = asyncio.create_task(handler(msg))
    try:
        await asyncio.wait_for(ticked.wait(), 1)
        assert not consuming.done()
        msg.ack.assert_not_awaited()
    finally:
        release.set()
        await consuming
    msg.ack.assert_awaited_once()
    ticks = msg.in_progress.await_count
    await asyncio.sleep(0.02)
    assert msg.in_progress.await_count == ticks
