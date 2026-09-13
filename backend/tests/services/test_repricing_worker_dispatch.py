"""Real NATS dispatch keeps repricing off-loop and renews until work drains."""

import asyncio
import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.sync import tasks
from preloop.sync.services import nats_worker


async def _handler(monkeypatch: pytest.MonkeyPatch) -> Any:
    worker = nats_worker.PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "repricing-test-pool",
        tasks_allowlist=["reprice_gateway_usage_task"],
    )
    worker.nc = MagicMock(is_connected=True)
    worker.js = MagicMock(
        add_consumer=AsyncMock(),
        pull_subscribe=AsyncMock(return_value=MagicMock(subject="repricing")),
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
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_repricing_dispatch_renews_until_sync_work_finishes(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """No early ack; cancellation drains before nak; failures retain delivery."""
    handler = await _handler(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    ticked = asyncio.Event()
    event_loop_thread = threading.get_ident()

    def reprice(**kwargs: Any) -> dict[str, int]:
        assert threading.get_ident() != event_loop_thread
        assert kwargs == {"job_id": "synthetic-job", "account_id": "synthetic-account"}
        entered.set()
        try:
            assert release.wait(3), "test did not release synchronous repricing"
            if outcome == "failure":
                raise RuntimeError("Synthetic repricing failure")
            return {"rows_updated": 1}
        finally:
            finished.set()

    async def progress() -> None:
        ticked.set()

    msg = MagicMock(
        subject="preloop.sync.tasks.reprice_gateway_usage_task",
        data=json.dumps(
            {
                "function": "reprice_gateway_usage_task",
                "kwargs": {
                    "job_id": "synthetic-job",
                    "account_id": "synthetic-account",
                },
            }
        ).encode(),
        ack=AsyncMock(),
        nak=AsyncMock(),
        in_progress=AsyncMock(side_effect=progress),
    )
    monkeypatch.setattr(tasks, "reprice_gateway_usage_task", reprice)
    monkeypatch.setattr(nats_worker, "WEBHOOK_PROGRESS_INTERVAL_SECONDS", 0.005)
    consuming = asyncio.create_task(handler(msg))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(ticked.wait(), 1)
        ticked.clear()
        if outcome == "cancel":
            consuming.cancel()
        await asyncio.wait_for(ticked.wait(), 1)
        assert not consuming.done()
        assert not finished.is_set()
        msg.ack.assert_not_awaited()
        msg.nak.assert_not_awaited()
    finally:
        release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await consuming
        else:
            await consuming
    assert finished.is_set()
    assert msg.ack.await_count == int(outcome == "success")
    assert msg.nak.await_count == int(outcome == "cancel")
    ticks = msg.in_progress.await_count
    await asyncio.sleep(0.02)
    assert msg.in_progress.await_count == ticks
