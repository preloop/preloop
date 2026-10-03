"""Public adapter boundaries that must reject before sending credentials."""

from typing import Any

import httpx
import pytest

from preloop.sync.trackers.bitbucket_dc import (
    BitbucketDCTracker,
    BitbucketDCUnsupportedOperation,
)
from preloop.sync.exceptions import TrackerResponseError
from preloop.utils.bitbucket_dc import BitbucketDCIdentityError


@pytest.fixture
def bound(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BitbucketDCTracker, list[httpx.Request]]:
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv(
        "PRELOOP_BITBUCKET_DC_INSTANCES", '["https://bitbucket.example.com/stash"]'
    )
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    return BitbucketDCTracker(
        "test",
        "synthetic-pat",
        {
            "instance_url": "https://bitbucket.example.com/stash",
            "project_key": "PRJ",
            "repository_slug": "repo",
            "repository_id": 7,
        },
        transport=httpx.MockTransport(record),
    ), requests


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", ["OTHER/repo", "PRJ/other"])
async def test_bound_repository_cannot_be_overridden(
    bound: tuple, repository: str
) -> None:
    tracker, requests = bound
    with pytest.raises(BitbucketDCIdentityError):
        await tracker.get_pull_request(1, repo_full_name=repository)
    assert requests == [], (
        "reject before credentials cross the configured repository boundary"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported",
    [
        {"labels": ["blocked"]},
        {"assignees": ["jane"]},
        {"milestone": "1"},
        {"close_source_branch": True},
    ],
)
async def test_create_refuses_unsupported_options(
    bound: tuple, unsupported: dict[str, Any]
) -> None:
    tracker, requests = bound
    with pytest.raises(NotImplementedError):
        await tracker.create_pull_request("PR", "topic", "main", **unsupported)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../../../../admin",
        "projects/PRJ/repos/repo/pull-requests/1/merge%2f",
        "projects/PRJ/repos/repo/pull-requests/1/%6derge",
        "projects/PRJ/repos/repo/pull-requests/1/../2/decline",
        "application-properties?unexpected=1",
    ],
)
async def test_transport_rejects_path_aliases(bound: tuple, path: str) -> None:
    tracker, requests = bound
    with pytest.raises((TrackerResponseError, BitbucketDCUnsupportedOperation)):
        await tracker._request("POST", path)
    assert requests == []


@pytest.mark.asyncio
async def test_list_page_follows_noncontiguous_cursors(bound: tuple) -> None:
    tracker, requests = bound
    starts = {0: 7, 7: 23}

    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repos/repo"):
            return httpx.Response(
                200, json={"id": 7, "slug": "repo", "project": {"key": "PRJ"}}
            )
        requests.append(request)
        start = int(request.url.params["start"])
        result = {"values": [], "isLastPage": start == 23}
        if start in starts:
            result["nextPageStart"] = starts[start]
        return httpx.Response(200, json=result)

    tracker._transport = httpx.MockTransport(pages)
    result = await tracker.list_pull_requests(page=3, limit=10)
    assert [r.url.params["start"] for r in requests] == ["0", "7", "23"]
    assert result == {"items": [], "has_more": False}


@pytest.mark.asyncio
async def test_ranged_anchor_is_explicitly_unsupported(bound: tuple) -> None:
    tracker, requests = bound
    with pytest.raises(NotImplementedError, match="read-only"):
        await tracker.add_pull_request_comment(
            1, "Review", path="app.py", line=10, start_line=5
        )
    assert requests == []


@pytest.mark.asyncio
async def test_error_response_cannot_echo_pat(bound: tuple) -> None:
    tracker, requests = bound
    tracker._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, json={"errors": [{"message": "rejected synthetic-pat"}]}
        )
    )
    with pytest.raises(Exception) as exc:
        await tracker.get_pull_request(1)
    assert "synthetic-pat" not in str(exc.value)
    assert "[redacted]" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["bad", "1/2", "", 0, -1, True, 1.5])
@pytest.mark.parametrize("operation", ["read", "reply", "update", "delete"])
async def test_malformed_comment_ids_fail_before_io(bound, value, operation):
    tracker, requests = bound
    with pytest.raises(TrackerResponseError, match="Invalid comment id"):
        if operation == "read":
            await tracker.get_pull_request_comment(1, value)
        elif operation == "reply":
            await tracker.add_pull_request_comment(1, "reply", parent_id=value)
        elif operation == "update":
            await tracker.update_pull_request_comment(1, value, "edit")
        else:
            await tracker.delete_pull_request_comment(1, value)
    assert requests == []
