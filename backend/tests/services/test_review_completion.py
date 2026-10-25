"""A reviewer run that submitted its review and then stalled (#1683)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from preloop.services import review_completion as rc
from preloop.services.dynamic_fastmcp import (
    TOOL_CALL_STATUS_FAILED,
    TOOL_CALL_STATUS_REFUSED,
    TOOL_CALL_STATUS_SUCCEEDED,
)
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


def _call(
    tool: str = "update_pull_request",
    *,
    status: str = TOOL_CALL_STATUS_SUCCEEDED,
    work_ref: dict[str, Any] | None = None,
    summary: dict[str, int] | None = None,
) -> SimpleNamespace:
    metadata: dict[str, Any] = {"arguments_summary": summary or {}}
    if work_ref is not None:
        metadata["work_ref"] = work_ref
    return SimpleNamespace(tool_name=tool, status=status, metadata_=metadata)


def _review(state: str = "approved") -> SimpleNamespace:
    return _call(
        work_ref={"action": "reviewed", "review_state": state},
        summary={"pull_request": 60, "review_action": 9, "review_body": 900},
    )


EYES_ADDED = _call(summary={"pull_request": 60, "add_reaction": 6})
DESCRIPTION = _call(
    work_ref={"action": "pull_request_updated"},
    summary={"pull_request": 60, "description": 4600},
)
EYES_REMOVED = _call(summary={"pull_request": 60, "remove_reaction": 6})


def test_no_review_means_no_progress() -> None:
    assert (
        rc.summarize_review_progress([EYES_ADDED, DESCRIPTION], result_confirmed=False)
        is None
    )


@pytest.mark.parametrize("status", [TOOL_CALL_STATUS_FAILED, TOOL_CALL_STATUS_REFUSED])
def test_failed_review_call_is_not_a_submitted_review(status: str) -> None:
    failed = _review()
    failed.status = status
    assert rc.summarize_review_progress([failed], result_confirmed=False) is None


def test_a_later_review_supersedes_the_first_and_its_steps() -> None:
    progress = rc.summarize_review_progress(
        [_review("commented"), DESCRIPTION, EYES_REMOVED, _review("approved")],
        result_confirmed=False,
    )
    assert progress is not None
    assert progress.review_state == "approved"
    assert not progress.description_updated
    assert not progress.reaction_removed
    assert progress.remaining == [rc.STEP_DESCRIPTION, rc.STEP_REACTION, rc.STEP_RESULT]


def test_stalled_after_approval_names_every_remaining_step() -> None:
    progress = rc.summarize_review_progress(
        [_call("get_pull_request"), EYES_ADDED, _review()], result_confirmed=False
    )
    assert progress is not None
    assert progress.remaining == [rc.STEP_DESCRIPTION, rc.STEP_REACTION, rc.STEP_RESULT]
    text = rc.describe_incomplete_review(progress, "STOPPED")
    assert text.startswith("Review submitted (approved); run stopped, incomplete: ")
    assert rc.STEP_DESCRIPTION in text and rc.STEP_RESULT in text
    assert len(text) <= rc.COMMIT_STATUS_DESCRIPTION_LIMIT


def test_only_the_missing_result_remains() -> None:
    progress = rc.summarize_review_progress(
        [EYES_ADDED, _review("changes_requested"), DESCRIPTION, EYES_REMOVED],
        result_confirmed=False,
    )
    assert progress is not None
    assert progress.remaining == [rc.STEP_RESULT]
    assert rc.describe_incomplete_review(progress, "FAILED") == (
        "Review submitted (changes requested); run failed, incomplete: "
        "completion result"
    )


def test_description_before_the_review_does_not_count() -> None:
    progress = rc.summarize_review_progress(
        [DESCRIPTION, _review()], result_confirmed=True
    )
    assert progress is not None
    assert progress.remaining == [rc.STEP_DESCRIPTION, rc.STEP_REACTION]


def test_null_description_is_not_an_update() -> None:
    progress = rc.summarize_review_progress(
        [_review(), _call(summary={"description": 4, "remove_reaction": 6})],
        result_confirmed=True,
    )
    assert progress is not None
    assert progress.remaining == [rc.STEP_DESCRIPTION]


def _orchestrator(activities: list[Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    from preloop.models.crud import crud_runtime_session_activity

    seen: dict[str, Any] = {}

    def fake_list(db: Any, *, account_id: Any, flow_execution_id: Any) -> list[Any]:
        seen["args"] = (account_id, flow_execution_id)
        return activities

    monkeypatch.setattr(
        crud_runtime_session_activity,
        "list_tool_calls_for_flow_execution",
        fake_list,
    )
    orch = SimpleNamespace(
        db=object(),
        execution_log=SimpleNamespace(id=uuid.uuid4()),
        flow=SimpleNamespace(account_id=uuid.uuid4()),
        seen=seen,
    )
    return orch


def test_orchestrator_describes_stop_after_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator([EYES_ADDED, _review()], monkeypatch)
    text = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "STOPPED", {"status": "running"}
    )
    assert text is not None
    assert text.startswith("Review submitted (approved); run stopped")
    assert rc.STEP_RESULT in text
    assert orch.seen["args"] == (orch.flow.account_id, orch.execution_log.id)


def test_orchestrator_keeps_generic_text_without_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator([EYES_ADDED], monkeypatch)
    assert (
        FlowExecutionOrchestrator._describe_incomplete_review(orch, "STOPPED", None)
        is None
    )


def test_orchestrator_confirmed_result_is_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator([_review(), DESCRIPTION, EYES_REMOVED], monkeypatch)
    text = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "FAILED", {"status": "success"}
    )
    assert text == "Review submitted (approved); run failed, post-review steps done"
