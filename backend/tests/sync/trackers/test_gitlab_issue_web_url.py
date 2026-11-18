"""Synced GitLab issues keep GitLab's ``web_url`` as their external URL."""

from types import SimpleNamespace

import pytest

from preloop.sync.trackers.gitlab import GitLabTracker


def _raw(web_url: str) -> dict:
    """A GitLab issue payload as ``project.issues.list`` attributes."""
    return {
        "id": 9001,
        "iid": 12,
        "key": "grp/sub/proj#12",
        "title": "Login fails",
        "description": "Steps",
        "state": "opened",
        "updated_at": "2026-11-01T10:00:00Z",
        "created_at": "2026-11-01T09:00:00Z",
        "labels": [],
        "web_url": web_url,
    }


@pytest.mark.parametrize("word", ["issues", "work_items"])
def test_gitlab_issue_stores_web_url_as_returned(word: str) -> None:
    tracker = GitLabTracker(
        tracker_id="tracker-1",
        api_key="token",
        connection_details={"url": "https://gitlab.example.com"},
        initialize_client=False,
    )
    url = f"https://gitlab.example.com/grp/sub/proj/-/{word}/12"
    row = tracker.transform_issue(_raw(url), SimpleNamespace(id="project-1"))
    assert row["external_url"] == url
    assert row["meta_data"]["external_url"] == url
    assert row["key"] == "grp/sub/proj#12"


def test_payload_with_url_only_is_unchanged() -> None:
    tracker = GitLabTracker(
        tracker_id="tracker-1",
        api_key="token",
        connection_details={"url": "https://gitlab.example.com"},
        initialize_client=False,
    )
    raw = _raw("")
    del raw["web_url"]
    raw["url"] = "https://tracker.example.com/items/12"
    row = tracker.transform_issue(raw, SimpleNamespace(id="project-1"))
    assert row["external_url"] == "https://tracker.example.com/items/12"
