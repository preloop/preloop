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
from tests.services.test_repricing_worker_dispatch import _handler


@pytest.mark.asyncio
async def test_maintenance_pass_renews_lease_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is renewed during the pass, then acked once, then left alone."""
    handler = await _handler(monkeypatch, "maintain_gateway_activity")
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
