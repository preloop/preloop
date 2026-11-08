"""Completion events follow the persisted result, and lost events are loud."""

import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints.flows import _raise_result_missing
from preloop.services.event_webhooks import outbox


@pytest.mark.asyncio
async def test_resumed_run_persists_result_before_completion_is_queued() -> None:
    """The terminal write carrying the result lands before the event is queued."""
    from preloop.services.flow_execution_runner import resume_existing_execution

    order: list[tuple[str, Any]] = []
    orchestrator = MagicMock()
    orchestrator.execution_log = SimpleNamespace(id=uuid.uuid4(), result=None)
    orchestrator.flow = SimpleNamespace(agent_type="codex", agent_config={})
    orchestrator.agent_type = "codex"
    orchestrator._get_flow_details = MagicMock()
    orchestrator._monitor_agent_execution = AsyncMock(
        return_value={"status": "SUCCEEDED", "result": {"review": "done"}}
    )

    async def update(**kwargs: Any) -> None:
        order.append(("persist", kwargs.get("result")))

    async def notify(**kwargs: Any) -> None:
        order.append(("notify", kwargs.get("status")))

    orchestrator._update_execution_log = update
    orchestrator._notify_terminal = notify
    orchestrator._sync_runtime_session = MagicMock()
    orchestrator._revoke_execution_runtime_tokens = MagicMock()
    executor = MagicMock()
    executor.aclose = None
    executor.cleanup = None
    with patch("preloop.agents.create_agent_executor", return_value=executor):
        await resume_existing_execution(orchestrator, "agent-session-ref")
    assert [step for step, _ in order] == ["persist", "notify"]
    assert order[0][1]["review"] == "done"


def _orchestrator(db: Any) -> Any:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db
    orchestrator.execution_log = SimpleNamespace(id=uuid.uuid4(), failure_category=None)
    orchestrator.flow = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4())
    return orchestrator


def test_completion_enqueue_failure_logs_error_and_keeps_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    db.commit.side_effect = RuntimeError("database went away")
    orchestrator = _orchestrator(db)
    with (
        patch("preloop.services.event_webhooks.emitters.emit_flow_execution_finished"),
        patch(
            "preloop.services.event_webhooks.emitters."
            "emit_cra_reportable_vulnerabilities"
        ),
        caplog.at_level(logging.ERROR, logger="preloop.services.flow_orchestrator"),
    ):
        orchestrator._emit_execution_finished_webhook("SUCCEEDED", None)
    records = [r for r in caplog.records if "flow.execution.finished" in r.message]
    assert records and records[0].levelno == logging.ERROR


def test_outbox_enqueue_failure_is_an_error_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = MagicMock()
    subject = uuid.uuid4()
    with (
        patch.object(outbox, "select_endpoints", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.ERROR, logger=outbox.__name__),
    ):
        result = outbox.enqueue_event(
            db,
            account_id=uuid.uuid4(),
            event_type="flow.execution.finished",
            data={},
            subject_id=subject,
        )
    assert result.failed is True
    assert not result.delivery_ids
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert str(subject) in record.getMessage()
    assert record.webhook_event_type == "flow.execution.finished"


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "PARKED", None])
def test_unfinished_execution_without_result_is_pending(status: Any) -> None:
    with pytest.raises(HTTPException) as raised:
        _raise_result_missing(SimpleNamespace(status=status))
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "result_pending"
    assert raised.value.detail["status"] == status


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"])
def test_finished_execution_without_result_keeps_404(status: str) -> None:
    with pytest.raises(HTTPException) as raised:
        _raise_result_missing(SimpleNamespace(status=status))
    assert raised.value.status_code == 404
    assert raised.value.detail == "Flow execution did not report a result artifact"


def test_human_result_route_withholds_unfinished_checkpoint() -> None:
    """The account route also answers 409 for an unfinished execution."""
    from preloop.api.endpoints import flows

    execution = SimpleNamespace(
        id=uuid.uuid4(), status="PARKED", result={"review": "checkpoint"}
    )
    account = SimpleNamespace(account_id=uuid.uuid4())
    with (
        patch.object(flows, "crud_flow_execution") as crud,
        pytest.raises(HTTPException) as raised,
    ):
        crud.get_without_workspace.return_value = execution
        flows.get_flow_execution_result.__wrapped__(
            db=MagicMock(), execution_id=execution.id, current_user=account
        )
    assert raised.value.status_code == 409
