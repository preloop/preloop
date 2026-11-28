"""The pre-check's GitHub reader (#2541): read-only, bounded, cached.

Every test runs against an ``httpx.MockTransport`` with synthetic responses;
nothing reaches a real tracker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

import httpx
import pytest

from preloop.services import flow_precheck_reader as reader_module
from preloop.services.flow_precheck_reader import (
    CAUSE_AUTH_FAILED,
    CAUSE_NOT_FOUND,
    CAUSE_RATE_LIMITED,
    CAUSE_REQUEST_BUDGET,
    CAUSE_SERVER_ERROR,
    CAUSE_UNREACHABLE,
    GitHubPrecheckReader,
    PrecheckBudget,
    PrecheckError,
)

REPO = "acme/app"

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    reader_module.RESULT_CACHE.clear()
    reader_module.ETAG_CACHE.clear()


def _pr_node(number: int, **overrides: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {
        "number": number,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "title": f"Change {number}",
        "isDraft": False,
        "updatedAt": "2026-01-01T07:00:00Z",
        "baseRefName": "main",
        "headRefOid": f"{number:07x}",
        "author": {"login": "jane-doe"},
        "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "labels": {"nodes": [{"name": "in-progress"}]},
        "reviewThreads": {"nodes": [{"isResolved": False}, {"isResolved": True}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
        "timelineItems": {
            "nodes": [
                {"createdAt": "2026-01-01T01:00:00Z", "label": {"name": "in-progress"}}
            ]
        },
    }
    node.update(overrides)
    return node


class Recorder:
    """A mock transport that records requests and replies from a handler."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.requests: List[httpx.Request] = []
        self._handler = handler
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _reader(
    recorder: Recorder, *, max_requests: int = 30, cache_ttl: float = 60.0
) -> GitHubPrecheckReader:
    return GitHubPrecheckReader(
        token="test-token",
        tracker_id="tracker-1",
        budget=PrecheckBudget(max_requests=max_requests, timeout_seconds=20),
        transport=recorder.transport,
        cache_ttl=cache_ttl,
    )


def _graphql_pages(*pages: List[Dict[str, Any]]) -> Callable:
    state = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = state["page"]
        state["page"] += 1
        nodes = pages[index]
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {
                                "hasNextPage": index + 1 < len(pages),
                                "endCursor": f"c{index}",
                            },
                            "nodes": nodes,
                        }
                    }
                }
            },
            headers={"x-ratelimit-remaining": "4000"},
        )

    return handler


async def test_pull_requests_come_from_one_graphql_query() -> None:
    recorder = Recorder(_graphql_pages([_pr_node(101), _pr_node(102, isDraft=True)]))
    page = await _reader(recorder).list_open_pull_requests_with_state(
        REPO, include_drafts=False
    )

    assert [pr.number for pr in page.items] == [101]
    pr = page.items[0]
    assert pr.unresolved_threads == 1
    assert pr.checks_state == "FAILURE"
    assert pr.labels == ("in-progress",)
    assert pr.label_applied_at["in-progress"] == datetime(
        2026, 1, 1, 1, tzinfo=timezone.utc
    )
    assert len(recorder.requests) == 1
    sent = json.loads(recorder.requests[0].content)
    assert sent["variables"] == {"owner": "acme", "name": "app", "cursor": None}
    assert recorder.requests[0].headers["authorization"] == "Bearer test-token"


async def test_pull_request_pages_follow_the_cursor() -> None:
    recorder = Recorder(_graphql_pages([_pr_node(1)], [_pr_node(2)]))
    page = await _reader(recorder).list_open_pull_requests_with_state(
        REPO, include_drafts=True
    )
    assert [pr.number for pr in page.items] == [1, 2]
    assert not page.truncated
    assert json.loads(recorder.requests[1].content)["variables"]["cursor"] == "c0"


async def test_only_get_and_graphql_queries_are_sent() -> None:
    """Every read: the verbs and paths the reader may use, nothing else."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/graphql":
            return _graphql_pages([_pr_node(7)])(request)
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if path.endswith("/status"):
            return httpx.Response(200, json={"sha": "abc", "statuses": []})
        if path.endswith("/required_status_checks"):
            return httpx.Response(404, json={})
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"total_count": 0, "workflow_runs": []})
        if path.endswith("/milestones"):
            return httpx.Response(200, json=[{"title": "1.1.0", "number": 4}])
        if path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    recorder = Recorder(handler)
    reader = _reader(recorder)
    await reader.list_open_pull_requests_with_state(REPO, include_drafts=False)
    await reader.default_branch_check_state(REPO, branch="", required_only=True)
    await reader.list_failed_ci_runs(
        REPO, since=datetime(2026, 1, 1, tzinfo=timezone.utc), branches=[], limit=5
    )
    await reader.list_issues(
        REPO, labels_all=["ready"], milestone="1.1.0", unassigned=True
    )

    for request in recorder.requests:
        if request.method == "POST":
            assert request.url.path == "/graphql"
            body = json.loads(request.content)
            assert body["query"].lstrip().startswith("query")
        else:
            assert request.method == "GET"
            assert request.url.path.startswith(f"/repos/{REPO}")
    assert {r.method for r in recorder.requests} == {"GET", "POST"}


async def test_a_mutation_is_refused_before_it_is_sent() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json={"data": {}}))
    with pytest.raises(ValueError, match="only sends GraphQL queries"):
        await _reader(recorder)._graphql(
            "mutation { closePullRequest(input: {}) { clientMutationId } }",
            {},
            what="x",
        )
    assert recorder.requests == []


def test_the_reader_has_no_write_method() -> None:
    """The transport surface is _get, _graphql and _send; nothing that writes."""
    public = {
        name
        for name in dir(GitHubPrecheckReader)
        if not name.startswith("_") and callable(getattr(GitHubPrecheckReader, name))
    }
    assert public == {
        "list_open_pull_requests_with_state",
        "default_branch_check_state",
        "list_failed_ci_runs",
        "list_issues",
    }


@pytest.mark.parametrize(
    "response,cause",
    [
        (httpx.Response(401, json={}), CAUSE_AUTH_FAILED),
        (httpx.Response(403, json={"message": "nope"}), CAUSE_AUTH_FAILED),
        (httpx.Response(404, json={}), CAUSE_NOT_FOUND),
        (httpx.Response(502, text="bad gateway"), CAUSE_SERVER_ERROR),
        (
            httpx.Response(403, json={}, headers={"retry-after": "30"}),
            CAUSE_RATE_LIMITED,
        ),
        (
            httpx.Response(429, json={}, headers={"x-ratelimit-remaining": "0"}),
            CAUSE_RATE_LIMITED,
        ),
        (
            httpx.Response(
                200,
                json={"default_branch": "main"},
                headers={"x-ratelimit-remaining": "12"},
            ),
            CAUSE_RATE_LIMITED,
        ),
    ],
)
async def test_failures_map_to_causes(response: httpx.Response, cause: str) -> None:
    recorder = Recorder(lambda request: response)
    with pytest.raises(PrecheckError) as caught:
        await _reader(recorder).default_branch_check_state(
            REPO, branch="", required_only=False
        )
    assert caught.value.cause == cause
    assert "test-token" not in caught.value.message
    # Stopped, not retried.
    assert len(recorder.requests) == 1


async def test_transport_failure_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(PrecheckError) as caught:
        await _reader(Recorder(handler)).list_issues(
            REPO, labels_all=[], milestone=None, unassigned=False
        )
    assert caught.value.cause == CAUSE_UNREACHABLE


async def test_the_request_cap_stops_the_evaluation() -> None:
    recorder = Recorder(_graphql_pages([_pr_node(1)], [_pr_node(2)], [_pr_node(3)]))
    with pytest.raises(PrecheckError) as caught:
        await _reader(recorder, max_requests=2).list_open_pull_requests_with_state(
            REPO, include_drafts=True
        )
    assert caught.value.cause == CAUSE_REQUEST_BUDGET
    assert len(recorder.requests) == 2


async def test_a_conditional_get_reuses_the_cached_body() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            json=[{"number": 5, "title": "t", "labels": []}],
            headers={"etag": '"v1"'},
        )

    recorder = Recorder(handler)
    first = await _reader(recorder, cache_ttl=0).list_issues(
        REPO, labels_all=[], milestone=None, unassigned=False
    )
    second = await _reader(recorder, cache_ttl=0).list_issues(
        REPO, labels_all=[], milestone=None, unassigned=False
    )
    assert [i.number for i in first.items] == [i.number for i in second.items] == [5]
    assert recorder.requests[1].headers["if-none-match"] == '"v1"'


async def test_the_result_cache_shares_one_fetch() -> None:
    recorder = Recorder(_graphql_pages([_pr_node(1)]))
    await _reader(recorder).list_open_pull_requests_with_state(
        REPO, include_drafts=True
    )
    await _reader(recorder).list_open_pull_requests_with_state(
        REPO, include_drafts=False
    )
    assert len(recorder.requests) == 1


async def test_the_cache_is_scoped_by_credential() -> None:
    recorder = Recorder(lambda request: _graphql_pages([_pr_node(1)])(request))
    await _reader(recorder).list_open_pull_requests_with_state(
        REPO, include_drafts=True
    )
    other = GitHubPrecheckReader(
        token="another-token",
        tracker_id="tracker-1",
        budget=PrecheckBudget(),
        transport=recorder.transport,
    )
    await other.list_open_pull_requests_with_state(REPO, include_drafts=True)
    assert len(recorder.requests) == 2


async def test_required_checks_narrow_the_branch_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "lint",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://example.com/runs/1",
                            "head_sha": "abc",
                        },
                        {"name": "tests", "status": "in_progress"},
                    ]
                },
            )
        if path.endswith("/status"):
            return httpx.Response(200, json={"sha": "abc", "statuses": []})
        return httpx.Response(200, json={"contexts": ["tests"], "checks": []})

    state = await _reader(Recorder(handler)).default_branch_check_state(
        REPO, branch="main", required_only=True
    )
    assert state.required == ("tests",)
    assert state.head_sha == "abc"
    assert [(c.name, c.state) for c in state.checks] == [
        ("lint", "failure"),
        ("tests", "pending"),
    ]


async def test_failed_runs_are_read_per_branch_newest_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        branch = request.url.params.get("branch")
        created = "2026-01-01T09:00:00Z" if branch == "main" else "2026-01-01T10:00:00Z"
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 11 if branch == "main" else 12,
                        "name": "CI",
                        "html_url": "https://example.com/r",
                        "head_branch": branch,
                        "created_at": created,
                    }
                ],
            },
        )

    recorder = Recorder(handler)
    page = await _reader(recorder).list_failed_ci_runs(
        REPO,
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        branches=["main", "release"],
        limit=10,
    )
    assert [run.id for run in page.items] == [12, 11]
    assert recorder.requests[0].url.params["created"] == ">=2026-01-01T00:00:00Z"
    assert recorder.requests[0].url.params["status"] == "failure"


async def test_issues_skip_pull_requests_and_unknown_milestones() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/milestones"):
            return httpx.Response(200, json=[{"title": "other", "number": 1}])
        return httpx.Response(200, json=[])

    recorder = Recorder(handler)
    page = await _reader(recorder).list_issues(
        REPO, labels_all=[], milestone="1.1.0", unassigned=False
    )
    assert page.items == ()
    assert len(recorder.requests) == 1

    def issues(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"number": 1, "title": "Issue", "labels": [{"name": "ready"}]},
                {"number": 2, "title": "PR", "pull_request": {"url": "x"}},
            ],
        )

    page = await _reader(Recorder(issues)).list_issues(
        REPO, labels_all=["ready"], milestone=None, unassigned=True
    )
    assert [issue.number for issue in page.items] == [1]
