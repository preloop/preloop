"""Write an opened pull request back onto the Jira issue that triggered it.

Two writes, each best effort and independent of the other:

1. A comment with the pull request URL and branch (``JiraTracker.add_comment``).
2. A remote issue link to the pull request
   (``POST /rest/api/3/issue/{issueIdOrKey}/remotelink``). Its ``globalId``
   is derived from the repository, so a later run on the same issue and
   repository updates the one link instead of adding another.

The Jira development panel (``/rest/devinfo/0.10/bulk``) is deliberately not
used: it accepts submissions only from Connect, Forge or on-premises OAuth
integrations that declare a development tool module, which an API token
cannot do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from preloop.services.flow_pr_binding import normalize_pr_url
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

# Path markers that separate the repository path from the pull request
# number: GitLab, Bitbucket Cloud, GitHub. Longest first.
_PR_PATH_MARKERS: Tuple[str, ...] = ("/-/merge_requests/", "/pull-requests/", "/pull/")

GLOBAL_ID_PREFIX = "preloop:pull-request:"
_GLOBAL_ID_MAX = 255


@dataclass
class JiraWritebackOutcome:
    """What the write-back did.

    Attributes:
        comment_posted: The comment with the PR URL was created.
        remote_link_written: The remote link was created or updated.
        skipped_reason: Why nothing was attempted, when applicable.
    """

    comment_posted: bool = False
    remote_link_written: bool = False
    skipped_reason: Optional[str] = None


def split_pull_request_url(pr_url: str) -> Tuple[str, str, Optional[str]]:
    """Split a pull request URL into host, repository path and number.

    Args:
        pr_url: Pull request or merge request web URL.

    Returns:
        ``(host, repository_path, number)``; the number is None when the URL
        has no recognizable pull request segment.
    """
    normalized = normalize_pr_url(pr_url) or pr_url.strip()
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    wrapped = f"/{path}/"
    for marker in _PR_PATH_MARKERS:
        if marker in wrapped:
            repository, _, rest = wrapped.partition(marker)
            number = rest.strip("/").split("/")[0] or None
            return host, repository.strip("/"), number
    return host, path, None


def remote_link_global_id(pr_url: str) -> str:
    """Stable ``globalId`` for the remote link of a pull request.

    Keyed on the repository, not the pull request number: a later run on
    the same issue that opens a new pull request in the same repository
    updates the existing link rather than adding a second one.

    Args:
        pr_url: Pull request web URL.

    Returns:
        A globalId of at most 255 characters.
    """
    host, repository, _ = split_pull_request_url(pr_url)
    key = f"{host}/{repository}".lower().strip("/")
    return f"{GLOBAL_ID_PREFIX}{key}"[:_GLOBAL_ID_MAX]


def format_writeback_comment(pr_url: str, branch: Optional[str]) -> str:
    """Comment body naming the pull request and its branch.

    Args:
        pr_url: Pull request web URL.
        branch: Source branch of the pull request, when known.

    Returns:
        Plain text; ``JiraTracker.add_comment`` turns the URL into a link.
    """
    lines = [f"Pull request opened: {pr_url}"]
    if branch:
        lines.append(f"Branch: {branch}")
    return "\n".join(lines)


def jira_issue_key(trigger_event_details: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the key of the Jira issue a trigger snapshot refers to.

    Args:
        trigger_event_details: Execution trigger snapshot (event envelope).

    Returns:
        Issue key (or numeric id), or None when the trigger is not a Jira
        issue event.
    """
    if not isinstance(trigger_event_details, dict):
        return None
    if str(trigger_event_details.get("source") or "").lower() != "jira":
        return None
    payload = trigger_event_details.get("payload")
    if not isinstance(payload, dict):
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    key = str(issue.get("key") or issue.get("id") or "").strip()
    return key or None


async def write_pull_request_to_jira(
    *,
    jira_client: Any,
    issue_key: str,
    pr_url: str,
    branch: Optional[str],
    execution_id: str,
) -> JiraWritebackOutcome:
    """Comment on the Jira issue and upsert the remote link to the PR.

    Never raises: the execution status is already persisted and a Jira
    failure must not change it.

    Args:
        jira_client: Jira tracker client (``add_comment``, ``add_remote_link``).
        issue_key: Jira issue key.
        pr_url: Pull request web URL.
        branch: Source branch of the pull request, when known.
        execution_id: Execution id, for logs.

    Returns:
        Which writes succeeded.
    """
    outcome = JiraWritebackOutcome()
    if jira_client is None:
        outcome.skipped_reason = "no_jira_client"
        return outcome
    add_remote_link = getattr(jira_client, "add_remote_link", None)
    if not callable(add_remote_link):
        # Only the Jira client links remote objects. Anything else resolved
        # here is a code-host client, which must not receive a Jira key.
        outcome.skipped_reason = "not_a_jira_client"
        return outcome

    body = format_writeback_comment(pr_url, branch)
    try:
        await jira_client.add_comment(issue_key, scrub_secrets(body) or body)
        outcome.comment_posted = True
    except Exception:
        logger.warning(
            "Could not comment the pull request on Jira issue %s (execution %s)",
            issue_key,
            execution_id,
            exc_info=True,
        )

    _, repository, number = split_pull_request_url(pr_url)
    title = f"Pull request {repository}#{number}" if number else "Pull request"
    try:
        await add_remote_link(
            issue_key,
            global_id=remote_link_global_id(pr_url),
            url=pr_url,
            title=title,
            summary=f"Branch {branch}" if branch else None,
            resolved=False,
        )
        outcome.remote_link_written = True
    except Exception:
        logger.warning(
            "Could not link the pull request on Jira issue %s (execution %s). "
            "Issue linking must be enabled on the Jira site.",
            issue_key,
            execution_id,
            exc_info=True,
        )
    return outcome
