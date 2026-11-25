"""Fetch missing issues only within projects already synced to the caller."""

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_project
from preloop.services.azure_work_item_scope import resolve_work_item_project
from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerError,
    TrackerIssueIsPullRequestError,
)
from preloop.sync.scanner.core import TrackerClient
from preloop.utils.azure_devops_work_items import parse_work_item_url
from preloop.utils.issue_urls import parse_issue_url

logger = logging.getLogger(__name__)
ISSUE_LOOKUP_TIMEOUT_SECONDS = 20.0
_ISSUE_TRACKERS = ["github", "gitlab", "jira", "azure_devops"]


class IssueNotFoundOnTracker(HTTPException):
    """A qualified issue was absent from the provider's issues endpoint."""

    def __init__(self, tracker_type: str) -> None:
        self.tracker_type = tracker_type
        super().__init__(
            404,
            f"Issue was looked up on the {tracker_type} tracker and not found. "
            "If this is a pull or merge request, use get_pull_request.",
        )


def _issue_reference(identifier: str) -> tuple[str, str, str] | None:
    """Return exact project path, number/key and URL host, if qualified."""
    identifier = identifier.strip()
    host = ""
    if identifier.startswith(("https://", "http://")):
        azure = parse_work_item_url(identifier)
        parsed = parse_issue_url(identifier)
        if azure:
            return f"{azure[0]}/{azure[1]}", str(azure[2]), "dev.azure.com"
        if parsed is None:
            return None
        identifier, host = parsed.key, parsed.host
    match = re.fullmatch(r"([^#\s]+/[^#]+)#([1-9][0-9]*)", identifier)
    if match:
        return match[1], match[2], host
    match = re.fullmatch(r"([A-Z][A-Z0-9_]*)-[0-9]+", identifier)
    if match:
        return match[1], identifier, host
    return None


def _tracker_host(tracker: models.Tracker) -> str:
    """Web origin used by an issue tracker, including a configured port."""
    defaults = {"github": "https://github.com", "gitlab": "https://gitlab.com"}
    raw = (
        tracker.url
        or (tracker.connection_details or {}).get("url")
        or defaults.get(tracker.tracker_type, "")
    )
    host = urlparse(raw).netloc.lower()
    if tracker.tracker_type == "github" and host == "api.github.com":
        return "github.com"
    if tracker.tracker_type == "azure_devops":
        return "dev.azure.com"
    return host


def resolve_synced_issue_project(
    db: Session, identifier: str, account_id: Any
) -> tuple[models.Project, str] | None:
    """Resolve one account-owned project; never import a missing project.

    This is the decision point for the project-not-synced policy. Full paths
    and Jira project keys must match exactly; suffixes and bare numbers cannot
    trigger provider requests. Azure Boards uses the scanner's repository home.

    Args:
        db: Database session.
        identifier: Issue key or supported URL.
        account_id: Caller account used to scope every candidate.

    Returns:
        One project and provider issue reference, or None for ambiguous forms.

    Raises:
        HTTPException: 404 when the named project is not synced in this account.
    """
    reference = _issue_reference(identifier)
    if reference is None:
        return None
    path, number, host = reference
    candidates = crud_project.list_active_for_tracker_types(
        db, account_id=account_id, tracker_types=_ISSUE_TRACKERS
    )
    matches = []
    azure_seen: set[str] = set()
    for project in candidates:
        tracker = project.organization.tracker
        if host and host != _tracker_host(tracker):
            continue
        kind = tracker.tracker_type
        if kind == "azure_devops":
            org, separator, board = path.partition("/")
            details = tracker.connection_details or {}
            if (
                not separator
                or "/" in board
                or org.casefold() != str(details.get("organization") or "").casefold()
            ):
                continue
            if str(tracker.id) in azure_seen:
                continue
            azure_seen.add(str(tracker.id))
            home = resolve_work_item_project(db, tracker, board)
            if home is not None:
                matches.append(home)
        elif kind == "jira":
            if "/" not in path and path == project.slug:
                matches.append(project)
        elif "/" in path and path.casefold() == (project.slug or "").casefold():
            matches.append(project)
    if len(matches) > 1:
        return None
    if not matches:
        raise HTTPException(
            404,
            "The issue's project is not synced in this account. Sync the project first.",
        )
    return matches[0], number


async def sync_missing_issue(
    db: Session,
    identifier: str,
    current_user: Any,
    client_factory: Callable[..., Awaitable[Any]],
) -> models.Issue | None:
    """Fetch one qualified issue and ingest it using the scanner's code path.

    Args:
        db: Database session.
        identifier: Reference that was not found locally.
        current_user: Authenticated caller; never supplied by tool arguments.
        client_factory: Existing account/scope-checking tracker client factory.

    Returns:
        The stored issue, or None when the reference is ambiguous/unqualified.

    Raises:
        HTTPException: Safe 404, 502 or 504 diagnostic without provider bodies.
        TrackerIssueIsPullRequestError: GitHub identified a pull request.
    """
    form = "url" if identifier.strip().startswith("http") else "key"
    kind = "unresolved"
    outcome = "unresolved"
    try:
        resolved = resolve_synced_issue_project(db, identifier, current_user.account_id)
        if resolved is None:
            outcome = "ambiguous_or_unqualified"
            return None
        project, number = resolved
        tracker = project.organization.tracker
        kind = tracker.tracker_type
        async with asyncio.timeout(ISSUE_LOOKUP_TIMEOUT_SECONDS):
            client = await client_factory(
                project.organization_id,
                project.id,
                db,
                current_user,
                request_timeout=ISSUE_LOOKUP_TIMEOUT_SECONDS,
            )
            upstream = await client.get_issue(number)
        reference = _issue_reference(identifier)
        assert reference is not None
        project_path, issue_number, _ = reference
        expected_key = (
            issue_number if kind == "jira" else f"{project_path}#{issue_number}"
        )
        if upstream.key.casefold() != expected_key.casefold():
            outcome = "identity_mismatch"
            raise HTTPException(
                502, "The tracker returned an issue from a different project or number."
            )
        scanner = TrackerClient(tracker, client=client)
        issue, _ = scanner.ingest_issue(db, project, upstream._sync_data)
        outcome = "stored"
        return issue
    except TrackerIssueIsPullRequestError:
        outcome = "pull_request"
        raise
    except (TimeoutError, httpx.TimeoutException):
        outcome = "timeout"
        raise HTTPException(
            504, f"The {kind} tracker could not be reached: lookup timed out."
        ) from None
    except (TrackerError, httpx.RequestError) as exc:
        status = getattr(exc, "status_code", None)
        if isinstance(exc, TrackerAuthenticationError):
            status = 401
        outcome = f"upstream_{status}" if status else "unreachable"
        if status == 404:
            raise IssueNotFoundOnTracker(kind) from None
        suffix = f" (upstream status {status})" if status else " (no upstream status)"
        raise HTTPException(
            502,
            f"The {kind} tracker could not be reached or refused the lookup{suffix}.",
        ) from None
    except HTTPException as exc:
        outcome = f"rejected_{exc.status_code}"
        raise
    finally:
        logger.info(
            "Issue lookup fallback form=%s tracker=%s outcome=%s", form, kind, outcome
        )
