"""Stale reopen requests and collection rows for the Delivery Report pages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from preloop.services import delivery_report_views as views

PR_URL = "https://github.com/example/app/pull/10"
ISSUE_URL = "https://github.com/example/app/issues/7"


def _report() -> dict[str, Any]:
    return {
        "shipped": [
            {
                "ref": "example/app#10",
                "url": PR_URL,
                "project": "example/app",
                "head_sha": "aaa111",
            }
        ],
        "closed_issues": [
            {
                "ref": "example/app#7",
                "url": ISSUE_URL,
                "updated_at": "2026-10-20T10:00:00Z",
                "closing_head_sha": "aaa111",
                "closed_by": ["example/app#10"],
            }
        ],
        "reopen_requests": [{"ref": "example/app#7", "revision": "aaa111"}],
    }


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    current: dict[str, Any] = {"issue": None, "pull": None}
    monkeypatch.setattr(
        views.crud_issue,
        "get_by_external_url",
        lambda db, *, external_url, account_id=None: current["issue"],
    )
    monkeypatch.setattr(
        views.crud_issue_cost,
        "get_pull_request",
        lambda db, *, account_id, pr_key: current["pull"],
    )
    return current


def test_current_request_is_not_stale(state: dict[str, Any]) -> None:
    state["issue"] = SimpleNamespace(updated_at=datetime(2026, 10, 20, 10, 0))
    state["pull"] = SimpleNamespace(head_sha="aaa111")
    assert views.stale_reopen_requests(None, account_id="a", report=_report()) == {}


def test_issue_edited_after_assessment_is_stale(state: dict[str, Any]) -> None:
    state["issue"] = SimpleNamespace(
        updated_at=datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    )
    stale = views.stale_reopen_requests(None, account_id="a", report=_report())
    assert stale == {"example/app#7": "The issue was edited after this assessment."}


def test_new_commits_on_closing_pr_are_stale(state: dict[str, Any]) -> None:
    state["pull"] = SimpleNamespace(head_sha="bbb222")
    stale = views.stale_reopen_requests(None, account_id="a", report=_report())
    assert "New commits landed on example/app#10" in stale["example/app#7"]


def test_lookup_failure_leaves_request_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(views.crud_issue, "get_by_external_url", boom)
    assert views.stale_reopen_requests(None, account_id="a", report=_report()) == {}


def test_row_tolerates_a_report_without_summary_or_spend() -> None:
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        flow_id=uuid.uuid4(),
        flow=SimpleNamespace(name="Delivery Report"),
        status="FAILED",
        start_time=None,
        end_time=None,
        result={"status": "error", "reason": "x", "shipped": "junk"},
    )
    row = views.report_row(execution)
    assert row.shipped == 0
    assert row.cost_usd is None
    assert row.report_status == "error"


def test_second_closing_pr_is_not_compared_to_another_prs_head(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report()
    report["shipped"].insert(
        0,
        {
            "ref": "example/app#9",
            "url": "https://github.com/example/app/pull/9",
            "head_sha": "zzz999",
        },
    )
    report["closed_issues"][0]["closed_by"] = ["example/app#9", "example/app#10"]
    pulls = {
        "https://github.com/example/app/pull/9": SimpleNamespace(head_sha="zzz999"),
        PR_URL: SimpleNamespace(head_sha="aaa111"),
    }
    monkeypatch.setattr(
        views.crud_issue_cost,
        "get_pull_request",
        lambda db, *, account_id, pr_key: pulls.get(pr_key),
    )
    assert views.stale_reopen_requests(None, account_id="a", report=report) == {}


def test_pending_attention_marks_stale_requests(state: dict[str, Any]) -> None:
    state["pull"] = SimpleNamespace(head_sha="new-head")
    report = views.report_attention(
        None,
        account_id="a",
        execution=SimpleNamespace(id=uuid.uuid4(), start_time=None, result=_report()),
    )
    assert report is not None
    assert "New commits" in report.proposals[0].stale_reason


def test_projects_include_open_work_without_any_merges() -> None:
    assert views.report_projects(
        {
            "open_past_window": [{"ref": "example/app#1"}],
            "follow_ups": [{"ref": "example/docs#2"}],
        }
    ) == ["example/app", "example/docs"]
