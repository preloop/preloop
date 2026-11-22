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
        _review_gate_failed=FlowExecutionOrchestrator._review_gate_failed,
    )
    return orch


def test_orchestrator_describes_stop_after_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator([EYES_ADDED, _review()], monkeypatch)
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "STOPPED", {"status": "running"}
    )
    assert described is not None
    state, text = described
    assert state == "failure"
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
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "FAILED", {"status": "success"}
    )
    # A FAILED run may have failed a gate on the review itself: it stays red.
    assert described == (
        "failure",
        "Review submitted (approved); run failed, post-review steps done",
    )


def test_stop_after_every_post_review_step_is_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review that finished all its steps before a stop is not a failure."""
    orch = _orchestrator(
        [EYES_ADDED, _review(), DESCRIPTION, EYES_REMOVED], monkeypatch
    )
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "STOPPED", {"status": "success"}
    )
    assert described == (
        "success",
        "Review submitted (approved); run stopped, post-review steps done",
    )


def test_stop_with_a_step_remaining_stays_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator([_review(), DESCRIPTION], monkeypatch)
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "STOPPED", {"status": "success"}
    )
    assert described is not None
    assert described[0] == "failure"
    assert rc.STEP_REACTION in described[1]


@pytest.mark.asyncio
async def test_settle_commit_status_posts_success_for_completed_stopped_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settlement path uses the same verdict as the main run."""
    orch = _orchestrator([_review(), DESCRIPTION, EYES_REMOVED], monkeypatch)
    orch.flow.name = "Pull Request Reviewer"
    posted: dict[str, Any] = {}

    async def fake_update(*, state: str, description: str) -> None:
        posted.update(state=state, description=description)

    orch._update_commit_status = fake_update
    orch._describe_incomplete_review = (
        lambda status, result: FlowExecutionOrchestrator._describe_incomplete_review(
            orch, status, result
        )
    )
    await FlowExecutionOrchestrator.settle_commit_status(
        orch, "STOPPED", result={"status": "success"}
    )
    assert posted == {
        "state": "success",
        "description": (
            "Review submitted (approved); run stopped, post-review steps done"
        ),
    }


@pytest.mark.parametrize(
    "gate",
    [
        {"review_publication": {"status": "not_published"}},
        {"review_publication": {"status": "unexpected"}},
        {"_review_integrity": {"status": "rejected"}},
    ],
)
def test_stop_with_a_failed_review_gate_stays_failure(
    monkeypatch: pytest.MonkeyPatch, gate: dict[str, Any]
) -> None:
    """A stop that won the status write over a gate's FAILED stays red."""
    orch = _orchestrator([_review(), DESCRIPTION, EYES_REMOVED], monkeypatch)
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch, "STOPPED", {"status": "success", **gate}
    )
    assert described is not None
    assert described[0] == "failure"


@pytest.mark.parametrize("status", ["published", "already_reviewed", "unverified"])
def test_stop_after_an_accepted_publication_is_success(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    orch = _orchestrator([_review(), DESCRIPTION, EYES_REMOVED], monkeypatch)
    described = FlowExecutionOrchestrator._describe_incomplete_review(
        orch,
        "STOPPED",
        {"status": "success", "review_publication": {"status": status}},
    )
    assert described is not None
    assert described[0] == "success"
