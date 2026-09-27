"""Tracker methods the backport flow relies on (issue #961)."""

from unittest.mock import AsyncMock, MagicMock, patch

import gitlab
import pytest

from preloop.sync.exceptions import TrackerResponseError
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.gitlab import GitLabTracker


def _github() -> GitHubTracker:
    return GitHubTracker("tracker-1", "token", {"owner": "acme", "repo": "widgets"})


def _raw_pr(number: int, head: str, base: str, **extra: object) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": "bot"},
        "head": {"ref": head},
        "base": {"ref": base},
        "state": "open",
        **extra,
    }


@pytest.mark.asyncio
async def test_github_find_pull_requests_by_branch_filters_and_marks_merged():
    tracker = _github()
    raw = [
        _raw_pr(3, "backport/pr-1-to-main", "main", state="closed", merged_at="x"),
        _raw_pr(4, "backport/pr-1-to-main", "release/1.1"),
        _raw_pr(5, "other", "main"),
    ]
    mock_request = AsyncMock(return_value=(raw, {}))
    with patch.object(tracker, "_request_with_headers", mock_request):
        found = await tracker.find_pull_requests_by_branch(
            "backport/pr-1-to-main", "main"
        )

    args, kwargs = mock_request.await_args
    assert args == ("GET", "/repos/acme/widgets/pulls")
    assert kwargs["params"]["state"] == "all"
    assert kwargs["params"]["head"] == "acme:backport/pr-1-to-main"
    assert kwargs["params"]["base"] == "main"
    assert [(item["number"], item["state"]) for item in found] == [(3, "merged")]


@pytest.mark.asyncio
async def test_github_request_reviewers_raises_on_failure():
    tracker = _github()
    mock_request = AsyncMock(side_effect=TrackerResponseError("no", status_code=422))
    with patch.object(tracker, "_request", mock_request):
        with pytest.raises(TrackerResponseError):
            await tracker.request_pull_request_reviewers(7, ["alice"])
    mock_request.assert_awaited_once_with(
        "POST",
        "/repos/acme/widgets/pulls/7/requested_reviewers",
        data={"reviewers": ["alice"]},
    )


@pytest.mark.asyncio
async def test_github_request_reviewers_with_nobody_is_a_no_op():
    tracker = _github()
    mock_request = AsyncMock()
    with patch.object(tracker, "_request", mock_request):
        await tracker.request_pull_request_reviewers(7, [])
    mock_request.assert_not_awaited()


def _mr(iid: int, source: str, target: str, state: str, source_project: int):
    mr = MagicMock()
    mr.attributes = {
        "iid": iid,
        "title": f"MR {iid}",
        "description": "",
        "web_url": f"https://gitlab.example.com/g/p/-/merge_requests/{iid}",
        "source_branch": source,
        "target_branch": target,
        "state": state,
        "source_project_id": source_project,
        "target_project_id": 99,
    }
    return mr


@pytest.mark.asyncio
async def test_gitlab_find_merge_requests_by_branch_skips_forks():
    mock_gl = MagicMock(spec=gitlab.Gitlab)
    mock_gl.auth.return_value = None
    mock_project = MagicMock()
    mock_gl.projects = MagicMock()
    mock_gl.projects.get.return_value = mock_project
    mock_project.mergerequests.list.return_value = [
        _mr(1, "backport/pr-2-to-main", "main", "merged", 99),
        _mr(2, "backport/pr-2-to-main", "main", "opened", 123),
    ]

    with patch("preloop.sync.trackers.gitlab.gitlab.Gitlab", return_value=mock_gl):
        tracker = GitLabTracker(
            "tracker-1",
            "token",
            {"project_id": "99", "url": "https://gitlab.example.com"},
        )
        found = await tracker.find_merge_requests_by_branch(
            "backport/pr-2-to-main", "main"
        )

    kwargs = mock_project.mergerequests.list.call_args.kwargs
    assert kwargs["source_branch"] == "backport/pr-2-to-main"
    assert kwargs["target_branch"] == "main"
    assert kwargs["state"] == "all"
    assert [(item["iid"], item["state"]) for item in found] == [(1, "merged")]
