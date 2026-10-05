"""The hosted-agent monitor must not idle inside a database transaction.

Every monitor poll reads ``flow_execution`` (stop and park requests) and
``runtime_session_activity`` (tool-loop detection) and awaits non-database
work in between: the agent status call, a NATS publish and the poll sleep.
With the read transaction still open the session sat "idle in transaction"
holding AccessShareLock on those tables, and a migration that needs ACCESS
EXCLUSIVE on ``flow_execution`` failed every attempt on ``lock_timeout``.
"""

from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents import AgentStatus
from preloop.models.crud import crud_flow_execution
from preloop.services import flow_orchestrator as orchestrator_module
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


def _orchestrator(db_session: Session) -> FlowExecutionOrchestrator:
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db_session
    orchestrator.execution_log = SimpleNamespace(id=uuid4())
    orchestrator.tool_calls_count = 0
    return orchestrator


@pytest.mark.asyncio
async def test_tool_activity_sync_releases_before_publish(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = _orchestrator(db_session)
    # Force the "new tool calls" branch, which awaits a NATS publish.
    orchestrator.tool_calls_count = -1
    seen: List[bool] = []

    async def publish(*_args, **_kwargs) -> None:
        seen.append(db_session.in_transaction())

    async def persist() -> None:
        seen.append(db_session.in_transaction())

    monkeypatch.setattr(orchestrator, "_publish_update", publish)
    monkeypatch.setattr(orchestrator, "_persist_live_metrics", persist)

    assert await orchestrator._sync_runtime_tool_activity_metrics() is None

    assert seen == [False, False]
    assert not db_session.in_transaction()


@pytest.mark.asyncio
async def test_tool_activity_sync_leaves_no_open_transaction(
    db_session: Session,
) -> None:
    orchestrator = _orchestrator(db_session)

    assert await orchestrator._sync_runtime_tool_activity_metrics() is None

    # The caller's next steps are artifact capture and the poll sleep.
    assert not db_session.in_transaction()


class _StopMonitor(BaseException):
    """Ends the monitor loop after one poll without being caught as Exception."""


@pytest.mark.asyncio
async def test_monitor_poll_holds_no_transaction_across_status_and_sleep(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator = FlowExecutionOrchestrator(
        db=db_session,
        flow_id=uuid4(),
        trigger_event_data={"source": "test", "type": "test", "payload": {}},
        nats_client=AsyncMock(),
    )
    orchestrator.execution_log = SimpleNamespace(id=uuid4())
    orchestrator._agent_exec_started = True
    seen: dict[str, List[bool]] = {"status": [], "sleep": []}

    async def park_check(*_args, **_kwargs):
        # The real check reads flow_execution; keep that read, skip the rest.
        crud_flow_execution.get_stop_request(
            db_session, execution_id=orchestrator.execution_log.id
        )
        return None

    async def get_status(_reference):
        seen["status"].append(db_session.in_transaction())
        return AgentStatus.RUNNING

    async def sleep(_seconds):
        seen["sleep"].append(db_session.in_transaction())
        raise _StopMonitor()

    monkeypatch.setattr(orchestrator, "_park_if_requested", park_check)
    monkeypatch.setattr(
        orchestrator, "_check_no_progress", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        orchestrator,
        "_sync_runtime_tool_activity_metrics",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(orchestrator, "_publish_update", AsyncMock())
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", sleep)
    executor = AsyncMock()
    executor.streams_logs_externally = False
    executor.get_status = AsyncMock(side_effect=get_status)

    with pytest.raises(_StopMonitor):
        await orchestrator._monitor_agent_execution("session-1", executor)

    assert seen == {"status": [False], "sleep": [False]}
