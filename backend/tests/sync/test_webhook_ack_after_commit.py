"""A webhook message is acked once its executions are committed.

Before this, `process_webhook_event` was acked only when the whole handler
returned. A rolling deploy drains the pod, the drain cancels in-flight
handlers, and the cancel path naks: the message came back after the execution
row had already been committed, and the surviving pod created a second
execution (production, 2026-09-08).
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.sync import tasks
from preloop.sync.services import nats_worker


@pytest.mark.asyncio
async def test_ack_runs_after_the_flow_trigger_stage(monkeypatch: Any) -> None:
    """The ack lands after the trigger stage, not before it and not never."""
    order: list[str] = []
    flow_db = MagicMock()

    def get_session() -> Any:
        order.append("open_flow_db")
        yield flow_db

    def generate(db: Any, request: dict[str, Any]) -> None:
        order.append("embedding")

    def run_owned(operation: Any) -> Any:
        with Session() as owned:
            return operation(owned)

    async def process(event: dict[str, Any]) -> None:
        order.append("flow")

    async def ack() -> None:
        order.append("ack")

    monkeypatch.setattr(tasks, "get_db_session", get_session)
    monkeypatch.setattr(tasks, "_generate_webhook_embeddings", generate)
    monkeypatch.setattr(tasks, "run_db_sync", run_owned)
    monkeypatch.setattr(
        tasks.crud_tracker,
        "get",
        MagicMock(
            return_value=SimpleNamespace(
                id="tracker", tracker_type="github", account_id="account"
            )
        ),
    )
    captured: list[dict[str, Any]] = []

    def service(db: Any) -> Any:
        captured.append({"db": db})
        return SimpleNamespace(process_event=process)

    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService", service
    )

    await tasks.process_webhook_event(
        "tracker",
        "issues",
        {},
        embedding_requests=[{"issue_id": "issue"}],
        _ack=ack,
        delivery_id="delivery-1",
    )

    assert order == ["embedding", "open_flow_db", "flow", "ack"]


@pytest.mark.asyncio
async def test_ack_callable_is_not_persisted_on_the_event(monkeypatch: Any) -> None:
    """``_ack`` must stay out of the event: kwargs are stored on the row."""
    events: list[dict[str, Any]] = []

    async def process(event: dict[str, Any]) -> None:
        events.append(event)

    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([MagicMock()]))
    monkeypatch.setattr(
        tasks.crud_tracker,
        "get",
        MagicMock(
            return_value=SimpleNamespace(
                id="tracker", tracker_type="github", account_id="account"
            )
        ),
    )
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        MagicMock(return_value=SimpleNamespace(process_event=process)),
    )

    await tasks.process_webhook_event(
        "tracker",
        "issues",
        {"issue": {"number": 1}},
        _ack=AsyncMock(),
        delivery_id="delivery-2",
    )

    assert events[0]["delivery_id"] == "delivery-2"
    assert "_ack" not in events[0]


async def build_handler(monkeypatch: Any) -> Any:
    """Return the worker's real message handler closure."""
    worker = nats_worker.PreloopSyncNatsWorker(
        "nats://example:4222",
        "webhook-pool",
        tasks_allowlist=["process_webhook_event"],
    )
    worker.nc = MagicMock(is_connected=True)
    worker.js = MagicMock(
        add_consumer=AsyncMock(),
        pull_subscribe=AsyncMock(return_value=MagicMock(subject="s")),
        consumer_info=AsyncMock(side_effect=RuntimeError("absent")),
    )
    handlers: list[Any] = []

    async def capture(self: Any, sub: Any, handler: Any) -> None:
        handlers.append(handler)

    monkeypatch.setattr(
        nats_worker.PreloopSyncNatsWorker, "_process_pull_messages", capture
    )
    await worker.start_listening()
    assert handlers, "worker did not start a pull loop"
    return handlers[0]


def webhook_message() -> Any:
    return MagicMock(
        subject="preloop.sync.tasks.process_webhook_event",
        ack=AsyncMock(),
        nak=AsyncMock(),
        in_progress=AsyncMock(),
        data=json.dumps(
            {
                "function": "process_webhook_event",
                "args": [],
                "kwargs": {
                    "tracker_id": "tracker",
                    "event_type": "issues",
                    "payload": {},
                    "delivery_id": "delivery-3",
                },
            }
        ).encode(),
    )


@pytest.mark.asyncio
async def test_drain_after_commit_does_not_redeliver(monkeypatch: Any) -> None:
    """Cancellation after the handler acked must not nak the message.

    This is the exact incident sequence: execution committed, pod drained,
    handler cancelled. Before the fix the cancel path naked and the delivery
    was replayed onto the surviving pod.
    """
    handler = await build_handler(monkeypatch)

    async def fake_task(*args: Any, _ack: Any = None, **kwargs: Any) -> None:
        assert _ack is not None, "worker did not inject the ack callable"
        await _ack()
        raise asyncio.CancelledError()

    monkeypatch.setattr(tasks, "process_webhook_event", fake_task)
    msg = webhook_message()

    with pytest.raises(asyncio.CancelledError):
        await handler(msg)

    assert msg.ack.await_count == 1
    msg.nak.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_before_commit_still_redelivers(monkeypatch: Any) -> None:
    """Nothing durable happened yet, so the message must come back."""
    handler = await build_handler(monkeypatch)

    async def fake_task(*args: Any, _ack: Any = None, **kwargs: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(tasks, "process_webhook_event", fake_task)
    msg = webhook_message()

    with pytest.raises(asyncio.CancelledError):
        await handler(msg)

    msg.ack.assert_not_awaited()
    assert msg.nak.await_count == 1


def test_webhook_task_is_registered_for_ack_after_commit() -> None:
    assert "process_webhook_event" in tasks.ACK_AFTER_COMMIT_TASKS
    assert not (tasks.ACK_AFTER_COMMIT_TASKS & tasks.ACK_AFTER_CLAIM_TASKS)
