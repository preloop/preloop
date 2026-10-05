"""The hosted-agent monitor must not idle inside a database transaction.

Every monitor poll reads ``runtime_session_activity`` for tool-loop detection
and then awaits non-database work: a NATS publish, the agent status call and
the poll sleep. With the read transaction still open the session sat "idle in
transaction" holding AccessShareLock on ``flow_execution`` and
``runtime_session_activity``, and a migration that needs ACCESS EXCLUSIVE on
``flow_execution`` failed every attempt on ``lock_timeout``.
"""

from types import SimpleNamespace
from typing import List
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

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

    # The caller's next step is an agent status call and the poll sleep.
    assert not db_session.in_transaction()
