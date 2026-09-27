"""Tests for the Bitbucket Cloud MCP pull request handlers."""

import json
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp, mcp_bitbucket
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.models.models.user import User
from preloop.sync.trackers.bitbucket import BitbucketTracker

pytestmark = pytest.mark.asyncio

PR = {
    "id": 7,
    "title": "Add parser",
    "description": "Adds a parser",
    "state": "OPEN",
    "author": {"nickname": "dev"},
    "reviewers": [{"nickname": "rev"}],
    "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
    "source": {"branch": {"name": "feature"}, "commit": {"hash": "abc"}},
    "destination": {"branch": {"name": "main"}},
    "created_on": "2026-09-01T00:00:00Z",
    "updated_on": "2026-09-02T00:00:00Z",
}


class FakeBitbucket:
    """Routes requests to canned responses and records them."""

    def __init__(self, overrides: Dict[Tuple[str, str], httpx.Response] | None = None):
        self.requests: List[httpx.Request] = []
        self.overrides = overrides or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        suffix = request.url.path.split("/pullrequests/7", 1)[-1]
        key = (request.method, suffix)
        if key in self.overrides:
            return self.overrides[key]
        if request.method == "GET" and suffix == "":
            return httpx.Response(200, json=PR)
        if suffix == "/comments" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"id": 1, "content": {"raw": "hi"}, "user": {"nickname": "a"}},
                        {"id": 2, "content": {"raw": "gone"}, "deleted": True},
                    ]
                },
            )
        if suffix == "/diffstat":
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "status": "modified",
                            "lines_added": 3,
                            "lines_removed": 1,
                            "new": {"path": "a.py"},
                            "old": {"path": "a.py"},
                        },
                        {
                            "status": "renamed",
                            "lines_added": 0,
                            "lines_removed": 0,
                            "new": {"path": "b.py"},
                            "old": {"path": "old_b.py"},
                        },
                    ]
                },
            )
        if suffix == "/diff":
            return httpx.Response(200, text="diff --git a/a.py b/a.py\n")
        if request.method == "POST" and suffix == "/comments":
            return httpx.Response(
                201,
                json={
                    "id": 100 + len(self.requests),
                    "links": {"html": {"href": "https://bitbucket.org/c"}},
                },
            )
        return httpx.Response(200, json={"id": 1})

    def calls(self) -> List[Tuple[str, str]]:
        return [
            (r.method, r.url.path.split("/pullrequests/7", 1)[-1])
            for r in self.requests
        ]

    def bodies(self, method: str, suffix: str) -> List[Dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == method
            and r.url.path.split("/pullrequests/7", 1)[-1] == suffix
        ]


def client_for(fake: FakeBitbucket) -> BitbucketTracker:
    return BitbucketTracker(
        "t",
        "tok",
        {"workspace": "ws", "repo_full_name": "ws/repo"},
        transport=httpx.MockTransport(fake),
    )


async def test_get_pull_request_maps_fields() -> None:
    fake = FakeBitbucket()
    result = await mcp_bitbucket.get_pull_request(client_for(fake), "7")
    assert result.number == 7
    assert result.state == "open"
    assert result.author == "dev"
    assert result.reviewers == ["rev"]
    assert result.source_branch == "feature"
    assert result.target_branch == "main"
    assert result.merged_at is None
    assert [c["id"] for c in result.comments] == [1]
    assert result.changes["files_changed"] == 2
    assert result.changes["additions"] == 3
    assert result.changes["deletions"] == 1
    assert result.changes["changed_files"][1]["previous_filename"] == "old_b.py"
    assert result.changes["diff"].startswith("diff --git")
    assert result.changes["diff_truncated"] is False


async def test_get_pull_request_skips_optional_parts() -> None:
    fake = FakeBitbucket()
    result = await mcp_bitbucket.get_pull_request(
        client_for(fake), 7, include_comments=False, include_diff=False
    )
    assert result.comments == []
    assert result.changes is None
    assert fake.calls() == [("GET", "")]


async def test_get_pull_request_truncates_large_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_bitbucket, "MAX_DIFF_CHARS", 10)
    result = await mcp_bitbucket.get_pull_request(client_for(FakeBitbucket()), 7)
    assert len(result.changes["diff"]) == 10
    assert result.changes["diff_truncated"] is True


async def test_get_pull_request_rejects_non_numeric_number() -> None:
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.get_pull_request(client_for(FakeBitbucket()), "abc")
    assert exc.value.status_code == 400


async def test_add_comment_variants() -> None:
    fake = FakeBitbucket()
    client = client_for(fake)
    general = await mcp_bitbucket.add_comment(client, 7, "general")
    assert general.status == "created"
    assert general.url == "https://bitbucket.org/c"
    await mcp_bitbucket.add_comment(client, 7, "new side", path="a.py", line=3)
    await mcp_bitbucket.add_comment(
        client, 7, "old side", path="a.py", line=2, side="LEFT"
    )
    await mcp_bitbucket.add_comment(client, 7, "reply", in_reply_to="1")
    bodies = fake.bodies("POST", "/comments")
    assert "inline" not in bodies[0]
    assert bodies[1]["inline"] == {"path": "a.py", "to": 3}
    assert bodies[2]["inline"] == {"path": "a.py", "from": 2}
    assert bodies[3]["parent"] == {"id": 1}


async def test_add_comment_rejects_non_numeric_reply() -> None:
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.add_comment(
            client_for(FakeBitbucket()), 7, "x", in_reply_to="abc"
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    ("action", "call"),
    [
        ("approve", ("POST", "/approve")),
        ("unapprove", ("DELETE", "/approve")),
        ("request_changes", ("POST", "/request-changes")),
        ("remove_request_changes", ("DELETE", "/request-changes")),
    ],
)
async def test_review_actions(action: str, call: Tuple[str, str]) -> None:
    fake = FakeBitbucket()
    result = await mcp_bitbucket.update_pull_request(
        client_for(fake), 7, review_action=action
    )
    assert fake.calls() == [call]
    assert result.status == "updated"
    assert f"review ({action})" in result.message


async def test_review_with_body_comments_and_tasks() -> None:
    fake = FakeBitbucket(
        overrides={("POST", "/tasks"): httpx.Response(403, json={"error": {}})}
    )
    result = await mcp_bitbucket.update_pull_request(
        client_for(fake),
        7,
        review_action="request_changes",
        review_body="Summary",
        review_comments=[
            {"path": "a.py", "line": 3, "body": "Fix this", "task": True},
            {"path": "a.py", "line": 1, "body": "Old line", "side": "LEFT"},
        ],
    )
    comment_bodies = fake.bodies("POST", "/comments")
    assert comment_bodies[0] == {"content": {"raw": "Summary"}}
    assert comment_bodies[1]["inline"] == {"path": "a.py", "to": 3}
    assert comment_bodies[2]["inline"] == {"path": "a.py", "from": 1}
    task_bodies = fake.bodies("POST", "/tasks")
    assert len(task_bodies) == 1
    assert task_bodies[0]["content"] == {"raw": "Fix this"}
    assert "comment" in task_bodies[0]
    assert "1 task(s) skipped" in result.message
    assert "2 inline comment(s)" in result.message
    assert result.status == "updated"


@pytest.mark.parametrize("state", ["closed", "declined", "merged", "MERGE"])
async def test_refuses_close_decline_merge(state: str) -> None:
    fake = FakeBitbucket()
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.update_pull_request(client_for(fake), 7, state=state)
    assert exc.value.status_code == 400
    assert fake.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"review_comments": [{"path": "a", "line": 1, "body": "b"}]},
        {"review_action": "dismiss"},
        {"review_action": "comment"},
        {"review_action": "comment", "review_comments": [{"path": "a"}]},
        {"review_action": "comment", "review_comments": ["text"]},
    ],
)
async def test_update_pull_request_validation(kwargs: Dict[str, Any]) -> None:
    fake = FakeBitbucket()
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.update_pull_request(client_for(fake), 7, **kwargs)
    assert exc.value.status_code == 400
    assert fake.requests == []


async def test_metadata_update_and_ignored_fields() -> None:
    fake = FakeBitbucket()
    result = await mcp_bitbucket.update_pull_request(
        client_for(fake),
        7,
        title="New title",
        labels=["x"],
        draft=True,
        state="open",
    )
    assert fake.bodies("PUT", "") == [{"title": "New title"}]
    assert "metadata update" in result.message
    assert "ignored on Bitbucket Cloud: labels, draft, state" in result.message


async def test_update_pull_request_with_nothing_to_do() -> None:
    result = await mcp_bitbucket.update_pull_request(
        client_for(FakeBitbucket()), 7, assignees=["someone"]
    )
    assert result.status == "failed"
    assert "assignees" in result.message


async def test_update_comment_body_and_resolution() -> None:
    fake = FakeBitbucket()
    result = await mcp_bitbucket.update_comment(
        client_for(fake), 7, "5", body="edited", resolved=True, thread_id="4"
    )
    assert fake.calls() == [("PUT", "/comments/5"), ("POST", "/comments/4/resolve")]
    assert "updated body and resolved" in result.message

    fake = FakeBitbucket()
    await mcp_bitbucket.update_comment(client_for(fake), 7, "5", resolved=False)
    assert fake.calls() == [("DELETE", "/comments/5/resolve")]


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"body": "x", "thread_id": "abc"}],
)
async def test_update_comment_validation(kwargs: Dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.update_comment(
            client_for(FakeBitbucket()), 7, "5", **kwargs
        )
    assert exc.value.status_code == 400


async def test_update_comment_rejects_non_numeric_comment_id() -> None:
    with pytest.raises(HTTPException) as exc:
        await mcp_bitbucket.update_comment(
            client_for(FakeBitbucket()), 7, "abc", body="x"
        )
    assert exc.value.status_code == 400


# --- mcp.py wiring -----------------------------------------------------------


async def test_parse_bitbucket_pr_url() -> None:
    info = mcp._parse_pr_key_from_url(
        "https://bitbucket.org/ws/repo/pull-requests/7/diff"
    )
    assert info == {
        "platform": "bitbucket",
        "project_path": "ws/repo",
        "owner": "ws",
        "repo": "repo",
        "pr_number": "7",
    }
    assert mcp._detect_platform_from_url("https://bitbucket.org/ws/repo") == (
        "bitbucket"
    )


async def test_run_bitbucket_maps_errors() -> None:
    async def boom() -> None:
        raise RuntimeError("down")

    async def bad() -> None:
        raise ValueError("never merges")

    with pytest.raises(HTTPException) as exc:
        await mcp._run_bitbucket("read", boom())
    assert exc.value.status_code == 502
    with pytest.raises(HTTPException) as exc:
        await mcp._run_bitbucket("read", bad())
    assert exc.value.status_code == 400


async def test_get_pull_request_dispatches_to_bitbucket(
    db_session: Session, test_user: User
) -> None:
    tracker = Tracker(
        name="bb",
        account_id=test_user.account_id,
        tracker_type="bitbucket",
        api_key="tok",
        url="https://bitbucket.org",
    )
    db_session.add(tracker)
    db_session.commit()
    organization = Organization(name="ws", identifier="ws", tracker_id=tracker.id)
    db_session.add(organization)
    db_session.commit()
    project = Project(
        name="repo",
        identifier="r-uuid",
        slug="ws/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    fake = FakeBitbucket()
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer t"}
        mock_auth.return_value = (MagicMock(wraps=db_session), test_user)
        mock_get_tracker.return_value = client_for(fake)
        result = await mcp.get_pull_request(
            pull_request="https://bitbucket.org/ws/repo/pull-requests/7",
            include_diff=False,
        )

    assert result.number == 7
    assert mock_get_tracker.await_args.args[1] == project.id
    assert fake.calls() == [("GET", ""), ("GET", "/comments")]
