"""Task wiring for gateway activity maintenance."""

import inspect
from unittest.mock import MagicMock

import pytest

from preloop.sync import tasks


def test_task_runs_off_the_worker_loop():
    """The NATS worker awaits coroutine tasks; a sync one would block it."""
    assert inspect.iscoroutinefunction(tasks.maintain_gateway_activity)


def test_task_is_dispatchable():
    assert "maintain_gateway_activity" in tasks.DISPATCHABLE_TASKS


@pytest.mark.asyncio
async def test_task_closes_the_session_and_swallows_errors(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(tasks, "get_db_session", lambda: iter([db]))

    def boom(session):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "preloop.services.gateway_activity_maintenance."
        "run_gateway_activity_maintenance",
        boom,
    )
    assert await tasks.maintain_gateway_activity() is None
    db.close.assert_called_once()
