"""Continuation threads when CI fails for a reason outside the change (#1207).

Pins three behaviours:

* a base branch that is already red is classified as ``ci_failure_base_branch``
  (never ``ci_failure_unclassified``), and its failures are not a CI repair;
* pending review findings are still repaired when CI is red for a reason the
  change did not cause, and a change that broke CI keeps its CI repair;
* a parked thread says so on the pull request in one comment that is edited in
  place when the thread resumes.
"""

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from preloop.services.flow_feedback import (
    PARK_NOTICE_MARKER,
    decide,
    park_notice_body,
    sync_park_notice,
)
from preloop.services.flow_feedback_provider import (
    FeedbackProvider,
    FeedbackState,
    failed_on_base,
)
from preloop.sync.exceptions import TrackerResponseError

NOW = datetime(2026, 11, 3)


def _thread(**changes: Any) -> SimpleNamespace:
    values = {
        "expires_at": NOW + timedelta(days=1),
        "policy": {},
        "cursor": {},
        "turns": 0,
        "cost": 0,
        "no_progress": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _item(kind: str, head_sha: str = "head") -> SimpleNamespace:
    return SimpleNamespace(kind=kind, head_sha=head_sha, payload={"body": kind})


# --- decision table ---------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["ci_failure_base_branch", "ci_failure_unclassified"]
)
def test_review_pending_and_ci_failed_outside_the_change_repairs_the_review(
    reason: str,
) -> None:
    state = FeedbackState("head", blocked_reason=reason)
    pending = [_item("review"), _item("inline_comment")]
    assert decide(_thread(), state, pending, now=NOW) == ("repair", None)


@pytest.mark.parametrize(
    "reason", ["ci_failure_base_branch", "ci_failure_unclassified"]
)
def test_no_review_pending_and_ci_failed_outside_the_change_parks(
    reason: str,
) -> None:
    state = FeedbackState("head", blocked_reason=reason)
    assert decide(_thread(), state, [], now=NOW) == ("blocked", reason)
    # A signal is not a finding.
    assert decide(_thread(), state, [_item("signal")], now=NOW) == ("blocked", reason)
    # Findings on an older head are not actionable.
    stale = [_item("review", head_sha="old")]
    assert decide(_thread(), state, stale, now=NOW) == ("blocked", reason)


def test_review_pending_and_ci_failed_inside_the_change_repairs_both() -> None:
    state = FeedbackState("head")
    pending = [_item("review"), _item("ci")]
    assert decide(_thread(), state, pending, now=NOW) == ("repair", None)


def test_review_repair_despite_red_ci_still_respects_the_turn_budget() -> None:
    state = FeedbackState("head", blocked_reason="ci_failure_base_branch")
    thread = _thread(turns=5, policy={"max_turns": 5})
    assert decide(thread, state, [_item("review")], now=NOW) == (
        "stopped",
        "turn_budget_exhausted",
    )


@pytest.mark.parametrize(
    "reason", ["ci_permission_required", "ci_cancelled", "required_checks_missing"]
)
def test_other_blocks_still_park_with_findings_pending(reason: str) -> None:
    state = FeedbackState("head", blocked_reason=reason)
    assert decide(_thread(), state, [_item("review")], now=NOW) == ("blocked", reason)


def test_green_ci_on_the_same_head_unparks_and_repairs_pending_findings() -> None:
    thread = _thread()
    parked = FeedbackState("head", blocked_reason="ci_failure_unclassified")
    assert decide(thread, parked, [], now=NOW) == ("blocked", "ci_failure_unclassified")
    rerun = FeedbackState("head", checks_passed=True)
    assert decide(thread, rerun, [_item("review")], now=NOW) == ("repair", None)


# --- base branch classification ---------------------------------------------


def test_failed_on_base_requires_every_failing_check_to_fail_on_base() -> None:
    base = [
        {"name": "tests (1)", "conclusion": "failure"},
        {"name": "lint", "conclusion": "failure"},
        {"name": "build", "conclusion": "success"},
    ]
    assert failed_on_base({"tests (1)", "lint"}, base)
    assert not failed_on_base({"tests (1)", "build"}, base)
    assert not failed_on_base({"tests (1)", "docs"}, base)
    assert not failed_on_base(set(), base)
    assert failed_on_base({"ci/status"}, [{"context": "ci/status", "state": "failure"}])


def _github_provider(
    *, head_checks: list[dict[str, Any]], base_checks: list[dict[str, Any]] | None
) -> tuple[FeedbackProvider, list[str]]:
    paths: list[str] = []
    pr = {
        "node_id": "PR_fixture",
        "state": "open",
        "head": {"sha": "head"},
        "base": {"ref": "main", "sha": "basesha", "repo": {"id": 123}},
    }

    async def request(method: str, path: str, data: Any = None) -> Any:
        paths.append(f"{method} {path}")
        if path.endswith("/pulls/7"):
            return deepcopy(pr)
        if path == "/graphql":
            return {
                "data": {
                    "node": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [],
                        }
                    }
                }
            }
        if "/commits/head/check-runs" in path:
            return {"total_count": len(head_checks), "check_runs": head_checks}
        if "/commits/basesha/check-runs" in path:
            if base_checks is None:
                raise AssertionError("base read not expected")
            return {"total_count": len(base_checks), "check_runs": base_checks}
        if "/status?" in path:
            return {"statuses": []}
        if "/reviews?" in path:
            return [
                {
                    "id": 4,
                    "user": {"id": 42},
                    "state": "COMMENTED",
                    "commit_id": "head",
                    "body": "please regenerate openapi.yaml",
                }
            ]
        if "/issues/" in path:
            return [
                {
                    "id": 9,
                    "user": {"id": 77, "type": "User"},
                    "body": f"Parked.\n\n{PARK_NOTICE_MARKER}",
                }
            ]
        if path.endswith("/protection"):
            raise TrackerResponseError("Branch not protected", status_code=404)
        if "/rules/branches/" in path:
            return []
        raise AssertionError(path)

    binding = SimpleNamespace(
        provider="github",
        repository_id="123",
        pr_number="7",
        policy={"trusted_reviewer_ids": [42], "implementer_actor_ids": [43]},
    )
    return (
        FeedbackProvider(
            SimpleNamespace(_request=AsyncMock(side_effect=request)), binding
        ),
        paths,
    )


def _shard(name: str, conclusion: str = "failure") -> dict[str, Any]:
    # A non-Actions check: a failure here reads as the change's own defect,
    # which is what made a red base branch look like a repair round.
    return {"id": hash(name) % 1000, "name": name, "conclusion": conclusion}


@pytest.mark.asyncio
async def test_github_every_shard_red_on_base_is_a_base_branch_failure() -> None:
    shards = [_shard("tests (1)"), _shard("tests (2)"), _shard("pre-commit")]
    provider, paths = _github_provider(head_checks=shards, base_checks=shards)
    state = await provider.read()
    assert state.blocked_reason == "ci_failure_base_branch"
    assert not state.checks_passed
    # The base's failures are not this change's to repair; the review is.
    assert {item["kind"] for item in state.feedback} == {"review"}
    assert any("/commits/basesha/check-runs" in path for path in paths)
    # Our own park notice is never read back as a human comment.
    assert all(
        PARK_NOTICE_MARKER not in item["payload"]["body"] for item in state.feedback
    )
    pending = [SimpleNamespace(**item) for item in state.feedback]
    assert decide(_thread(), state, pending, now=NOW) == ("repair", None)


@pytest.mark.asyncio
async def test_github_failure_green_on_base_stays_the_change() -> None:
    provider, _ = _github_provider(
        head_checks=[_shard("tests (1)"), _shard("lint")],
        base_checks=[_shard("tests (1)"), _shard("lint", "success")],
    )
    state = await provider.read()
    # Not proven to be the base's: the failures stay the change's to repair.
    assert state.blocked_reason is None
    assert {item["kind"] for item in state.feedback} == {"review", "ci"}


@pytest.mark.asyncio
async def test_github_green_head_never_reads_the_base() -> None:
    provider, paths = _github_provider(
        head_checks=[_shard("tests (1)", "success")], base_checks=None
    )
    state = await provider.read()
    assert state.checks_passed
    assert not any("basesha" in path for path in paths)


# --- park notice lifecycle --------------------------------------------------


def _notice_provider() -> tuple[FeedbackProvider, list[tuple[str, str, Any]]]:
    calls: list[tuple[str, str, Any]] = []

    async def request(method: str, path: str, data: Any = None) -> Any:
        calls.append((method, path, data))
        if method == "POST":
            return {"id": 555}
        return {"id": int(path.rsplit("/", 1)[-1])}

    binding = SimpleNamespace(
        provider="github", repository_id="123", pr_number="7", policy={}
    )
    return (
        FeedbackProvider(
            SimpleNamespace(_request=AsyncMock(side_effect=request)), binding
        ),
        calls,
    )


def test_park_notice_states_reason_findings_and_resume_conditions() -> None:
    body = park_notice_body("blocked", "ci_failure_base_branch", pending_findings=4)
    assert "ci_failure_base_branch" in body
    assert "4" in body
    assert "push" in body.lower() and "ci" in body.lower()
    assert PARK_NOTICE_MARKER in body
    assert "—" not in body


@pytest.mark.asyncio
async def test_parking_posts_one_comment_and_unparking_edits_it() -> None:
    provider, calls = _notice_provider()
    cursor: dict[str, Any] = {}

    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=2
    )
    assert [c[0] for c in calls] == ["POST"]
    assert calls[0][1] == "/repositories/123/issues/7/comments"
    assert cursor["park_notice"]["id"] == "555"

    # Still parked for the same reason: nothing is written again.
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=2
    )
    assert len(calls) == 1

    # A different park reason edits the same comment.
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_deadline_exceeded", pending=2
    )
    assert calls[-1][0] == "PATCH"
    assert calls[-1][1] == "/repositories/123/issues/comments/555"
    assert "ci_deadline_exceeded" in calls[-1][2]["body"]

    # Unparking edits the same comment once.
    await sync_park_notice(provider, cursor, outcome="repair", reason=None, pending=2)
    assert calls[-1][0] == "PATCH"
    assert calls[-1][1].endswith("/555")
    assert "resumed" in calls[-1][2]["body"].lower()
    count = len(calls)
    await sync_park_notice(
        provider, cursor, outcome="waiting", reason="ci_pending", pending=0
    )
    assert len(calls) == count

    # Parking again reuses the same comment.
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=1
    )
    assert calls[-1][0] == "PATCH" and calls[-1][1].endswith("/555")


@pytest.mark.asyncio
async def test_never_parked_threads_write_nothing() -> None:
    provider, calls = _notice_provider()
    cursor: dict[str, Any] = {}
    await sync_park_notice(
        provider, cursor, outcome="waiting", reason="ci_pending", pending=1
    )
    await sync_park_notice(provider, cursor, outcome="repair", reason=None, pending=1)
    assert calls == [] and "park_notice" not in cursor


@pytest.mark.asyncio
async def test_park_notice_write_failure_never_breaks_reconciliation() -> None:
    provider, _ = _notice_provider()
    provider.client._request.side_effect = TrackerResponseError(
        "forbidden", status_code=403
    )
    cursor: dict[str, Any] = {}
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=1
    )
    # Recorded so the same notice is not retried every tick.
    assert cursor["park_notice"]["reason"] == "ci_failure_base_branch"
    assert cursor["park_notice"].get("id") is None
    assert cursor["park_notice"]["error"]


@pytest.mark.asyncio
async def test_providers_without_notice_support_write_nothing() -> None:
    provider, calls = _notice_provider()
    provider.thread.provider = "bitbucket"
    cursor: dict[str, Any] = {}
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=1
    )
    assert calls == [] and "park_notice" not in cursor


@pytest.mark.asyncio
async def test_github_unclassified_actions_failures_red_on_base_are_base_branch() -> (
    None
):
    """Actions jobs whose steps were not read used to park as unclassified."""
    actions = [
        {
            "id": n,
            "name": f"tests ({n})",
            "conclusion": "failure",
            "app": {"slug": "github-actions"},
            "head_sha": "head",
            "details_url": "https://example.com/not-an-actions-job",
        }
        for n in (1, 2, 3)
    ]
    provider, _ = _github_provider(head_checks=actions, base_checks=actions)
    state = await provider.read()
    assert state.blocked_reason == "ci_failure_base_branch"


@pytest.mark.asyncio
async def test_early_return_transitions_sync_the_notice(monkeypatch) -> None:
    """Page limit enters blocked with a notice; a close edits it to ended."""
    from preloop.services import flow_feedback

    monkeypatch.setattr(
        flow_feedback.crud_flow_feedback,
        "pending",
        lambda db, thread_id: [_item("review"), _item("signal")],
    )
    monkeypatch.setattr(
        flow_feedback.crud_flow_feedback, "release_read", lambda db: None
    )
    provider, calls = _notice_provider()
    thread = SimpleNamespace(id="t", state="waiting", cursor={})
    changes = await flow_feedback._notice_changes(
        None,
        provider,
        thread,
        {"state": "blocked", "stop_reason": "provider_page_limit"},
        head_sha="head",
    )
    assert [c[0] for c in calls] == ["POST"]
    assert "Pending review findings: 1." in calls[0][2]["body"]
    thread.cursor = changes["cursor"]
    changes = await flow_feedback._notice_changes(
        None, provider, thread, {"state": "closed", "stop_reason": "pr_merged"}
    )
    assert calls[-1][0] == "PATCH" and "ended" in calls[-1][2]["body"].lower()
    assert changes["cursor"]["park_notice"]["state"] == "closed"


@pytest.mark.asyncio
async def test_a_revived_stop_corrects_the_notice() -> None:
    """A no-progress stop can be revived; the notice must follow it."""
    provider, calls = _notice_provider()
    cursor: dict[str, Any] = {}
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=1
    )
    await sync_park_notice(
        provider, cursor, outcome="stopped", reason="no_progress", pending=1
    )
    assert "stopped" in calls[-1][2]["body"].lower()
    assert "will not resume" not in calls[-1][2]["body"].lower()
    count = len(calls)
    await sync_park_notice(
        provider, cursor, outcome="stopped", reason="no_progress", pending=1
    )
    assert len(calls) == count
    # revive() puts the thread back to waiting; the comment is edited again.
    await sync_park_notice(provider, cursor, outcome="waiting", reason=None, pending=1)
    assert len(calls) == count + 1
    assert "resumed" in calls[-1][2]["body"].lower()
    assert all(call[1].endswith("/555") for call in calls[1:])


@pytest.mark.asyncio
async def test_a_stop_or_end_after_a_resume_edits_the_notice() -> None:
    provider, calls = _notice_provider()
    cursor: dict[str, Any] = {}
    await sync_park_notice(
        provider, cursor, outcome="blocked", reason="ci_failure_base_branch", pending=1
    )
    await sync_park_notice(provider, cursor, outcome="repair", reason=None, pending=1)
    await sync_park_notice(
        provider, cursor, outcome="stopped", reason="no_progress", pending=0
    )
    assert "stopped" in calls[-1][2]["body"].lower()
    await sync_park_notice(
        provider, cursor, outcome="stopped", reason="turn_budget_exhausted", pending=0
    )
    assert "turn_budget_exhausted" in calls[-1][2]["body"]
    await sync_park_notice(
        provider, cursor, outcome="closed", reason="pr_merged", pending=0
    )
    assert "ended" in calls[-1][2]["body"].lower()
    count = len(calls)
    await sync_park_notice(
        provider, cursor, outcome="closed", reason="pr_merged", pending=0
    )
    assert len(calls) == count
    assert all(call[1].endswith("/555") for call in calls[1:])
