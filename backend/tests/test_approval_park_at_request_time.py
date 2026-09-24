"""Long approval windows park when the request is created.

A gated call used to poll in process for ``approval_park_after_seconds``
before writing the park. Harnesses that close the tool transport inside
that wait then failed the run with a pending approval still attached.
Windows over the threshold now park before any poll. Windows at or under
it keep the in-process wait.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services import approval_park

UTC = timezone.utc


def _workflow(*, timeout_seconds: int = 300, async_enabled: bool = False):
    workflow = MagicMock()
    workflow.id = uuid4()
    workflow.approval_type = "manual"
    workflow.name = "test-workflow"
    workflow.timeout_seconds = timeout_seconds
    workflow.escalation_user_ids = None
    workflow.escalation_team_ids = None
    workflow.async_approval_enabled = async_enabled
    workflow.approver_user_ids = None
    return workflow


def _request(status: str = "pending"):
    request = MagicMock()
    request.id = uuid4()
    request.status = status
    request.approver_comment = None
    request.structured_answer = None
    request.resolved_at = None
    request.responses = []
    request.expires_at = datetime.now(UTC) + timedelta(days=3)
    return request


@asynccontextmanager
async def _session():
    session = MagicMock()
    executed = MagicMock()
    executed.scalars.return_value = []
    session.execute = AsyncMock(return_value=executed)
    session.run_sync = AsyncMock(return_value=set())
    session.commit = AsyncMock()
    session.close = AsyncMock()
    session.add = MagicMock()
    try:
        yield session
    finally:
        pass


class _Clock:
    """Counts poll sleeps. A park-at-request-time path must not sleep."""

    def __init__(self) -> None:
        self.sleeps = 0

    def sleep(self):
        clock = self

        async def _sleep(_seconds: float) -> None:
            clock.sleeps += 1

        return _sleep


def _patches(workflow, pending, *, execution_id: str, on_poll=None):
    config = MagicMock()
    config.id = uuid4()
    config.approval_workflow_id = workflow.id
    service = AsyncMock()
    service.create_and_notify = AsyncMock(return_value=pending)
    state = {"current": pending}

    async def _get(_db, request_id=None):
        if on_poll is not None:
            on_poll(state)
        return state["current"]

    caller = SimpleNamespace(flow_execution_id=execution_id)
    return (
        patch("preloop.models.db.session.get_async_db_session", new=_session),
        patch(
            "preloop.models.crud.tool_configuration.get_tool_config_by_name_and_source_async",
            new_callable=AsyncMock,
            return_value=config,
        ),
        patch(
            "preloop.models.crud.approval_workflow.get_approval_workflow_async",
            new_callable=AsyncMock,
            return_value=workflow,
        ),
        patch(
            "preloop.services.approval_service.ApprovalService",
            return_value=service,
        ),
        patch(
            "preloop.models.crud.approval_request.get_approval_request_async",
            new=AsyncMock(side_effect=_get),
        ),
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=caller,
        ),
    )


@pytest.mark.asyncio
async def test_a_window_over_the_threshold_parks_before_any_poll(monkeypatch):
    """Multi-day ask_user: WAITING_FOR_HUMAN, container stopped, zero polls."""
    from preloop.services.approval_helper import require_approval
    from preloop.services import flow_orchestrator as module

    execution_id = str(uuid4())
    pending = _request()
    clock = _Clock()
    write = MagicMock(return_value=True)
    monkeypatch.setattr("preloop.models.db.session.get_session_factory", MagicMock)
    monkeypatch.setattr(
        "preloop.api.loop_safety.run_db_off_loop",
        AsyncMock(side_effect=lambda fn: fn()),
    )
    monkeypatch.setattr(approval_park, "request_park", write)

    patches = _patches(
        _workflow(), pending, execution_id=execution_id, on_poll=lambda _state: None
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with patch("preloop.services.approval_helper.asyncio.sleep", new=clock.sleep()):
            approved, payload = await require_approval(
                tool_name="ask_user",
                tool_source="builtin",
                account_id=str(uuid4()),
                arguments={"question": "Which option should ship?"},
                requested_timeout_seconds=259200,
            )

    assert approved is False
    parsed = json.loads(payload)
    assert parsed["status"] == "parked_for_human"
    assert "resumes" in parsed["message"]
    assert clock.sleeps == 0
    assert write.call_args.kwargs["execution_id"] == execution_id

    request_id = write.call_args.kwargs["approval_request_id"]
    orchestrator = object.__new__(module.FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = SimpleNamespace(id=uuid.UUID(execution_id))
    orchestrator.execution_logger = MagicMock()
    orchestrator.execution_logger.get_actions_taken.return_value = []
    orchestrator.execution_logger.get_mcp_usage_logs.return_value = []
    orchestrator._publish_update = AsyncMock()
    orchestrator._capture_result_artifact = AsyncMock(return_value=None)
    orchestrator._chain_consumed_seconds = MagicMock(return_value=0)
    monkeypatch.setattr(
        module.crud_flow_execution,
        "get_park_request",
        MagicMock(
            return_value={
                "request_id": request_id,
                "kind": "human",
                "requested_at": datetime.now(UTC),
                "expires_at": pending.expires_at,
                "parked_at": None,
            }
        ),
    )
    monkeypatch.setattr(
        module.crud_flow_execution,
        "get_stop_request",
        MagicMock(return_value=None),
    )
    monkeypatch.setattr(
        module.crud_flow_execution,
        "get",
        MagicMock(return_value=SimpleNamespace(status="RUNNING")),
    )
    executor = SimpleNamespace(stop=AsyncMock())
    result = await orchestrator._park_if_requested(executor, "session-1", 0)
    assert result["status"] == "WAITING_FOR_HUMAN"
    executor.stop.assert_awaited_once_with("session-1")


@pytest.mark.asyncio
async def test_a_window_under_the_threshold_waits_in_process(monkeypatch):
    """Sixty seconds is answered in place. No park, and the wait completes."""
    from preloop.services.approval_helper import require_approval

    execution_id = str(uuid4())
    pending = _request()
    approved_row = _request("approved")
    approved_row.id = pending.id
    approved_row.approver_comment = "ship the first one"
    clock = _Clock()
    write = MagicMock(return_value=True)
    monkeypatch.setattr(approval_park, "request_park", write)

    def _answer(state):
        if clock.sleeps:
            state["current"] = approved_row

    patches = _patches(
        _workflow(timeout_seconds=60),
        pending,
        execution_id=execution_id,
        on_poll=_answer,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        with patch("preloop.services.approval_helper.asyncio.sleep", new=clock.sleep()):
            approved, comment = await require_approval(
                tool_name="ask_user",
                tool_source="builtin",
                account_id=str(uuid4()),
                arguments={"question": "Which option should ship?"},
                requested_timeout_seconds=60,
                return_comment_on_approve=True,
            )

    assert approved is True
    assert comment == "ship the first one"
    assert clock.sleeps >= 1
    write.assert_not_called()


@pytest.mark.asyncio
async def test_answering_a_parked_approval_puts_the_answer_in_the_prompt(
    monkeypatch,
):
    """Resume of the same execution carries the answer in ``_answers_prompt``."""
    parked_id = uuid4()
    request_id = uuid4()
    parked = SimpleNamespace(
        id=parked_id,
        flow_id=uuid4(),
        trigger_event_details={"payload": {"source": "schedule"}},
        cli_session={"session_id": "codex-session"},
        park_request_id=request_id,
        parked_compute_seconds=12,
        start_time=datetime.now(UTC),
        parked_at=datetime.now(UTC),
    )
    request = SimpleNamespace(
        id=request_id,
        status="approved",
        tool_name="ask_user",
        tool_args={"question": "Which option should ship?"},
        answer_text="the first option",
        selected_option=None,
        approver_comment=None,
        responses=[],
        resolved_at=datetime.now(UTC),
        structured_answer=None,
    )
    started = []

    monkeypatch.setattr(approval_park, "_load_request", lambda db, rid: request)
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.list_parked_for_request",
        MagicMock(return_value=[parked]),
    )
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.claim_parked_for_resume",
        MagicMock(return_value=True),
    )
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow.get",
        MagicMock(return_value=SimpleNamespace(id=parked.flow_id)),
    )

    async def _start(db, flow, row, details):
        started.append((row.id, details))
        return uuid4()

    monkeypatch.setattr(approval_park, "_start_resume_execution", _start)
    ids = await approval_park.resume_parked_executions(request_id)
    assert len(ids) == 1
    resumed_from, details = started[0]
    assert resumed_from == parked_id
    prompt = details["_answers_prompt"]
    assert "the first option" in prompt
    assert str(request_id) in prompt


def test_a_failed_execution_cancels_its_pending_approval():
    """Terminal FAILED leaves no pending request, and records why."""
    from preloop.models.crud.approval_request import crud_approval_request

    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.update.return_value = 1

    cancelled = crud_approval_request.cancel_pending_for_execution(
        db, execution_id="exec-1"
    )
    assert cancelled == 1
    values = query.update.call_args.args[0]
    assert values["status"] == "cancelled"
    assert "ended before the approval was answered" in values["approver_comment"]
    assert values["resolved_at"] is not None
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrator_terminal_write_cancels_pending_approvals(monkeypatch):
    from preloop.services import flow_orchestrator as module

    execution_id = uuid4()
    orchestrator = object.__new__(module.FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.execution_log = SimpleNamespace(
        id=execution_id, error_message=None, failure_category=None
    )
    orchestrator._publish_update = AsyncMock()
    monkeypatch.setattr(
        module.crud_flow_execution,
        "update",
        MagicMock(return_value=orchestrator.execution_log),
    )
    cancel = MagicMock(return_value=1)
    monkeypatch.setattr(
        "preloop.models.crud.crud_approval_request.cancel_pending_for_execution",
        cancel,
    )

    await orchestrator._update_execution_log(
        status="FAILED", error_message="agent died"
    )
    assert cancel.call_args.kwargs["execution_id"] == str(execution_id)
    assert "FAILED" in cancel.call_args.kwargs["reason"]

    cancel.reset_mock()
    await orchestrator._update_execution_log(status="SUCCEEDED")
    cancel.assert_not_called()
