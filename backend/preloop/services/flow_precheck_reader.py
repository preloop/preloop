"""Read-only tracker reads for flow pre-checks (#2541).

The pre-check asks a tracker four questions before a flow run is dispatched.
This module answers them for GitHub. The reader is read-only by
construction: its only transport methods are :meth:`GitHubPrecheckReader._get`
(HTTP ``GET``) and :meth:`GitHubPrecheckReader._graphql` (a GraphQL *query*;
a document that declares a mutation or subscription is refused before it is
sent). It never reuses a tracker client, because those carry write methods.

Every request is charged to a :class:`PrecheckBudget`, which bounds the
request count and the wall clock of one evaluation. A response whose
remaining-quota header is under the floor, a ``Retry-After`` or a secondary
rate limit stops the evaluation (``rate_limited``) instead of waiting or
looping. REST reads send the ``ETag`` of the previous response for the same
URL, and a short in-process TTL cache lets flows that ask the same question
of the same repository in the same minute share one fetch.

The credential is resolved by the caller and held only in the request
headers; it is never part of a result, an error message or a log line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"

#: Default budgets of one evaluation; see ``flow_precheck`` for the settings.
DEFAULT_MAX_REQUESTS = 30
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CACHE_TTL_SECONDS = 60.0
#: Stop when the tracker reports fewer requests left than this.
DEFAULT_RATE_LIMIT_FLOOR = 200
#: Longest single request; the evaluation deadline still applies.
MAX_REQUEST_SECONDS = 10.0
#: Pages of open pull requests read per repository (50 each).
MAX_PULL_REQUEST_PAGES = 4
#: Pages of open issues read per repository (100 each).
MAX_ISSUE_PAGES = 5
#: Failed workflow runs scanned per branch, before the check's own filters.
FAILED_RUNS_SCAN = 100

#: Causes a pre-check error can name. Stable strings: they appear in the
#: execution's error message and in the decision document.
CAUSE_UNREACHABLE = "unreachable"
CAUSE_SERVER_ERROR = "server_error"
CAUSE_AUTH_FAILED = "auth_failed"
CAUSE_NOT_FOUND = "not_found"
CAUSE_RATE_LIMITED = "rate_limited"
CAUSE_TIMEOUT = "timeout"
CAUSE_REQUEST_BUDGET = "request_budget"
CAUSE_UNSUPPORTED = "unsupported"
CAUSE_NO_CREDENTIAL = "no_credential"
CAUSE_BAD_RESPONSE = "bad_response"
CAUSE_ENGINE_ERROR = "engine_error"

_WRITE_OPERATION = re.compile(r"^\s*(mutation|subscription)\b", re.IGNORECASE)


class PrecheckError(Exception):
    """A pre-check could not answer; names the cause, never the credential."""

    def __init__(self, cause: str, message: str, *, check: Optional[str] = None):
        """Build the error.

        Args:
            cause: One of the ``CAUSE_*`` constants.
            message: Human readable detail, free of credentials.
            check: Id of the check that was being evaluated, when known.
        """
        super().__init__(message)
        self.cause = cause
        self.message = message
        self.check = check


class PrecheckBudget:
    """Request count and wall-clock bounds shared by one evaluation."""

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the budget now.

        Args:
            max_requests: Most tracker requests the evaluation may send.
            timeout_seconds: Wall clock the evaluation may take.
            clock: Monotonic clock, injectable for tests.
        """
        self.max_requests = max_requests
        self.requests = 0
        self._clock = clock
        self.deadline = clock() + timeout_seconds

    def remaining_seconds(self) -> float:
        """Seconds left before the deadline, never negative."""
        return max(0.0, self.deadline - self._clock())

    def spend(self) -> None:
        """Charge one request.

        Raises:
            PrecheckError: The request cap or the deadline is reached.
        """
        if self.remaining_seconds() <= 0:
            raise PrecheckError(CAUSE_TIMEOUT, "pre-check ran out of time")
        if self.requests >= self.max_requests:
            raise PrecheckError(
                CAUSE_REQUEST_BUDGET,
                f"pre-check used its {self.max_requests} tracker requests",
            )
        self.requests += 1


class _TTLCache:
    """A small in-process cache with per-entry expiry."""

    def __init__(self, max_entries: int = 512) -> None:
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self._max_entries = max_entries

    def get(self, key: str) -> Any:
        """Return the live value for ``key``, or None."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, ttl: float) -> None:
        """Store ``value`` for ``ttl`` seconds, evicting the oldest on overflow."""
        if ttl <= 0:
            return
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.monotonic() + ttl, value)

    def clear(self) -> None:
        """Drop every entry."""
        self._entries.clear()


#: Results of reads, shared by every reader in the process.
RESULT_CACHE = _TTLCache()
#: ``ETag`` and body of the last ``200`` per REST URL, for conditional GETs.
ETAG_CACHE = _TTLCache(max_entries=1024)
#: How long an ``ETag`` entry is kept; a 304 costs no rate-limit quota.
ETAG_TTL_SECONDS = 3600.0


def cache_ttl_seconds() -> float:
    """``PRELOOP_PRECHECK_CACHE_TTL_SECONDS``, default 60, 0 disables."""
    raw = os.environ.get("PRELOOP_PRECHECK_CACHE_TTL_SECONDS")
    if raw is None or raw.strip() == "":
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse a tracker ISO 8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PullRequestState:
    """What the pre-check needs to know about one open pull request."""

    repository: str
    number: int
    url: str
    title: str
    author: Optional[str]
    head_sha: Optional[str]
    base_branch: Optional[str]
    updated_at: Optional[datetime]
    is_draft: bool
    labels: Tuple[str, ...]
    #: When each current label was last applied, from the timeline.
    label_applied_at: Dict[str, datetime] = field(default_factory=dict)
    #: APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED or None.
    review_decision: Optional[str] = None
    #: MERGEABLE, CONFLICTING or UNKNOWN.
    mergeable: Optional[str] = None
    #: SUCCESS, FAILURE, ERROR, PENDING, EXPECTED or None (no checks).
    checks_state: Optional[str] = None
    unresolved_threads: int = 0


@dataclass(frozen=True)
class CheckRunState:
    """One check or commit status on a commit."""

    name: str
    state: str  # success, failure, pending, neutral
    url: Optional[str] = None


@dataclass(frozen=True)
class BranchCheckState:
    """The checks on a branch head."""

    repository: str
    branch: str
    head_sha: Optional[str]
    checks: Tuple[CheckRunState, ...]
    #: Names branch protection requires, or None when unknown.
    required: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class FailedRun:
    """A workflow run that failed."""

    repository: str
    id: int
    url: str
    workflow: str
    branch: Optional[str]
    head_sha: Optional[str]
    event: Optional[str]
    created_at: Optional[datetime]


@dataclass(frozen=True)
class IssueState:
    """What the pre-check needs to know about one open issue."""

    repository: str
    number: int
    url: str
    title: str
    labels: Tuple[str, ...]
    assignees: Tuple[str, ...]
    milestone: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


@dataclass(frozen=True)
class ReadPage:
    """A bounded read: the items, and whether the tracker had more."""

    items: Tuple[Any, ...]
    truncated: bool = False


class PrecheckTrackerReader(Protocol):
    """The four read-only questions a pre-check asks a tracker."""

    async def list_open_pull_requests_with_state(
        self, repository: str, *, include_drafts: bool
    ) -> ReadPage:
        """Open pull requests with review, merge, check and thread state."""
        ...

    async def default_branch_check_state(
        self, repository: str, *, branch: str, required_only: bool
    ) -> BranchCheckState:
        """Checks on the head of ``branch`` (default branch when empty)."""
        ...

    async def list_failed_ci_runs(
        self,
        repository: str,
        *,
        since: datetime,
        branches: Sequence[str],
        limit: int,
    ) -> ReadPage:
        """Workflow runs that failed since ``since``, newest first."""
        ...

    async def list_issues(
        self,
        repository: str,
        *,
        labels_all: Sequence[str],
        milestone: Optional[str],
        unassigned: bool,
    ) -> ReadPage:
        """Open issues, not pull requests, oldest first."""
        ...


_PULL_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: OPEN, first: 50, after: $cursor,
      orderBy: {field: UPDATED_AT, direction: ASC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url title isDraft updatedAt baseRefName headRefOid
        author { login }
        reviewDecision mergeable
        labels(first: 20) { nodes { name } }
        reviewThreads(first: 100) { nodes { isResolved } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
        timelineItems(last: 100, itemTypes: [LABELED_EVENT]) {
          nodes { ... on LabeledEvent { createdAt label { name } } }
        }
      }
    }
  }
}
"""

_CHECK_RUN_STATES = {
    "success": "success",
    "neutral": "neutral",
    "skipped": "neutral",
    "failure": "failure",
    "timed_out": "failure",
    "cancelled": "failure",
    "action_required": "failure",
    "startup_failure": "failure",
    "stale": "neutral",
}
_STATUS_STATES = {
    "success": "success",
    "failure": "failure",
    "error": "failure",
    "pending": "pending",
}


class GitHubPrecheckReader:
    """Read-only GitHub reads for pre-checks; GET and GraphQL queries only."""

    tracker_type = "github"

    def __init__(
        self,
        *,
        token: str,
        tracker_id: str,
        budget: PrecheckBudget,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        rate_limit_floor: int = DEFAULT_RATE_LIMIT_FLOOR,
        cache_ttl: Optional[float] = None,
    ) -> None:
        """Bind the reader to one credential and one evaluation budget.

        Args:
            token: Tracker credential; kept in the request headers only.
            tracker_id: Tracker row id, part of every cache key.
            budget: Budget of the evaluation this reader serves.
            transport: httpx transport, injectable for tests.
            rate_limit_floor: Stop when fewer requests than this remain.
            cache_ttl: Result cache TTL; None reads the setting.
        """
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "preloop-precheck",
        }
        self._tracker_id = str(tracker_id)
        self._budget = budget
        self._transport = transport
        self._floor = rate_limit_floor
        self._cache_ttl = cache_ttl_seconds() if cache_ttl is None else cache_ttl
        # Keys are scoped by tracker: a tracker row belongs to one account and
        # holds one credential, so two accounts never share an answer. The
        # credential itself is never part of a key.
        self._scope = "tracker:" + self._tracker_id

    # -- transport -----------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """A client for one request, bounded by the evaluation deadline."""
        timeout = min(MAX_REQUEST_SECONDS, max(0.1, self._budget.remaining_seconds()))
        return httpx.AsyncClient(
            base_url=GITHUB_API_URL,
            headers=self._headers,
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        )

    def _check_response(self, response: httpx.Response, what: str) -> None:
        """Map an HTTP failure or a low quota to a :class:`PrecheckError`."""
        status = response.status_code
        remaining = response.headers.get("x-ratelimit-remaining")
        if status in (403, 429) and (
            response.headers.get("retry-after") is not None
            or remaining == "0"
            or "rate limit" in response.text[:500].lower()
        ):
            raise PrecheckError(
                CAUSE_RATE_LIMITED, f"GitHub rate limited the read of {what}"
            )
        if status == 401:
            raise PrecheckError(
                CAUSE_AUTH_FAILED, f"GitHub refused the credential reading {what}"
            )
        if status == 403:
            raise PrecheckError(
                CAUSE_AUTH_FAILED, f"GitHub denied access reading {what} (403)"
            )
        if status == 404:
            raise PrecheckError(CAUSE_NOT_FOUND, f"GitHub found no {what} (404)")
        if status >= 500:
            raise PrecheckError(
                CAUSE_SERVER_ERROR, f"GitHub returned {status} reading {what}"
            )
        if status >= 400:
            raise PrecheckError(
                CAUSE_BAD_RESPONSE, f"GitHub returned {status} reading {what}"
            )
        if remaining is not None:
            try:
                left = int(remaining)
            except ValueError:
                left = None
            if left is not None and left < self._floor:
                raise PrecheckError(
                    CAUSE_RATE_LIMITED,
                    f"GitHub has {left} requests left for this credential, "
                    f"under the pre-check floor of {self._floor}",
                )

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request, charged to the budget, mapping transport errors."""
        self._budget.spend()
        try:
            async with self._client() as client:
                return await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PrecheckError(CAUSE_TIMEOUT, "GitHub did not answer in time") from (
                exc
            )
        except httpx.TransportError as exc:
            raise PrecheckError(CAUSE_UNREACHABLE, "GitHub is unreachable") from exc

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None, *, what: str
    ) -> Any:
        """``GET`` a REST path with an ``ETag`` conditional, return the JSON.

        Args:
            path: Path under the API root, starting with ``/``.
            params: Query parameters.
            what: Description of the resource for error messages.

        Returns:
            The decoded JSON body.

        Raises:
            PrecheckError: The read failed or quota is under the floor.
        """
        etag_key = (
            self._scope
            + " GET "
            + path
            + "?"
            + json.dumps(params or {}, sort_keys=True)
        )
        cached = ETAG_CACHE.get(etag_key)
        headers = {"If-None-Match": cached[0]} if cached else {}
        response = await self._send("GET", path, params=params, headers=headers)
        if response.status_code == 304 and cached:
            return cached[1]
        self._check_response(response, what)
        try:
            body = response.json()
        except ValueError as exc:
            raise PrecheckError(
                CAUSE_BAD_RESPONSE, f"GitHub sent no JSON for {what}"
            ) from exc
        etag = response.headers.get("etag")
        if etag:
            ETAG_CACHE.put(etag_key, (etag, body), ETAG_TTL_SECONDS)
        return body

    async def _graphql(
        self, query: str, variables: Dict[str, Any], *, what: str
    ) -> Any:
        """Run a GraphQL query (never a mutation) and return ``data``.

        Raises:
            ValueError: ``query`` declares a mutation or a subscription.
            PrecheckError: The read failed or returned errors.
        """
        if _WRITE_OPERATION.match(query):
            raise ValueError("the pre-check reader only sends GraphQL queries")
        response = await self._send(
            "POST", "/graphql", json={"query": query, "variables": variables}
        )
        self._check_response(response, what)
        try:
            body = response.json()
        except ValueError as exc:
            raise PrecheckError(
                CAUSE_BAD_RESPONSE, f"GitHub sent no JSON for {what}"
            ) from exc
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            kinds = {str(e.get("type") or "") for e in errors if isinstance(e, dict)}
            if "RATE_LIMITED" in kinds:
                raise PrecheckError(
                    CAUSE_RATE_LIMITED, f"GitHub rate limited the read of {what}"
                )
            if "NOT_FOUND" in kinds:
                raise PrecheckError(CAUSE_NOT_FOUND, f"GitHub found no {what}")
            raise PrecheckError(
                CAUSE_BAD_RESPONSE, f"GitHub reported an error reading {what}"
            )
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise PrecheckError(CAUSE_BAD_RESPONSE, f"GitHub sent no data for {what}")
        return data

    def _cache_key(self, method: str, *parts: Any) -> str:
        return self._scope + " " + method + " " + json.dumps(parts, default=str)

    async def _cached(self, key: str, load: Callable[[], Any]) -> Any:
        """Return the cached value of ``key`` or load and cache it."""
        hit = RESULT_CACHE.get(key)
        if hit is not None:
            return hit
        value = await load()
        RESULT_CACHE.put(key, value, self._cache_ttl)
        return value

    @staticmethod
    def _split(repository: str) -> Tuple[str, str]:
        owner, _, name = repository.partition("/")
        if not owner or not name or "/" in name:
            raise PrecheckError(
                CAUSE_UNSUPPORTED, f"'{repository}' is not an owner/name repository"
            )
        return owner, name

    # -- reads ---------------------------------------------------------------

    async def list_open_pull_requests_with_state(
        self, repository: str, *, include_drafts: bool
    ) -> ReadPage:
        """Open pull requests, least recently updated first.

        One GraphQL query returns 50 pull requests with everything a check
        filters on; at most four pages are read.
        """
        owner, name = self._split(repository)

        async def load() -> ReadPage:
            items: List[PullRequestState] = []
            cursor: Optional[str] = None
            truncated = False
            for page in range(MAX_PULL_REQUEST_PAGES):
                data = await self._graphql(
                    _PULL_REQUESTS_QUERY,
                    {"owner": owner, "name": name, "cursor": cursor},
                    what=f"open pull requests of {repository}",
                )
                repo = data.get("repository") or {}
                connection = repo.get("pullRequests") or {}
                for node in connection.get("nodes") or []:
                    if isinstance(node, dict):
                        items.append(_pull_request_from_node(repository, node))
                info = connection.get("pageInfo") or {}
                if not info.get("hasNextPage"):
                    break
                cursor = info.get("endCursor")
                if page == MAX_PULL_REQUEST_PAGES - 1:
                    truncated = True
            return ReadPage(items=tuple(items), truncated=truncated)

        result: ReadPage = await self._cached(
            self._cache_key("pull_requests", repository), load
        )
        if include_drafts:
            return result
        return ReadPage(
            items=tuple(pr for pr in result.items if not pr.is_draft),
            truncated=result.truncated,
        )

    async def default_branch_check_state(
        self, repository: str, *, branch: str, required_only: bool
    ) -> BranchCheckState:
        """Check runs and commit statuses on the head of ``branch``."""
        self._split(repository)

        async def load() -> BranchCheckState:
            target = branch
            if not target:
                repo = await self._get(
                    f"/repos/{repository}", what=f"repository {repository}"
                )
                target = str((repo or {}).get("default_branch") or "")
                if not target:
                    raise PrecheckError(
                        CAUSE_BAD_RESPONSE,
                        f"GitHub named no default branch for {repository}",
                    )
            runs = await self._get(
                f"/repos/{repository}/commits/{target}/check-runs",
                {"per_page": 100, "filter": "latest"},
                what=f"check runs on {repository}@{target}",
            )
            statuses = await self._get(
                f"/repos/{repository}/commits/{target}/status",
                {"per_page": 100},
                what=f"commit statuses on {repository}@{target}",
            )
            checks: List[CheckRunState] = []
            head_sha = statuses.get("sha") if isinstance(statuses, dict) else None
            for run in (runs or {}).get("check_runs") or []:
                if not isinstance(run, dict):
                    continue
                head_sha = head_sha or run.get("head_sha")
                if run.get("status") != "completed":
                    state = "pending"
                else:
                    state = _CHECK_RUN_STATES.get(
                        str(run.get("conclusion") or ""), "pending"
                    )
                checks.append(
                    CheckRunState(
                        name=str(run.get("name") or ""),
                        state=state,
                        url=run.get("html_url"),
                    )
                )
            for status in (statuses or {}).get("statuses") or []:
                if not isinstance(status, dict):
                    continue
                checks.append(
                    CheckRunState(
                        name=str(status.get("context") or ""),
                        state=_STATUS_STATES.get(
                            str(status.get("state") or ""), "pending"
                        ),
                        url=status.get("target_url"),
                    )
                )
            required: Optional[Tuple[str, ...]] = None
            if required_only:
                try:
                    protection = await self._get(
                        f"/repos/{repository}/branches/{target}/protection/"
                        "required_status_checks",
                        what=f"required checks of {repository}@{target}",
                    )
                except PrecheckError as exc:
                    # No protection (404) or no admin read (403): count every
                    # check, as the schema documents.
                    if exc.cause not in (CAUSE_NOT_FOUND, CAUSE_AUTH_FAILED):
                        raise
                    protection = None
                if isinstance(protection, dict):
                    names = [str(c) for c in protection.get("contexts") or []]
                    names += [
                        str(c.get("context"))
                        for c in protection.get("checks") or []
                        if isinstance(c, dict) and c.get("context")
                    ]
                    required = tuple(dict.fromkeys(names)) or None
            return BranchCheckState(
                repository=repository,
                branch=target,
                head_sha=head_sha,
                checks=tuple(checks),
                required=required,
            )

        state: BranchCheckState = await self._cached(
            self._cache_key("branch_checks", repository, branch, required_only), load
        )
        return state

    async def list_failed_ci_runs(
        self,
        repository: str,
        *,
        since: datetime,
        branches: Sequence[str],
        limit: int,
    ) -> ReadPage:
        """Failed workflow runs created at or after ``since``, newest first."""
        self._split(repository)
        since_utc = since.astimezone(timezone.utc).replace(microsecond=0)
        stamp = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        per_page = max(1, min(100, limit))

        async def load() -> ReadPage:
            targets: List[Optional[str]] = list(branches) or [None]
            items: List[FailedRun] = []
            truncated = False
            for target in targets:
                params: Dict[str, Any] = {
                    "status": "failure",
                    "created": f">={stamp}",
                    "per_page": per_page,
                    "exclude_pull_requests": "false",
                }
                if target:
                    params["branch"] = target
                body = await self._get(
                    f"/repos/{repository}/actions/runs",
                    params,
                    what=f"failed workflow runs of {repository}",
                )
                runs = (body or {}).get("workflow_runs") or []
                total = (body or {}).get("total_count")
                if isinstance(total, int) and total > len(runs):
                    truncated = True
                for run in runs:
                    if not isinstance(run, dict) or run.get("id") is None:
                        continue
                    items.append(
                        FailedRun(
                            repository=repository,
                            id=int(run["id"]),
                            url=str(run.get("html_url") or ""),
                            workflow=str(run.get("name") or ""),
                            branch=run.get("head_branch"),
                            head_sha=run.get("head_sha"),
                            event=run.get("event"),
                            created_at=_parse_time(run.get("created_at")),
                        )
                    )
            items.sort(
                key=lambda run: (
                    run.created_at or datetime.min.replace(tzinfo=timezone.utc)
                ),
                reverse=True,
            )
            return ReadPage(items=tuple(items), truncated=truncated)

        page: ReadPage = await self._cached(
            self._cache_key("failed_runs", repository, stamp, list(branches), limit),
            load,
        )
        return page

    async def list_issues(
        self,
        repository: str,
        *,
        labels_all: Sequence[str],
        milestone: Optional[str],
        unassigned: bool,
    ) -> ReadPage:
        """Open issues carrying every label in ``labels_all``, oldest first."""
        self._split(repository)

        async def load() -> ReadPage:
            params: Dict[str, Any] = {
                "state": "open",
                "per_page": 100,
                "sort": "created",
                "direction": "asc",
            }
            if labels_all:
                params["labels"] = ",".join(labels_all)
            if unassigned:
                params["assignee"] = "none"
            if milestone:
                number = await self._milestone_number(repository, milestone)
                if number is None:
                    return ReadPage(items=())
                params["milestone"] = number
            items: List[IssueState] = []
            truncated = False
            # Labels other than ``all`` are filtered by the check, so a
            # matching issue can sit past the first page: read up to
            # MAX_ISSUE_PAGES pages, each charged to the request budget.
            for page in range(1, MAX_ISSUE_PAGES + 1):
                body = await self._get(
                    f"/repos/{repository}/issues",
                    {**params, "page": page},
                    what=f"open issues of {repository}",
                )
                rows = body if isinstance(body, list) else []
                items.extend(
                    _issue_from_row(repository, row)
                    for row in rows
                    if isinstance(row, dict) and "pull_request" not in row
                )
                if len(rows) < 100:
                    break
                if page == MAX_ISSUE_PAGES:
                    truncated = True
            return ReadPage(items=tuple(items), truncated=truncated)

        issues: ReadPage = await self._cached(
            self._cache_key(
                "issues", repository, list(labels_all), milestone, unassigned
            ),
            load,
        )
        return issues

    async def _milestone_number(self, repository: str, title: str) -> Optional[int]:
        """Number of the open milestone titled ``title``, or None."""
        rows = await self._get(
            f"/repos/{repository}/milestones",
            {"state": "open", "per_page": 100},
            what=f"milestones of {repository}",
        )
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("title") == title:
                number = row.get("number")
                return int(number) if number is not None else None
        return None


def _pull_request_from_node(repository: str, node: Dict[str, Any]) -> PullRequestState:
    """Build a :class:`PullRequestState` from one GraphQL node."""
    labels = tuple(
        str(label.get("name"))
        for label in ((node.get("labels") or {}).get("nodes") or [])
        if isinstance(label, dict) and label.get("name")
    )
    applied: Dict[str, datetime] = {}
    for event in (node.get("timelineItems") or {}).get("nodes") or []:
        if not isinstance(event, dict):
            continue
        name = (event.get("label") or {}).get("name")
        when = _parse_time(event.get("createdAt"))
        if name and when and (name not in applied or when > applied[name]):
            applied[str(name)] = when
    threads = (node.get("reviewThreads") or {}).get("nodes") or []
    unresolved = sum(
        1 for t in threads if isinstance(t, dict) and t.get("isResolved") is False
    )
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = None
    if commits and isinstance(commits[-1], dict):
        rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup")) or {}
    return PullRequestState(
        repository=repository,
        number=int(node.get("number") or 0),
        url=str(node.get("url") or ""),
        title=str(node.get("title") or ""),
        author=(node.get("author") or {}).get("login"),
        head_sha=node.get("headRefOid"),
        base_branch=node.get("baseRefName"),
        updated_at=_parse_time(node.get("updatedAt")),
        is_draft=bool(node.get("isDraft")),
        labels=labels,
        label_applied_at=applied,
        review_decision=node.get("reviewDecision"),
        mergeable=node.get("mergeable"),
        checks_state=(rollup or {}).get("state") if rollup else None,
        unresolved_threads=unresolved,
    )


def _issue_from_row(repository: str, row: Dict[str, Any]) -> IssueState:
    """Build an :class:`IssueState` from one REST issue row."""
    return IssueState(
        repository=repository,
        number=int(row.get("number") or 0),
        url=str(row.get("html_url") or ""),
        title=str(row.get("title") or ""),
        labels=tuple(
            str(label.get("name"))
            for label in row.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        ),
        assignees=tuple(
            str(user.get("login"))
            for user in row.get("assignees") or []
            if isinstance(user, dict) and user.get("login")
        ),
        milestone=(row.get("milestone") or {}).get("title")
        if isinstance(row.get("milestone"), dict)
        else None,
        created_at=_parse_time(row.get("created_at")),
        updated_at=_parse_time(row.get("updated_at")),
    )


#: Tracker types that have a pre-check reader.
SUPPORTED_TRACKER_TYPES = frozenset({"github"})


async def build_reader(tracker: Any, budget: PrecheckBudget) -> PrecheckTrackerReader:
    """Build the reader for ``tracker`` with its stored credential.

    The credential is resolved the way git clones resolve it: the GitHub App
    installation token for an App-installed tracker, otherwise the stored
    token.

    Args:
        tracker: ``Tracker`` ORM row.
        budget: Budget of the evaluation.

    Returns:
        A reader for the tracker's type.

    Raises:
        PrecheckError: No reader for the tracker type, or no credential.
    """
    tracker_type = str(getattr(tracker, "tracker_type", "") or "")
    if tracker_type not in SUPPORTED_TRACKER_TYPES:
        raise PrecheckError(
            CAUSE_UNSUPPORTED, f"pre-checks cannot read a {tracker_type} tracker yet"
        )
    from preloop.services.tracker_git_token import resolve_tracker_git_token

    try:
        token = await resolve_tracker_git_token(tracker)
    except Exception as exc:  # noqa: BLE001 - mapped to a typed cause
        raise PrecheckError(
            CAUSE_NO_CREDENTIAL, "the tracker credential could not be resolved"
        ) from exc
    if not token:
        raise PrecheckError(CAUSE_NO_CREDENTIAL, "the tracker has no usable credential")
    return GitHubPrecheckReader(
        token=token, tracker_id=str(getattr(tracker, "id", "")), budget=budget
    )
