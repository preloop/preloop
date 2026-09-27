"""Tests for the Bitbucket Cloud tracker client."""

import json
from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock, patch

import httpx
import pytest

from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerPermissionError,
    TrackerResponseError,
)
from preloop.sync.trackers.bitbucket import BitbucketTracker
from preloop.sync.trackers.factory import create_tracker_client
from preloop.utils.bitbucket import BITBUCKET_WEBHOOK_EVENTS

pytestmark = pytest.mark.asyncio

API = "https://api.bitbucket.org/2.0"
Handler = Callable[[httpx.Request], httpx.Response]


def make_tracker(
    handler: Handler, requests: List[httpx.Request], **details: Any
) -> BitbucketTracker:
    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    connection = {"workspace": "ws", "repository": "repo", **details}
    return BitbucketTracker(
        "tracker-1", "secret-token", connection, transport=httpx.MockTransport(record)
    )


def ok(data: Any = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=data if data is not None else {})


async def test_bearer_header_is_sent() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"full_name": "ws/repo"}), requests)
    result = await tracker.test_connection()
    assert result.connected
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    assert requests[0].url.path == "/2.0/repositories/ws/repo"
    assert result.server_info["auth"] == "bearer"


async def test_basic_fallback_after_401_with_email() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"].startswith("Bearer"):
            return ok({"error": {"message": "nope"}}, 401)
        return ok({"full_name": "ws/repo"})

    tracker = make_tracker(handler, requests, email="dev@example.com")
    result = await tracker.test_connection()
    assert result.connected
    assert result.server_info["auth"] == "basic"
    expected = httpx.BasicAuth("dev@example.com", "secret-token")
    basic_header = next(expected.auth_flow(httpx.Request("GET", API))).headers[
        "Authorization"
    ]
    assert requests[1].headers["Authorization"] == basic_header

    # Later calls go straight to Basic.
    await tracker.get_pull_request(1)
    assert requests[2].headers["Authorization"] == basic_header
    assert len(requests) == 3


async def test_no_basic_fallback_for_access_tokens() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({}, 401),
        requests,
        email="dev@example.com",
        token_kind="access_token",
    )
    with pytest.raises(TrackerAuthenticationError) as exc:
        await tracker.get_pull_request(1)
    assert len(requests) == 1
    assert "expired" in str(exc.value)


async def test_basic_fallback_failure_reverts_to_bearer() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({}, 401), requests, email="dev@example.com")
    with pytest.raises(TrackerAuthenticationError):
        await tracker.get_pull_request(1)
    assert len(requests) == 2
    assert tracker._use_basic is False


@pytest.mark.parametrize(
    "path",
    [
        "repositories/ws/repo/pullrequests/1/merge",
        "repositories/ws/repo/pullrequests/1/decline/",
        "repositories/ws/repo/pullrequests/1/merge?x=1",
    ],
)
async def test_merge_and_decline_are_refused(path: str) -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(), requests)
    with pytest.raises(ValueError, match="never merges or declines"):
        await tracker._request("POST", path)
    assert requests == []


async def test_error_statuses_map_to_tracker_errors() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({"error": {"message": "forbidden"}}, 403), requests
    )
    with pytest.raises(TrackerPermissionError, match="forbidden"):
        await tracker.get_pull_request(1)

    tracker = make_tracker(lambda r: ok({"error": {"message": "boom"}}, 500), [])
    with pytest.raises(TrackerResponseError) as exc:
        await tracker.get_pull_request(1)
    assert exc.value.status_code == 500


async def test_pagination_follows_next_on_api_host() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return ok({"values": [{"id": 2}]})
        return ok(
            {
                "values": [{"id": 1}],
                "next": f"{API}/repositories/ws/repo/pullrequests/1/comments?page=2",
            }
        )

    tracker = make_tracker(handler, requests)
    comments = await tracker.get_pull_request_comments(1)
    assert [c["id"] for c in comments] == [1, 2]
    assert requests[0].url.params["pagelen"] == "100"


async def test_pagination_refuses_other_host() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({"values": [], "next": "https://evil.example.com/steal"}),
        requests,
    )
    with pytest.raises(TrackerResponseError, match="another host"):
        await tracker.get_pull_request_comments(1)
    assert len(requests) == 1


async def test_list_pull_requests_shape_and_params() -> None:
    requests: List[httpx.Request] = []
    pr = {
        "id": 9,
        "title": "Fix",
        "state": "OPEN",
        "author": {"nickname": "dev"},
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/9"}},
        "source": {"branch": {"name": "fix"}},
        "destination": {"branch": {"name": "main"}},
        "created_on": "2026-09-01T00:00:00Z",
        "updated_on": "2026-09-02T00:00:00Z",
    }
    tracker = make_tracker(lambda r: ok({"values": [pr], "next": "x"}), requests)
    result = await tracker.list_pull_requests(state="open", limit=500, page=0)
    params = requests[0].url.params
    assert params["state"] == "OPEN"
    assert params["pagelen"] == "50"
    assert params["page"] == "1"
    assert params["sort"] == "-updated_on"
    assert result["has_more"] is True
    item = result["items"][0]
    assert item["number"] == 9
    assert item["state"] == "open"
    assert item["source_branch"] == "fix"
    assert item["author"] == "dev"


async def test_comment_payloads() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"id": 77}), requests)
    await tracker.add_pull_request_comment(3, "general")
    await tracker.add_pull_request_comment(3, "inline", path="a.py", line=10)
    await tracker.add_pull_request_comment(3, "old side", path="a.py", old_line=4)
    await tracker.add_pull_request_comment(3, "reply", parent_id="12")
    bodies = [json.loads(r.content) for r in requests]
    assert bodies[0] == {"content": {"raw": "general"}}
    assert bodies[1]["inline"] == {"path": "a.py", "to": 10}
    assert bodies[2]["inline"] == {"path": "a.py", "from": 4}
    assert bodies[3]["parent"] == {"id": 12}
    assert all(r.url.path.endswith("/pullrequests/3/comments") for r in requests)


async def test_update_delete_resolve_comment() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resolve"):
            return ok({}, 409)
        return ok({"id": 5})

    tracker = make_tracker(handler, requests)
    await tracker.update_pull_request_comment(1, 5, "edited")
    assert await tracker.delete_pull_request_comment(1, 5)
    assert await tracker.set_comment_resolved(1, 5, True)
    assert await tracker.set_comment_resolved(1, 5, False)
    methods = [(r.method, r.url.path.rsplit("/", 2)[-2:]) for r in requests]
    assert methods == [
        ("PUT", ["comments", "5"]),
        ("DELETE", ["comments", "5"]),
        ("POST", ["5", "resolve"]),
        ("DELETE", ["5", "resolve"]),
    ]


async def test_approval_and_changes_requested() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({}, 409 if r.method == "POST" else 404), requests
    )
    assert await tracker.set_approval(1, True)
    assert await tracker.set_approval(1, False)
    assert await tracker.set_changes_requested(1, True)
    assert await tracker.set_changes_requested(1, False)
    assert [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in requests] == [
        ("POST", "approve"),
        ("DELETE", "approve"),
        ("POST", "request-changes"),
        ("DELETE", "request-changes"),
    ]


async def test_task_skipped_on_403() -> None:
    tracker = make_tracker(lambda r: ok({}, 403), [])
    assert await tracker.create_pull_request_task(1, "do it", comment_id=4) is None


async def test_task_created() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"id": 8}), requests)
    task = await tracker.create_pull_request_task(1, "do it", comment_id=4)
    assert task == {"id": 8}
    assert json.loads(requests[0].content) == {
        "content": {"raw": "do it"},
        "comment": {"id": 4},
    }


async def test_update_pull_request_only_sends_given_fields() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"id": 1}), requests)
    await tracker.update_pull_request(1, title="New")
    assert requests[0].method == "PUT"
    assert json.loads(requests[0].content) == {"title": "New"}
    await tracker.update_pull_request(1)
    assert requests[1].method == "GET"


async def test_get_projects_groups_by_bitbucket_project() -> None:
    repo = {
        "uuid": "{r-1}",
        "name": "repo",
        "slug": "repo",
        "full_name": "ws/repo",
        "project": {"key": "PRJ", "name": "Platform"},
        "mainbranch": {"name": "main"},
    }
    tracker = BitbucketTracker(
        "t",
        "tok",
        {"workspace": "ws"},
        transport=httpx.MockTransport(lambda r: ok({"values": [repo]})),
    )
    projects = await tracker.get_projects("ws")
    assert projects[0]["identifier"] == "r-1"
    assert projects[0]["group"] == "Platform"
    transformed = tracker.transform_project(projects[0], "org")
    assert transformed["slug"] == "ws/repo"
    assert transformed["meta_data"]["project_name"] == "Platform"
    assert transformed["meta_data"]["default_branch"] == "main"


async def test_issue_methods_are_unsupported() -> None:
    tracker = make_tracker(lambda r: ok(), [])
    assert await tracker.get_issues("ws", "p") == []
    with pytest.raises(NotImplementedError):
        await tracker.get_issue("1")


async def test_normalize_comment() -> None:
    comment = BitbucketTracker.normalize_comment(
        {
            "id": 7,
            "user": {"nickname": "rev"},
            "content": {"raw": "hi"},
            "inline": {"path": "a.py", "from": 3, "to": None},
            "parent": {"id": 2},
            "resolution": {"type": "resolved"},
        }
    )
    assert comment["side"] == "LEFT"
    assert comment["type"] == "review_comment"
    assert comment["thread_id"] == 2
    assert comment["resolved"] is True


async def test_factory_creates_bitbucket_tracker() -> None:
    client = await create_tracker_client(
        tracker_type="bitbucket",
        tracker_id="t",
        api_key="tok",
        connection_details={"workspace": "ws"},
    )
    assert isinstance(client, BitbucketTracker)


async def test_register_webhook_creates_then_updates() -> None:
    requests: List[httpx.Request] = []
    hooks: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return ok({"values": hooks})
        return ok({"uuid": "{h-1}", "url": "https://preloop.test/hook"})

    tracker = make_tracker(handler, requests)
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={})
    db = MagicMock()
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        crud.get_by_external_id.return_value = None
        assert await tracker.register_webhook(
            db=db, project=project, webhook_url="https://preloop.test/hook", secret="s"
        )
        crud.create.assert_called_once()
    post = requests[1]
    assert post.method == "POST"
    body = json.loads(post.content)
    assert body["secret"] == "s"
    assert body["events"] == list(BITBUCKET_WEBHOOK_EVENTS)

    hooks.append({"uuid": "{h-1}", "url": "https://preloop.test/hook"})
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        crud.get_by_external_id.return_value = object()
        assert await tracker.register_webhook(
            db=db, project=project, webhook_url="https://preloop.test/hook", secret="s"
        )
        crud.create.assert_not_called()
    assert requests[-1].method == "PUT"
    assert requests[-1].url.path.endswith("/hooks/{h-1}")


async def test_is_webhook_registered_for_project() -> None:
    tracker = make_tracker(
        lambda r: ok({"values": [{"url": "https://preloop.test/hook"}]}), []
    )
    project = MagicMock(slug="ws/repo", meta_data={})
    assert await tracker.is_webhook_registered_for_project(
        project, "https://preloop.test/hook"
    )
    assert not await tracker.is_webhook_registered_for_project(
        project, "https://other.test/hook"
    )
