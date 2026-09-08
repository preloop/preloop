"""Terminal-path flow notifications. No database.

The tracker client is stubbed: these tests assert when a comment is posted and
what it contains. The failure comment was removed in 2026-09, so the tests
below also pin that a flow still carrying the old key is ignored rather than
rejected, and that nothing is posted when a run fails.
"""

from __future__ import annotations

from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models.schemas.flow import (
    FlowCreate,
    FlowFailureNotifications,
    FlowNotifications,
    FlowSuccessNotifications,
)
from preloop.services import flow_execution_notifications
from preloop.services.flow_execution_notifications import (
    extract_opened_pr_url,
    extract_trigger_comment_target,
    format_success_comment,
    is_success_status,
    needs_tracker_comment,
    notify_terminal_execution,
    parse_notifications,
)

GITHUB_PAT = "github_pat_" + ("A" * 24)
PR_URL = "https://github.com/example/repo/pull/9"
EXECUTION_URL = (
    "http://localhost:8000/console/flows/executions/"
    "85b67a24-0000-4000-8000-000000000001"
)


def _notifications(
    *,
    failure_comment: bool = False,
    attention: bool = False,
    success_comment: bool = False,
) -> dict:
    """A stored blob, including the two ignored failure-side keys."""
    return {
        "on_failure": {
            "comment_on_trigger_issue": failure_comment,
            "attention_item": attention,
        },
        "on_success": {"comment_on_trigger_issue": success_comment},
    }


def _issue_trigger(number: int = 42) -> dict:
    return {
        "source": "github",
        "type": "issue_created",
        "_subject": {
            "reference": f"#{number}",
            "url": f"https://github.com/example/repo/issues/{number}",
            "text": f"example/repo #{number}",
        },
        "payload": {
            "issue": {
                "number": number,
                "html_url": f"https://github.com/example/repo/issues/{number}",
            },
            "repository": {"full_name": "example/repo"},
        },
    }


class StubTracker:
    """Tracker client stand-in used by the terminal-path tests."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[tuple[str, str]] = []
        self.add_comment = AsyncMock(side_effect=self._add_comment)

    async def _add_comment(self, issue_id: str, comment: str) -> Any:
        if self.fail:
            raise RuntimeError("tracker unavailable")
        self.calls.append((issue_id, comment))
        return {"id": "c1", "body": comment}


class TestFailureCommentIsGone:
    """The removed option must be inert, not fatal, on flows that stored it."""

    def test_module_no_longer_formats_failure_comments(self) -> None:
        for name in (
            "format_failure_comment",
            "tail_log_lines",
            "is_failure_status",
            "FAILURE_STATUSES",
        ):
            assert not hasattr(flow_execution_notifications, name), name

    def test_stored_failure_flag_is_ignored(self) -> None:
        assert parse_notifications(_notifications(failure_comment=True)) is None
        assert (
            parse_notifications(_notifications(failure_comment=True, attention=True))
            is None
        )
        parsed = parse_notifications(
            _notifications(failure_comment=True, success_comment=True)
        )
        assert parsed is not None
        assert parsed.on_success_comment is True
        assert not hasattr(parsed, "on_failure_comment")

    def test_no_tracker_client_is_resolved_for_a_failed_run(self) -> None:
        flags = _notifications(failure_comment=True, success_comment=True)
        assert not needs_tracker_comment(flags, "FAILED")
        assert not needs_tracker_comment(flags, "TIMEOUT")

    @pytest.mark.asyncio
    async def test_failed_run_posts_nothing(self) -> None:
        tracker = StubTracker()
        for status in ("FAILED", "TIMEOUT", "STOPPED"):
            outcome = await notify_terminal_execution(
                notifications=_notifications(
                    failure_comment=True, success_comment=True
                ),
                status=status,
                execution_id="exec-1",
                trigger_event_details=_issue_trigger(),
                result={"pr_url": PR_URL},
                tracker_client=tracker,
            )
            assert outcome.success_comment_posted is False
            assert not hasattr(outcome, "failure_comment_posted")
        tracker.add_comment.assert_not_awaited()

    def test_api_schema_still_accepts_the_old_key(self) -> None:
        """An old client sending the key gets no 422, and no behaviour."""
        flow = FlowCreate(
            name="Legacy flow",
            prompt_template="do the work",
            agent_type="codex",
            agent_config={"sandbox_type": "exec"},
            notifications=FlowNotifications(
                on_failure=FlowFailureNotifications(
                    comment_on_trigger_issue=True,
                    attention_item=True,
                ),
                on_success=FlowSuccessNotifications(comment_on_trigger_issue=True),
            ),
        )
        dumped = flow.model_dump()
        assert dumped["notifications"]["on_failure"]["comment_on_trigger_issue"] is True
        assert dumped["notifications"]["on_success"]["comment_on_trigger_issue"] is True
        assert parse_notifications(dumped["notifications"]).on_success_comment is True


class TestParseNotifications:
    def test_none_and_empty_are_unset(self) -> None:
        assert parse_notifications(None) is None
        assert parse_notifications({}) is None
        assert parse_notifications(_notifications()) is None
        assert parse_notifications("not a blob") is None
        assert parse_notifications({"on_success": "broken"}) is None

    def test_dict_and_pydantic_model(self) -> None:
        parsed = parse_notifications(_notifications(success_comment=True))
        assert parsed is not None
        assert parsed.on_success_comment is True

        model = FlowNotifications(
            on_success=FlowSuccessNotifications(comment_on_trigger_issue=True),
        )
        parsed_model = parse_notifications(model)
        assert parsed_model is not None
        assert parsed_model.on_success_comment is True

        assert parse_notifications(FlowNotifications()) is None

    def test_a_flow_saved_by_the_new_console_has_no_failure_block(self) -> None:
        parsed = parse_notifications({"on_success": {"comment_on_trigger_issue": True}})
        assert parsed is not None
        assert parsed.on_success_comment is True


class TestTriggerTarget:
    def test_subject_reference(self) -> None:
        assert extract_trigger_comment_target(_issue_trigger(85)) == "85"

    def test_gitlab_bang_reference(self) -> None:
        details = {
            "_subject": {"reference": "!123"},
            "payload": {},
        }
        assert extract_trigger_comment_target(details) == "123"

    def test_branch_reference_is_not_a_comment_target(self) -> None:
        details = {
            "_subject": {"reference": "preloop/issue-42"},
            "payload": {},
        }
        assert extract_trigger_comment_target(details) is None

    def test_branch_reference_falls_through_to_payload_issue(self) -> None:
        details = {
            "_subject": {"reference": "preloop/issue-42"},
            "payload": {"issue": {"number": 42}},
        }
        assert extract_trigger_comment_target(details) == "42"

    def test_payload_issue_number_without_subject(self) -> None:
        details = {
            "payload": {"issue": {"number": 7}, "repository": {"full_name": "a/b"}}
        }
        assert extract_trigger_comment_target(details) == "7"

    def test_jira_key(self) -> None:
        details = {"payload": {"issue": {"key": "PROJ-12"}}}
        assert extract_trigger_comment_target(details) == "PROJ-12"

    def test_gitlab_iid(self) -> None:
        details = {"payload": {"object_attributes": {"iid": 9}, "object_kind": "issue"}}
        assert extract_trigger_comment_target(details) == "9"

    def test_missing(self) -> None:
        assert extract_trigger_comment_target(None) is None
        assert extract_trigger_comment_target({"payload": {}}) is None


class TestCommentFormatting:
    def test_success_comment(self) -> None:
        assert (
            format_success_comment("https://github.com/example/repo/pull/3")
            == "PR opened: https://github.com/example/repo/pull/3"
        )

    def test_pr_url_keys(self) -> None:
        assert extract_opened_pr_url({"pr_url": "https://example.com/pull/1"})
        assert extract_opened_pr_url({"merge_request_url": PR_URL}) == PR_URL
        assert extract_opened_pr_url(None) is None
        assert extract_opened_pr_url({}) is None


class TestStatusHelpers:
    def test_success_statuses(self) -> None:
        assert is_success_status("SUCCEEDED")
        assert is_success_status("success")
        assert not is_success_status("FAILED")
        assert not is_success_status("STOPPED")

    def test_needs_tracker_comment(self) -> None:
        flags = _notifications(success_comment=True)
        assert needs_tracker_comment(flags, "SUCCEEDED")
        assert not needs_tracker_comment(flags, "STOPPED")
        assert not needs_tracker_comment(None, "SUCCEEDED")
        assert not needs_tracker_comment(_notifications(), "SUCCEEDED")


@pytest.mark.asyncio
class TestNotifyTerminalExecution:
    async def test_success_posts_pr_opened_comment(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details=_issue_trigger(),
            result={"pr_url": PR_URL},
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is True
        assert tracker.calls == [("42", f"PR opened: {PR_URL}")]

    async def test_success_without_pr_url_skips_comment(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details=_issue_trigger(),
            result={},
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is False
        tracker.add_comment.assert_not_awaited()

    async def test_unset_notifications_are_a_no_op(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=None,
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details=_issue_trigger(),
            result={"pr_url": PR_URL},
            tracker_client=tracker,
        )
        assert outcome.skipped_reason == "notifications_unset"
        tracker.add_comment.assert_not_awaited()

    async def test_missing_tracker_or_issue_does_not_raise(self) -> None:
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details=_issue_trigger(),
            result={"pr_url": PR_URL},
            tracker_client=None,
        )
        assert outcome.success_comment_posted is False

        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details={"payload": {}},
            result={"pr_url": PR_URL},
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is False
        tracker.add_comment.assert_not_awaited()

    async def test_tracker_error_is_swallowed(self) -> None:
        tracker = StubTracker(fail=True)
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            execution_id="exec-1",
            trigger_event_details=_issue_trigger(),
            result={"pr_url": PR_URL},
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is False
        assert tracker.add_comment.await_count == 1


@pytest.mark.asyncio
async def test_orchestrator_notify_terminal_posts_via_flow_notifications() -> None:
    """``_notify_terminal`` resolves the tracker and the PR URL it comments."""

    from types import SimpleNamespace

    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    tracker = StubTracker()
    execution_id = uuid4()
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(spec=Session),
        flow_id=uuid4(),
        trigger_event_data=_issue_trigger(42),
        nats_client=MagicMock(),
    )
    orchestrator.flow = SimpleNamespace(
        notifications=_notifications(success_comment=True),
    )
    orchestrator.execution_log = SimpleNamespace(
        id=execution_id,
        trigger_event_details=_issue_trigger(42),
        failure_category=None,
        result={"pr_url": PR_URL},
    )
    orchestrator.execution_logger = MagicMock()

    with patch.object(
        orchestrator,
        "_get_tracker_client_for_status",
        new=AsyncMock(return_value=tracker),
    ):
        await orchestrator._notify_terminal(status="SUCCEEDED")

    tracker.add_comment.assert_awaited_once()
    assert tracker.calls == [("42", f"PR opened: {PR_URL}")]
    # The log tail the failure comment used to need is no longer read.
    orchestrator.execution_logger.get_agent_output_summary.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_notify_terminal_on_a_legacy_failure_flow() -> None:
    """A flow saved with the old failure option must not post or raise."""

    from types import SimpleNamespace

    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    tracker = StubTracker()
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(spec=Session),
        flow_id=uuid4(),
        trigger_event_data=_issue_trigger(42),
        nats_client=MagicMock(),
    )
    orchestrator.flow = SimpleNamespace(
        notifications=_notifications(failure_comment=True, attention=True),
    )
    orchestrator.execution_log = SimpleNamespace(
        id=uuid4(),
        trigger_event_details=_issue_trigger(42),
        failure_category="agent_error",
        result=None,
    )
    orchestrator.execution_logger = MagicMock()

    with patch.object(
        orchestrator,
        "_get_tracker_client_for_status",
        new=AsyncMock(return_value=tracker),
    ) as resolve_tracker:
        await orchestrator._notify_terminal(status="FAILED")

    tracker.add_comment.assert_not_awaited()
    resolve_tracker.assert_not_awaited()
