"""Terminal-path flow notifications. No database.

The tracker client is stubbed: these tests assert when a comment is posted,
what it contains, and that secrets in the log tail are redacted.
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
from preloop.services.flow_execution_notifications import (
    extract_opened_pr_url,
    extract_trigger_comment_target,
    format_failure_comment,
    format_success_comment,
    is_failure_status,
    needs_tracker_comment,
    notify_terminal_execution,
    parse_notifications,
    tail_log_lines,
)

GITHUB_PAT = "github_pat_" + ("A" * 24)
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


class TestParseNotifications:
    def test_none_and_empty_are_unset(self) -> None:
        assert parse_notifications(None) is None
        assert parse_notifications({}) is None
        assert parse_notifications(_notifications()) is None

    def test_dict_and_pydantic_model(self) -> None:
        parsed = parse_notifications(_notifications(failure_comment=True))
        assert parsed is not None
        assert parsed.on_failure_comment is True
        assert parsed.on_success_comment is False

        model = FlowNotifications(
            on_failure=FlowFailureNotifications(
                comment_on_trigger_issue=False,
                attention_item=True,
            ),
            on_success=FlowSuccessNotifications(comment_on_trigger_issue=True),
        )
        parsed_model = parse_notifications(model)
        assert parsed_model is not None
        assert parsed_model.on_success_comment is True
        assert parsed_model.on_failure_comment is False

        assert parse_notifications(_notifications(attention=True)) is None


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
    def test_failure_comment_includes_status_link_category_and_tail(self) -> None:
        lines = [f"line-{i}" for i in range(25)]
        body = format_failure_comment(
            status="FAILED",
            execution_url=EXECUTION_URL,
            failure_category="timeout",
            log_lines=tail_log_lines(lines),
        )
        assert "Status: FAILED" in body
        assert f"Execution: {EXECUTION_URL}" in body
        assert "Failure category: timeout" in body
        assert "line-0" not in body
        assert "line-5" in body
        assert "line-24" in body
        assert "Last 20 log lines:" in body

    def test_timeout_status_is_named(self) -> None:
        body = format_failure_comment(
            status="TIMEOUT",
            execution_url=EXECUTION_URL,
            failure_category="timeout",
            log_lines=[],
        )
        assert "Status: TIMEOUT" in body
        assert "No log lines were captured." in body

    def test_secrets_are_redacted_in_the_tail(self) -> None:
        lines = [
            f"cloning https://x-access-token:{GITHUB_PAT}@github.com/example/repo.git",
            "done",
        ]
        redacted = tail_log_lines(lines)
        assert GITHUB_PAT not in "\n".join(redacted)
        body = format_failure_comment(
            status="FAILED",
            execution_url=EXECUTION_URL,
            failure_category="agent_error",
            log_lines=redacted,
        )
        assert GITHUB_PAT not in body
        assert "[REDACTED]" in body

    def test_success_comment(self) -> None:
        assert (
            format_success_comment("https://github.com/example/repo/pull/3")
            == "PR opened: https://github.com/example/repo/pull/3"
        )

    def test_pr_url_keys(self) -> None:
        assert extract_opened_pr_url({"pr_url": "https://example.com/pull/1"})
        assert extract_opened_pr_url(None) is None
        assert extract_opened_pr_url({}) is None


class TestStatusHelpers:
    def test_failed_and_timeout(self) -> None:
        assert is_failure_status("FAILED")
        assert is_failure_status("TIMEOUT")
        assert is_failure_status("FAILED", "timeout")
        assert not is_failure_status("SUCCEEDED", "timeout")
        assert not is_failure_status("SUCCEEDED")
        assert not is_failure_status("STOPPED")

    def test_needs_tracker_comment(self) -> None:
        flags = _notifications(failure_comment=True, success_comment=True)
        assert needs_tracker_comment(flags, "FAILED")
        assert needs_tracker_comment(flags, "TIMEOUT")
        assert needs_tracker_comment(flags, "SUCCEEDED")
        assert not needs_tracker_comment(flags, "STOPPED")
        assert not needs_tracker_comment(None, "FAILED")


@pytest.mark.asyncio
class TestNotifyTerminalExecution:
    async def test_failure_posts_one_comment(self) -> None:
        tracker = StubTracker()
        lines = [f"log {i}" for i in range(5)] + [f"token {GITHUB_PAT}"]
        outcome = await notify_terminal_execution(
            notifications=_notifications(failure_comment=True, attention=True),
            status="FAILED",
            failure_category="agent_error",
            execution_id="85b67a24-0000-4000-8000-000000000001",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(42),
            result=None,
            log_lines=lines,
            tracker_client=tracker,
        )
        assert outcome.failure_comment_posted is True
        assert outcome.success_comment_posted is False
        assert tracker.add_comment.await_count == 1
        issue_id, comment = tracker.calls[0]
        assert issue_id == "42"
        assert "Status: FAILED" in comment
        assert EXECUTION_URL in comment
        assert "Failure category: agent_error" in comment
        assert GITHUB_PAT not in comment

    async def test_timeout_status_posts_failure_comment(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(failure_comment=True),
            status="TIMEOUT",
            failure_category="timeout",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result=None,
            log_lines=["Execution timed out after 3600 seconds"],
            tracker_client=tracker,
        )
        assert outcome.failure_comment_posted is True
        assert "Status: TIMEOUT" in tracker.calls[0][1]
        assert "Failure category: timeout" in tracker.calls[0][1]

    async def test_failed_without_comment_flag_does_not_post(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(attention=True),
            status="FAILED",
            failure_category="runner_error",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result=None,
            log_lines=["boom"],
            tracker_client=tracker,
        )
        assert outcome.failure_comment_posted is False
        tracker.add_comment.assert_not_awaited()

    async def test_success_posts_pr_opened_comment(self) -> None:
        tracker = StubTracker()
        pr_url = "https://github.com/example/repo/pull/9"
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            failure_category=None,
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result={"pr_url": pr_url},
            log_lines=[],
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is True
        assert outcome.failure_comment_posted is False
        assert tracker.calls == [("42", f"PR opened: {pr_url}")]

    async def test_success_without_pr_url_skips_comment(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(success_comment=True),
            status="SUCCEEDED",
            failure_category=None,
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result={},
            log_lines=[],
            tracker_client=tracker,
        )
        assert outcome.success_comment_posted is False
        tracker.add_comment.assert_not_awaited()

    async def test_unset_notifications_are_a_no_op(self) -> None:
        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=None,
            status="FAILED",
            failure_category="agent_error",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result=None,
            log_lines=["boom"],
            tracker_client=tracker,
        )
        assert outcome.skipped_reason == "notifications_unset"
        tracker.add_comment.assert_not_awaited()

    async def test_missing_tracker_or_issue_does_not_raise(self) -> None:
        outcome = await notify_terminal_execution(
            notifications=_notifications(failure_comment=True),
            status="FAILED",
            failure_category="unknown",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result=None,
            log_lines=[],
            tracker_client=None,
        )
        assert outcome.failure_comment_posted is False

        tracker = StubTracker()
        outcome = await notify_terminal_execution(
            notifications=_notifications(failure_comment=True),
            status="FAILED",
            failure_category="unknown",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details={"payload": {}},
            result=None,
            log_lines=[],
            tracker_client=tracker,
        )
        assert outcome.failure_comment_posted is False
        tracker.add_comment.assert_not_awaited()

    async def test_tracker_error_is_swallowed(self) -> None:
        tracker = StubTracker(fail=True)
        outcome = await notify_terminal_execution(
            notifications=_notifications(failure_comment=True),
            status="FAILED",
            failure_category="agent_error",
            execution_id="exec-1",
            execution_url=EXECUTION_URL,
            trigger_event_details=_issue_trigger(),
            result=None,
            log_lines=["boom"],
            tracker_client=tracker,
        )
        assert outcome.failure_comment_posted is False
        assert tracker.add_comment.await_count == 1


class TestFlowNotificationsSchema:
    def test_create_payload_round_trips(self) -> None:
        flow = FlowCreate(
            name="Notify on failure",
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
        assert dumped["notifications"]["on_failure"]["attention_item"] is True
        assert dumped["notifications"]["on_success"]["comment_on_trigger_issue"] is True


@pytest.mark.asyncio
async def test_orchestrator_notify_terminal_posts_via_flow_notifications() -> None:
    """``_notify_terminal`` must resolve tracker + log tail from the orchestrator."""

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
        notifications=_notifications(failure_comment=True),
    )
    orchestrator.execution_log = SimpleNamespace(
        id=execution_id,
        trigger_event_details=_issue_trigger(42),
        failure_category="agent_error",
        result=None,
    )
    orchestrator.execution_logger = MagicMock()
    orchestrator.execution_logger.get_agent_output_summary.return_value = (
        f"boom\ntoken {GITHUB_PAT}"
    )

    with patch.object(
        orchestrator,
        "_get_tracker_client_for_status",
        new=AsyncMock(return_value=tracker),
    ):
        await orchestrator._notify_terminal(
            status="FAILED", failure_category="agent_error"
        )

    tracker.add_comment.assert_awaited_once()
    issue_id, comment = tracker.calls[0]
    assert issue_id == "42"
    assert "Status: FAILED" in comment
    assert str(execution_id) in comment
    assert GITHUB_PAT not in comment
    orchestrator.execution_logger.get_agent_output_summary.assert_called_once()
