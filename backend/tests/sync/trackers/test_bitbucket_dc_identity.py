"""End-to-end identity preflight tests; no adapter methods are mocked."""

import json
from typing import Any

import httpx
import pytest

from preloop.sync.trackers.bitbucket_dc import BitbucketDCTracker
from preloop.sync.exceptions import TrackerResponseError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "comment",
        "task",
        "create_pr",
        "approve",
        "request_changes",
        "status",
        "read",
        "resolve_task",
    ],
)
@pytest.mark.parametrize("found", [False, True])
async def test_reused_slug_never_receives_a_bound_operation(
    monkeypatch: pytest.MonkeyPatch, operation: str, found: bool
) -> None:
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv(
        "PRELOOP_BITBUCKET_DC_INSTANCES", '["https://bitbucket.example.com"]'
    )
    calls: list[httpx.Request] = []
    old = "/rest/api/1.0/projects/PRJ/repos/old"
    new = "/rest/api/1.0/projects/PRJ/repos/new"
    repo = {"id": 17, "slug": "new", "project": {"key": "PRJ"}}
    pr = {
        "id": 1,
        "version": 1,
        "state": "OPEN",
        "title": "Review",
        "fromRef": {"id": "refs/heads/topic", "repository": repo},
        "toRef": {"id": "refs/heads/main", "repository": repo},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == old and request.method == "GET":
            return httpx.Response(
                200, json={"id": 99, "slug": "old", "project": {"key": "PRJ"}}
            )
        if path == "/rest/api/1.0/projects/PRJ/repos":
            return httpx.Response(
                200, json={"values": [repo] if found else [], "isLastPage": True}
            )
        if path == new and request.method == "GET":
            return httpx.Response(200, json=repo)
        assert path.startswith(new + "/"), (
            "No operation may contact the replacement repository"
        )
        if "/participants/" in path:
            return httpx.Response(
                200, json={"status": json.loads(request.content)["status"]}
            )
        if path.endswith("/builds"):
            return httpx.Response(204)
        if path.endswith("/comments/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "version": 1,
                    "text": "task",
                    "severity": "BLOCKER",
                    "state": "OPEN",
                },
            )
        if path.endswith("/comments") or path.endswith("/blocker-comments"):
            return httpx.Response(
                201,
                json={
                    "id": 2,
                    "version": 1,
                    "text": "confidential",
                    "severity": "BLOCKER",
                },
            )
        return httpx.Response(200, json=pr)

    tracker = BitbucketDCTracker(
        "fixture",
        "synthetic-pat",
        {
            "instance_url": "https://bitbucket.example.com",
            "project_key": "PRJ",
            "repository_slug": "old",
            "repository_id": 17,
            "username": "jane",
        },
        transport=httpx.MockTransport(handler),
    )

    async def act() -> Any:
        if operation == "comment":
            return await tracker.add_pull_request_comment(1, "confidential")
        if operation == "task":
            return await tracker.create_pull_request_task(1, "confidential")
        if operation == "create_pr":
            return await tracker.create_pull_request("confidential", "topic", "main")
        if operation == "approve":
            return await tracker.set_approval(1, True)
        if operation == "request_changes":
            return await tracker.set_changes_requested(1, True)
        if operation == "status":
            return await tracker.create_commit_status("a" * 40, "success")
        if operation == "resolve_task":
            return await tracker.resolve_pull_request_task(1, 2)
        return await tracker.get_pull_request(1)

    if found:
        await act()
        assert tracker.repository_id == 17
        assert tracker.repository_slug == "new"
        assert any(r.url.path.startswith(new + "/") for r in calls)
    else:
        with pytest.raises(TrackerResponseError):
            await act()
        assert all(r.method == "GET" for r in calls)
    assert calls[0].method == "GET" and calls[0].url.path == old
    assert not any(r.url.path.startswith(old + "/") for r in calls)
    for request in calls:
        if request.method == "POST" and request.url.path.endswith("/pull-requests"):
            payload = json.loads(request.content)
            for ref in ("fromRef", "toRef"):
                assert payload[ref]["repository"]["id"] == 17
                assert payload[ref]["repository"]["slug"] == "new"


@pytest.mark.asyncio
async def test_identity_preflight_is_not_cached_between_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv(
        "PRELOOP_BITBUCKET_DC_INSTANCES", '["https://bitbucket.example.com"]'
    )
    writes: list[httpx.Request] = []
    identity_reads: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repos/old"):
            identity_reads.append(request)
            return httpx.Response(
                200,
                json={
                    "id": 99 if writes else 17,
                    "slug": "old",
                    "project": {"key": "PRJ"},
                },
            )
        if request.url.path.endswith("/repos"):
            return httpx.Response(200, json={"values": [], "isLastPage": True})
        assert request.method == "POST"
        writes.append(request)
        return httpx.Response(201, json={"id": 2, "text": "first"})

    tracker = BitbucketDCTracker(
        "fixture",
        "synthetic-pat",
        {
            "instance_url": "https://bitbucket.example.com",
            "project_key": "PRJ",
            "repository_slug": "old",
            "repository_id": 17,
        },
        transport=httpx.MockTransport(handle),
    )
    await tracker.add_pull_request_comment(1, "first")
    with pytest.raises(TrackerResponseError, match="no longer exists"):
        await tracker.add_pull_request_comment(1, "must not reach replacement")
    assert len(identity_reads) == 2
    assert len(writes) == 1
