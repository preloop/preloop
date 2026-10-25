"""Tests for the Bitbucket Cloud tracker client."""

import json
from typing import Any, Callable, Dict, List
from urllib.parse import unquote
from unittest.mock import AsyncMock, MagicMock, patch

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


# ----------------------------------------------------------------------
# Pull request creation, merge status, branches and commit build statuses
# ----------------------------------------------------------------------

CREATED_PR = {
    "id": 7,
    "title": "Add feature",
    "description": "Body text",
    "state": "OPEN",
    "draft": False,
    "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
    "source": {"branch": {"name": "feat/x"}, "commit": {"hash": "abc123"}},
    "destination": {"branch": {"name": "main"}},
    "author": {"display_name": "Dev"},
}


async def test_create_pull_request_payload_and_normalized_shape() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok(CREATED_PR, 201), requests)
    result = await tracker.create_pull_request(
        title="Add feature",
        source_branch="feat/x",
        target_branch="main",
        description="Body text",
        close_source_branch=True,
    )
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/2.0/repositories/ws/repo/pullrequests"
    payload = json.loads(requests[0].content)
    assert payload == {
        "title": "Add feature",
        "description": "Body text",
        "source": {"branch": {"name": "feat/x"}},
        "destination": {"branch": {"name": "main"}},
        "close_source_branch": True,
    }
    assert result == {
        "id": "7",
        "number": 7,
        "title": "Add feature",
        "description": "Body text",
        "state": "open",
        "url": "https://bitbucket.org/ws/repo/pull-requests/7",
        "is_draft": False,
        "source_branch": "feat/x",
        "target_branch": "main",
    }


async def test_create_pull_request_draft_and_ignored_fields() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({**CREATED_PR, "draft": True}, 201), requests)
    result = await tracker.create_pull_request(
        title="Add feature",
        source_branch="feat/x",
        target_branch="main",
        draft=True,
        assignees=["someone"],
        labels=["bug"],
        milestone="v1",
    )
    payload = json.loads(requests[0].content)
    assert payload["draft"] is True
    assert result["is_draft"] is True
    # Assignees, labels and milestone never reach the API.
    assert "assignees" not in payload
    assert "labels" not in payload
    assert "milestone" not in payload


async def test_create_pull_request_adds_reviewers_with_follow_up_put() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return ok(CREATED_PR, 201)
        return ok({**CREATED_PR, "title": "Add feature"})

    tracker = make_tracker(handler, requests)
    uuid = "9f4620ba-cf24-4b18-b9d1-12ff9ff9ff9f"
    await tracker.create_pull_request(
        title="Add feature",
        source_branch="feat/x",
        target_branch="main",
        reviewers=["{" + uuid + "}", "712020:abcd"],
    )
    assert [r.method for r in requests] == ["POST", "PUT"]
    assert requests[1].url.path == "/2.0/repositories/ws/repo/pullrequests/7"
    put = json.loads(requests[1].content)
    assert put["reviewers"] == [
        {"uuid": "{" + uuid + "}"},
        {"account_id": "712020:abcd"},
    ]


async def test_create_pull_request_reviewer_failure_keeps_the_created_pr() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return ok(CREATED_PR, 201)
        return ok({"error": {"message": "reviewer not found"}}, 400)

    tracker = make_tracker(handler, requests)
    result = await tracker.create_pull_request(
        title="Add feature",
        source_branch="feat/x",
        target_branch="main",
        reviewers=["712020:missing"],
    )
    assert result["number"] == 7
    assert result["url"].endswith("/pull-requests/7")


async def test_list_pull_requests_source_branch_filter_is_escaped() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"values": [CREATED_PR]}), requests)
    listing = await tracker.list_open_pull_requests_by_source_branch('fe"at')
    assert requests[0].url.params["q"] == 'source.branch.name = "fe\\"at"'
    assert requests[0].url.params["state"] == "OPEN"
    assert listing["items"][0]["source_branch"] == "feat/x"
    assert listing["items"][0]["url"].endswith("/pull-requests/7")
    assert listing["has_more"] is False


async def test_branch_exists_found_absent_and_error() -> None:
    tracker = make_tracker(lambda r: ok({"name": "feat/x"}), [])
    assert await tracker.branch_exists("feat/x") is True

    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({}, 404), requests)
    assert await tracker.branch_exists("feat/x") is False
    assert requests[0].url.raw_path.endswith(b"/refs/branches/feat%2Fx")

    tracker = make_tracker(lambda r: ok({}, 502), [])
    with pytest.raises(TrackerResponseError):
        await tracker.branch_exists("feat/x")


async def test_create_commit_status_posts_build_state() -> None:
    requests: List[httpx.Request] = []
    tracker = make_tracker(
        lambda r: ok({"key": "preloop", "state": "SUCCESSFUL"}, 201), requests
    )
    result = await tracker.create_commit_status(
        "abc123", "success", description="Approved", target_url="https://p.test/e/1"
    )
    assert requests[0].method == "POST"
    assert (
        requests[0].url.path == "/2.0/repositories/ws/repo/commit/abc123/statuses/build"
    )
    payload = json.loads(requests[0].content)
    assert payload == {
        "key": "preloop",
        "state": "SUCCESSFUL",
        "url": "https://p.test/e/1",
        "description": "Approved",
    }
    assert result["state"] == "SUCCESSFUL"


async def test_create_commit_status_sets_refname_for_the_pull_request() -> None:
    # Bitbucket shows a build status on a pull request only when refname
    # names the pull request's source branch.
    requests: List[httpx.Request] = []
    tracker = make_tracker(lambda r: ok({"key": "preloop"}, 201), requests)
    await tracker.create_commit_status("abc123", "pending", refname="feat/x")
    assert json.loads(requests[0].content)["refname"] == "feat/x"


async def test_create_commit_status_retries_duplicate_key_as_put() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return ok({"error": {"message": "already exists"}}, 409)
        return ok({"key": "preloop", "state": "FAILED"})

    tracker = make_tracker(handler, requests)
    result = await tracker.create_commit_status("abc123", "failure")
    assert [r.method for r in requests] == ["POST", "PUT"]
    assert requests[1].url.path.endswith("/commit/abc123/statuses/build/preloop")
    # No absolute target URL was given: the repository page is used.
    assert json.loads(requests[1].content)["url"] == "https://bitbucket.org/ws/repo"
    assert result["state"] == "FAILED"


async def test_create_commit_status_rejects_unknown_state() -> None:
    tracker = make_tracker(lambda r: ok({}), [])
    with pytest.raises(ValueError):
        await tracker.create_commit_status("abc123", "not-a-state")


# ---------------------------------------------------------------------------
# Managed Bitbucket Cloud grants (issue #1065)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from preloop.services.managed_credentials import (  # noqa: E402
    ManagedCredential,
    ManagedCredentialUnavailableError,
    ManagedReconnectRequiredError,
)
from preloop.utils.bitbucket import BitbucketConfigError  # noqa: E402

CLOCK_START = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


class FakeGrant:
    """Provider stand-in: a clock-driven token A/B rotation with call records."""

    def __init__(self, lifetime: timedelta = timedelta(hours=1)) -> None:
        self.now = CLOCK_START
        self.lifetime = lifetime
        self.version = 1
        self.issued_at = CLOCK_START
        self.calls: List[bool] = []
        self.failure: Exception | None = None

    @property
    def token(self) -> str:
        return f"token-{'abcdefgh'[self.version - 1]}"

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    async def __call__(self, *, force_refresh: bool = False) -> ManagedCredential:
        self.calls.append(force_refresh)
        if self.failure is not None:
            raise self.failure
        expired = self.issued_at + self.lifetime <= self.now + timedelta(seconds=60)
        if force_refresh or expired:
            self.version += 1
            self.issued_at = self.now
        return ManagedCredential(
            access_token=self.token,
            expires_at=self.issued_at + self.lifetime,
            rotation_version=self.version,
            provider="bitbucket",
        )


def make_managed(
    handler: Handler, requests: List[httpx.Request], grant: FakeGrant, **details: Any
) -> BitbucketTracker:
    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    connection = {
        "workspace": "ws",
        "repository": "repo",
        "auth_type": "managed_oauth",
        **details,
    }
    return BitbucketTracker(
        "tracker-1",
        "",
        connection,
        transport=httpx.MockTransport(record),
        credential_source=grant,
    )


async def test_managed_client_resolves_before_each_request_and_rotates() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()
    tracker = make_managed(lambda r: ok({"full_name": "ws/repo"}), requests, grant)
    assert tracker.managed is True

    result = await tracker.test_connection()
    assert result.connected
    assert requests[0].headers["Authorization"] == "Bearer token-a"
    assert result.server_info["auth"] == "managed"
    assert result.server_info["capabilities_verified"] is False
    assert result.server_info["rotation_version"] == 1

    # The same long-lived client, hours later: a fresh token is used without
    # anyone touching the client.
    grant.advance(timedelta(hours=2))
    await tracker.get_pull_request(1)
    assert requests[1].headers["Authorization"] == "Bearer token-b"
    assert grant.calls == [False, False]
    assert tracker.managed_credential.rotation_version == 2


async def test_managed_401_forces_exactly_one_refresh() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer token-a":
            return ok({"error": {"message": "expired"}}, 401)
        return ok({"id": 1})

    tracker = make_managed(handler, requests, grant, email="dev@example.com")
    await tracker.get_pull_request(1)
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer token-a",
        "Bearer token-b",
    ]
    assert grant.calls == [False, True]
    # No Basic fallback for managed grants, even with an email on the row.
    assert all(not r.headers["Authorization"].startswith("Basic") for r in requests)


async def test_managed_second_401_reports_reconnect_without_looping() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()
    tracker = make_managed(lambda r: ok({}, 401), requests, grant)
    with pytest.raises(TrackerAuthenticationError, match="Reconnect"):
        await tracker.get_pull_request(1)
    assert len(requests) == 2
    assert grant.calls == [False, True]


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        (ManagedReconnectRequiredError("invalid_grant"), "requires reconnect"),
        (ManagedCredentialUnavailableError("not_configured"), "could not provide"),
    ],
)
async def test_managed_resolver_failure_is_explicit_and_sends_nothing(
    failure: Exception, fragment: str
) -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()
    grant.failure = failure
    tracker = make_managed(lambda r: ok({}), requests, grant)
    result = await tracker.test_connection()
    assert result.connected is False
    assert fragment in result.message
    assert "token-" not in result.message
    assert requests == []


async def test_managed_without_resolver_never_sends_anonymously() -> None:
    requests: List[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok({})

    tracker = BitbucketTracker(
        "tracker-1",
        "",
        {"workspace": "ws", "auth_type": "managed_oauth"},
        transport=httpx.MockTransport(record),
    )
    assert tracker.managed is True
    with pytest.raises(TrackerAuthenticationError, match="no managed-provider plugin"):
        await tracker.get_pull_request(1, "ws/repo")
    assert requests == []


async def test_managed_rejects_custom_origin_and_pasted_token() -> None:
    with pytest.raises(BitbucketConfigError, match="pinned"):
        make_managed(
            lambda r: ok({}), [], FakeGrant(), api_url="https://api.example.com/2.0"
        )
    with pytest.raises(BitbucketConfigError, match="pasted token"):
        BitbucketTracker(
            "t", "pasted", {"workspace": "ws"}, credential_source=FakeGrant()
        )
    # A pasted-token client keeps its configurable origin and Basic fallback.
    manual = make_tracker(lambda r: ok({}), [], api_url="https://api.example.com/2.0")
    assert manual.managed is False
    assert manual.api_base_url == "https://api.example.com/2.0"


async def test_managed_refuses_redirects() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()
    tracker = make_managed(
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example/x"}),
        requests,
        grant,
    )
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker.get_pull_request(1)
    assert len(requests) == 1
    assert requests[0].url.host == "api.bitbucket.org"


async def test_managed_simultaneous_callers_each_resolve_fresh() -> None:
    import asyncio

    requests: List[httpx.Request] = []
    grant = FakeGrant()
    tracker = make_managed(lambda r: ok({"id": 1}), requests, grant)
    await asyncio.gather(*(tracker.get_pull_request(i) for i in range(3)))
    assert len(requests) == 3
    assert grant.calls == [False, False, False]
    assert {r.headers["Authorization"] for r in requests} == {"Bearer token-a"}


async def test_factory_refuses_managed_without_resolver_and_builds_with_one() -> None:
    assert (
        await create_tracker_client(
            "bitbucket",
            "tracker-1",
            "",
            {"workspace": "ws", "auth_type": "managed_oauth"},
        )
        is None
    )
    client = await create_tracker_client(
        "bitbucket",
        "tracker-1",
        "stale-key-must-be-ignored",
        {"workspace": "ws", "auth_type": "managed_oauth"},
        credential_source=FakeGrant(),
    )
    assert isinstance(client, BitbucketTracker)
    assert client.managed is True
    assert client.api_key == ""
    assert (
        await create_tracker_client(
            "github",
            "t",
            "",
            {"auth_type": "managed_oauth"},
            credential_source=FakeGrant(),
        )
        is None
    )


@pytest.mark.parametrize("kind", ["diff", "diffstat"])
async def test_managed_follows_canonical_diff_redirect(kind: str) -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()
    destination = f"/2.0/repositories/ws/repo/{kind}/ws/repo:abcdef123456%0D123456abcdef?context=3"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pullrequests/" in request.url.path:
            return httpx.Response(302, headers={"Location": destination})
        if kind == "diff":
            return httpx.Response(200, text="diff --git a/test b/test")
        return ok({"values": [{"status": "modified"}]})

    tracker = make_managed(handler, requests, grant)
    result = await (
        tracker.get_pull_request_diff(2)
        if kind == "diff"
        else tracker.get_pull_request_diffstat(2)
    )
    assert result == (
        "diff --git a/test b/test" if kind == "diff" else [{"status": "modified"}]
    )
    assert len(requests) == 2
    assert requests[1].url.raw_path.endswith(b"abcdef123456%0D123456abcdef?context=3")
    assert all(r.headers["Authorization"] == "Bearer token-a" for r in requests)
    assert grant.calls == [False, False]


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/2.0/repositories/ws/repo/diff/x",
        "http://api.bitbucket.org/2.0/repositories/ws/repo/diff/x",
        "https://user:password@api.bitbucket.org/2.0/repositories/ws/repo/diff/x",
        "https://api.bitbucket.org:8443/2.0/repositories/ws/repo/diff/x",
        "//evil.example/2.0/repositories/ws/repo/diff/x",
        "/untrusted/path",
        "/2.0/%2e%2e/untrusted",
        "/2.0/repositories/ws/repo/pullrequests/2/merge",
        "",
    ],
)
async def test_managed_redirect_rejects_unsafe_destination(location: str) -> None:
    requests: List[httpx.Request] = []
    tracker = make_managed(
        lambda r: httpx.Response(302, headers={"Location": location}),
        requests,
        FakeGrant(),
    )
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker.get_pull_request_diff(2)
    assert len(requests) == 1


async def test_managed_redirect_loop_is_bounded() -> None:
    requests: List[httpx.Request] = []
    tracker = make_managed(
        lambda r: httpx.Response(302, headers={"Location": str(r.url)}),
        requests,
        FakeGrant(),
    )
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker.get_pull_request_diff(2)
    assert len(requests) <= 4


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_managed_never_follows_write_redirect(method: str) -> None:
    requests: List[httpx.Request] = []
    tracker = make_managed(
        lambda r: httpx.Response(
            307, headers={"Location": "/2.0/repositories/ws/repo/comments"}
        ),
        requests,
        FakeGrant(),
    )
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker._request(
            method,
            "repositories/ws/repo/pullrequests/2/comments",
            json={"content": "test"},
        )
    assert len(requests) == 1


async def test_managed_redirect_refreshes_once_and_keeps_query_at_destination() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pullrequests/" in request.url.path:
            return httpx.Response(
                302,
                headers={"Location": "/2.0/repositories/ws/repo/diff/range?context=3"},
            )
        if request.headers["Authorization"] == "Bearer token-a":
            return ok({}, 401)
        return httpx.Response(200, text="diff")

    tracker = make_managed(handler, requests, grant)
    result = await tracker._request(
        "GET", "repositories/ws/repo/pullrequests/2/diff", params={"original": "only"}
    )
    assert result.text == "diff"
    assert grant.calls == [False, False, True]
    assert len(requests) == 3
    assert str(requests[1].url.query, "ascii") == "context=3"
    assert requests[2].headers["Authorization"] == "Bearer token-b"


async def test_managed_validates_every_redirect_hop_before_sending_token() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        location = (
            "/2.0/repositories/ws/repo/diff/range"
            if "/pullrequests/" in request.url.path
            else "https://evil.example/2.0/steal"
        )
        return httpx.Response(302, headers={"Location": location})

    tracker = make_managed(handler, requests, FakeGrant())
    with pytest.raises(TrackerResponseError, match="redirect"):
        await tracker.get_pull_request_diff(2)
    assert len(requests) == 2
    assert all(r.url.host == "api.bitbucket.org" for r in requests)


async def test_managed_redirected_diffstat_keeps_pagination() -> None:
    requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/pullrequests/" in request.url.path:
            return httpx.Response(
                302, headers={"Location": "/2.0/repositories/ws/repo/diffstat/range"}
            )
        if request.url.params.get("page") == "2":
            return ok({"values": [{"status": "added"}]})
        return ok(
            {
                "values": [{"status": "modified"}],
                "next": "https://api.bitbucket.org/2.0/repositories/ws/repo/diffstat/range?page=2",
            }
        )

    tracker = make_managed(handler, requests, FakeGrant())
    assert await tracker.get_pull_request_diffstat(2) == [
        {"status": "modified"},
        {"status": "added"},
    ]
    assert len(requests) == 3


async def test_managed_only_refreshes_once_across_redirect_chain() -> None:
    requests: List[httpx.Request] = []
    grant = FakeGrant()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer token-a":
            return ok({}, 401)
        if "/pullrequests/" in request.url.path:
            return httpx.Response(
                302, headers={"Location": "/2.0/repositories/ws/repo/diff/range"}
            )
        return ok({}, 401)

    tracker = make_managed(handler, requests, grant)
    with pytest.raises(TrackerAuthenticationError, match="Reconnect"):
        await tracker.get_pull_request_diff(2)
    assert grant.calls == [False, True, False]
    assert len(requests) == 3


HOOK_URL = "https://preloop.test/hook"


class FakeBitbucketHooks:
    """In-memory Bitbucket hooks API with two-page listing."""

    def __init__(self, hooks: List[Dict[str, Any]]) -> None:
        self.hooks = hooks
        self.counter = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            if request.url.params.get("page") == "2":
                return ok({"values": self.hooks[1:]})
            data: Dict[str, Any] = {"values": self.hooks[:1]}
            if len(self.hooks) > 1:
                data["next"] = f"{API}/repositories/ws/repo/hooks?page=2"
            return ok(data)
        if request.method == "POST":
            self.counter += 1
            hook = {"uuid": f"{{new-{self.counter}}}", **json.loads(request.content)}
            self.hooks.append(hook)
            return ok(hook)
        uuid = unquote(path.rsplit("/", 1)[-1])
        if request.method == "DELETE":
            self.hooks = [h for h in self.hooks if h["uuid"] != uuid]
            return httpx.Response(204)
        hook = next(h for h in self.hooks if h["uuid"] == uuid)
        hook.update(json.loads(request.content))
        return ok(hook)


async def _register(tracker: BitbucketTracker, crud: MagicMock) -> bool:
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={})
    return await tracker.register_webhook(
        db=MagicMock(), project=project, webhook_url=HOOK_URL, secret="s"
    )


def _stored_crud(store: Dict[str, Any]) -> Callable[..., Any]:
    def get_by_external_id(db: Any, external_id: str, tracker_id: str) -> Any:
        return store.get(external_id)

    return get_by_external_id


async def test_repeated_registration_keeps_one_hook() -> None:
    api = FakeBitbucketHooks([])
    requests: List[httpx.Request] = []
    tracker = make_tracker(api, requests)
    store: Dict[str, Any] = {}
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        crud.get_by_external_id.side_effect = _stored_crud(store)
        crud.create.side_effect = lambda db, obj_in: store.setdefault(
            obj_in["external_id"], MagicMock(id="w", secret=obj_in["secret"])
        )
        for _ in range(3):
            assert await _register(tracker, crud)
    assert len(api.hooks) == 1
    assert [r.method for r in requests].count("POST") == 1
    assert "PUT" not in [r.method for r in requests]
    assert "DELETE" not in [r.method for r in requests]


async def test_outdated_events_are_reconciled() -> None:
    api = FakeBitbucketHooks(
        [{"uuid": "{old}", "url": HOOK_URL, "active": True, "events": ["repo:push"]}]
    )
    requests: List[httpx.Request] = []
    tracker = make_tracker(api, requests)
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        crud.get_by_external_id.return_value = MagicMock(id="w", secret="s")
        assert await _register(tracker, crud)
    assert len(api.hooks) == 1
    assert sorted(api.hooks[0]["events"]) == sorted(BITBUCKET_WEBHOOK_EVENTS)
    assert [r.method for r in requests if r.method != "GET"] == ["PUT"]


async def test_unrelated_urls_preserved_and_duplicates_collapsed() -> None:
    events = list(BITBUCKET_WEBHOOK_EVENTS)
    other = {"uuid": "{other}", "url": "https://ci.test/hook", "events": events}
    api = FakeBitbucketHooks(
        [
            other,
            {"uuid": "{a}", "url": HOOK_URL, "active": True, "events": events},
            {"uuid": "{b}", "url": HOOK_URL, "active": True, "events": events},
            {"uuid": "{c}", "url": HOOK_URL, "active": True, "events": events},
        ]
    )
    requests: List[httpx.Request] = []
    tracker = make_tracker(api, requests)
    stored_b = MagicMock(id="w-b", secret="s")
    stored_c = MagicMock(id="w-c", secret="s")
    store = {"{b}": stored_b, "{c}": stored_c}
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        crud.get_by_external_id.side_effect = _stored_crud(store)
        assert await _register(tracker, crud)
        crud.create.assert_not_called()
        removed = [call.kwargs["id"] for call in crud.remove.call_args_list]
    assert {h["uuid"] for h in api.hooks} == {"{other}", "{b}"}
    assert removed == ["w-c"]
    assert "POST" not in [r.method for r in requests]
    assert "PUT" not in [r.method for r in requests]
    assert not any(r.url.path.endswith("other%7D") for r in requests)


def _crud_with_store(crud: MagicMock, store: Dict[str, Any]) -> None:
    """Wire a patched crud_webhook to an in-memory row store."""

    def create(db: Any, obj_in: Dict[str, Any]) -> Any:
        row = MagicMock(id=f"w-{obj_in['external_id']}", **obj_in)
        store[obj_in["external_id"]] = row
        return row

    def update(db: Any, db_obj: Any, obj_in: Dict[str, Any]) -> Any:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    def remove(db: Any, id: str) -> int:
        for key, row in list(store.items()):
            if row.id == id:
                del store[key]
        return 1

    crud.get_by_external_id.side_effect = _stored_crud(store)
    crud.create.side_effect = create
    crud.update.side_effect = update
    crud.remove.side_effect = remove


async def test_secret_rotation_updates_row_so_next_run_skips_put() -> None:
    api = FakeBitbucketHooks([])
    requests: List[httpx.Request] = []
    tracker = make_tracker(api, requests)
    store: Dict[str, Any] = {}
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={})
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        _crud_with_store(crud, store)
        for secret in ("s1", "s2", "s2"):
            assert await tracker.register_webhook(
                db=MagicMock(), project=project, webhook_url=HOOK_URL, secret=secret
            )
    writes = [r.method for r in requests if r.method != "GET"]
    assert writes == ["POST", "PUT"]
    assert store["{new-1}"].secret == "s2"


async def test_removed_duplicate_row_is_committed() -> None:
    events = list(BITBUCKET_WEBHOOK_EVENTS)
    api = FakeBitbucketHooks(
        [
            {"uuid": "{a}", "url": HOOK_URL, "active": True, "events": events},
            {"uuid": "{b}", "url": HOOK_URL, "active": True, "events": events},
        ]
    )
    tracker = make_tracker(api, [])
    store: Dict[str, Any] = {
        "{a}": MagicMock(id="w-a", url=HOOK_URL, secret="s", events=events),
        "{b}": MagicMock(id="w-b", url=HOOK_URL, secret="s", events=events),
    }
    db = MagicMock()
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={})
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        _crud_with_store(crud, store)
        assert await tracker.register_webhook(
            db=db, project=project, webhook_url=HOOK_URL, secret="s"
        )
    assert set(store) == {"{a}"}
    assert [h["uuid"] for h in api.hooks] == ["{a}"]
    db.commit.assert_called()


async def test_overlapping_registration_converges_on_one_hook() -> None:
    """A racer's hook appearing between list and POST is collapsed."""
    events = list(BITBUCKET_WEBHOOK_EVENTS)

    class RacingHooks(FakeBitbucketHooks):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                # The other registration lands first, with a lower uuid.
                self.hooks.append(
                    {"uuid": "{0-racer}", "url": HOOK_URL, "events": events}
                )
            return super().__call__(request)

    api = RacingHooks([])
    requests: List[httpx.Request] = []
    tracker = make_tracker(api, requests)
    store: Dict[str, Any] = {}
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={})
    with patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud:
        _crud_with_store(crud, store)
        assert await tracker.register_webhook(
            db=MagicMock(), project=project, webhook_url=HOOK_URL, secret="s"
        )
    assert [h["uuid"] for h in api.hooks] == ["{0-racer}"]
    assert set(store) == {"{0-racer}"}
    assert [r.method for r in requests].count("DELETE") == 1


async def test_scanner_collapses_preexisting_duplicates(monkeypatch: Any) -> None:
    """The scan path reconciles even when a same-URL hook already exists."""
    from preloop.sync.scanner.core import TrackerClient, _process_organization

    monkeypatch.setenv("PRELOOP_URL", "https://preloop.test")
    events = list(BITBUCKET_WEBHOOK_EVENTS)
    url = "https://preloop.test/api/v1/private/webhooks/bitbucket/org-1"
    api = FakeBitbucketHooks(
        [
            {"uuid": "{a}", "url": url, "active": True, "events": events},
            {"uuid": "{b}", "url": url, "active": True, "events": events},
        ]
    )
    tracker = make_tracker(api, [])
    project = MagicMock(id="p-1", slug="ws/repo", meta_data={}, identifier="ws/repo")
    client = MagicMock(spec=TrackerClient)
    client.tracker = MagicMock(id="tracker-1", meta_data={})
    client.tracker_type = "bitbucket"
    client.client = tracker
    client.scan_projects = AsyncMock(return_value=[project])
    client.scan_issues = AsyncMock(return_value=([], 0))
    org = MagicMock(id="org-1", webhook_secret="s")
    store: Dict[str, Any] = {}
    with (
        patch("preloop.sync.trackers.bitbucket.crud_webhook") as crud,
        patch("preloop.sync.scanner.core.crud_organization"),
    ):
        _crud_with_store(crud, store)
        stats = await _process_organization(
            db=MagicMock(), client=client, org=org, since=None, force_update=False
        )
    assert stats["errors"] == 0
    assert len([h for h in api.hooks if h["url"] == url]) == 1
