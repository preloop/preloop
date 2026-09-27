"""Branch filters and ``branch_exists`` used by the deterministic PR binding."""

from unittest.mock import AsyncMock, MagicMock, patch

import gitlab
import pytest

from preloop.sync.exceptions import TrackerResponseError
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.gitlab import GitLabTracker

BRANCH = "preloop/issue-951-0976028b"


def _github():
    return GitHubTracker("tracker-1", "token", {"owner": "acme", "repo": "app"})


@pytest.mark.asyncio
async def test_github_list_pull_requests_filters_by_head_branch():
    tracker = _github()
    request = AsyncMock(return_value=([], {}))
    with patch.object(tracker, "_request_with_headers", request):
        await tracker.list_pull_requests(state="open", limit=5, head_branch=BRANCH)
    assert request.await_args.kwargs["params"]["head"] == f"acme:{BRANCH}"


@pytest.mark.asyncio
async def test_github_list_pull_requests_without_branch_has_no_head_param():
    tracker = _github()
    request = AsyncMock(return_value=([], {}))
    with patch.object(tracker, "_request_with_headers", request):
        await tracker.list_pull_requests()
    assert "head" not in request.await_args.kwargs["params"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect,expected",
    [
        ({"ref": f"refs/heads/{BRANCH}"}, True),
        (TrackerResponseError("nf", status_code=404), False),
    ],
    ids=["found", "absent"],
)
async def test_github_branch_exists(side_effect, expected):
    tracker = _github()
    request = AsyncMock(side_effect=[side_effect])
    with patch.object(tracker, "_request", request):
        assert await tracker.branch_exists(BRANCH) is expected
    assert request.await_args.args == ("GET", f"/repos/acme/app/git/ref/heads/{BRANCH}")


@pytest.mark.asyncio
async def test_github_branch_exists_raises_when_it_cannot_tell():
    tracker = _github()
    request = AsyncMock(side_effect=TrackerResponseError("boom", status_code=502))
    with patch.object(tracker, "_request", request):
        with pytest.raises(TrackerResponseError):
            await tracker.branch_exists(BRANCH)


def _gitlab(project):
    mock_gl = MagicMock(spec=gitlab.Gitlab)
    mock_gl.auth.return_value = None
    mock_gl.projects = MagicMock()
    mock_gl.projects.get.return_value = project
    with patch("gitlab.Gitlab", return_value=mock_gl):
        return GitLabTracker(
            "tracker-1",
            "token",
            {"url": "https://gitlab.example.com", "project_id": "123"},
        )


@pytest.mark.asyncio
async def test_gitlab_list_merge_requests_filters_by_source_branch():
    project = MagicMock()
    project.mergerequests.list.return_value = []
    tracker = _gitlab(project)
    await tracker.list_merge_requests(state="open", limit=5, source_branch=BRANCH)
    assert project.mergerequests.list.call_args.kwargs["source_branch"] == BRANCH
    assert project.mergerequests.list.call_args.kwargs["state"] == "opened"


@pytest.mark.asyncio
async def test_gitlab_list_merge_requests_without_branch_has_no_filter():
    project = MagicMock()
    project.mergerequests.list.return_value = []
    tracker = _gitlab(project)
    await tracker.list_merge_requests()
    assert "source_branch" not in project.mergerequests.list.call_args.kwargs


@pytest.mark.asyncio
async def test_gitlab_branch_exists():
    project = MagicMock()
    tracker = _gitlab(project)
    assert await tracker.branch_exists(BRANCH) is True
    project.branches.get.assert_called_once_with(BRANCH)

    project.branches.get.side_effect = gitlab.exceptions.GitlabGetError(
        "404 Branch Not Found", response_code=404
    )
    assert await tracker.branch_exists(BRANCH) is False


@pytest.mark.asyncio
async def test_gitlab_branch_exists_raises_when_it_cannot_tell():
    project = MagicMock()
    tracker = _gitlab(project)
    project.branches.get.side_effect = gitlab.exceptions.GitlabGetError(
        "500", response_code=500
    )
    with pytest.raises(TrackerResponseError):
        await tracker.branch_exists(BRANCH)
