"""Roll up cost and cycle time per tracker issue (#958).

Executions that touch one tracker issue (triage, implementation, review,
repair turns, completion audits) are grouped into one ``issue_cost_rollup``
row per issue. Each finished execution leaves exactly one
``issue_cost_execution`` fact, keyed on its id, so recording it again (a
replay, a retry of the hook, a backfill) overwrites instead of adding. Pull
request timestamps live in ``issue_cost_pull_request`` rows, keyed on the
canonical PR URL, so approval and merge webhooks can land before or after
the executions that reference the PR.

Attribution order for one execution (first match wins):

1. A lifecycle binding (``IssueLifecycle.execution_id`` or the lifecycle
   envelope on the trigger) names the issue directly.
2. A repair turn (``_resume``), a delegated child or a retry inherits the
   issue of the execution it continues.
3. The trigger subject is an issue (issue opened, labeled, commented).
4. The trigger subject is a pull request that is already linked to one
   issue (the implementation that opened it claimed it).
5. The pull request text has exactly one closing reference ("Fixes #12").

Anything else, and any pull request two issues both claim, is left on the
pull request only and reported in the unassigned bucket. Cost is the sum of
``flow_execution.estimated_cost``; nothing else is added.

That sum is the known subtotal, not total spend. A run whose cost is unknown
(``estimated_cost`` is null, as for a subscription-backed run that has no
per-ticket price) contributes nothing to it, so every row, summary and the
unassigned bucket also report ``cost_coverage`` and how many of their runs
carry a known cost (#1057).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_flow_execution, crud_issue_cost
from preloop.schemas.issue_cost import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNKNOWN,
    CostCoverage,
    IssueCostExecutionRow,
    IssueCostReport,
    IssueCostRow,
    IssueCostSummary,
    IssueCostUnassigned,
)
from preloop.services.flow_pr_binding import normalize_pr_url
from preloop.services.issue_estimate import (
    estimate_config,
    label_names,
    read_estimate,
)
from preloop.services.issue_references import KIND_CLOSES, extract_issue_references

logger = logging.getLogger(__name__)

#: How many hops of resume, delegation and retry lineage are followed.
MAX_LINEAGE_DEPTH = 8

#: Row cap for one report; the response says when it was reached.
MAX_REPORT_ROWS = 5000

#: Row cap for one rebuild call.
MAX_REBUILD_EXECUTIONS = 2000

# Column widths of issue_cost_rollup.issue_key and the pull request keys.
MAX_ISSUE_KEY_LENGTH = 512
MAX_PR_KEY_LENGTH = 1000

LINK_LIFECYCLE = "lifecycle"
LINK_RESUME = "resume"
LINK_DELEGATED = "delegated"
LINK_RETRY = "retry"
LINK_TRIGGER_ISSUE = "trigger_issue"
LINK_PULL_REQUEST = "pull_request"
LINK_CLOSING_REFERENCE = "closing_reference"
LINK_AMBIGUOUS = "ambiguous"
LINK_UNASSIGNED = "unassigned"

APPROVAL_EVENT_TYPES = frozenset({"pull_request_approved", "merge_request_approved"})
MERGE_EVENT_TYPES = frozenset({"pull_request_merged", "merge_request_merged"})
REVIEW_EVENT_TYPE = "pull_request_review"

CSV_COLUMNS: tuple[str, ...] = (
    "tracker",
    "issue_key",
    "title",
    "project",
    "estimated_cost",
    "total_tokens",
    "run_count",
    "failed_run_count",
    "first_event_at",
    "pr_opened_at",
    "approved_at",
    "merged_at",
    "first_event_to_pr_opened_hours",
    "pr_opened_to_approved_hours",
    "approved_to_merged_hours",
    "issue_url",
    "pr_url",
    "pr_opened_at_source",
    "estimate_hours",
    "estimate_hours_source",
    "estimate_points",
    "estimate_points_source",
    "cost_coverage",
    "known_cost_run_count",
    "unknown_cost_run_count",
    "attributed_cost_usd",
)

UNASSIGNED_ISSUE_KEY = "(unassigned)"

#: ``IssueCostPullRequest.opened_at_source`` values. A forge time replaces a
#: bind or run-end time; otherwise the first recorded time wins.
OPENED_FORGE = "forge"
OPENED_BIND = "bind"
OPENED_RUN_END = "run_end"


# --- parsing ---------------------------------------------------------------


@dataclass(frozen=True)
class TriggerSubject:
    """What a trigger payload is about.

    Attributes:
        kind: ``issue`` or ``pull_request``.
        issue_key: Canonical issue key for an issue subject.
        title: Issue or pull request title.
        url: Issue URL, or the canonical pull request URL.
        pr_body: Pull request description, for closing references.
        pr_branch: Pull request source branch.
        pr_number: Pull request number (used to drop self references).
        repo_path: Repository path (``org/repo``), when known.
        host: Web host of the repository, when known.
        platform: ``github``, ``gitlab``, ``bitbucket`` or ``jira``.
        fields: Raw issue fields, for reading the tracker estimate.
        labels: Issue label names, for reading the tracker estimate.
    """

    kind: str
    issue_key: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    pr_body: Optional[str] = None
    pr_branch: Optional[str] = None
    pr_number: Optional[Any] = None
    repo_path: Optional[str] = None
    host: Optional[str] = None
    platform: Optional[str] = None
    fields: Optional[dict[str, Any]] = field(default=None, compare=False)
    labels: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class IssueTarget:
    """A tracker issue an execution or pull request is attributed to."""

    tracker_id: uuid.UUID
    issue_key: str
    issue_id: Optional[uuid.UUID] = None
    project_id: Optional[uuid.UUID] = None
    title: Optional[str] = None
    issue_url: Optional[str] = None


@dataclass(frozen=True)
class Attribution:
    """The outcome of attributing one execution."""

    link: str
    target: Optional[IssueTarget] = None
    pr_key: Optional[str] = None


def _pr_key(url: Optional[str]) -> str:
    """Canonical pull request URL, or "" when it cannot be stored."""
    key = normalize_pr_url(url)
    return key if len(key) <= MAX_PR_KEY_LENGTH else ""


def canonical_issue_key(key: Optional[str]) -> Optional[str]:
    """Canonical form of a tracker issue key.

    Repository paths are case-insensitive on both forges, Jira keys are
    upper case. ``Org/Repo#12`` and ``org/repo#12`` must be one issue.

    Args:
        key: ``path#number`` or a Jira key.

    Returns:
        The canonical key, or None for an empty, malformed or oversized key.
    """
    if not key or not isinstance(key, str):
        return None
    key = key.strip()
    if "#" in key:
        path, _, number = key.rpartition("#")
        path = path.strip().strip("/")
        number = number.strip()
        if not path or not number.isdigit():
            return None
        canonical = f"{path.lower()}#{number}"
    else:
        canonical = key.upper()
    if not canonical or len(canonical) > MAX_ISSUE_KEY_LENGTH:
        return None
    return canonical


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def _host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    return (urlparse(url).hostname or "").lower() or None


def parse_trigger_subject(details: Any) -> Optional[TriggerSubject]:
    """Find the issue or pull request a trigger payload is about.

    Args:
        details: ``FlowExecution.trigger_event_details`` or a webhook
            ``event_data`` dict (``source``, ``type``, ``payload``).

    Returns:
        The subject, or None when the payload names neither.
    """
    details = _dict(details)
    payload = _dict(details.get("payload"))
    if not payload:
        return None
    source = (_str(details.get("source")) or "").lower()

    if _dict(payload.get("pullrequest")) or source == "bitbucket":
        subject = _parse_bitbucket(payload)
        if subject is not None:
            return subject
    if _dict(payload.get("project")).get("path_with_namespace") or source == "gitlab":
        subject = _parse_gitlab(payload)
        if subject is not None:
            return subject
    if _dict(payload.get("repository")).get("full_name") or source == "github":
        subject = _parse_github(payload)
        if subject is not None:
            return subject
    return _parse_jira(payload)


def _parse_github(payload: dict[str, Any]) -> Optional[TriggerSubject]:
    repo_path = _str(_dict(payload.get("repository")).get("full_name"))
    pull = _dict(payload.get("pull_request"))
    issue = _dict(payload.get("issue"))
    if pull:
        url = _pr_key(_str(pull.get("html_url")))
        if not url:
            return None
        return TriggerSubject(
            kind="pull_request",
            title=_str(pull.get("title")),
            url=url,
            pr_body=_str(pull.get("body")),
            pr_branch=_str(_dict(pull.get("head")).get("ref")),
            pr_number=pull.get("number"),
            repo_path=repo_path,
            host=_host(url),
            platform="github",
        )
    if issue:
        if issue.get("pull_request"):
            url = _pr_key(
                _str(_dict(issue.get("pull_request")).get("html_url"))
                or _str(issue.get("html_url"))
            )
            if not url:
                return None
            return TriggerSubject(
                kind="pull_request",
                title=_str(issue.get("title")),
                url=url,
                pr_body=_str(issue.get("body")),
                pr_number=issue.get("number"),
                repo_path=repo_path,
                host=_host(url),
                platform="github",
            )
        number = _str(issue.get("number"))
        key = canonical_issue_key(f"{repo_path}#{number}") if repo_path else None
        if not key:
            return None
        return TriggerSubject(
            kind="issue",
            issue_key=key,
            title=_str(issue.get("title")),
            url=_str(issue.get("html_url")),
            repo_path=repo_path,
            platform="github",
            labels=tuple(label_names(issue.get("labels"))),
        )
    return None


def _parse_bitbucket(payload: dict[str, Any]) -> Optional[TriggerSubject]:
    """Bitbucket Cloud ``pullrequest:*`` deliveries (and comments on them)."""
    pull = _dict(payload.get("pullrequest"))
    if not pull:
        return None
    url = _pr_key(_str(_dict(_dict(pull.get("links")).get("html")).get("href")))
    if not url:
        return None
    return TriggerSubject(
        kind="pull_request",
        title=_str(pull.get("title")),
        url=url,
        pr_body=_str(pull.get("description")),
        pr_branch=_str(_dict(_dict(pull.get("source")).get("branch")).get("name")),
        pr_number=pull.get("id"),
        repo_path=_str(_dict(payload.get("repository")).get("full_name")),
        host=_host(url),
        platform="bitbucket",
    )


def _gitlab_pr(
    request: dict[str, Any], repo_path: Optional[str]
) -> Optional[TriggerSubject]:
    # Webhook notes carry both an API ``url`` and a browser ``web_url``; only
    # the browser form matches the stored publication URL.
    url = _pr_key(_str(request.get("web_url"))) or _pr_key(_str(request.get("url")))
    if not url:
        return None
    return TriggerSubject(
        kind="pull_request",
        title=_str(request.get("title")),
        url=url,
        pr_body=_str(request.get("description")),
        pr_branch=_str(request.get("source_branch")),
        pr_number=request.get("iid"),
        repo_path=repo_path,
        host=_host(url),
        platform="gitlab",
    )


def _gitlab_issue(
    issue: dict[str, Any], repo_path: Optional[str], labels: Any = None
) -> Optional[TriggerSubject]:
    iid = _str(issue.get("iid"))
    key = canonical_issue_key(f"{repo_path}#{iid}") if repo_path and iid else None
    if not key:
        return None
    return TriggerSubject(
        kind="issue",
        issue_key=key,
        title=_str(issue.get("title")),
        url=_str(issue.get("web_url")) or _str(issue.get("url")),
        repo_path=repo_path,
        platform="gitlab",
        fields=issue,
        # Issue webhooks list labels next to object_attributes; notes and
        # the API put them on the issue itself.
        labels=tuple(label_names(issue.get("labels")) or label_names(labels)),
    )


def _parse_gitlab(payload: dict[str, Any]) -> Optional[TriggerSubject]:
    repo_path = _str(_dict(payload.get("project")).get("path_with_namespace"))
    kind = (_str(payload.get("object_kind")) or "").lower()
    attributes = _dict(payload.get("object_attributes"))
    if kind == "merge_request":
        return _gitlab_pr(attributes, repo_path)
    if kind in {"issue", "work_item"}:
        return _gitlab_issue(attributes, repo_path, payload.get("labels"))
    if kind == "note":
        if _dict(payload.get("merge_request")):
            return _gitlab_pr(_dict(payload.get("merge_request")), repo_path)
        if _dict(payload.get("issue")):
            return _gitlab_issue(_dict(payload.get("issue")), repo_path)
    return None


def _parse_jira(payload: dict[str, Any]) -> Optional[TriggerSubject]:
    issue = _dict(payload.get("issue"))
    key = _str(issue.get("key"))
    if not key or "#" in key:
        return None
    canonical = canonical_issue_key(key)
    if not canonical:
        return None
    fields = _dict(issue.get("fields"))
    return TriggerSubject(
        kind="issue",
        issue_key=canonical,
        title=_str(fields.get("summary")),
        platform="jira",
        fields=fields,
        labels=tuple(label_names(fields.get("labels"))),
    )


def closing_issue_keys(subject: TriggerSubject) -> list[str]:
    """Distinct canonical keys a pull request says it closes.

    Only closing keywords count ("Fixes #12", "Closes ORG-7"). A bare
    mention or a branch name is too weak to charge an issue for the work.

    Args:
        subject: A pull request subject.

    Returns:
        Canonical keys in order of first appearance.
    """
    if subject.kind != "pull_request":
        return []
    references = extract_issue_references(
        description=subject.pr_body,
        title=subject.title,
        branch=subject.pr_branch,
        repo_path=subject.repo_path,
        host=subject.host,
        platform=subject.platform,
        self_number=subject.pr_number,
        limit=20,
    )
    keys: list[str] = []
    for reference in references:
        if reference.kind != KIND_CLOSES:
            continue
        key = canonical_issue_key(reference.key)
        if key and key not in keys:
            keys.append(key)
    return keys


def published_pr_keys(execution: Any) -> list[str]:
    """Canonical URLs of the pull requests an execution published.

    Reads ``result.pr_url`` (bound mid-run by the runner) and the trusted
    isolated publication (one URL, or one per repository).

    Args:
        execution: A flow execution.

    Returns:
        Distinct canonical pull request URLs.
    """
    result = _dict(getattr(execution, "result", None))
    candidates: list[Any] = [result.get("pr_url")]
    publication = _dict(result.get("trusted_publication"))
    candidates.append(publication.get("url"))
    candidates.append(publication.get("pr_url"))
    for repository in publication.get("repositories") or []:
        repository = _dict(repository)
        candidates.append(repository.get("url"))
        candidates.append(repository.get("pr_url"))
    keys: list[str] = []
    for candidate in candidates:
        key = _pr_key(_str(candidate))
        if key and key not in keys:
            keys.append(key)
    return keys


def parse_event_time(value: Any, *, now: datetime) -> datetime:
    """Timestamp a webhook reports for itself, else its arrival time.

    Forges put the review or merge time in the payload. A value that does
    not parse, or that lies in the future, falls back to ``now``.

    Args:
        value: ISO 8601 string from the payload, or None.
        now: Arrival time (timezone aware).

    Returns:
        A timezone-aware timestamp.
    """
    text = _str(value)
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed <= now + timedelta(minutes=5):
                return parsed
    return now


def parse_forge_time(value: Any, *, now: datetime) -> Optional[datetime]:
    """A pull request ``created_at`` the forge reported, or None.

    Unlike ``parse_event_time`` there is no fallback: a value that is
    missing, does not parse or lies in the future is not a forge time.
    Accepts ISO 8601 (``2026-09-01T08:00:00Z``), the older GitLab webhook
    form (``2026-09-01 08:00:00 UTC``) and datetimes.

    Args:
        value: The reported time.
        now: Current time (timezone aware).

    Returns:
        A timezone-aware timestamp, or None.
    """
    if isinstance(value, datetime):
        parsed: Optional[datetime] = value
    else:
        text = _str(value)
        if not text:
            return None
        if text.endswith(" UTC"):
            text = text[: -len(" UTC")] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    assert parsed is not None
    parsed = _aware(parsed)
    if parsed > now + timedelta(minutes=5):
        return None
    return parsed


def stamp_opened(
    pull: models.IssueCostPullRequest, stamp: datetime, source: str
) -> bool:
    """Set a pull request's "opened" time according to its source.

    The forge's own ``created_at`` is the truth, so it replaces a time
    Preloop recorded (the bind or the run's end) even when that time is
    earlier. Between two forge times the earlier wins. A bind or run-end
    time only fills an empty value, so a replay never moves it.

    Args:
        pull: The pull request row.
        stamp: The candidate time.
        source: ``forge``, ``bind`` or ``run_end``.

    Returns:
        True when the row changed.
    """
    current = _optional_aware(pull.opened_at)
    if current is None:
        replace = True
    elif source == OPENED_FORGE:
        replace = pull.opened_at_source != OPENED_FORGE or stamp < current
    else:
        replace = False
    if replace:
        pull.opened_at = stamp
        pull.opened_at_source = source
    return replace


def interval_hours(
    earlier: Optional[datetime], later: Optional[datetime]
) -> Optional[float]:
    """Hours between two milestones, blank when either is missing.

    A negative interval means the milestones were recorded out of order
    (for example an approval that predates the recorded publication); it
    is reported blank rather than as a misleading zero or negative.

    Args:
        earlier: Start milestone.
        later: End milestone.

    Returns:
        Hours rounded to two decimals, or None.
    """
    if earlier is None or later is None:
        return None
    seconds = (_aware(later) - _aware(earlier)).total_seconds()
    if seconds < 0:
        return None
    return round(seconds / 3600.0, 2)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_aware(value: Optional[datetime]) -> Optional[datetime]:
    return None if value is None else _aware(value)


def _uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# --- attribution -------------------------------------------------------------


def _target_from_issue(issue: models.Issue) -> Optional[IssueTarget]:
    key = canonical_issue_key(issue.key)
    if not key:
        return None
    return IssueTarget(
        tracker_id=issue.tracker_id,
        issue_key=key,
        issue_id=issue.id,
        project_id=issue.project_id,
        title=issue.title,
        issue_url=issue.external_url,
    )


def _target_from_rollup(rollup: models.IssueCostRollup) -> IssueTarget:
    return IssueTarget(
        tracker_id=rollup.tracker_id,
        issue_key=rollup.issue_key,
        issue_id=rollup.issue_id,
        project_id=rollup.project_id,
        title=rollup.title,
        issue_url=rollup.issue_url,
    )


def _resolve_key(
    db: Session,
    *,
    account_id: uuid.UUID,
    key: str,
    tracker_id: Optional[uuid.UUID],
    title: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[IssueTarget]:
    """Map a canonical key to an issue of the account.

    A synced issue row wins (it carries project, title and URL). Without
    one, the trigger's own tracker is used; a key that matches synced
    issues in two trackers is not guessed.
    """
    if tracker_id is not None:
        issues = crud_issue_cost.find_issues_by_key(
            db, account_id=account_id, issue_key=key, tracker_id=tracker_id
        )
        if len(issues) == 1:
            return _target_from_issue(issues[0])
        return IssueTarget(
            tracker_id=tracker_id, issue_key=key, title=title, issue_url=url
        )
    issues = crud_issue_cost.find_issues_by_key(
        db, account_id=account_id, issue_key=key
    )
    if len(issues) == 1:
        return _target_from_issue(issues[0])
    return None


def _trigger_tracker(
    db: Session, *, account_id: uuid.UUID, details: dict[str, Any]
) -> Optional[uuid.UUID]:
    tracker_id = _uuid(details.get("tracker_id"))
    if tracker_id is None:
        return None
    if not crud_issue_cost.tracker_belongs_to_account(
        db, account_id=account_id, tracker_id=tracker_id
    ):
        return None
    return tracker_id


def _lifecycle_target(
    db: Session, *, account_id: uuid.UUID, execution: models.FlowExecution
) -> Optional[IssueTarget]:
    issue = crud_issue_cost.lifecycle_issue_for_execution(
        db, account_id=account_id, execution_id=execution.id
    )
    if issue is None:
        details = _dict(execution.trigger_event_details)
        envelope = _dict(_dict(details.get("payload")).get("lifecycle")) or _dict(
            details.get("lifecycle_refinement")
        )
        issue_id = _uuid(envelope.get("issue_id"))
        if issue_id is not None:
            candidate = db.get(models.Issue, issue_id)
            if candidate is not None and crud_issue_cost.tracker_belongs_to_account(
                db, account_id=account_id, tracker_id=candidate.tracker_id
            ):
                issue = candidate
    return _target_from_issue(issue) if issue is not None else None


def _pull_request_attribution(
    db: Session,
    *,
    account_id: uuid.UUID,
    subject: TriggerSubject,
    tracker_id: Optional[uuid.UUID],
) -> Attribution:
    pr_key = subject.url
    if pr_key:
        pull = crud_issue_cost.get_pull_request(
            db, account_id=account_id, pr_key=pr_key
        )
        if pull is not None and pull.ambiguous:
            return Attribution(link=LINK_AMBIGUOUS, pr_key=pr_key)
        if pull is not None and pull.rollup_id is not None:
            rollup = db.get(models.IssueCostRollup, pull.rollup_id)
            if rollup is not None:
                return Attribution(
                    link=LINK_PULL_REQUEST,
                    target=_target_from_rollup(rollup),
                    pr_key=pr_key,
                )
    keys = closing_issue_keys(subject)
    if len(keys) > 1:
        return Attribution(link=LINK_AMBIGUOUS, pr_key=pr_key)
    if len(keys) == 1:
        target = _resolve_key(
            db, account_id=account_id, key=keys[0], tracker_id=tracker_id
        )
        if target is not None:
            return Attribution(
                link=LINK_CLOSING_REFERENCE, target=target, pr_key=pr_key
            )
    return Attribution(link=LINK_UNASSIGNED, pr_key=pr_key)


def resolve_execution(
    db: Session,
    *,
    account_id: uuid.UUID,
    execution: models.FlowExecution,
    depth: int = 0,
    visited: Optional[set[uuid.UUID]] = None,
) -> Attribution:
    """Decide which tracker issue an execution worked on.

    Args:
        db: Database session.
        account_id: The execution's account.
        execution: The execution to attribute.
        depth: Lineage hops already followed.
        visited: Executions already examined in this call. An ancestor
            reached through several pointers is resolved once; a repeat
            visit is a dead end (it either is on the current path or
            already failed to yield an issue).

    Returns:
        The attribution; ``target`` is None when the execution stays on
        its pull request only.
    """
    if visited is None:
        visited = set()
    visited.add(execution.id)
    details = _dict(execution.trigger_event_details)
    own_prs = published_pr_keys(execution)
    own_pr = own_prs[0] if own_prs else None

    target = _lifecycle_target(db, account_id=account_id, execution=execution)
    if target is not None:
        return Attribution(link=LINK_LIFECYCLE, target=target, pr_key=own_pr)

    resume = _dict(details.get("_resume"))
    lineage: list[tuple[str, Any, Optional[str]]] = [
        (
            LINK_RESUME,
            resume.get("execution_id") or resume.get("resume_root"),
            _pr_key(_str(resume.get("pr_url"))) or None,
        ),
        (LINK_DELEGATED, execution.parent_execution_id, None),
        (LINK_RETRY, execution.retry_of_execution_id, None),
    ]
    if depth < MAX_LINEAGE_DEPTH:
        for link, prior_id, pr_key in lineage:
            if not prior_id or str(prior_id) == str(execution.id):
                continue
            inherited = _inherit(
                db,
                account_id=account_id,
                prior_id=prior_id,
                depth=depth,
                visited=visited,
            )
            if inherited is not None:
                return Attribution(
                    link=link,
                    target=inherited.target,
                    pr_key=pr_key or own_pr or inherited.pr_key,
                )

    subject = parse_trigger_subject(details)
    tracker_id = _trigger_tracker(db, account_id=account_id, details=details)
    if subject is not None and subject.kind == "issue" and subject.issue_key:
        target = _resolve_key(
            db,
            account_id=account_id,
            key=subject.issue_key,
            tracker_id=tracker_id,
            title=subject.title,
            url=subject.url,
        )
        if target is not None:
            return Attribution(link=LINK_TRIGGER_ISSUE, target=target, pr_key=own_pr)
    if subject is not None and subject.kind == "pull_request":
        return _pull_request_attribution(
            db, account_id=account_id, subject=subject, tracker_id=tracker_id
        )
    return Attribution(link=LINK_UNASSIGNED, pr_key=own_pr)


def _inherit(
    db: Session,
    *,
    account_id: uuid.UUID,
    prior_id: Any,
    depth: int,
    visited: set[uuid.UUID],
) -> Optional[Attribution]:
    prior_uuid = _uuid(prior_id)
    if prior_uuid is None or prior_uuid in visited:
        return None
    prior = crud_issue_cost.get_execution(
        db, account_id=account_id, execution_id=prior_uuid
    )
    if prior is None:
        visited.add(prior_uuid)
        return None
    fact = crud_issue_cost.get_fact(db, execution_id=prior.id)
    if fact is not None and fact.rollup_id is not None:
        rollup = db.get(models.IssueCostRollup, fact.rollup_id)
        if rollup is not None:
            return Attribution(
                link=fact.link, target=_target_from_rollup(rollup), pr_key=fact.pr_key
            )
    attribution = resolve_execution(
        db,
        account_id=account_id,
        execution=prior,
        depth=depth + 1,
        visited=visited,
    )
    return attribution if attribution.target is not None else None


# --- writes --------------------------------------------------------------------


def _rollup_for(
    db: Session,
    *,
    account_id: uuid.UUID,
    target: IssueTarget,
    fallback_project_id: Optional[uuid.UUID] = None,
) -> models.IssueCostRollup:
    rollup = crud_issue_cost.get_or_create_rollup(
        db,
        account_id=account_id,
        tracker_id=target.tracker_id,
        issue_key=target.issue_key,
    )
    crud_issue_cost.describe_rollup(
        db,
        rollup=rollup,
        issue_id=target.issue_id,
        project_id=target.project_id or fallback_project_id,
        title=target.title,
        issue_url=target.issue_url,
    )
    return rollup


# ``Session.info`` key of the per-pass tracker estimate settings cache.
_TRACKER_SETTINGS_CACHE = "issue_cost_tracker_estimate_settings"


def _tracker_estimate_settings(
    db: Session, *, tracker_id: uuid.UUID
) -> tuple[str, dict[str, Any]]:
    """Tracker type and estimate configuration, cached for a scheduled pass.

    ``scheduled_rebuild`` puts a dict on ``db.info`` for the length of one
    pass, so each tracker's configuration is read once per pass instead of
    once per execution and issue row. Outside a pass every call reads it.
    """
    cache = db.info.get(_TRACKER_SETTINGS_CACHE)
    if not isinstance(cache, dict):
        return crud_issue_cost.tracker_estimate_settings(db, tracker_id=tracker_id)
    if tracker_id not in cache:
        cache[tracker_id] = crud_issue_cost.tracker_estimate_settings(
            db, tracker_id=tracker_id
        )
    return cache[tracker_id]


def observe_estimate(
    db: Session,
    *,
    rollup: models.IssueCostRollup,
    subject: Optional[TriggerSubject] = None,
) -> bool:
    """Refresh an issue row's estimate from what the tracker says.

    Two readings, applied in order so the second wins where both have a
    value: the trigger payload (only when it is about this very issue),
    then the synced issue row, which the tracker sync keeps current. A part
    neither reading states keeps its stored value; nothing is invented.

    Args:
        db: Database session. The caller commits.
        rollup: The issue row.
        subject: The trigger subject of the execution being recorded.

    Returns:
        True when the stored estimate changed.
    """
    tracker_type, raw_config = _tracker_estimate_settings(
        db, tracker_id=rollup.tracker_id
    )
    config = estimate_config(raw_config)
    readings = []
    if (
        subject is not None
        and subject.kind == "issue"
        and subject.issue_key == rollup.issue_key
    ):
        readings.append(
            read_estimate(
                tracker_type=tracker_type,
                fields=subject.fields,
                labels=subject.labels,
                config=config,
            )
        )
    if rollup.issue_id is not None:
        issue = crud_issue_cost.get_issue(db, issue_id=rollup.issue_id)
        meta = _dict(issue.meta_data) if issue is not None else {}
        if issue is not None:
            readings.append(
                read_estimate(
                    tracker_type=tracker_type,
                    fields=_dict(meta.get("estimate_fields")),
                    labels=label_names(meta.get("labels")),
                    config=config,
                )
            )
    changed = False
    for estimate in readings:
        if estimate.empty:
            continue
        changed |= crud_issue_cost.set_rollup_estimate(
            db,
            rollup=rollup,
            hours=estimate.hours,
            hours_source=estimate.hours_source,
            points=estimate.points,
            points_source=estimate.points_source,
        )
    return changed


def _trigger_project(
    db: Session, *, account_id: uuid.UUID, execution: models.FlowExecution
) -> Optional[uuid.UUID]:
    """The project the trigger resolved, when it is the account's own."""
    project_id = _uuid(_dict(execution.trigger_event_details).get("project_id"))
    if project_id is None:
        return None
    if not crud_issue_cost.project_belongs_to_account(
        db, account_id=account_id, project_id=project_id
    ):
        return None
    return project_id


def _claim_pull_request(
    db: Session,
    *,
    account_id: uuid.UUID,
    pull: models.IssueCostPullRequest,
    rollup_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Link a pull request to an issue; a second, different claim voids both.

    Returns:
        Rollup ids whose pull request timestamps or facts changed.
    """
    if pull.ambiguous or pull.rollup_id == rollup_id:
        return set()
    if pull.rollup_id is None:
        pull.rollup_id = rollup_id
        db.flush()
        return {rollup_id}
    previous = pull.rollup_id
    pull.rollup_id = None
    pull.ambiguous = True
    db.flush()
    logger.info(
        "Pull request is claimed by two issues; leaving it unassigned (rollups %s, %s)",
        previous,
        rollup_id,
    )
    touched = crud_issue_cost.unassign_pull_request_facts(
        db, account_id=account_id, pr_key=pull.pr_key
    )
    return touched | {previous, rollup_id}


def _recompute(db: Session, rollup_ids: Iterable[Optional[uuid.UUID]]) -> None:
    for rollup_id in sorted({r for r in rollup_ids if r is not None}, key=str):
        crud_issue_cost.recompute_rollup(db, rollup_id=rollup_id)


def record_execution_finished(
    db: Session, execution: models.FlowExecution
) -> Optional[models.IssueCostExecution]:
    """Record or refresh the fact of one finished execution.

    Idempotent on the execution id: calling it again with the same state
    changes nothing, and with new state (a repriced cost, a later status)
    replaces the fact instead of adding a second one.

    Args:
        db: Database session. The caller commits.
        execution: A terminal flow execution.

    Returns:
        The fact, or None when the execution is not terminal or has no flow.
    """
    if (
        str(execution.status or "").upper()
        not in crud_flow_execution.TERMINAL_EXECUTION_STATUSES
    ):
        return None
    flow = crud_issue_cost.get_flow(db, flow_id=execution.flow_id)
    if flow is None or flow.account_id is None:
        return None
    account_id = flow.account_id
    previous = crud_issue_cost.get_fact(db, execution_id=execution.id)
    attribution = resolve_execution(db, account_id=account_id, execution=execution)

    trigger_project = _trigger_project(db, account_id=account_id, execution=execution)
    rollup: Optional[models.IssueCostRollup] = None
    if attribution.target is not None:
        rollup = _rollup_for(
            db,
            account_id=account_id,
            target=attribution.target,
            fallback_project_id=trigger_project,
        )
        observe_estimate(
            db,
            rollup=rollup,
            subject=parse_trigger_subject(execution.trigger_event_details),
        )

    touched: set[Optional[uuid.UUID]] = {previous.rollup_id if previous else None}
    end_time = _optional_aware(execution.end_time)
    published = published_pr_keys(execution)
    claims = list(published)
    if attribution.link == LINK_CLOSING_REFERENCE and attribution.pr_key:
        claims.append(attribution.pr_key)
    for pr_key in claims:
        pull = crud_issue_cost.get_or_create_pull_request(
            db, account_id=account_id, pr_key=pr_key
        )
        if pull.opened_at is None and pr_key in published:
            # The runner normally records the publication time mid-run
            # (record_publication). A publication that reached this row
            # only through the final result falls back to the run's end.
            stamp_opened(pull, end_time or datetime.now(UTC), OPENED_RUN_END)
            db.flush()
            touched.add(pull.rollup_id)
        if rollup is not None:
            touched |= _claim_pull_request(
                db, account_id=account_id, pull=pull, rollup_id=rollup.id
            )

    link = attribution.link
    rollup_id = rollup.id if rollup is not None else None
    if attribution.pr_key and link in {LINK_PULL_REQUEST, LINK_CLOSING_REFERENCE}:
        pull = crud_issue_cost.get_pull_request(
            db, account_id=account_id, pr_key=attribution.pr_key
        )
        if pull is not None and pull.ambiguous:
            link, rollup_id = LINK_AMBIGUOUS, None

    project_id = (rollup.project_id if rollup is not None else None) or trigger_project
    fact = crud_issue_cost.upsert_fact(
        db,
        values={
            "account_id": account_id,
            "execution_id": execution.id,
            "flow_id": execution.flow_id,
            "rollup_id": rollup_id,
            "project_id": project_id,
            "pr_key": attribution.pr_key,
            "link": link,
            "status": str(execution.status or "").upper()[:32],
            "total_tokens": int(execution.total_tokens or 0),
            "estimated_cost": execution.estimated_cost,
            "start_time": _aware(execution.start_time or datetime.now(UTC)),
            "end_time": end_time,
        },
    )
    touched.add(rollup_id)
    _recompute(db, touched)
    return fact


def record_publication(
    db: Session,
    execution: models.FlowExecution,
    pr_url: str,
    *,
    now: Optional[datetime] = None,
    forge_opened_at: Any = None,
) -> Optional[models.IssueCostPullRequest]:
    """Stamp the moment an execution opened a pull request.

    With ``forge_opened_at`` (the pull request's ``created_at`` read from
    the forge), that time is used. Otherwise the bind time is used and the
    first recorded time wins, so a repeated handoff marker or a replay
    never moves "PR opened" later.

    Args:
        db: Database session. The caller commits.
        execution: The publishing execution.
        pr_url: The pull request URL the runner reported.
        now: Bind time, for tests.
        forge_opened_at: The forge's ``created_at`` (ISO text or datetime),
            when the caller read the pull request from the forge.

    Returns:
        The pull request row, or None when the URL is not a PR URL.
    """
    pr_key = _pr_key(pr_url)
    if not pr_key:
        return None
    flow = crud_issue_cost.get_flow(db, flow_id=execution.flow_id)
    if flow is None or flow.account_id is None:
        return None
    account_id = flow.account_id
    pull = crud_issue_cost.get_or_create_pull_request(
        db, account_id=account_id, pr_key=pr_key
    )
    touched: set[Optional[uuid.UUID]] = set()
    bound_at = now or datetime.now(UTC)
    forge_time = parse_forge_time(forge_opened_at, now=bound_at)
    changed = (
        stamp_opened(pull, forge_time, OPENED_FORGE)
        if forge_time is not None
        else stamp_opened(pull, bound_at, OPENED_BIND)
    )
    if changed:
        db.flush()
        touched.add(pull.rollup_id)
    attribution = resolve_execution(db, account_id=account_id, execution=execution)
    if attribution.target is not None:
        rollup = _rollup_for(db, account_id=account_id, target=attribution.target)
        observe_estimate(
            db,
            rollup=rollup,
            subject=parse_trigger_subject(execution.trigger_event_details),
        )
        touched |= _claim_pull_request(
            db, account_id=account_id, pull=pull, rollup_id=rollup.id
        )
    _recompute(db, touched)
    return pull


def record_pull_request_event(
    db: Session, event_data: dict[str, Any], *, now: Optional[datetime] = None
) -> Optional[models.IssueCostPullRequest]:
    """Record an approval, merge or "opened" time webhook on its pull request.

    Approval is GitHub ``pull_request_review`` with review state approved
    (webhooks send lower case ``approved``; the API spelling ``APPROVE`` is
    accepted too), GitLab ``merge_request_approved`` or Bitbucket Cloud
    ``pullrequest:approved`` (normalized to ``pull_request_approved``). Merge is
    ``pull_request_merged`` or ``merge_request_merged``. The earliest
    timestamp wins, so a redelivered webhook never moves a milestone.

    Every pull request event also carries the forge's ``created_at``, which
    replaces a bind or run-end "opened" time (``stamp_opened``). Events other
    than approval and merge only apply it to a pull request already on
    record.

    Args:
        db: Database session. The caller commits.
        event_data: Normalized webhook event (``type``, ``payload``,
            ``account_id``, ``tracker_id``, ``source``).
        now: Arrival time, for tests.

    Returns:
        The pull request row, or None when nothing was recorded.
    """
    event_type = _str(event_data.get("type")) or ""
    payload = _dict(event_data.get("payload"))
    review = _dict(payload.get("review"))
    approved = event_type in APPROVAL_EVENT_TYPES or (
        event_type == REVIEW_EVENT_TYPE
        and (_str(review.get("state")) or "").lower() in {"approved", "approve"}
    )
    merged = event_type in MERGE_EVENT_TYPES
    account_id = _uuid(event_data.get("account_id"))
    subject = parse_trigger_subject(event_data)
    if account_id is None or subject is None or subject.kind != "pull_request":
        return None
    if not subject.url:
        return None
    arrival = now or datetime.now(UTC)
    attributes = _dict(payload.get("object_attributes"))
    bitbucket_pull = _dict(payload.get("pullrequest"))
    created = parse_forge_time(
        _dict(payload.get("pull_request")).get("created_at")
        or attributes.get("created_at")
        or _dict(payload.get("merge_request")).get("created_at")
        or bitbucket_pull.get("created_on"),
        now=arrival,
    )
    if not approved and not merged:
        # Any other pull request event only corrects the "opened" time of a
        # pull request already on record; it never creates one.
        return _stamp_forge_opened(
            db, account_id=account_id, pr_key=subject.url, created=created
        )
    pull = crud_issue_cost.get_or_create_pull_request(
        db, account_id=account_id, pr_key=subject.url
    )
    if created is not None:
        stamp_opened(pull, created, OPENED_FORGE)
    if approved:
        # Bitbucket Cloud states the approval time on ``approval.date``.
        stamp = parse_event_time(
            review.get("submitted_at") or _dict(payload.get("approval")).get("date"),
            now=arrival,
        )
        if pull.approved_at is None or stamp < _aware(pull.approved_at):
            pull.approved_at = stamp
    if merged:
        stamp = parse_event_time(
            _dict(payload.get("pull_request")).get("merged_at")
            or attributes.get("merged_at")
            # Bitbucket Cloud has no merged_at; ``pullrequest:fulfilled``
            # is sent as the state changes, so updated_on is the merge.
            or bitbucket_pull.get("updated_on"),
            now=arrival,
        )
        if pull.merged_at is None or stamp < _aware(pull.merged_at):
            pull.merged_at = stamp
    db.flush()
    touched: set[Optional[uuid.UUID]] = {pull.rollup_id}
    if pull.rollup_id is None and not pull.ambiguous:
        # A pull request nobody claimed yet may still name one issue it
        # closes. Only link it to an issue that already has a rollup: a
        # webhook alone never creates an issue row.
        keys = closing_issue_keys(subject)
        if len(keys) == 1:
            tracker_id = _trigger_tracker(db, account_id=account_id, details=event_data)
            target = _resolve_key(
                db, account_id=account_id, key=keys[0], tracker_id=tracker_id
            )
            if target is not None:
                rollup = crud_issue_cost.find_rollup(
                    db,
                    account_id=account_id,
                    tracker_id=target.tracker_id,
                    issue_key=target.issue_key,
                )
                if rollup is not None:
                    touched |= _claim_pull_request(
                        db, account_id=account_id, pull=pull, rollup_id=rollup.id
                    )
    _recompute(db, touched)
    return pull


def _stamp_forge_opened(
    db: Session,
    *,
    account_id: uuid.UUID,
    pr_key: str,
    created: Optional[datetime],
) -> Optional[models.IssueCostPullRequest]:
    """Apply a webhook's ``created_at`` to a pull request already on record.

    Args:
        db: Database session. The caller commits.
        account_id: Owning account.
        pr_key: Normalized pull request URL.
        created: The forge's ``created_at``, or None.

    Returns:
        The pull request row when it changed, else None.
    """
    if created is None:
        return None
    pull = crud_issue_cost.get_pull_request(db, account_id=account_id, pr_key=pr_key)
    if pull is None or not stamp_opened(pull, created, OPENED_FORGE):
        return None
    db.flush()
    _recompute(db, {pull.rollup_id})
    return pull


def refresh_execution_cost(
    db: Session, *, execution_id: uuid.UUID, estimated_cost: Any
) -> None:
    """Carry a repriced execution cost into its fact and issue row.

    Args:
        db: Database session. The caller commits.
        execution_id: The repriced execution.
        estimated_cost: Its new ``estimated_cost`` (None for unknown).
    """
    rollup_id = crud_issue_cost.update_fact_cost(
        db, execution_id=execution_id, estimated_cost=estimated_cost
    )
    _recompute(db, [rollup_id])


# --- best-effort hooks -----------------------------------------------------


def _savepoint(db: Session, label: str, operation: Any) -> None:
    """Run a rollup write in a savepoint; never fail the caller."""
    try:
        with db.begin_nested():
            operation()
    except Exception:
        logger.warning(
            "Issue cost rollup %s failed; rebuild recovers it", label, exc_info=True
        )


def record_execution_finished_safely(db: Session, execution_id: Any) -> None:
    """Terminal-status hook for the orchestrator; commits its own write.

    Args:
        db: The orchestrator's session (the terminal status is committed).
        execution_id: The finished execution.
    """
    execution_uuid = _uuid(execution_id)
    if execution_uuid is None:
        return

    def operation() -> None:
        execution = db.get(models.FlowExecution, execution_uuid)
        if execution is not None:
            db.refresh(execution)
            record_execution_finished(db, execution)

    _savepoint(db, "execution finish", operation)
    try:
        db.commit()
    except Exception:
        logger.warning("Could not commit the issue cost rollup", exc_info=True)
        db.rollback()


def record_publication_safely(
    db: Session, execution_id: Any, pr_url: str, *, forge_opened_at: Any = None
) -> None:
    """Publication hook for ``record_opened_pr``; commits its own write.

    Args:
        db: The caller's session.
        execution_id: The publishing execution.
        pr_url: The bound pull request URL.
        forge_opened_at: The forge's ``created_at`` for the pull request,
            when the bind path read it from the forge.
    """
    execution_uuid = _uuid(execution_id)
    if execution_uuid is None:
        return

    def operation() -> None:
        execution = db.get(models.FlowExecution, execution_uuid)
        if execution is not None:
            record_publication(db, execution, pr_url, forge_opened_at=forge_opened_at)

    _savepoint(db, "publication", operation)
    try:
        db.commit()
    except Exception:
        logger.warning("Could not commit the issue cost rollup", exc_info=True)
        db.rollback()


def record_pull_request_event_safely(db: Session, event_data: dict[str, Any]) -> None:
    """Webhook hook for approval and merge events; commits its own write."""
    event_type = _str(_dict(event_data).get("type")) or ""
    if event_type not in APPROVAL_EVENT_TYPES | MERGE_EVENT_TYPES | {REVIEW_EVENT_TYPE}:
        return
    _savepoint(db, "webhook", lambda: record_pull_request_event(db, event_data))
    try:
        db.commit()
    except Exception:
        logger.warning("Could not commit the issue cost rollup", exc_info=True)
        db.rollback()


def refresh_execution_cost_safely(
    db: Session, *, execution_id: Any, estimated_cost: Any
) -> None:
    """Repricing hook; flushes inside the caller's transaction only."""
    execution_uuid = _uuid(execution_id)
    if execution_uuid is None:
        return
    _savepoint(
        db,
        "reprice",
        lambda: refresh_execution_cost(
            db, execution_id=execution_uuid, estimated_cost=estimated_cost
        ),
    )


def rebuild(
    db: Session,
    *,
    account_id: uuid.UUID,
    start: datetime,
    end: datetime,
    limit: int = MAX_REBUILD_EXECUTIONS,
) -> tuple[int, int, int]:
    """Record finished executions of a period that have no fact yet.

    Covers history from before this table existed and runs that ended on
    a path that does not pass the orchestrator's terminal hook.

    Args:
        db: Database session. The caller commits.
        account_id: Account to rebuild.
        start: Inclusive lower bound on execution start.
        end: Exclusive upper bound on execution start.
        limit: Maximum executions recorded in one call.

    Returns:
        ``(examined, recorded, failed)``; ``examined == limit`` means more
        remain. Each execution is recorded in its own savepoint, so one that
        fails is counted and skipped instead of aborting the window.
    """
    executions = crud_issue_cost.terminal_executions_without_fact(
        db,
        account_id=account_id,
        start=_aware(start).astimezone(UTC),
        end=_aware(end).astimezone(UTC),
        terminal_statuses=crud_flow_execution.TERMINAL_EXECUTION_STATUSES,
        limit=limit,
    )
    recorded = 0
    failed = 0
    for execution in executions:
        execution_id = execution.id
        try:
            with db.begin_nested():
                if record_execution_finished(db, execution) is not None:
                    recorded += 1
        except Exception:
            failed += 1
            logger.warning(
                "Issue cost rebuild skipped execution %s",
                execution_id,
                exc_info=True,
            )
    return len(executions), recorded, failed


@dataclass
class ScheduledRebuildSummary:
    """Counts of one scheduled rebuild pass, logged as one line."""

    accounts: int = 0
    # Another replica held the account's lock; benign.
    accounts_skipped: int = 0
    # The account's rebuild raised and was rolled back.
    accounts_failed: int = 0
    examined: int = 0
    recorded: int = 0
    failed: int = 0
    limit_reached: int = 0
    estimates_checked: int = 0
    estimates_changed: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counts, for logging."""
        return {
            "accounts": self.accounts,
            "accounts_skipped": self.accounts_skipped,
            "accounts_failed": self.accounts_failed,
            "examined": self.examined,
            "recorded": self.recorded,
            "failed": self.failed,
            "limit_reached": self.limit_reached,
            "estimates_checked": self.estimates_checked,
            "estimates_changed": self.estimates_changed,
        }


def rebuild_lock_key(account_id: uuid.UUID) -> str:
    """Advisory lock name that keeps two replicas off one account's rebuild."""
    return f"issue_cost_rebuild:{account_id}"


def scheduled_rebuild(
    db: Session,
    *,
    lookback: timedelta,
    per_account_limit: int = MAX_REBUILD_EXECUTIONS,
    max_accounts: int = 500,
    estimate_limit: int = 1000,
    now: Optional[datetime] = None,
) -> ScheduledRebuildSummary:
    """Record recent finished executions that no hook recorded.

    The terminal hooks are best effort: a failed savepoint, a crash between
    the status write and the hook, or a terminal path without a hook leaves
    an execution without a fact. This pass finds them for the last
    ``lookback`` and records them, one account at a time, each account in
    its own transaction under a try-lock so two replicas never rebuild the
    same account at once (the work is idempotent either way). It then
    re-reads the estimate of recent issues from their synced issue rows.

    Args:
        db: Database session. Committed per account.
        lookback: How far back to look, by execution start.
        per_account_limit: Executions recorded per account per pass.
        max_accounts: Accounts visited per pass.
        estimate_limit: Issue rows whose estimate is re-read per pass.
        now: Current time, for tests.

    Returns:
        The pass summary.
    """
    db.info[_TRACKER_SETTINGS_CACHE] = {}
    try:
        return _scheduled_rebuild(
            db,
            lookback=lookback,
            per_account_limit=per_account_limit,
            max_accounts=max_accounts,
            estimate_limit=estimate_limit,
            now=now,
        )
    finally:
        db.info.pop(_TRACKER_SETTINGS_CACHE, None)


def _scheduled_rebuild(
    db: Session,
    *,
    lookback: timedelta,
    per_account_limit: int,
    max_accounts: int,
    estimate_limit: int,
    now: Optional[datetime],
) -> ScheduledRebuildSummary:
    """The body of ``scheduled_rebuild``, run with the settings cache set."""
    summary = ScheduledRebuildSummary()
    end = now or datetime.now(UTC)
    start = end - lookback
    account_ids = crud_issue_cost.accounts_with_unrecorded_executions(
        db,
        start=start,
        end=end,
        terminal_statuses=crud_flow_execution.TERMINAL_EXECUTION_STATUSES,
        limit=max_accounts,
    )
    db.rollback()
    for account_id in account_ids:
        try:
            if not crud_issue_cost.try_account_lock(
                db, key=rebuild_lock_key(account_id)
            ):
                summary.accounts_skipped += 1
                db.rollback()
                continue
            examined, recorded, failed = rebuild(
                db,
                account_id=account_id,
                start=start,
                end=end,
                limit=per_account_limit,
            )
            db.commit()
        except Exception:
            logger.warning(
                "Scheduled issue cost rebuild failed for account %s",
                account_id,
                exc_info=True,
            )
            db.rollback()
            summary.accounts_failed += 1
            continue
        summary.accounts += 1
        summary.examined += examined
        summary.recorded += recorded
        summary.failed += failed
        if examined >= per_account_limit:
            summary.limit_reached += 1

    for rollup in crud_issue_cost.rollups_with_issue_since(
        db, since=start, limit=estimate_limit
    ):
        summary.estimates_checked += 1
        rollup_id = rollup.id
        try:
            with db.begin_nested():
                if observe_estimate(db, rollup=rollup):
                    summary.estimates_changed += 1
        except Exception:
            logger.warning(
                "Could not refresh the estimate of issue row %s",
                rollup_id,
                exc_info=True,
            )
    db.commit()
    if (
        summary.recorded
        or summary.failed
        or summary.accounts_failed
        or summary.estimates_changed
    ):
        logger.info("Issue cost scheduled rebuild: %s", summary.as_dict())
    return summary


# --- report --------------------------------------------------------------------


def cost_coverage(known_cost_runs: int, unknown_cost_runs: int) -> CostCoverage:
    """How much of a bucket's run count carries a cost estimate.

    Coverage is about execution-cost availability, never invoice accuracy: a
    complete bucket is still an estimate priced from published rates, and a
    subscription-backed run has no per-ticket price to find. A known zero
    counts as known, so a genuinely free run is never reported as unknown. An
    empty bucket is unknown, because nothing about it is priced.

    Args:
        known_cost_runs: Runs whose ``estimated_cost`` is not null.
        unknown_cost_runs: Runs with no ``estimated_cost``.

    Returns:
        ``complete`` when every run is priced, ``partial`` when both kinds are
        present and ``unknown`` when none is.
    """
    if known_cost_runs <= 0:
        return COVERAGE_UNKNOWN
    if unknown_cost_runs <= 0:
        return COVERAGE_COMPLETE
    return COVERAGE_PARTIAL


def attributed_cost(coverage: CostCoverage, subtotal: float) -> Optional[float]:
    """The subtotal, only when every contributing run had a cost.

    Args:
        coverage: The bucket's ``cost_coverage``.
        subtotal: The legacy ``estimated_cost`` subtotal.

    Returns:
        The subtotal for complete coverage, else None. A partial or unknown
        bucket has no attributable total, and reporting the subtotal there
        would read as "this is what the ticket cost".
    """
    return subtotal if coverage == COVERAGE_COMPLETE else None


@dataclass
class _Totals:
    tokens: int = 0
    cost: Decimal = Decimal("0")
    runs: int = 0
    failed: int = 0
    #: Runs of this bucket whose cost estimate is known and unknown.
    known_cost_runs: int = 0
    unknown_cost_runs: int = 0
    issues: set[uuid.UUID] = field(default_factory=set)

    def add(
        self,
        rollup_id: uuid.UUID,
        tokens: int,
        cost: Decimal,
        runs: int,
        failed: int,
        known_cost_runs: int,
        unknown_cost_runs: int,
    ) -> None:
        self.tokens += tokens
        self.cost += cost
        self.runs += runs
        self.failed += failed
        self.known_cost_runs += known_cost_runs
        self.unknown_cost_runs += unknown_cost_runs
        self.issues.add(rollup_id)

    @property
    def coverage(self) -> CostCoverage:
        """Whether every run behind these totals has a cost estimate."""
        return cost_coverage(self.known_cost_runs, self.unknown_cost_runs)

    def money(self) -> float:
        """The legacy subtotal of the priced runs."""
        return _money(self.cost)

    def attributed(self) -> Optional[float]:
        """The subtotal when coverage is complete, else None."""
        return attributed_cost(self.coverage, self.money())


def _money(value: Optional[Decimal]) -> float:
    return float(round(value or Decimal("0"), 4))


def _optional_float(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def build_report(
    db: Session,
    *,
    account_id: uuid.UUID,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    project_id: Optional[uuid.UUID] = None,
    flow_id: Optional[uuid.UUID] = None,
    include_execution_ids: bool = False,
    limit: int = MAX_REPORT_ROWS,
) -> IssueCostReport:
    """Issue rows, summaries and the unassigned bucket for one filter.

    An issue is in the period when its first event is. Its totals are
    lifetime totals, restricted to one flow when a flow filter is set, so
    the per-flow and per-project summaries are exact sums of the rows. Rows,
    summaries and the unassigned bucket also carry ``cost_coverage`` and the
    two counts behind it, read from the same account-scoped fact aggregates,
    so coverage follows the filter instead of being a stored lifetime value.

    Args:
        db: Database session.
        account_id: Account to report on.
        start: Inclusive lower bound on the first event.
        end: Exclusive upper bound on the first event.
        project_id: Only this project.
        flow_id: Only work of this flow.
        include_execution_ids: Attach contributing execution ids and the
            unassigned executions (JSON export). The unassigned totals are
            always computed.
        limit: Maximum issue rows.

    Returns:
        The report.
    """
    start = _optional_aware(start)
    end = _optional_aware(end)
    rollups = crud_issue_cost.list_rollups(
        db,
        account_id=account_id,
        start=start,
        end=end,
        project_id=project_id,
        flow_id=flow_id,
        limit=limit + 1,
    )
    truncated = len(rollups) > limit
    rollups = rollups[:limit]
    rollup_ids = [rollup.id for rollup in rollups]

    per_issue: dict[uuid.UUID, _Totals] = {rid: _Totals() for rid in rollup_ids}
    per_flow: dict[uuid.UUID, _Totals] = {}
    for (
        rid,
        fid,
        tokens,
        cost,
        runs,
        failed,
        known_cost_runs,
        unknown_cost_runs,
    ) in crud_issue_cost.fact_totals_by_rollup_and_flow(db, rollup_ids=rollup_ids):
        if flow_id is not None and fid != flow_id:
            continue
        per_issue[rid].add(
            rid, tokens, cost, runs, failed, known_cost_runs, unknown_cost_runs
        )
        per_flow.setdefault(fid, _Totals()).add(
            rid, tokens, cost, runs, failed, known_cost_runs, unknown_cost_runs
        )

    execution_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    if include_execution_ids:
        for fact, _name in crud_issue_cost.list_facts(
            db,
            account_id=account_id,
            rollup_ids=rollup_ids,
            flow_id=flow_id,
            limit=limit * 50,
        ):
            if fact.rollup_id is not None:
                execution_ids.setdefault(fact.rollup_id, []).append(fact.execution_id)

    unassigned_filter: dict[str, Any] = {
        "account_id": account_id,
        "start": start,
        "end": end,
        "project_id": project_id,
        "flow_id": flow_id,
    }
    # Totals come from an uncapped aggregate. The execution list is only
    # needed by the JSON export, so a plain report does not load it.
    unassigned_facts = (
        crud_issue_cost.list_facts(
            db, unassigned=True, limit=limit, **unassigned_filter
        )
        if include_execution_ids
        else []
    )
    (
        unassigned_tokens,
        unassigned_cost,
        unassigned_runs,
        unassigned_failed,
        unassigned_known_cost_runs,
        unassigned_unknown_cost_runs,
    ) = crud_issue_cost.unassigned_totals(db, **unassigned_filter)
    unassigned_totals = _Totals(
        tokens=unassigned_tokens,
        cost=unassigned_cost,
        runs=unassigned_runs,
        failed=unassigned_failed,
        known_cost_runs=unassigned_known_cost_runs,
        unknown_cost_runs=unassigned_unknown_cost_runs,
    )

    trackers, projects, flows = crud_issue_cost.names(
        db,
        tracker_ids=[rollup.tracker_id for rollup in rollups],
        project_ids=[rollup.project_id for rollup in rollups if rollup.project_id],
        flow_ids=list(per_flow),
    )

    rows: list[IssueCostRow] = []
    per_project: dict[Optional[uuid.UUID], _Totals] = {}
    for rollup in rollups:
        totals = per_issue[rollup.id]
        per_project.setdefault(rollup.project_id, _Totals()).add(
            rollup.id,
            totals.tokens,
            totals.cost,
            totals.runs,
            totals.failed,
            totals.known_cost_runs,
            totals.unknown_cost_runs,
        )
        tracker_name, tracker_type = trackers.get(rollup.tracker_id, ("", ""))
        rows.append(
            IssueCostRow(
                id=rollup.id,
                tracker_id=rollup.tracker_id,
                tracker_name=tracker_name,
                tracker_type=tracker_type,
                issue_key=rollup.issue_key,
                issue_id=rollup.issue_id,
                title=rollup.title,
                issue_url=rollup.issue_url,
                pr_url=rollup.pr_url,
                project_id=rollup.project_id,
                project_name=projects.get(rollup.project_id)
                if rollup.project_id
                else None,
                estimated_cost=totals.money(),
                cost_coverage=totals.coverage,
                known_cost_run_count=totals.known_cost_runs,
                unknown_cost_run_count=totals.unknown_cost_runs,
                attributed_cost_usd=totals.attributed(),
                total_tokens=totals.tokens,
                run_count=totals.runs,
                failed_run_count=totals.failed,
                first_event_at=_optional_aware(rollup.first_event_at),
                pr_opened_at=_optional_aware(rollup.pr_opened_at),
                approved_at=_optional_aware(rollup.approved_at),
                merged_at=_optional_aware(rollup.merged_at),
                first_event_to_pr_opened_hours=interval_hours(
                    rollup.first_event_at, rollup.pr_opened_at
                ),
                pr_opened_to_approved_hours=interval_hours(
                    rollup.pr_opened_at, rollup.approved_at
                ),
                approved_to_merged_hours=interval_hours(
                    rollup.approved_at, rollup.merged_at
                ),
                pr_opened_at_source=rollup.pr_opened_at_source,
                estimate_hours=_optional_float(rollup.estimate_hours),
                estimate_hours_source=rollup.estimate_hours_source,
                estimate_points=_optional_float(rollup.estimate_points),
                estimate_points_source=rollup.estimate_points_source,
                execution_ids=execution_ids.get(rollup.id, [])
                if include_execution_ids
                else None,
            )
        )

    def summary(
        key: Optional[uuid.UUID], name: str, totals: _Totals
    ) -> IssueCostSummary:
        return IssueCostSummary(
            id=key,
            name=name,
            issue_count=len(totals.issues),
            estimated_cost=totals.money(),
            cost_coverage=totals.coverage,
            known_cost_run_count=totals.known_cost_runs,
            unknown_cost_run_count=totals.unknown_cost_runs,
            attributed_cost_usd=totals.attributed(),
            total_tokens=totals.tokens,
            run_count=totals.runs,
            failed_run_count=totals.failed,
        )

    by_project = sorted(
        (
            summary(pid, projects.get(pid, "") if pid else "", totals)
            for pid, totals in per_project.items()
        ),
        key=lambda item: (-item.estimated_cost, item.name),
    )
    by_flow = sorted(
        (summary(fid, flows.get(fid, ""), totals) for fid, totals in per_flow.items()),
        key=lambda item: (-item.estimated_cost, item.name),
    )

    unassigned = IssueCostUnassigned(
        estimated_cost=unassigned_totals.money(),
        cost_coverage=unassigned_totals.coverage,
        known_cost_run_count=unassigned_totals.known_cost_runs,
        unknown_cost_run_count=unassigned_totals.unknown_cost_runs,
        attributed_cost_usd=unassigned_totals.attributed(),
        total_tokens=unassigned_totals.tokens,
        run_count=unassigned_totals.runs,
        failed_run_count=unassigned_totals.failed,
        executions=[_execution_row(fact, name) for fact, name in unassigned_facts],
    )
    return IssueCostReport(
        start=start,
        end=end,
        project_id=project_id,
        flow_id=flow_id,
        issues=rows,
        by_project=by_project,
        by_flow=by_flow,
        unassigned=unassigned,
        truncated=truncated or len(unassigned_facts) >= limit,
    )


def _execution_row(
    fact: models.IssueCostExecution, flow_name: str
) -> IssueCostExecutionRow:
    return IssueCostExecutionRow(
        execution_id=fact.execution_id,
        flow_id=fact.flow_id,
        flow_name=flow_name,
        status=fact.status,
        link=fact.link,
        pr_url=fact.pr_key,
        estimated_cost=None
        if fact.estimated_cost is None
        else _money(Decimal(fact.estimated_cost)),
        total_tokens=int(fact.total_tokens or 0),
        start_time=_aware(fact.start_time),
        end_time=_optional_aware(fact.end_time),
    )


def list_issue_executions(
    db: Session,
    *,
    account_id: uuid.UUID,
    rollup_id: uuid.UUID,
    flow_id: Optional[uuid.UUID] = None,
) -> Optional[list[IssueCostExecutionRow]]:
    """Contributing executions of one issue, oldest first.

    Args:
        db: Database session.
        account_id: Owning account.
        rollup_id: The issue row.
        flow_id: Only executions of this flow.

    Returns:
        The executions, or None when the issue row is not the account's.
    """
    rollup = crud_issue_cost.get_rollup(db, account_id=account_id, rollup_id=rollup_id)
    if rollup is None:
        return None
    return [
        _execution_row(fact, name)
        for fact, name in crud_issue_cost.list_facts(
            db, account_id=account_id, rollup_ids=[rollup.id], flow_id=flow_id
        )
    ]


def list_unassigned_executions(
    db: Session,
    *,
    account_id: uuid.UUID,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    project_id: Optional[uuid.UUID] = None,
    flow_id: Optional[uuid.UUID] = None,
    limit: int = MAX_REPORT_ROWS,
) -> list[IssueCostExecutionRow]:
    """Executions in the unassigned bucket of a report filter, oldest first.

    Uses the same filters as the bucket totals in ``build_report``, so the
    list adds up to the unassigned row (up to ``limit``).

    Args:
        db: Database session.
        account_id: Owning account.
        start: Inclusive lower bound on execution start.
        end: Exclusive upper bound on execution start.
        project_id: Only executions of this project.
        flow_id: Only executions of this flow.
        limit: Row cap.

    Returns:
        The executions, each with the reason it is unassigned in ``link``.
    """
    return [
        _execution_row(fact, name)
        for fact, name in crud_issue_cost.list_facts(
            db,
            account_id=account_id,
            unassigned=True,
            start=_optional_aware(start),
            end=_optional_aware(end),
            project_id=project_id,
            flow_id=flow_id,
            limit=limit,
        )
    ]


# --- export --------------------------------------------------------------------


def _csv_cell(value: Any) -> str:
    """Render one CSV cell and neutralize spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = _aware(value).isoformat()
    else:
        text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def report_to_csv(report: IssueCostReport) -> str:
    """Flat issue-grain CSV of a report, plus one unassigned row.

    The coverage columns are appended after the original ones, so a consumer
    that reads ``estimated_cost`` by name keeps its previous meaning: the
    known subtotal of the priced runs, never total spend.

    Args:
        report: The report to export.

    Returns:
        CSV text with a header row.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in report.issues:
        writer.writerow(
            [
                _csv_cell(value)
                for value in (
                    row.tracker_name or row.tracker_type,
                    row.issue_key,
                    row.title,
                    row.project_name,
                    row.estimated_cost,
                    row.total_tokens,
                    row.run_count,
                    row.failed_run_count,
                    row.first_event_at,
                    row.pr_opened_at,
                    row.approved_at,
                    row.merged_at,
                    row.first_event_to_pr_opened_hours,
                    row.pr_opened_to_approved_hours,
                    row.approved_to_merged_hours,
                    row.issue_url,
                    row.pr_url,
                    row.pr_opened_at_source,
                    row.estimate_hours,
                    row.estimate_hours_source,
                    row.estimate_points,
                    row.estimate_points_source,
                    row.cost_coverage,
                    row.known_cost_run_count,
                    row.unknown_cost_run_count,
                    row.attributed_cost_usd,
                )
            ]
        )
    if report.unassigned.run_count:
        bucket = report.unassigned
        # The unassigned row fills the issue-grain columns it has, the
        # appended coverage columns at the end, and leaves the cycle-time
        # columns in between blank.
        leading = (
            "",
            UNASSIGNED_ISSUE_KEY,
            "",
            "",
            bucket.estimated_cost,
            bucket.total_tokens,
            bucket.run_count,
            bucket.failed_run_count,
        )
        coverage = (
            bucket.cost_coverage,
            bucket.known_cost_run_count,
            bucket.unknown_cost_run_count,
            bucket.attributed_cost_usd,
        )
        blanks = ("",) * (len(CSV_COLUMNS) - len(leading) - len(coverage))
        writer.writerow([_csv_cell(value) for value in leading + blanks + coverage])
    return buffer.getvalue()


def report_to_json(report: IssueCostReport) -> str:
    """JSON export of a report, with contributing execution ids.

    Args:
        report: The report, built with ``include_execution_ids=True``.

    Returns:
        JSON text.
    """
    document = report.model_dump(mode="json")
    unassigned = document.get("unassigned") or {}
    unassigned["execution_ids"] = [
        item["execution_id"] for item in unassigned.get("executions") or []
    ]
    return json.dumps(document, indent=2, sort_keys=False)
