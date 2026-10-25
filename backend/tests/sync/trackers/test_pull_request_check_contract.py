"""Forge data survives the MCP response contract and deterministic scoping."""

from unittest.mock import AsyncMock

import pytest

from preloop.schemas.mcp import PullRequestResponse
from preloop.services.delivery_report_checks import bounded_pr_input, check_conclusion
from preloop.sync.exceptions import TrackerPermissionError
from preloop.sync.trackers.github import GitHubTracker

HEAD = "a" * 40
MERGE = "b" * 40


@pytest.mark.asyncio
async def test_github_response_preserves_checks_and_revisions() -> None:
    tracker = GitHubTracker("test", "token", {"owner": "acme", "repo": "app"})
    tracker._request = AsyncMock(
        side_effect=[
            {
                "id": 1,
                "number": 1,
                "title": "Fix",
                "state": "closed",
                "user": {"login": "dev"},
                "html_url": "https://github.com/acme/app/pull/1",
                "head": {"ref": "fix", "sha": HEAD},
                "base": {"ref": "main"},
                "merge_commit_sha": MERGE,
                "created_at": "2026-10-25T00:00:00Z",
                "updated_at": "2026-10-25T00:00:00Z",
            },
            [],
            [],
            {
                "total_count": 1,
                "check_runs": [
                    {"name": "CI", "status": "completed", "conclusion": "success"}
                ],
            },
            {"total_count": 1, "state": "success"},
        ]
    )
    tracker._make_request = AsyncMock(return_value=[])
    raw = await tracker.get_pull_request("1")
    response = PullRequestResponse(**raw).model_dump(mode="json")
    scope = bounded_pr_input(response)
    assert scope["head_sha"] == HEAD
    assert scope["merge_sha"] == MERGE
    assert scope["check_conclusion"] == "success"
    assert all(HEAD in call.args[1] for call in tracker._request.call_args_list[-2:])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runs", "status", "expected"),
    [
        (
            {"total_count": 101, "check_runs": [{"conclusion": "success"}]},
            {"total_count": 0},
            "unknown",
        ),
        (
            {"total_count": 1, "check_runs": [{"conclusion": "success"}]},
            {"total_count": 1, "state": "failure"},
            "failure",
        ),
        (
            {"total_count": 1, "check_runs": [{"status": "in_progress"}]},
            {"total_count": 0},
            "pending",
        ),
        (
            TrackerPermissionError("checks unavailable"),
            {"total_count": 1, "state": "success"},
            "unknown",
        ),
        ({"total_count": 0, "check_runs": []}, {"total_count": 0}, "unknown"),
    ],
)
async def test_partial_or_unavailable_checks_are_not_success(
    runs: object, status: dict, expected: str
) -> None:
    tracker = GitHubTracker("test", "token", {"owner": "acme", "repo": "app"})
    tracker._request = AsyncMock(side_effect=[runs, status])
    checks = await tracker._pull_request_checks("acme", "app", HEAD)
    assert check_conclusion({"checks": checks}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline_sha", [HEAD, "stale"])
async def test_gitlab_mapper_reads_current_head_pipeline(pipeline_sha: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from preloop.sync.trackers.gitlab import GitLabTracker

    tracker = GitLabTracker(
        "test", "token", {"project_id": "1"}, initialize_client=False
    )
    tracker.gl = MagicMock()
    project = MagicMock()
    mr = SimpleNamespace(
        id=1,
        iid=1,
        title="Fix",
        description="",
        state="merged",
        author={"username": "dev"},
        web_url="https://gitlab.com/acme/app/-/merge_requests/1",
        source_branch="fix",
        target_branch="main",
        created_at="2026-10-25T00:00:00Z",
        updated_at="2026-10-25T00:00:00Z",
        sha=HEAD,
        merge_commit_sha=None,
        squash_commit_sha=MERGE,
        head_pipeline={"status": "success", "sha": pipeline_sha},
        notes=MagicMock(),
        changes=MagicMock(),
    )
    tracker._make_request = AsyncMock(side_effect=[project, mr, [], {"changes": []}])
    raw = await tracker.get_merge_request("1")
    assert raw["head_sha"] == HEAD
    assert raw["merge_commit_sha"] == MERGE
    assert check_conclusion(raw) == ("success" if pipeline_sha == HEAD else "unknown")
