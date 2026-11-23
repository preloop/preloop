"""Parse tracker issue URLs into canonical issue keys.

GitLab serves an issue at ``/<namespace>/<project>/-/issues/<iid>`` and, from
GitLab 18.10 on (the Work items view), at ``/<namespace>/<project>/-/work_items/<iid>``;
the web UI redirects the first form to the second, and a REST ``web_url`` may
carry either. Both name the same issue, so both map to ``namespace/project#iid``.
"""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

#: Path words GitLab uses for an issue page.
GITLAB_ISSUE_PATH_WORDS = ("issues", "work_items")

_GITLAB_RE = re.compile(
    r"^/(?P<project>[^/].*?)/-/(?P<word>issues|work_items)/(?P<iid>[1-9][0-9]*)$"
)
# Two-segment ``/owner/repo/issues/<n>``: GitHub, and GitLab's legacy form
# without ``/-/`` (still shown in GitLab's REST API examples).
_PLAIN_RE = re.compile(r"^/(?P<project>[^/]+/[^/]+)/issues/(?P<iid>[1-9][0-9]*)$")
_JIRA_RE = re.compile(r"^/browse/(?P<key>[A-Z][A-Z0-9]+-[0-9]+)$")
# Azure Boards: https://dev.azure.com/{org}/{project}/_workitems/edit/{id}.
_AZURE_WORK_ITEM_RE = re.compile(
    r"^/(?P<org>[^/]+)/(?P<project>[^/]+)/_workitems/edit/(?P<iid>[1-9][0-9]*)$"
)


@dataclass(frozen=True)
class ParsedIssueUrl:
    """An issue named by a tracker URL.

    Attributes:
        key: Canonical issue key (``group/project#12`` or ``PROJ-12``).
        project: Project path for GitHub/GitLab, ``None`` for Jira.
        number: Issue number or iid as a string, ``None`` for Jira.
        path_word: ``issues`` or ``work_items`` for GitHub/GitLab URLs.
        host: Lower-cased host (with port) of the URL.
    """

    key: str
    project: Optional[str]
    number: Optional[str]
    path_word: Optional[str]
    host: str


def parse_issue_path(path: str, host: str = "") -> Optional[ParsedIssueUrl]:
    """Parse an already split URL path (query and fragment removed).

    Args:
        path: URL path, for example ``/group/sub/project/-/work_items/12``.
        host: Host the path belongs to, kept on the result.

    Returns:
        The parsed issue, or ``None`` when the path names no issue.
    """
    path = unquote(path).rstrip("/")
    host = host.lower()
    match = _GITLAB_RE.match(path)
    if match:
        project, iid = match.group("project"), match.group("iid")
        return ParsedIssueUrl(
            f"{project}#{iid}", project, iid, match.group("word"), host
        )
    match = _PLAIN_RE.match(path)
    if match:
        project, iid = match.group("project"), match.group("iid")
        return ParsedIssueUrl(f"{project}#{iid}", project, iid, "issues", host)
    match = _JIRA_RE.match(path)
    if match:
        return ParsedIssueUrl(match.group("key"), None, None, None, host)
    match = _AZURE_WORK_ITEM_RE.match(path)
    if match and host == "dev.azure.com":
        project = f"{match.group('org')}/{match.group('project')}"
        iid = match.group("iid")
        return ParsedIssueUrl(f"{project}#{iid}", project, iid, "_workitems", host)
    return None


def parse_issue_url(url: str) -> Optional[ParsedIssueUrl]:
    """Parse a tracker issue URL; query string and fragment are ignored.

    Accepts GitLab ``/-/issues/<iid>`` and ``/-/work_items/<iid>`` on any host
    and nested group path, two-segment ``/owner/repo/issues/<n>`` (GitHub and
    GitLab's legacy form), Jira ``/browse/KEY-1`` and Azure Boards
    ``dev.azure.com/{org}/{project}/_workitems/edit/{id}``.

    Args:
        url: Absolute ``http(s)`` URL.

    Returns:
        The parsed issue, or ``None`` when the URL names no issue.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parse_issue_path(parsed.path, parsed.netloc)


def issue_key_from_url(url: str) -> Optional[str]:
    """Canonical issue key for a tracker issue URL, or ``None``."""
    parsed = parse_issue_url(url)
    return parsed.key if parsed else None
