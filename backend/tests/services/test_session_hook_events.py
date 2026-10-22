"""Tool-end, wait-for-input and turn-state rows from harness hooks.

PostToolUse, PostToolUseFailure and Notification add two content-light
activity kinds, and every hook row moves ``runtime_session.turn_state`` so
lists can say Working, Needs approval or Your turn without guessing from
recency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_approval_request
from preloop.services import session_hook_events


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_hook_events, "emit_account_event", events.append)
    return events


@pytest.fixture
def runtime(db_session: Session, test_user: models.User) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=f"laptop:{uuid.uuid4()}",
        runtime_principal_type="claude_code",
        runtime_principal_id="laptop",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    return session


def _event(
    db: Session, runtime: models.RuntimeSession, name: str, **details: Any
) -> Any:
    return session_hook_events.record_session_event(
        db,
        session=runtime,
        api_key_id=None,
        actor_user_id=None,
        source="claude_code",
        hook_event_name=name,
        details=details,
    )


def _prompt(db: Session, runtime: models.RuntimeSession) -> Any:
    return session_hook_events.record_prompt(
        db,
        account_id=runtime.account_id,
        runtime_session_id=runtime.id,
        api_key_id=None,
        actor_user_id=None,
        source="claude_code",
        hook_event_name="UserPromptSubmit",
    )


def _approval(
    db: Session, runtime: models.RuntimeSession, status: str = "pending"
) -> models.ApprovalRequest:
    workflow = models.ApprovalWorkflow(
        account_id=runtime.account_id,
        name=f"Synthetic {uuid.uuid4()}",
        approval_type="manual",
        approver_user_ids=[],
        approvals_required=1,
    )
    db.add(workflow)
    db.flush()
    tool = models.ToolConfiguration(
        account_id=runtime.account_id,
        tool_name=f"synthetic_{uuid.uuid4().hex[:8]}",
        approval_workflow_id=workflow.id,
    )
    db.add(tool)
    db.flush()
    request = models.ApprovalRequest(
        account_id=runtime.account_id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args={},
        status=status,
        runtime_session_id=runtime.id,
    )
    db.add(request)
    db.flush()
    return request


def test_post_tool_use_stores_tool_end_and_broadcasts(
    db_session: Session, runtime: models.RuntimeSession, emitted: list
) -> None:
    secret = "Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz0123456789"
    first = _event(
        db_session,
        runtime,
        "PostToolUse",
        tool_use_id="toolu_1",
        tool_name="Bash",
        exit_code=0,
        duration_ms=1250,
        output_preview=secret + "\n" + "x" * 1000,
    )
    second = _event(
        db_session,
        runtime,
        "PostToolUseFailure",
        tool_use_id="toolu_2",
        tool_name="Read",
    )

    assert first is not None and second is not None
    assert first.activity_type == "tool_end"
    assert first.summary is None
    assert first.metadata_["tool_use_id"] == "toolu_1"
    assert first.metadata_["exit_code"] == 0
    assert first.metadata_["duration_ms"] == 1250
    preview = first.metadata_["output_preview"]
    assert len(preview) <= session_hook_events.TOOL_OUTPUT_PREVIEW_CHARS
    assert "sk-live-abcdefghijklmnopqrstuvwxyz0123456789" not in preview
    assert second.metadata_["is_error"] is True

    assert [e["type"] for e in emitted] == [
        "runtime_session_tool_ended",
        "runtime_session_tool_ended",
    ]
    event = emitted[0]
    assert event["topic"] == "runtime_sessions"
    assert event["account_id"] == str(runtime.account_id)
    assert event["runtime_session_id"] == str(runtime.id)
    assert event["payload"]["tool_name"] == "Bash"
    assert event["payload"]["turn_state"] == "working"
    assert "output_preview" not in event["payload"]


def test_turn_state_transitions(
    db_session: Session, runtime: models.RuntimeSession, emitted: list
) -> None:
    assert runtime.turn_state is None
    _prompt(db_session, runtime)
    db_session.refresh(runtime)
    assert runtime.turn_state == "working"
    assert runtime.turn_started_at is not None
    assert runtime.turn_ended_at is None
    turn_started = runtime.turn_started_at

    approval = _approval(db_session, runtime)
    db_session.refresh(runtime)
    assert runtime.turn_state == "waiting_for_approval"

    # A tool that ends while the approval is pending does not clear it.
    _event(db_session, runtime, "PostToolUse", tool_use_id="t1", tool_name="Read")
    db_session.refresh(runtime)
    assert runtime.turn_state == "waiting_for_approval"

    approval.status = "approved"
    db_session.flush()
    db_session.refresh(runtime)
    assert runtime.turn_state == "working"

    # Expiry in bulk also releases the wait.
    stale = _approval(db_session, runtime)
    stale.expires_at = datetime(2000, 1, 1)
    db_session.flush()
    db_session.refresh(runtime)
    assert runtime.turn_state == "waiting_for_approval"
    crud_approval_request.expire_stale_pending(
        db_session, account_id=str(runtime.account_id), commit=False
    )
    db_session.flush()
    db_session.refresh(runtime)
    assert runtime.turn_state == "working"

    _event(db_session, runtime, "Stop", interrupted=True)
    db_session.refresh(runtime)
    assert runtime.turn_state == "your_turn"
    # The turn keeps its start across requests and tools; the end is stamped.
    assert runtime.turn_started_at == turn_started
    assert runtime.turn_ended_at is not None
    assert runtime.turn_ended_at >= turn_started
    assert runtime.turn_interrupted is True
    assert emitted[-1]["type"] == "runtime_session_turn_ended"
    assert emitted[-1]["payload"]["interrupted"] is True
    assert emitted[-1]["payload"]["turn_state"] == "your_turn"

    _event(db_session, runtime, "SessionEnd")
    db_session.refresh(runtime)
    assert runtime.turn_state == "ended"

    # Nothing re-opens an ended session.
    _approval(db_session, runtime)
    db_session.refresh(runtime)
    assert runtime.turn_state == "ended"


def test_notification_idle_prompt_sets_waiting_for_input(
    db_session: Session, runtime: models.RuntimeSession, emitted: list
) -> None:
    _prompt(db_session, runtime)
    ignored = _event(
        db_session, runtime, "Notification", notification_type="permission_prompt"
    )
    assert ignored is None
    db_session.refresh(runtime)
    assert runtime.turn_state == "working"

    row = _event(db_session, runtime, "Notification", notification_type="idle_prompt")
    assert row is not None
    assert row.activity_type == "waiting_for_input"
    db_session.refresh(runtime)
    assert runtime.turn_state == "your_turn"
    assert emitted[-1]["type"] == "runtime_session_waiting_for_input"
    assert emitted[-1]["runtime_session_id"] == str(runtime.id)
