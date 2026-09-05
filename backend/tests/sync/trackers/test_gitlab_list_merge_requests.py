"""Tests for GitLabTracker.list_merge_requests."""

from unittest.mock import MagicMock, patch

import gitlab
import pytest

from preloop.sync.trackers.gitlab import GitLabTracker


def _opened_mr(*, iid: int) -> MagicMock:
    mr = MagicMock()
    mr.iid = iid
    mr.title = "Fix login" if iid == 7 else f"MR {iid}"
    mr.description = "Login 401" if iid == 7 else ""
    mr.web_url = f"https://gitlab.example.com/group/project/-/merge_requests/{iid}"
    mr.author = {"username": "janedoe"}
    mr.source_branch = "feature"
    mr.target_branch = "main"
    mr.state = "opened"
    mr.draft = False
    mr.work_in_progress = False
    mr.created_at = "2026-01-01T00:00:00Z"
    mr.updated_at = "2026-01-02T12:00:00Z"
    mr.attributes = {
        "iid": iid,
        "title": mr.title,
        "description": mr.description,
        "web_url": mr.web_url,
        "author": {"username": "janedoe"},
        "source_branch": "feature",
        "target_branch": "main",
        "state": "opened",
        "draft": False,
        "work_in_progress": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T12:00:00Z",
    }
    return mr


@pytest.mark.asyncio
async def test_list_opened_mrs_normalization():
    mock_gl = MagicMock(spec=gitlab.Gitlab)
    mock_gl.auth.return_value = None
    mock_project = MagicMock()
    mock_gl.projects = MagicMock()
    mock_gl.projects.get.return_value = mock_project

    mrs = [_opened_mr(iid=7 + index) for index in range(21)]
    mock_project.mergerequests.list.return_value = mrs

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
        per_page=21,
        page=1,
    )
    assert len(result["items"]) == 20
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


@pytest.mark.asyncio
async def test_list_opened_mrs_has_more_false_without_extra_item():
    mock_gl = MagicMock(spec=gitlab.Gitlab)
    mock_gl.auth.return_value = None
    mock_project = MagicMock()
    mock_gl.projects = MagicMock()
    mock_gl.projects.get.return_value = mock_project
    mock_project.mergerequests.list.return_value = [_opened_mr(iid=7)]

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
        per_page=21,
        page=1,
    )
    assert len(result["items"]) == 1
    assert result["has_more"] is False
