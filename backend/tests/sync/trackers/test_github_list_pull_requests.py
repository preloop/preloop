"""Tests for GitHubTracker.list_pull_requests."""

from unittest.mock import AsyncMock, patch

import pytest

from preloop.sync.trackers.github import GitHubTracker, _github_link_has_next


def _raw_pr() -> dict:
    return {
        "number": 42,
        "title": "Add login",
        "body": "Please review",
        "html_url": "https://github.com/acme/widgets/pull/42",
        "user": {"login": "janedoe"},
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
        "state": "open",
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_list_open_prs_params_and_normalization():
    tracker = GitHubTracker(
        "tracker-1",
        "token",
        {"owner": "acme", "repo": "widgets"},
    )
    mock_request = AsyncMock(return_value=([_raw_pr()], {}))
    with patch.object(tracker, "_request_with_headers", mock_request):
        result = await tracker.list_pull_requests(state="open", limit=20, page=1)

    mock_request.assert_awaited_once()
    args, kwargs = mock_request.await_args
    assert args[0] == "GET"
    assert args[1] == "/repos/acme/widgets/pulls"
    assert kwargs["params"] == {
        "state": "open",
        "per_page": 20,
        "page": 1,
        "sort": "updated",
        "direction": "desc",
    }
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["number"] == 42
    assert item["iid"] == 42
    assert item["title"] == "Add login"
    assert item["description"] == "Please review"
    assert item["url"] == "https://github.com/acme/widgets/pull/42"
    assert item["author"] == "janedoe"
    assert item["source_branch"] == "feature"
    assert item["target_branch"] == "main"
    assert item["state"] == "open"
    assert item["draft"] is False
    assert item["created_at"] == "2026-01-01T00:00:00Z"
    assert item["updated_at"] == "2026-01-02T12:00:00Z"
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_has_more_from_link_header():
    tracker = GitHubTracker(
        "tracker-1",
        "token",
        {"owner": "acme", "repo": "widgets"},
    )
    link = (
        '<https://api.github.com/repos/acme/widgets/pulls?page=2>; rel="next", '
        '<https://api.github.com/repos/acme/widgets/pulls?page=5>; rel="last"'
    )
    mock_request = AsyncMock(return_value=([_raw_pr()], {"Link": link}))
    with patch.object(tracker, "_request_with_headers", mock_request):
        result = await tracker.list_pull_requests(state="open", limit=20, page=1)

    assert result["has_more"] is True
    assert _github_link_has_next(link) is True
    assert _github_link_has_next(None) is False
    assert _github_link_has_next('<https://example.com>; rel="prev"') is False
