"""Bounded forge refresh of the pull request record.

Each tick claims at most ``REFRESH_BATCH`` due pull requests (see
``crud_pull_request_refresh.claim_due``), reads each from its forge with
``If-None-Match`` and records ``head_sha``, ``state`` and ``merged_at``. A 304
changes nothing but the bookkeeping. A throttled forge ends the tick for that
forge and defers its unread rows by the advised wait. Webhooks only make a
pull request due (``accelerate``); forge reads, never payloads, are the truth.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_forge_webhook_delivery,
    crud_pull_request_refresh,
    crud_tracker,
)
from preloop.models.crud.forge_reconciliation import (
    REFRESH_REARM,
    UNREADABLE_BACKOFF,
)
from preloop.services.flow_feedback import FEEDBACK_TYPES
from preloop.services.flow_feedback_provider import (
    bitbucket_terminal,
    canonical_bitbucket_head,
    feedback_tracker_client,
    github_terminal,
    gitlab_terminal,
)
from preloop.services.flow_pr_binding import normalize_pr_url
from preloop.sync.exceptions import TrackerError
from preloop.sync.trackers.utils import rate_limit_delay

logger = logging.getLogger(__name__)

#: Pull requests claimed and forge reads per tick. Fallback credentials and
#: Bitbucket commit lookups consume the same request budget.
REFRESH_BATCH = 25
#: Claimed rows that did not fit the request budget retry on the next tick.
REFRESH_BUDGET_RETRY = timedelta(minutes=1)
#: Bounds on a provider Retry-After applied to deferred rows.
RETRY_FLOOR = timedelta(minutes=1)
RETRY_CEILING = timedelta(hours=1)
#: Forge delivery claims kept for dedupe; forges redeliver within days.
DELIVERY_RETENTION = timedelta(days=7)
DELIVERY_PRUNE_BATCH = 1000

#: Normalized events that make a known pull request's record due: every
#: feedback event plus the lifecycle events the feedback loop does not read.
#: A reopen is the one that matters most, because a closed record is not
#: otherwise scheduled again.
REFRESH_EVENT_TYPES = FEEDBACK_TYPES | frozenset(
    {
        "pull_request_opened",
        "pull_request_reopened",
        "pull_request_ready_for_review",
        "merge_request_opened",
        "merge_request_reopened",
    }
)

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_BITBUCKET_HOSTS = frozenset({"bitbucket.org", "www.bitbucket.org"})


@dataclass(frozen=True)
class PullRequestRef:
    """Where a pull request lives and the API path that reads it."""

    provider: str
    host: str
    api_path: str


def parse_pr_key(pr_key: str) -> Optional[PullRequestRef]:
    """Map a normalized pull request URL to its forge and API path.

    Args:
        pr_key: ``https://github.com/o/r/pull/1``,
            ``https://<gitlab host>/g/p/-/merge_requests/1`` or
            ``https://bitbucket.org/w/r/pull-requests/1``.

    Returns:
        The reference, or None for a URL no supported forge shape matches.
    """
    parsed = urlparse(pr_key)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if not host or len(parts) < 4 or not parts[-1].isdigit():
        return None
    number = parts[-1]
    if host in _GITHUB_HOSTS and len(parts) == 4 and parts[2] == "pull":
        owner, repo = (quote(part, safe="") for part in parts[:2])
        return PullRequestRef("github", host, f"/repos/{owner}/{repo}/pulls/{number}")
    if host in _BITBUCKET_HOSTS and len(parts) == 4 and parts[2] == "pull-requests":
        workspace, repo = (quote(part, safe="") for part in parts[:2])
        return PullRequestRef(
            "bitbucket",
            host,
            f"/repositories/{workspace}/{repo}/pullrequests/{number}",
        )
    if len(parts) >= 5 and parts[-3:-1] == ["-", "merge_requests"]:
        project = quote("/".join(parts[:-3]), safe="")
        return PullRequestRef(
            "gitlab", host, f"/projects/{project}/merge_requests/{number}"
        )
    return None


def _tracker_host(tracker: models.Tracker) -> str:
    """Web host of a forge tracker's repositories."""
    if tracker.tracker_type == "github":
        return "github.com"
    if tracker.tracker_type == "bitbucket":
        return "bitbucket.org"
    url = (tracker.connection_details or {}).get("url") or tracker.url
    return (urlparse(url or "https://gitlab.com").hostname or "").lower()


def _parse_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def forge_facts(provider: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """``state``, ``head_sha`` and ``merged_at`` from a forge response.

    Args:
        provider: ``github``, ``gitlab`` or ``bitbucket``.
        body: The pull or merge request response.

    Returns:
        Keyword arguments for ``crud_pull_request_refresh.record``.
    """
    if provider == "github":
        terminal = github_terminal(body)
        head = (body.get("head") or {}).get("sha")
    elif provider == "gitlab":
        terminal = gitlab_terminal(body)
        head = body.get("sha")
    else:
        terminal = bitbucket_terminal(body)
        # A 12-character prefix; ``refresh_pull_requests`` resolves it.
        head = ((body.get("source") or {}).get("commit") or {}).get("hash")
    state = "merged" if terminal.merged else "closed" if terminal.closed else "open"
    return {
        "state": state,
        "head_sha": str(head)[:64] if head else None,
        "merged_at": _parse_time(terminal.merged_at),
    }


class _Readers:
    """Read clients per tracker, created lazily and reused within one tick."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.clients: dict[uuid.UUID, Any] = {}
        self.trackers: dict[uuid.UUID, list[models.Tracker]] = {}

    def candidates(self, account_id: uuid.UUID, ref: PullRequestRef) -> list[Any]:
        if account_id not in self.trackers:
            self.trackers[account_id] = [
                tracker
                for tracker in crud_tracker.get_for_account(
                    self.db, account_id=str(account_id)
                )
                if tracker.is_active
                and tracker.tracker_type in {"github", "gitlab", "bitbucket"}
            ]
        return [
            tracker
            for tracker in self.trackers[account_id]
            if tracker.tracker_type == ref.provider
            and _tracker_host(tracker) == ref.host
        ]

    async def client(self, tracker: models.Tracker) -> Any:
        """The tracker's read client, or None when it cannot authenticate."""
        if tracker.id not in self.clients:
            try:
                client = await feedback_tracker_client(self.db, tracker)
            except (TrackerError, ValueError) as error:
                logger.warning(
                    "Pull request refresh has no client for tracker %s: %s",
                    tracker.id,
                    type(error).__name__,
                )
                client = None
            self.clients[tracker.id] = client
        return self.clients[tracker.id]


def _not_readable(error: TrackerError) -> bool:
    """A 403/404 from one tracker: try the account's next one."""
    return getattr(error, "status_code", None) in {403, 404}


async def refresh_pull_requests(
    db: Session, *, now: Optional[datetime] = None, limit: int = REFRESH_BATCH
) -> dict[str, int]:
    """Refresh at most ``limit`` records using at most ``limit`` forge reads.

    Args:
        db: Database session.
        now: Current time; defaults to the wall clock.
        limit: Bound on claimed rows and API reads, including fallback tracker
            attempts and Bitbucket commit lookups. Transport authentication
            retries and redirects remain bounded by each provider client.

    Returns:
        Counts: ``claimed``, ``read`` (forge requests sent), ``updated``
        (200s), ``not_modified`` (304s), ``deferred`` and ``unreadable``.
    """
    now = now or datetime.now(UTC)
    counts = dict.fromkeys(
        ("claimed", "read", "updated", "not_modified", "deferred", "unreadable"), 0
    )
    rows = crud_pull_request_refresh.claim_due(db, now=now, limit=limit)
    counts["claimed"] = len(rows)
    claimed = [
        (
            row.id,
            row.account_id,
            row.pr_key,
            row.refresh_etag,
            row.head_sha,
        )
        for row in rows
    ]
    # ``claim_due`` stamped every claimed row with this due time. A row whose
    # due time differs by the time it is recorded was marked by a webhook
    # mid-read, and keeps that mark.
    claim = now + REFRESH_REARM
    readers = _Readers(db)
    throttled: dict[str, datetime] = {}
    for pull_request_id, account_id, pr_key, etag, known_head in claimed:
        ref = parse_pr_key(pr_key)
        if ref is None:
            crud_pull_request_refresh.defer(
                db,
                pull_request_ids=[pull_request_id],
                until=now + UNREADABLE_BACKOFF,
                claimed_due=claim,
            )
            counts["unreadable"] += 1
            continue
        if ref.host in throttled:
            crud_pull_request_refresh.defer(
                db,
                pull_request_ids=[pull_request_id],
                until=throttled[ref.host],
                claimed_due=claim,
            )
            counts["deferred"] += 1
            continue
        if counts["read"] >= limit:
            crud_pull_request_refresh.defer(
                db,
                pull_request_ids=[pull_request_id],
                until=now + REFRESH_BUDGET_RETRY,
                claimed_due=claim,
            )
            counts["deferred"] += 1
            continue
        response = client = None
        for tracker in readers.candidates(account_id, ref):
            if counts["read"] >= limit:
                break
            try:
                client = await readers.client(tracker)
                if client is None or not hasattr(client, "conditional_get"):
                    continue
                counts["read"] += 1
                response = await client.conditional_get(ref.api_path, etag)
                break
            except TrackerError as error:
                delay = rate_limit_delay(error)
                if delay is not None:
                    wait = min(
                        max(timedelta(seconds=delay), RETRY_FLOOR), RETRY_CEILING
                    )
                    throttled[ref.host] = now + wait
                    logger.warning(
                        "Pull request refresh throttled by %s; retry in %ss",
                        ref.host,
                        int(wait.total_seconds()),
                    )
                    break
                if not _not_readable(error):
                    logger.warning(
                        "Pull request refresh failed for %s: %s",
                        pr_key,
                        type(error).__name__,
                    )
                    break
        if response is None:
            if ref.host in throttled:
                until, key = throttled[ref.host], "deferred"
            elif counts["read"] >= limit:
                until, key = now + REFRESH_BUDGET_RETRY, "deferred"
            else:
                until, key = now + UNREADABLE_BACKOFF, "unreadable"
            crud_pull_request_refresh.defer(
                db, pull_request_ids=[pull_request_id], until=until, claimed_due=claim
            )
            counts[key] += 1
            continue
        if response.not_modified:
            crud_pull_request_refresh.record(
                db,
                claimed_due=claim,
                pull_request_id=pull_request_id,
                now=now,
                etag=response.etag,
                not_modified=True,
            )
            counts["not_modified"] += 1
            continue
        facts = forge_facts(ref.provider, response.body or {})
        head = facts["head_sha"]
        if (
            ref.provider == "bitbucket"
            and head
            and len(head) < 40
            and known_head
            and known_head.startswith(head.lower())
        ):
            # Unchanged head: keep the full hash already recorded.
            facts["head_sha"] = known_head
        elif ref.provider == "bitbucket" and head and len(head) < 40:
            # Bitbucket reports a prefix; resolve a changed head once so the
            # record holds the full hash the feedback loop also stores.
            if counts["read"] >= limit:
                # Do not cache the PR validator before its canonical head has
                # been read: a later 304 would leave that head unresolved.
                crud_pull_request_refresh.defer(
                    db,
                    pull_request_ids=[pull_request_id],
                    until=now + REFRESH_BUDGET_RETRY,
                    claimed_due=claim,
                )
                counts["deferred"] += 1
                continue
            counts["read"] += 1
            try:
                facts["head_sha"] = await canonical_bitbucket_head(
                    client, ref.api_path.rsplit("/pullrequests/", 1)[0], head
                )
            except (TrackerError, ValueError) as error:
                delay = rate_limit_delay(error)
                until, key = now + REFRESH_REARM, "unreadable"
                if delay is not None:
                    wait = min(
                        max(timedelta(seconds=delay), RETRY_FLOOR), RETRY_CEILING
                    )
                    until, key = now + wait, "deferred"
                    throttled[ref.host] = until
                # Retry before caching the new validator. Recording a terminal
                # state here would also stop refresh with the old head forever.
                crud_pull_request_refresh.defer(
                    db,
                    pull_request_ids=[pull_request_id],
                    until=until,
                    claimed_due=claim,
                )
                counts[key] += 1
                continue
        recorded, merged_now = crud_pull_request_refresh.record(
            db,
            claimed_due=claim,
            pull_request_id=pull_request_id,
            now=now,
            etag=response.etag,
            **facts,
        )
        if recorded is not None and merged_now:
            from preloop.services.fleet_code_activity import emit_merge_event
            from preloop.services.readiness.scheduler import schedule_pull_request

            emit_merge_event(
                db, account_id=account_id, pr_key=pr_key, at=recorded.merged_at
            )
            schedule_pull_request(db, recorded)
        counts["updated"] += 1
    crud_forge_webhook_delivery.prune(
        db, before=now - DELIVERY_RETENTION, limit=DELIVERY_PRUNE_BATCH
    )
    return counts


def pull_request_url(payload: Mapping[str, Any]) -> Optional[str]:
    """The pull request web URL a forge webhook payload names, if any."""
    for key in ("pull_request", "merge_request", "pullrequest", "object_attributes"):
        item = payload.get(key)
        if not isinstance(item, Mapping):
            continue
        links = item.get("links")
        html = links.get("html") if isinstance(links, Mapping) else None
        href = html.get("href") if isinstance(html, Mapping) else None
        for candidate in (item.get("html_url"), item.get("url"), href):
            key_url = normalize_pr_url(candidate) if candidate else ""
            if key_url and parse_pr_key(key_url) is not None:
                return key_url
    return None


def accelerate(db: Session, event: Mapping[str, Any]) -> bool:
    """Make the pull request a forge webhook names due for refresh now.

    Args:
        db: Database session; the mark is committed.
        event: Flow trigger event data with ``type``, ``account_id`` and
            ``payload``; only ``REFRESH_EVENT_TYPES`` accelerate.

    Returns:
        Whether a known pull request was marked due.
    """
    if event.get("type") not in REFRESH_EVENT_TYPES:
        return False
    account_id = event.get("account_id")
    payload = event.get("payload")
    if not account_id or not isinstance(payload, Mapping):
        return False
    pr_key = pull_request_url(payload)
    if pr_key is None:
        return False
    return crud_pull_request_refresh.request(
        db,
        account_id=uuid.UUID(str(account_id)),
        pr_key=pr_key,
        now=datetime.now(UTC),
    )
