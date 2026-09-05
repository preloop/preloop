"""Tests for GitLabTracker.list_merge_requests."""

from unittest.mock import MagicMock, patch

import pytest

from preloop.sync.trackers.gitlab import GitLabTracker


@pytest.mark.asyncio
async def test_list_opened_mrs_normalization():
    mock_gl = MagicMock()
    mock_gl.auth.return_value = None
    mock_project = MagicMock()
    mock_gl.projects.get.return_value = mock_project

    mr = MagicMock()
    mr.iid = 7
    mr.title = "Fix login"
    mr.description = "Login 401"
    mr.web_url = "https://gitlab.example.com/group/project/-/merge_requests/7"
    mr.author = {"username": "janedoe"}
    mr.source_branch = "feature"
    mr.target_branch = "main"
    mr.state = "opened"
    mr.draft = False
    mr.work_in_progress = False
    mr.created_at = "2026-01-01T00:00:00Z"
    mr.updated_at = "2026-01-02T12:00:00Z"
    mr.attributes = {
        "iid": 7,
        "title": "Fix login",
        "description": "Login 401",
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/7",
        "author": {"username": "janedoe"},
        "source_branch": "feature",
        "target_branch": "main",
        "state": "opened",
        "draft": False,
        "work_in_progress": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T12:00:00Z",
    }
    mock_project.mergerequests.list.return_value = [mr]
    mock_gl.http_last_response.headers = {"X-Next-Page": "2"}

    with patch("preloop.sync.trackers.gitlab.gitlab.Gitlab", return_value=mock_gl):
        tracker = GitLabTracker(
            "tracker-1",
            "token",
            {"project_id": "99", "url": "https://gitlab.example.com"},
        )
        result = await tracker.list_merge_requests(state="open", limit=20, page=1)

    mock_project.mergerequests.list.assert_called_once_with(
        state="opened",
        order_by="updated_at",
        sort="desc",
        per_page=20,
        page=1,
    )
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["number"] == 7
    assert item["iid"] == 7
    assert item["title"] == "Fix login"
    assert item["description"] == "Login 401"
    assert item["url"] == (
        "https://gitlab.example.com/group/project/-/merge_requests/7"
    )
    assert item["author"] == "janedoe"
    assert item["source_branch"] == "feature"
    assert item["target_branch"] == "main"
    assert item["state"] == "open"
    assert item["draft"] is False
    assert result["has_more"] is True
