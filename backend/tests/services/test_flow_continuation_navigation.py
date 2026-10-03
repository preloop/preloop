"""Continuation navigation preserves ownership and shared publication links."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from preloop.services.flow_continuation_navigation import (
    project_continuation_navigation,
)


def test_publisher_and_continuations_share_links() -> None:
    account_id = uuid.uuid4()
    publisher_id = uuid.uuid4()
    publisher = SimpleNamespace(
        id=publisher_id,
        status="SUCCEEDED",
        start_time=datetime(2026, 10, 1),
        trigger_event_details={
            "payload": {"issue": {"html_url": "https://github.com/org/repo/issues/1"}}
        },
        result={"pr_url": "https://github.com/org/repo/pull/2"},
    )
    repair = SimpleNamespace(
        id=uuid.uuid4(),
        resume_of=publisher_id,
        status="FAILED",
        start_time=datetime(2026, 10, 2),
        trigger_event_details={"_resume": {"resume_root": str(publisher_id)}},
        result=None,
    )
    publisher.resume_of = None
    with patch(
        "preloop.services.flow_continuation_navigation.crud_flow_execution.get_continuation_navigation",
        return_value=[publisher, repair],
    ) as read:
        for execution in [publisher, repair]:
            project_continuation_navigation(None, execution, account_id=account_id)
            assert execution.continuation_navigation == {
                "original_execution_id": publisher_id,
                "issue_url": "https://github.com/org/repo/issues/1",
                "pr_url": "https://github.com/org/repo/pull/2",
                "follow_ups_truncated": False,
                "follow_ups": [
                    {
                        "id": repair.id,
                        "status": "FAILED",
                        "start_time": repair.start_time,
                    }
                ],
            }
        read.assert_called_with(None, root_id=publisher_id, account_id=account_id)


def test_foreign_publisher_is_not_exposed() -> None:
    execution = SimpleNamespace(id=uuid.uuid4(), resume_of=uuid.uuid4())
    with patch(
        "preloop.services.flow_continuation_navigation.crud_flow_execution.get_continuation_navigation",
        return_value=[],
    ):
        project_continuation_navigation(None, execution, account_id=uuid.uuid4())
    assert not hasattr(execution, "continuation_navigation")
    assert execution.resume_of is None


def test_long_chains_are_capped_and_marked() -> None:
    publisher = SimpleNamespace(
        id=uuid.uuid4(),
        resume_of=None,
        status="SUCCEEDED",
        start_time=datetime(2026, 10, 1),
        trigger_event_details={},
        result=None,
    )
    repairs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            status="FAILED",
            start_time=datetime(2026, 10, 2),
            trigger_event_details={},
            result=None,
        )
        for _ in range(101)
    ]
    with patch(
        "preloop.services.flow_continuation_navigation.crud_flow_execution.get_continuation_navigation",
        return_value=[publisher, *repairs],
    ):
        project_continuation_navigation(None, publisher, account_id=uuid.uuid4())
    assert publisher.continuation_navigation["follow_ups_truncated"] is True
    assert len(publisher.continuation_navigation["follow_ups"]) == 100


def test_unsafe_urls_are_not_exposed() -> None:
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        resume_of=None,
        status="SUCCEEDED",
        start_time=datetime(2026, 10, 1),
        trigger_event_details={
            "payload": {"issue": {"html_url": "javascript:alert(1)"}}
        },
        result={"pr_url": "//example.com/pr/1"},
    )
    with patch(
        "preloop.services.flow_continuation_navigation.crud_flow_execution.get_continuation_navigation",
        return_value=[execution],
    ):
        project_continuation_navigation(None, execution, account_id=uuid.uuid4())
    assert execution.continuation_navigation["issue_url"] is None
    assert execution.continuation_navigation["pr_url"] is None
