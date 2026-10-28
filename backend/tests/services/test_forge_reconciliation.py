"""Forge reconciliation: merged vs closed, conditional reads, Retry-After (#2017)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import gitlab
import httpx
import pytest

from preloop.services import flow_feedback
from preloop.services.flow_feedback import decide, feedback_retry_delay
from preloop.services.flow_feedback_provider import FeedbackProvider, FeedbackState
from preloop.services.pull_request_refresh import (
    forge_facts,
    parse_pr_key,
    pull_request_url,
)
from preloop.sync.exceptions import TrackerRateLimitError, TrackerResponseError
from preloop.sync.trackers import github as github_module
from preloop.sync.trackers.bitbucket import BitbucketTracker
from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.trackers.gitlab import GitLabTracker
from preloop.sync.trackers.utils import parse_retry_after, rate_limit_delay

MERGED_AT = "2026-10-01T12:00:00Z"
HEAD = "a" * 40


def binding(provider: str, repository_id: str = "123") -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider, repository_id=repository_id, pr_number="7", policy={}
    )


# --- providers: merged vs closed --------------------------------------------


def github_provider(pr: dict[str, Any]) -> FeedbackProvider:
    body = {"head": {"sha": HEAD}, "base": {"repo": {"id": 123}}, **pr}
    return FeedbackProvider(
        SimpleNamespace(_request=AsyncMock(return_value=body)), binding("github")
    )


def gitlab_provider(mr: dict[str, Any]) -> FeedbackProvider:
    body = {"project_id": 123, "sha": HEAD, **mr}
    return FeedbackProvider(
        SimpleNamespace(
            conditional_get=AsyncMock(return_value=SimpleNamespace(body=body)),
        ),
        binding("gitlab"),
    )


def bitbucket_provider(pr: dict[str, Any]) -> FeedbackProvider:
    body = {
        "destination": {"repository": {"full_name": "ws/repo"}},
        "source": {"commit": {"hash": HEAD}},
        **pr,
    }
    response = SimpleNamespace(json=lambda: body)
    return FeedbackProvider(
        SimpleNamespace(_request=AsyncMock(return_value=response)),
        binding("bitbucket", "ws/repo"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "merged"),
    [
        (
            github_provider(
                {"state": "closed", "merged": True, "merged_at": MERGED_AT}
            ),
            True,
        ),
        (
            github_provider({"state": "closed", "merged": False, "merged_at": None}),
            False,
        ),
        (gitlab_provider({"state": "merged", "merged_at": MERGED_AT}), True),
        (gitlab_provider({"state": "closed", "merged_at": None}), False),
        (bitbucket_provider({"state": "MERGED", "updated_on": MERGED_AT}), True),
        (bitbucket_provider({"state": "DECLINED", "updated_on": MERGED_AT}), False),
        (bitbucket_provider({"state": "SUPERSEDED"}), False),
    ],
    ids=[
        "github-merged",
        "github-closed",
        "gitlab-merged",
        "gitlab-closed",
        "bitbucket-merged",
        "bitbucket-declined",
        "bitbucket-superseded",
    ],
)
async def test_each_forge_reports_merged_apart_from_closed(
    provider: FeedbackProvider, merged: bool
) -> None:
    state = await provider.read()

    assert state.closed is True
    assert state.merged is merged
    assert state.merged_at == (MERGED_AT if merged else None)
    assert decide(None, state, [], now=datetime(2026, 10, 2)) == (
        "closed",
        "pr_merged" if merged else "pr_closed",
    )


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("github", {"state": "open", "merged": False, "head": {"sha": HEAD}}),
        ("gitlab", {"state": "opened", "sha": HEAD}),
        ("bitbucket", {"state": "OPEN", "source": {"commit": {"hash": HEAD}}}),
    ],
)
def test_open_pull_requests_are_neither_closed_nor_merged(
    provider: str, body: dict[str, Any]
) -> None:
    assert forge_facts(provider, body) == {
        "state": "open",
        "head_sha": HEAD,
        "merged_at": None,
    }


# --- conditional requests and Retry-After per client ------------------------


def patched_github(
    monkeypatch: pytest.MonkeyPatch, handler: Any, seen: list[httpx.Request]
) -> GitHubTracker:
    real = httpx.AsyncClient

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        github_module.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(record), **kwargs),
    )
    return GitHubTracker(str(uuid.uuid4()), "token", {})


@pytest.mark.asyncio
async def test_github_conditional_read_sends_etag_and_reports_304(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    tracker = patched_github(
        monkeypatch, lambda request: httpx.Response(304, headers={}), seen
    )

    response = await tracker.conditional_get("/repos/o/r/pulls/7", '"v1"')

    assert seen[0].headers["If-None-Match"] == '"v1"'
    assert response.not_modified and response.body is None
    assert response.etag == '"v1"'


@pytest.mark.asyncio
async def test_github_conditional_read_returns_body_and_new_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    tracker = patched_github(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"state": "open"}, headers={"ETag": '"v2"'}
        ),
        seen,
    )

    response = await tracker.conditional_get("/repos/o/r/pulls/7", None)

    assert "If-None-Match" not in seen[0].headers
    assert (response.status, response.body, response.etag) == (
        200,
        {"state": "open"},
        '"v2"',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "delay"),
    [
        (429, {"Retry-After": "90"}, 90.0),
        (403, {"Retry-After": "30"}, 30.0),
        (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1120"}, 120.0),
    ],
    ids=["429", "secondary-limit", "primary-limit"],
)
async def test_github_throttling_carries_the_advised_wait(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    delay: float,
) -> None:
    monkeypatch.setattr("preloop.sync.trackers.utils.time.time", lambda: 1000.0)
    tracker = patched_github(
        monkeypatch, lambda request: httpx.Response(status, headers=headers), []
    )

    with pytest.raises(TrackerRateLimitError) as raised:
        await tracker.conditional_get("/repos/o/r/pulls/7", None)
    assert raised.value.retry_after == delay
    # The plain request path used by the feedback loop tags the same wait.
    with pytest.raises(TrackerResponseError) as plain:
        await tracker._request("GET", "/repos/o/r/pulls/7")
    assert rate_limit_delay(plain.value) == delay


@pytest.mark.asyncio
async def test_github_permission_403_is_not_throttling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = patched_github(
        monkeypatch, lambda request: httpx.Response(403, json={"message": "no"}), []
    )

    with pytest.raises(TrackerResponseError) as raised:
        await tracker.conditional_get("/repos/o/r/pulls/7", None)
    assert not isinstance(raised.value, TrackerRateLimitError)
    assert rate_limit_delay(raised.value) is None


def gitlab_tracker(response: Any, seen: list[dict[str, Any]]) -> GitLabTracker:
    tracker = GitLabTracker(
        str(uuid.uuid4()),
        "token",
        {"url": "https://gitlab.example.com"},
        initialize_client=False,
    )
    tracker.gl = gitlab.Gitlab("https://gitlab.example.com", private_token="token")

    def request(method: str, url: str, **options: Any) -> Any:
        seen.append({"method": method, "url": url, **options})
        return response

    tracker.gl.session = MagicMock(request=request)
    return tracker


@pytest.mark.asyncio
async def test_gitlab_conditional_read_sends_etag_and_reports_304() -> None:
    seen: list[dict[str, Any]] = []
    tracker = gitlab_tracker(
        SimpleNamespace(status_code=304, headers={"ETag": '"v3"'}), seen
    )

    response = await tracker.conditional_get("/projects/g%2Fp/merge_requests/7", '"v2"')

    assert seen[0]["url"] == (
        "https://gitlab.example.com/api/v4/projects/g%2Fp/merge_requests/7"
    )
    assert seen[0]["headers"]["If-None-Match"] == '"v2"'
    assert seen[0]["auth"] is tracker.gl._auth is not None
    assert response.not_modified and response.etag == '"v3"'
    # The client's shared headers are not left carrying the validator.
    assert "If-None-Match" not in tracker.gl.headers


@pytest.mark.asyncio
async def test_gitlab_throttling_carries_the_advised_wait() -> None:
    tracker = gitlab_tracker(
        SimpleNamespace(status_code=429, headers={"Retry-After": "45"}), []
    )

    with pytest.raises(TrackerRateLimitError) as raised:
        await tracker.conditional_get("/projects/1/merge_requests/7", None)
    assert raised.value.retry_after == 45.0


def bitbucket_tracker(handler: Any, seen: list[httpx.Request]) -> BitbucketTracker:
    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return BitbucketTracker(
        "tracker-1",
        "token",
        {"workspace": "ws", "repository": "repo"},
        transport=httpx.MockTransport(record),
    )


@pytest.mark.asyncio
async def test_bitbucket_conditional_read_sends_etag_and_reports_304() -> None:
    seen: list[httpx.Request] = []
    tracker = bitbucket_tracker(lambda request: httpx.Response(304), seen)

    response = await tracker.conditional_get(
        "/repositories/ws/repo/pullrequests/7", '"v1"'
    )

    assert seen[0].headers["If-None-Match"] == '"v1"'
    assert response.not_modified and response.etag == '"v1"'


@pytest.mark.asyncio
async def test_bitbucket_throttling_carries_the_advised_wait() -> None:
    tracker = bitbucket_tracker(
        lambda request: httpx.Response(429, headers={"Retry-After": "12"}), []
    )

    with pytest.raises(TrackerRateLimitError) as raised:
        await tracker.conditional_get("/repositories/ws/repo/pullrequests/7", None)
    assert raised.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_bitbucket_pr_without_validators_degrades_to_plain_reads() -> None:
    seen: list[httpx.Request] = []
    tracker = bitbucket_tracker(
        lambda request: httpx.Response(
            200,
            json={
                "state": "OPEN",
                "updated_on": MERGED_AT,
                "source": {"commit": {"hash": HEAD}},
            },
        ),
        seen,
    )
    path = "/repositories/ws/repo/pullrequests/7"
    first = await tracker.conditional_get(path, '"obsolete"')
    second = await tracker.conditional_get(path, first.etag)
    assert first.etag is second.etag is None
    assert first.status == second.status == 200
    assert not second.not_modified
    assert forge_facts("bitbucket", second.body)["head_sha"] == HEAD
    assert "If-None-Match" not in seen[1].headers
    assert "If-Modified-Since" not in seen[1].headers


@pytest.mark.asyncio
async def test_bitbucket_uses_an_etag_only_when_the_server_supplies_one() -> None:
    seen: list[httpx.Request] = []
    tracker = bitbucket_tracker(
        lambda request: (
            httpx.Response(304)
            if request.headers.get("If-None-Match") == '"v1"'
            else httpx.Response(200, json={"state": "OPEN"}, headers={"ETag": '"v1"'})
        ),
        seen,
    )
    path = "/repositories/ws/repo/pullrequests/7"
    first = await tracker.conditional_get(path, None)
    second = await tracker.conditional_get(path, first.etag)
    assert first.etag == second.etag == '"v1"'
    assert second.not_modified


def test_retry_after_accepts_seconds_dates_and_reset_headers() -> None:
    now = 1_700_000_000.0
    assert parse_retry_after({"Retry-After": "5"}, now=now) == 5.0
    assert parse_retry_after(
        {"retry-after": "Tue, 14 Nov 2023 22:14:20 GMT"}, now=now
    ) == pytest.approx(60.0)
    assert parse_retry_after({"RateLimit-Reset": str(int(now) + 60)}, now=now) == 60
    assert parse_retry_after({"Retry-After": "-3"}, now=now) == 0.0
    assert parse_retry_after({}, now=now) is None
    assert rate_limit_delay(TrackerRateLimitError("x")) == 0.0
    assert rate_limit_delay(TrackerResponseError("x", status_code=500)) is None


# --- decide(): every stop reason, unchanged except the split ----------------

NOW = datetime(2026, 10, 25, 12, 0, 0)


def thread(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "expires_at": NOW + timedelta(days=1),
        "cursor": {},
        "policy": {},
        "turns": 0,
        "cost": 0.0,
        "no_progress": 0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def item(kind: str = "comment", head_sha: str | None = "head") -> SimpleNamespace:
    return SimpleNamespace(kind=kind, head_sha=head_sha)


def gate(**changes: Any) -> FeedbackState:
    return FeedbackState("head", **changes)


INFRA = {"failure_reason": "runner_system_failure", "status": "failed"}
TIMEOUT = {"failure_reason": "job_execution_timeout", "status": "failed"}
PASSED = {"checks_passed": True, "reviews_passed": True}

# (case, thread, state, pending, outcome, reason, reason before the split)
GOLDEN: list[tuple[str, Any, FeedbackState, list[Any], str, Any, Any]] = [
    (
        "merged",
        thread(),
        gate(closed=True, merged=True),
        [],
        "closed",
        "pr_merged",
        "pr_closed_or_merged",
    ),
    (
        "closed",
        thread(),
        gate(closed=True),
        [],
        "closed",
        "pr_closed",
        "pr_closed_or_merged",
    ),
    (
        "merged-after-expiry",
        thread(expires_at=NOW),
        gate(closed=True, merged=True),
        [],
        "closed",
        "pr_merged",
        "pr_closed_or_merged",
    ),
    (
        "expired",
        thread(expires_at=NOW),
        gate(),
        [],
        "expired",
        "subscription_expired",
        "subscription_expired",
    ),
    (
        "blocked",
        thread(),
        gate(blocked_reason="ci_permission_failure"),
        [],
        "blocked",
        "ci_permission_failure",
        "ci_permission_failure",
    ),
    (
        "ci-deadline",
        thread(cursor={"ci_wait_started": (NOW - timedelta(hours=2)).isoformat()}),
        gate(checks_pending=True),
        [],
        "blocked",
        "ci_deadline_exceeded",
        "ci_deadline_exceeded",
    ),
    (
        "ci-pending",
        thread(),
        gate(checks_pending=True),
        [],
        "waiting",
        "ci_pending",
        "ci_pending",
    ),
    ("ready", thread(), gate(**PASSED), [], "ready", None, None),
    (
        "ready-ignores-stale-and-signals",
        thread(),
        gate(**PASSED),
        [item(head_sha="old"), item(kind="signal")],
        "ready",
        None,
        None,
    ),
    (
        "infra-retry",
        thread(),
        gate(infra_failures=[INFRA]),
        [],
        "waiting",
        "ci_infrastructure_failure_retry",
        "ci_infrastructure_failure_retry",
    ),
    (
        "timeout-retry",
        thread(),
        gate(infra_failures=[TIMEOUT]),
        [],
        "waiting",
        "ci_timeout_retry",
        "ci_timeout_retry",
    ),
    (
        "infra-exhausted",
        thread(cursor={"ci_infra_attempts": 4}),
        gate(infra_failures=[INFRA]),
        [],
        "blocked",
        "ci_infrastructure_failure",
        "ci_infrastructure_failure",
    ),
    (
        "timeout-exhausted",
        thread(cursor={"ci_infra_attempts": 4}),
        gate(infra_failures=[TIMEOUT]),
        [],
        "blocked",
        "ci_timeout",
        "ci_timeout",
    ),
    (
        "turns",
        thread(turns=5),
        gate(),
        [item()],
        "stopped",
        "turn_budget_exhausted",
        "turn_budget_exhausted",
    ),
    (
        "cost",
        thread(cost=100.0),
        gate(),
        [item()],
        "stopped",
        "cost_budget_exhausted",
        "cost_budget_exhausted",
    ),
    (
        "no-progress",
        thread(no_progress=2),
        gate(),
        [item()],
        "stopped",
        "no_progress",
        "no_progress",
    ),
    ("repair", thread(), gate(), [item()], "repair", None, None),
    (
        "repair-early",
        thread(policy={"repair_early": True}),
        gate(checks_pending=True),
        [item()],
        "repair",
        None,
        None,
    ),
    (
        "requirements-unknown",
        thread(),
        gate(blocked_reason="repository_requirements_unavailable"),
        [],
        "blocked",
        "repository_requirements_unavailable",
        "repository_requirements_unavailable",
    ),
    (
        "pending-review",
        thread(),
        gate(checks_passed=True),
        [],
        "waiting",
        "review_or_ci_pending",
        "review_or_ci_pending",
    ),
]
# Provider gate failures pass through decide unchanged. Keep their complete
# vocabulary explicit, including dynamically constructed CI outcome reasons.
PROVIDER_STOP_REASONS = (
    "required_checks_missing",
    "ci_permission_required",
    "ci_failure_unclassified",
    "ci_unknown_outcome",
    "ci_cancelled",
    "ci_canceled",
    "ci_action_required",
    "ci_manual",
    "ci_error",
    "ci_stale",
    "provider_page_limit",
    "repository_gate_requires_external_evidence",
    "head_changed_during_reconciliation",
)
GOLDEN.extend(
    (reason, thread(), gate(blocked_reason=reason), [], "blocked", reason, reason)
    for reason in PROVIDER_STOP_REASONS
)
SPLIT = {"pr_merged": "pr_closed_or_merged", "pr_closed": "pr_closed_or_merged"}


@pytest.mark.parametrize(
    ("case", "subject", "state", "pending", "outcome", "reason", "legacy"),
    GOLDEN,
    ids=[row[0] for row in GOLDEN],
)
def test_decide_golden_table(
    case: str,
    subject: Any,
    state: FeedbackState,
    pending: list[Any],
    outcome: str,
    reason: Any,
    legacy: Any,
) -> None:
    decided = decide(subject, state, pending, now=NOW)

    assert decided == (outcome, reason)
    # Folding the split back gives exactly the reason recorded before it.
    assert SPLIT.get(decided[1], decided[1]) == legacy


def test_golden_table_covers_every_stop_reason_decide_returns() -> None:
    import ast
    import inspect

    source = inspect.getsource(flow_feedback.decide)
    literal = {
        node.elts[1].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Tuple)
        and len(node.elts) == 2
        and isinstance(node.elts[1], ast.Constant)
        and isinstance(node.elts[1].value, str)
    }
    covered = {row[5] for row in GOLDEN}
    assert literal <= covered
    # These reasons are computed, so the literal-return scan cannot see them.
    assert {
        "pr_merged",
        "pr_closed",
        "ci_timeout",
        "ci_timeout_retry",
        "ci_infrastructure_failure",
        "ci_infrastructure_failure_retry",
    } <= covered


# --- Retry-After on the feedback tick ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "delay"),
    [
        (TrackerRateLimitError("slow down", retry_after=120), 120.0),
        (TrackerResponseError("x", status_code=429), 30.0),
        (TrackerResponseError("x", status_code=403, retry_after=86400), 3600.0),
    ],
    ids=["hinted", "unhinted-floor", "ceiling"],
)
async def test_throttled_reconciliation_waits_out_retry_after(
    error: Exception, delay: float
) -> None:
    claim = (uuid.uuid4(), uuid.uuid4())
    crud = MagicMock()
    crud.unregistered_publications.return_value = []
    crud.stopped_for_no_progress.return_value = []
    crud.claim_due.return_value = [claim]
    with (
        patch.object(flow_feedback, "crud_flow_feedback", crud),
        patch.object(flow_feedback, "_reconcile", AsyncMock(side_effect=error)),
    ):
        await flow_feedback.run_feedback_tick(MagicMock(), now=NOW)

    crud.update.assert_called_once_with(
        crud.update.call_args.args[0],
        *claim,
        changes={"state": "waiting", "stop_reason": "provider_rate_limited"},
        now=NOW,
        delay_seconds=delay,
    )


@pytest.mark.asyncio
async def test_other_provider_errors_still_block() -> None:
    claim = (uuid.uuid4(), uuid.uuid4())
    crud = MagicMock()
    crud.unregistered_publications.return_value = []
    crud.stopped_for_no_progress.return_value = []
    crud.claim_due.return_value = [claim]
    with (
        patch.object(flow_feedback, "crud_flow_feedback", crud),
        patch.object(
            flow_feedback,
            "_reconcile",
            AsyncMock(side_effect=TrackerResponseError("x", status_code=500)),
        ),
    ):
        await flow_feedback.run_feedback_tick(MagicMock(), now=NOW)

    assert crud.update.call_args.kwargs["changes"] == {
        "state": "blocked",
        "stop_reason": "provider_or_dispatch_unavailable",
    }
    assert feedback_retry_delay(5) == 30.0


# --- pull request references -----------------------------------------------


@pytest.mark.parametrize(
    ("pr_key", "expected"),
    [
        (
            "https://github.com/acme/widgets/pull/12",
            ("github", "github.com", "/repos/acme/widgets/pulls/12"),
        ),
        (
            "https://gitlab.example.com/group/sub/app/-/merge_requests/3",
            (
                "gitlab",
                "gitlab.example.com",
                "/projects/group%2Fsub%2Fapp/merge_requests/3",
            ),
        ),
        (
            "https://bitbucket.org/ws/repo/pull-requests/4",
            ("bitbucket", "bitbucket.org", "/repositories/ws/repo/pullrequests/4"),
        ),
        ("https://github.com/acme/widgets/issues/12", None),
        ("https://example.com/acme", None),
    ],
)
def test_pr_keys_map_to_forge_reads(pr_key: str, expected: Any) -> None:
    ref = parse_pr_key(pr_key)
    assert (None if ref is None else (ref.provider, ref.host, ref.api_path)) == (
        expected
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"pull_request": {"html_url": "https://github.com/acme/widgets/pull/12"}},
        {
            "issue": {},
            "pull_request": {"html_url": "https://github.com/acme/widgets/pull/12/"},
        },
    ],
)
def test_webhook_payloads_name_their_pull_request(payload: dict[str, Any]) -> None:
    assert pull_request_url(payload) == "https://github.com/acme/widgets/pull/12"


def test_gitlab_and_bitbucket_payloads_name_their_pull_request() -> None:
    assert (
        pull_request_url(
            {
                "object_attributes": {
                    "url": "https://gitlab.example.com/g/p/-/merge_requests/3"
                }
            }
        )
        == "https://gitlab.example.com/g/p/-/merge_requests/3"
    )
    assert (
        pull_request_url(
            {
                "pullrequest": {
                    "links": {
                        "html": {
                            "href": "https://bitbucket.org/ws/repo/pull-requests/4"
                        }
                    }
                }
            }
        )
        == "https://bitbucket.org/ws/repo/pull-requests/4"
    )
    assert (
        pull_request_url({"issue": {"html_url": "https://github.com/a/b/issues/1"}})
        is None
    )


@pytest.mark.asyncio
async def test_gitlab_provider_retry_after_reaches_feedback_tick() -> None:
    tracker = gitlab_tracker(
        SimpleNamespace(status_code=429, headers={"Retry-After": "180"}), []
    )
    provider = FeedbackProvider(tracker, binding("gitlab"))
    claim = (uuid.uuid4(), uuid.uuid4())
    crud = MagicMock()
    crud.unregistered_publications.return_value = []
    crud.stopped_for_no_progress.return_value = []
    crud.claim_due.return_value = [claim]

    async def reconcile(*args: Any, **kwargs: Any) -> None:
        await provider.read()

    with (
        patch.object(flow_feedback, "crud_flow_feedback", crud),
        patch.object(flow_feedback, "_reconcile", reconcile),
    ):
        await flow_feedback.run_feedback_tick(MagicMock(), now=NOW)
    assert crud.update.call_args.kwargs["delay_seconds"] == 180.0
    assert (
        crud.update.call_args.kwargs["changes"]["stop_reason"]
        == "provider_rate_limited"
    )


@pytest.mark.asyncio
async def test_gitlab_feedback_trace_uses_header_aware_stream() -> None:
    seen: list[dict[str, Any]] = []
    raw = SimpleNamespace(status_code=200, headers={})
    tracker = gitlab_tracker(raw, seen)
    response = await tracker.conditional_get(
        "/projects/1/jobs/2/trace", None, streamed=True
    )
    assert response.body is raw
    assert seen[0]["stream"] is True
