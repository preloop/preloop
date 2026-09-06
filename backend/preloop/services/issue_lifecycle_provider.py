"""Live provider authority and idempotent external effects for issue lifecycles."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from collections.abc import AsyncIterator
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Protocol
from urllib.parse import urlparse


PUBLICATION_TIMEOUT_SECONDS = 8.0
PUBLICATION_MAX_REQUESTS = 128


@dataclass
class _PublicationBudget:
    remaining_requests: int


_publication_budget: ContextVar[_PublicationBudget | None] = ContextVar(
    "lifecycle_publication_budget", default=None
)


@asynccontextmanager
async def publication_budget() -> AsyncIterator[None]:
    """Bound provider work while retaining serialized, deduplicated effects."""
    token = _publication_budget.set(_PublicationBudget(PUBLICATION_MAX_REQUESTS))
    deadline = asyncio.timeout(PUBLICATION_TIMEOUT_SECONDS)
    try:
        async with deadline:
            yield
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise ValueError("lifecycle_publication_deadline_exceeded") from exc
    finally:
        _publication_budget.reset(token)


@dataclass(frozen=True)
class IssueSnapshot:
    """A scope revision excludes labels/comments changed by automation."""

    number: int
    title: str
    body: str
    state: str
    url: str
    labels: tuple[str, ...]

    @property
    def revision(self) -> str:
        """Fingerprint implementation scope, not mutable presentation metadata."""
        return sha256(
            json.dumps([self.title, self.body], ensure_ascii=False).encode()
        ).hexdigest()


@dataclass(frozen=True)
class MergeLink:
    """Provider-verified PR association and immutable merged commit."""

    number: int
    sha: str
    url: str


class LifecycleProvider(Protocol):
    """Implementations must re-read authority and use additive label updates."""

    async def issue(self, number: int) -> IssueSnapshot: ...
    async def merged_links(self, number: int) -> list[MergeLink]: ...
    async def require_ready_label(self, label: str) -> None: ...
    async def add_ready_label(self, number: int, label: str, revision: str) -> None: ...
    async def upsert_comment(self, number: int, marker: str, body: str) -> str: ...
    async def ensure_follow_up(
        self, number: int, marker: str, title: str, body: str
    ) -> str: ...


class GitHubRequests(Protocol):
    """Existing tracker transport handles PAT, installation and OAuth auth."""

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any: ...


class GitHubLifecycleProvider:
    """GitHub adapter. Unknown/truncated authority fails closed."""

    def __init__(self, client: GitHubRequests, repository: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid_lifecycle_repository")
        self.client = client
        self.repository = repository
        self.root = f"/repos/{repository}"

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        budget = _publication_budget.get()
        if budget is not None:
            if budget.remaining_requests <= 0:
                raise ValueError("lifecycle_publication_request_budget_exhausted")
            budget.remaining_requests -= 1
        return await self.client._request(method, endpoint, data=data, params=params)

    async def _rows(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        # Page 101 is an exhaustion probe. Exactly 10,000 results are complete;
        # anything beyond the bound fails closed before a new remote effect.
        for page in range(1, 102):
            batch = await self._request(
                "GET",
                endpoint,
                params={**(params or {}), "per_page": 100, "page": page},
            )
            if not isinstance(batch, list):
                raise ValueError("invalid_lifecycle_provider_response")
            if page == 101 and batch:
                raise ValueError("lifecycle_provider_results_truncated")
            for row in batch:
                yield row
            if len(batch) < 100:
                return

    async def _pages(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [row async for row in self._rows(endpoint, params)]

    async def issue(self, number: int) -> IssueSnapshot:
        """Read authoritative scope and preserve project-owned label names."""
        raw = await self._request("GET", f"{self.root}/issues/{number}")
        if raw.get("pull_request"):
            raise ValueError("lifecycle_target_is_pull_request")
        return IssueSnapshot(
            number,
            raw["title"],
            raw.get("body") or "",
            raw["state"],
            raw["html_url"],
            tuple(label["name"] for label in raw.get("labels", [])),
        )

    async def merged_links(self, number: int) -> list[MergeLink]:
        """Require a closing commit tied to a provider-linked merged PR.

        A completed/manual close with no closing commit never qualifies.
        Linked PRs are reloaded rather than trusting webhook body text.
        """
        snapshot = await self.issue(number)
        if snapshot.state != "closed":
            return []
        timeline = await self._pages(f"{self.root}/issues/{number}/timeline")
        closes = [item for item in timeline if item.get("event") == "closed"]
        if not closes or not closes[-1].get("commit_id"):
            return []
        closing_sha = closes[-1]["commit_id"]
        numbers = set()
        for item in timeline:
            source = (item.get("source") or {}).get("issue") or {}
            pull_request = source.get("pull_request") or {}
            source_repo = source.get("repository", {}).get("full_name")
            pr_path = urlparse(pull_request.get("url", "")).path
            if pull_request and (
                source_repo == self.repository
                or pr_path == f"{self.root}/pulls/{source.get('number')}"
            ):
                numbers.add(source["number"])
        # The closing commit's associated PRs also cover auto-close timelines
        # where GitHub omitted a separate cross-reference event.
        associated = await self._pages(f"{self.root}/commits/{closing_sha}/pulls")
        numbers.update(
            pr["number"]
            for pr in associated
            if pr.get("base", {}).get("repo", {}).get("full_name") == self.repository
        )
        links = []
        for pr_number in sorted(numbers):
            pr = await self._request("GET", f"{self.root}/pulls/{pr_number}")
            sha = pr.get("merge_commit_sha") or ""
            if (
                pr.get("merged")
                and re.fullmatch(r"[0-9a-f]{40}", sha)
                and pr.get("base", {}).get("repo", {}).get("full_name")
                == self.repository
            ):
                links.append(MergeLink(pr_number, sha, pr["html_url"]))
        if not any(link.sha == closing_sha for link in links):
            return []
        # Audit the final closing revision once, retaining all linked merged
        # contributions in its evidence envelope.
        return sorted(links, key=lambda link: link.sha == closing_sha)

    async def require_ready_label(self, label: str) -> None:
        """Require an existing repository label; never create project vocabulary."""
        async for row in self._rows(f"{self.root}/labels"):
            if row.get("name") == label:
                return
        raise ValueError("ready_label_not_found")

    async def add_ready_label(self, number: int, label: str, revision: str) -> None:
        """Apply the ready label after `_ready` already required it exists.

        Scope is re-read here because the issue can change between the two
        advisory locks; the repository label list is not enumerated again.
        """
        current = await self.issue(number)
        if current.revision != revision or current.state != "open":
            raise ValueError("issue_scope_changed")
        if label not in current.labels:
            await self._request(
                "POST", f"{self.root}/issues/{number}/labels", {"labels": [label]}
            )

    async def upsert_comment(self, number: int, marker: str, body: str) -> str:
        """Recover a previous provider-success/local-rollback by durable marker."""
        existing = None
        async for item in self._rows(f"{self.root}/issues/{number}/comments"):
            if marker in (item.get("body") or ""):
                existing = item
                break
        payload = {"body": f"{body}\n\n{marker}"}
        if existing:
            result = await self._request(
                "PATCH", f"{self.root}/issues/comments/{existing['id']}", payload
            )
        else:
            result = await self._request(
                "POST", f"{self.root}/issues/{number}/comments", payload
            )
        return result["html_url"]

    async def ensure_follow_up(
        self, number: int, marker: str, title: str, body: str
    ) -> str:
        """Search linked work before creating; omit all readiness labels.

        List instead of search avoids GitHub search-index lag after a timeout.
        Markers intentionally survive across subsequent merges of the issue.
        """
        async for issue in self._rows(
            f"{self.root}/issues",
            {"state": "all", "sort": "created", "direction": "desc"},
        ):
            existing_body = issue.get("body") or ""
            # Human-created linked follow-ups need not know our marker. An
            # exact criterion reference or matching title plus source link is
            # sufficient to reuse them instead of filing parallel work.
            source = next(
                (
                    line.removeprefix("Source: ")
                    for line in body.splitlines()
                    if line.startswith("Source: ")
                ),
                "",
            )
            criterion = next(
                (
                    line
                    for line in body.splitlines()
                    if line.startswith("Acceptance criterion: ")
                ),
                "",
            )
            linked = (
                bool(source)
                and source in existing_body
                and (
                    issue.get("title") == title
                    or (criterion and criterion in existing_body.splitlines())
                )
            )
            if not issue.get("pull_request") and (marker in existing_body or linked):
                return issue["html_url"]
        result = await self._request(
            "POST",
            f"{self.root}/issues",
            {"title": title, "body": f"{body}\n\n{marker}", "labels": []},
        )
        return result["html_url"]
