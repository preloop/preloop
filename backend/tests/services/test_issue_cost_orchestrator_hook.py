"""The orchestrator records the issue cost fact when a run reaches a terminal status."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


def _orchestrator() -> Any:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = object()
    orchestrator.execution_log = SimpleNamespace(
        id=uuid.uuid4(), failure_category=None, trigger_event_details={}
    )
    orchestrator.flow = SimpleNamespace(id=uuid.uuid4(), notifications=None)
    orchestrator._emit_execution_finished_webhook = lambda *args: None
    return orchestrator


@pytest.fixture
def quiet_neighbours(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "preloop.services.issue_lifecycle_runtime.lifecycle_execution_finished",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "preloop.services.security_maintenance_runtime.maintenance_execution_finished",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "preloop.services.flow_child_wait.notify_parent_child_finished", AsyncMock()
    )


@pytest.mark.asyncio
async def test_terminal_status_records_the_issue_cost_fact(
    monkeypatch: Any, quiet_neighbours: None
) -> None:
    calls: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        "preloop.services.issue_cost_rollup.record_execution_finished_safely",
        lambda db, execution_id: calls.append((db, execution_id)),
    )
    orchestrator = _orchestrator()

    await orchestrator._notify_terminal(status="SUCCEEDED")

    assert calls == [(orchestrator.db, orchestrator.execution_log.id)]


@pytest.mark.asyncio
async def test_rollup_failure_does_not_stop_the_terminal_hooks(
    monkeypatch: Any, quiet_neighbours: None
) -> None:
    def explode(db: Any, execution_id: Any) -> None:
        raise RuntimeError("rollup down")

    monkeypatch.setattr(
        "preloop.services.issue_cost_rollup.record_execution_finished_safely",
        explode,
    )
    notify = AsyncMock()
    monkeypatch.setattr(
        "preloop.services.flow_child_wait.notify_parent_child_finished", notify
    )

    await _orchestrator()._notify_terminal(status="FAILED")

    notify.assert_awaited_once()
