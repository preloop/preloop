"""Consume runner-owned raw logs without duplicating their storage or broadcast."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.fixture
def execution(db_session: Session) -> models.FlowExecution:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Log cursor test"}
    )
    flow = crud_flow.create(
        db_session,
        account_id=account.id,
        flow_in=FlowCreate(
            name="Runner log flow",
            account_id=account.id,
            agent_type="codex",
            agent_config={},
            prompt_template="Implement issue",
            trigger_event_source="github",
            trigger_event_types=["issue_updated"],
        ),
    )
    return crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )


def test_log_cursor_is_scoped_and_handles_timestamp_ties(
    db_session: Session, execution: models.FlowExecution
) -> None:
    other = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=execution.flow_id)
    )
    timestamp = datetime.now(timezone.utc)
    for index, (target, kind) in enumerate(
        [
            (execution.id, "agent_log_line"),
            (other.id, "agent_log_line"),
            (execution.id, "mcp_call"),
            (execution.id, "agent_log_line"),
            (execution.id, "agent_log_line"),
        ],
        start=1,
    ):
        row = crud_flow_execution_log.append_log(
            db_session, str(target), {"type": kind, "message": f"line-{index}"}
        )
        row.id = UUID(int=index)
        row.timestamp = timestamp
        db_session.flush()
    first = crud_flow_execution_log.get_agent_log_page(
        db_session, execution.id, limit=2
    )
    assert [row.message for row in first] == ["line-1", "line-4"]
    second = crud_flow_execution_log.get_agent_log_page(
        db_session,
        execution.id,
        after=(first[-1].timestamp, first[-1].id),
        limit=2,
    )
    assert [row.message for row in second] == ["line-5"]
    assert (
        crud_flow_execution_log.get_agent_log_page(
            db_session,
            execution.id,
            after=(second[-1].timestamp, second[-1].id),
            limit=2,
        )
        == []
    )


def orchestrator() -> FlowExecutionOrchestrator:
    instance = FlowExecutionOrchestrator(MagicMock(), uuid4(), {}, AsyncMock())
    instance.execution_log = SimpleNamespace(id=uuid4())
    instance._publish_update = AsyncMock()
    instance._persist_live_metrics = AsyncMock()
    instance._note_opened_pr = MagicMock()
    instance._note_agent_session = MagicMock()
    instance._note_verification_evidence = MagicMock()
    return instance


@pytest.mark.asyncio
async def test_runner_log_pages_parse_markers_and_metrics_once() -> None:
    instance = orchestrator()
    lines = [
        "PRELOOP_AGENT_EXEC_START",
        "tokens used",
        "1,234",
        "Calling tool: github/get_issue",
        'PRELOOP_PR_OPENED {"number":7}',
        'PRELOOP_AGENT_SESSION {"agent_type":"codex","session_id":"native-session"}',
        'PRELOOP_VERIFICATION {"status":"passed"}',
        "FLOW_EXECUTION_SUCCESS",
    ]
    rows = [
        SimpleNamespace(
            id=UUID(int=i + 1), timestamp=datetime.now(timezone.utc), message=line
        )
        for i, line in enumerate(lines)
    ]
    cursors = []

    def page(
        _db: Session,
        execution_id: UUID,
        *,
        after: tuple[datetime, UUID] | None,
        limit: int,
    ) -> list[SimpleNamespace]:
        assert execution_id == instance.execution_log.id
        cursors.append(after)
        return [
            row for row in rows if after is None or (row.timestamp, row.id) > after
        ][:limit]

    with (
        patch("preloop.services.flow_orchestrator.RUNNER_LOG_PAGE_SIZE", 2),
        patch("preloop.services.flow_orchestrator.RUNNER_LOG_SUMMARY_LINES", 3),
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution_log.get_agent_log_page",
            side_effect=page,
        ),
    ):
        # Process one bounded page per live poll, including a split token pair.
        for _ in range(6):
            await instance._consume_runner_log_page("runner:queued:auto:session")
    assert instance.total_tokens == 1234
    assert instance.tool_calls_count == 1
    assert instance._agent_exec_started
    assert instance._success_sentinel_seen.is_set()
    instance._note_opened_pr.assert_called_once()
    instance._note_agent_session.assert_called_once()
    instance._note_verification_evidence.assert_called_once()
    assert instance.execution_logger.get_agent_output_lines() == lines[-3:]
    assert cursors[1] == (rows[1].timestamp, rows[1].id)
    assert all(
        call.args[0] != "agent_log_line"
        for call in instance._publish_update.await_args_list
    )
    assert [call.args[0] for call in instance._publish_update.await_args_list].count(
        "mcp_call"
    ) == 1


@pytest.mark.asyncio
async def test_terminal_monitor_drains_runner_logs_before_result() -> None:
    instance = orchestrator()
    instance.flow = SimpleNamespace(
        agent_type="codex", agent_config={}, timeout_seconds=60
    )
    instance._listen_for_commands = AsyncMock()
    instance._cleanup_monitoring = AsyncMock()
    instance._capture_result_artifact = AsyncMock(return_value={"status": "success"})
    instance._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
    executor = AsyncMock()
    executor.streams_logs_externally = True
    executor.get_status.return_value = AgentStatus.SUCCEEDED
    executor.get_result.return_value = AgentExecutionResult(
        status=AgentStatus.SUCCEEDED,
        session_reference="runner:test",
        exit_code=0,
    )
    drains = AsyncMock(side_effect=[False, False, True])
    with patch.object(instance, "_consume_runner_log_page", drains):
        result = await instance._monitor_agent_execution("runner:test", executor)
    assert drains.await_count == 3
    executor.get_result.assert_awaited_once()
    assert result["status"] == "SUCCEEDED"
