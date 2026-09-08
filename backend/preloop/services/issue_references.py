"""Parse issue references out of pull-request text and branch names.

The Pull Request Reviewer preset judges whether a PR addresses the issue it
references. Nothing in the tracker payload carries that relation: the GitHub
``get_pull_request`` mapping (``preloop/sync/trackers/github.py``) and the
GitLab merge-request mapping (``preloop/api/endpoints/mcp.py``) both return a
fixed field list with no ``closes_issues``. So the reference has to be read
off the PR itself: closing keywords in the body, plain issue URLs, and the
branch name.

Doing it here rather than in the prompt keeps it deterministic and testable,
and hands the agent identifiers that ``get_issue`` already accepts (a full
issue URL, ``org/repo#123``, or a Jira key).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

# Kind ranks: a closing keyword is a promise, a plain mention is a hint, and a
# branch number is a guess. Stronger kinds win when the same issue is found
# twice and are listed first.
KIND_CLOSES = "closes"
KIND_REFERENCE = "reference"
KIND_BRANCH = "branch"

_KIND_RANK = {KIND_CLOSES: 0, KIND_REFERENCE: 1, KIND_BRANCH: 2}

# Upper bound on the PR text scanned for references.
MAX_SCANNED_CHARS = 50_000

# GitHub's closing keywords plus GitLab's "implement" family.
_CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "closing",
    "fix",
    "fixes",
    "fixed",
    "fixing",
    "resolve",
    "resolves",
    "resolved",
    "resolving",
    "implement",
    "implements",
    "implemented",
)

# Weaker phrases that still name an issue this PR is about.
_REFERENCE_KEYWORDS = (
    "ref",
    "refs",
    "references",
    "related to",
    "relates to",
    "part of",
    "addresses",
    "towards",
    "see",
)

_JIRA_KEY = r"[A-Z][A-Z0-9]{1,9}-\d{1,6}"
# Project paths: "org/repo" for GitHub, "group/sub/project" for GitLab.
_PATH = r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+"
_NUMBER = r"\d{1,7}"

_KEYWORD_TARGET = rf"(?:(?P<path>{_PATH})?#(?P<number>{_NUMBER})|(?P<jira>{_JIRA_KEY}))"


def _keyword_pattern(keywords: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(
        sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    )
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternatives})\b\s*:?\s+{_KEYWORD_TARGET}",
        re.IGNORECASE,
    )


_CLOSING_RE = _keyword_pattern(_CLOSING_KEYWORDS)
_REFERENCE_RE = _keyword_pattern(_REFERENCE_KEYWORDS)

# Bare "#123" or "org/repo#123" with no keyword in front of it.
_BARE_RE = re.compile(
    rf"(?<![A-Za-z0-9/#-])(?P<path>{_PATH})?#(?P<number>{_NUMBER})(?![0-9])"
)

# Issue URLs: GitHub /o/r/issues/12, GitLab /g/p/-/issues/12, Jira /browse/KEY-1.
_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9.:-]+)/(?P<rest>[^\s)>\]\"']+)",
    re.IGNORECASE,
)

_BRANCH_PATTERNS = (
    # 123-slug, 123
    re.compile(rf"^(?P<number>{_NUMBER})(?:[-_]|$)"),
    # issue-123, issues/123, gh-123, #123 forms inside a branch path
    re.compile(
        rf"(?:^|/)(?:issue|issues|gh|bug)[-_/]?(?P<number>{_NUMBER})(?:[-_/]|$)",
        re.IGNORECASE,
    ),
    # fix/123-slug, feature/123
    re.compile(rf"(?:^|/)[A-Za-z]+/(?P<number>{_NUMBER})(?:[-_]|$)"),
)

# Jira-style branch: feature/PROJ-123-slug. Uppercase only, so "fix/abc-1"
# stays a slug rather than becoming a fake project key.
_BRANCH_JIRA_RE = re.compile(rf"(?:^|[/_-])(?P<jira>{_JIRA_KEY})(?:[/_-]|$)")


@dataclass(frozen=True)
class IssueReference:
    """One issue named by a pull request."""

    key: str
    kind: str
    source: str
    url: Optional[str] = None

    def identifier(self) -> str:
        """The value to hand to the ``get_issue`` tool."""
        return self.url or self.key


def _issue_url(
    host: Optional[str], path: Optional[str], number: str, platform: Optional[str]
) -> Optional[str]:
    """Build a canonical issue URL when host, path, and platform are known."""
    if not host or not path or not platform:
        return None
    scheme = (
        "http" if host.startswith("localhost") or host.startswith("127.") else "https"
    )
    if platform == "github":
        return f"{scheme}://{host}/{path}/issues/{number}"
    if platform == "gitlab":
        return f"{scheme}://{host}/{path}/-/issues/{number}"
    return None


def _parse_issue_url(host: str, rest: str) -> Optional[tuple[str, str]]:
    """Return (key, normalized_url) for a tracker issue URL, else None."""
    path = "/" + rest.rstrip("/").rstrip(".,;")
    match = re.match(r"^/(?P<path>.+?)/-/issues/(?P<number>\d+)$", path)
    if match:
        key = f"{match.group('path')}#{match.group('number')}"
        return key, f"https://{host}{path}"
    match = re.match(r"^/(?P<path>[^/]+/[^/]+)/issues/(?P<number>\d+)$", path)
    if match:
        key = f"{match.group('path')}#{match.group('number')}"
        return key, f"https://{host}{path}"
    match = re.match(rf"^/browse/(?P<jira>{_JIRA_KEY})$", path)
    if match:
        return match.group("jira"), f"https://{host}{path}"
    return None


def _add(
    found: List[IssueReference],
    *,
    key: str,
    kind: str,
    source: str,
    url: Optional[str],
) -> None:
    found.append(IssueReference(key=key, kind=kind, source=source, url=url))


def _scan_keywords(
    text: str,
    pattern: re.Pattern[str],
    kind: str,
    source: str,
    repo_path: Optional[str],
    host: Optional[str],
    platform: Optional[str],
    found: List[IssueReference],
) -> None:
    for match in pattern.finditer(text):
        jira = match.group("jira")
        if jira:
            _add(found, key=jira, kind=kind, source=source, url=None)
            continue
        path = match.group("path") or repo_path
        number = match.group("number")
        if not path:
            continue
        _add(
            found,
            key=f"{path}#{number}",
            kind=kind,
            source=source,
            url=_issue_url(host, path, number, platform),
        )


def extract_issue_references(
    *,
    description: Optional[str] = None,
    title: Optional[str] = None,
    branch: Optional[str] = None,
    repo_path: Optional[str] = None,
    host: Optional[str] = None,
    platform: Optional[str] = None,
    self_number: Optional[Any] = None,
    limit: int = 5,
) -> List[IssueReference]:
    """Extract issue references from a pull request's text and branch.

    ``repo_path``/``host``/``platform`` describe the PR's own repository and
    are used to expand same-repo references (``#12``) into keys and URLs.
    ``self_number`` drops a PR's reference to itself (squash-merge titles
    routinely carry ``(#45)``).
    """

    found: List[IssueReference] = []
    # PR bodies can carry a whole changelog. Scanning the head of the text is
    # enough for references and keeps the regex work bounded.
    body = (description or "")[:MAX_SCANNED_CHARS]

    for text, source in ((body, "body"), (title or "", "title")):
        if not text:
            continue
        _scan_keywords(
            text, _CLOSING_RE, KIND_CLOSES, source, repo_path, host, platform, found
        )
        _scan_keywords(
            text,
            _REFERENCE_RE,
            KIND_REFERENCE,
            source,
            repo_path,
            host,
            platform,
            found,
        )

    # Plain issue URLs anywhere in the body count as references.
    for match in _URL_RE.finditer(body):
        parsed = _parse_issue_url(match.group("host"), match.group("rest"))
        if parsed:
            key, url = parsed
            _add(found, key=key, kind=KIND_REFERENCE, source="body", url=url)

    # Bare "#12" / "org/repo#12" in the body, no keyword.
    for match in _BARE_RE.finditer(body):
        path = match.group("path") or repo_path
        if not path:
            continue
        number = match.group("number")
        _add(
            found,
            key=f"{path}#{number}",
            kind=KIND_REFERENCE,
            source="body",
            url=_issue_url(host, path, number, platform),
        )

    if branch:
        jira_match = _BRANCH_JIRA_RE.search(branch)
        if jira_match:
            _add(
                found,
                key=jira_match.group("jira"),
                kind=KIND_BRANCH,
                source="branch",
                url=None,
            )
        for pattern in _BRANCH_PATTERNS:
            match = pattern.search(branch)
            if not match or not repo_path:
                continue
            number = match.group("number")
            _add(
                found,
                key=f"{repo_path}#{number}",
                kind=KIND_BRANCH,
                source="branch",
                url=_issue_url(host, repo_path, number, platform),
            )
            break

    return _dedupe(found, repo_path=repo_path, self_number=self_number, limit=limit)


def _dedupe(
    refs: List[IssueReference],
    *,
    repo_path: Optional[str],
    self_number: Optional[Any],
    limit: int,
) -> List[IssueReference]:
    """Keep the strongest kind per issue, strongest kinds first, capped."""
    self_key = None
    if repo_path and self_number not in (None, ""):
        self_key = f"{repo_path}#{self_number}".lower()

    best: Dict[str, tuple[int, int, IssueReference]] = {}
    for order, ref in enumerate(refs):
        key = ref.key.lower()
        if self_key and key == self_key:
            continue
        rank = _KIND_RANK.get(ref.kind, len(_KIND_RANK))
        current = best.get(key)
        if current is None or rank < current[0]:
            # Keep the earliest position so equal kinds stay in document order.
            position = current[1] if current else order
            merged = ref if current is None else _merge(ref, current[2])
            best[key] = (rank, position, merged)
    ordered = sorted(best.values(), key=lambda item: (item[0], item[1]))
    return [item[2] for item in ordered][:limit]


def _merge(winner: IssueReference, loser: IssueReference) -> IssueReference:
    """Keep the winner's kind but do not lose a URL the loser had."""
    if winner.url or not loser.url:
        return winner
    return IssueReference(
        key=winner.key, kind=winner.kind, source=winner.source, url=loser.url
    )


NO_REFERENCES = "none detected"


def format_issue_references(refs: Iterable[IssueReference]) -> str:
    """One prompt-safe line, or ``none detected``."""
    parts = [
        f"{ref.key} [{ref.kind}, from {ref.source}]"
        + (f" {ref.url}" if ref.url else "")
        for ref in refs
    ]
    return "; ".join(parts) if parts else NO_REFERENCES


def _payload_repo_path_and_host(
    payload: Dict[str, Any], attrs: Dict[str, Any]
) -> tuple[Optional[str], Optional[str]]:
    """Derive "org/repo" and the tracker host from a trigger payload."""
    url = attrs.get("url") or ""
    host: Optional[str] = None
    repo_path: Optional[str] = None
    if isinstance(url, str) and url.startswith("http"):
        parsed = urlparse(url)
        host = parsed.netloc or None
        path = parsed.path.rstrip("/")
        match = re.match(r"^/(?P<path>.+?)/-/merge_requests/\d+$", path) or re.match(
            r"^/(?P<path>[^/]+/[^/]+)/(?:pull|pulls)/\d+$", path
        )
        if match:
            repo_path = match.group("path")
    if not repo_path:
        repository = payload.get("repository")
        if isinstance(repository, dict):
            candidate = repository.get("full_name") or repository.get(
                "path_with_namespace"
            )
            if isinstance(candidate, str) and candidate:
                repo_path = candidate
        project = payload.get("project")
        if not repo_path and isinstance(project, dict):
            candidate = project.get("path_with_namespace")
            if isinstance(candidate, str) and candidate:
                repo_path = candidate
    return repo_path, host


def _payload_platform(
    payload: Dict[str, Any], source: str, host: Optional[str]
) -> Optional[str]:
    if "pull_request" in payload:
        return "github"
    if payload.get("object_kind") == "merge_request":
        return "gitlab"
    if source in ("github", "gitlab"):
        return source
    if host and "gitlab" in host:
        return "gitlab"
    if host and "github" in host:
        return "github"
    return None


def references_from_trigger_payload(
    payload: Dict[str, Any], attrs: Dict[str, Any], source: str = ""
) -> List[IssueReference]:
    """Extract references for a normalized PR/MR trigger payload."""
    repo_path, host = _payload_repo_path_and_host(payload, attrs)
    platform = _payload_platform(payload, (source or "").lower(), host)
    return extract_issue_references(
        description=attrs.get("description"),
        title=attrs.get("title"),
        branch=attrs.get("source_branch"),
        repo_path=repo_path,
        host=host,
        platform=platform,
        self_number=attrs.get("number") or attrs.get("iid"),
    )
