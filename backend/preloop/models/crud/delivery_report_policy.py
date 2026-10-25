"""Delivery Report policy rows, and the facts a deterministic report reads.

The policy rows store only overrides (see
:class:`preloop.models.models.DeliveryReportPolicySetting`); validation and
layering live in :mod:`preloop.services.delivery_report`. The fact readers
build the ``get_pull_request`` / ``get_issue`` shaped dicts that
:func:`preloop.services.delivery_report.assemble_report` takes, from what the
tracker sync and the PR ledger already stored, so a run that may not call a
model still produces the shipped, closed and open sections.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from preloop.models import models

#: Closed tracker states, matching the report's own reading of ``status``.
_CLOSED_STATES = ("closed", "done", "resolved", "completed")
#: Upper bound on rows a deterministic run reads per section.
MAX_FACT_ROWS = 500


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _row(
    db: Session, *, account_id: Any, project_id: Any = None, lock: bool = False
) -> Optional[models.DeliveryReportPolicySetting]:
    query = select(models.DeliveryReportPolicySetting).where(
        models.DeliveryReportPolicySetting.account_id == _uuid(account_id)
    )
    if project_id is None:
        query = query.where(models.DeliveryReportPolicySetting.project_id.is_(None))
    else:
        query = query.where(
            models.DeliveryReportPolicySetting.project_id == _uuid(project_id)
        )
    if lock:
        query = query.with_for_update()
    return db.scalar(query)


def get_overrides(
    db: Session, *, account_id: Any, project_id: Any = None, lock: bool = False
) -> Optional[Dict[str, Any]]:
    """The stored overrides of the account row, or of one project row.

    Args:
        db: Session.
        account_id: Owning account.
        project_id: A project, or None for the account row.
        lock: Hold the row until the caller commits, for a read-modify-write.

    Returns:
        The overrides, or None when no row exists.
    """
    row = _row(db, account_id=account_id, project_id=project_id, lock=lock)
    return dict(row.overrides or {}) if row is not None else None


def project_overrides(db: Session, *, account_id: Any) -> Dict[str, Dict[str, Any]]:
    """Every project row of the account, keyed by repository path.

    The key is the project's slug (``owner/repo`` on the forges), else its
    identifier, lower-cased, which is how report refs name a repository.

    Args:
        db: Session.
        account_id: Owning account.

    Returns:
        ``{repository: overrides}``.
    """
    rows = db.execute(
        select(models.DeliveryReportPolicySetting, models.Project)
        .join(
            models.Project,
            models.Project.id == models.DeliveryReportPolicySetting.project_id,
        )
        .where(models.DeliveryReportPolicySetting.account_id == _uuid(account_id))
    ).all()
    return {
        str(project.slug or project.identifier).lower(): dict(row.overrides or {})
        for row, project in rows
    }


def project_in_account(db: Session, *, account_id: Any, project_id: Any) -> bool:
    """Whether the project belongs to one of the account's trackers.

    Args:
        db: Session.
        account_id: Account asking.
        project_id: Project named in the request.

    Returns:
        True when the project is the account's.
    """
    found = db.scalar(
        select(models.Project.id)
        .join(models.Organization)
        .join(models.Tracker)
        .where(
            models.Project.id == _uuid(project_id),
            models.Tracker.account_id == _uuid(account_id),
        )
    )
    return found is not None


def save_overrides(
    db: Session,
    *,
    account_id: Any,
    overrides: Dict[str, Any],
    project_id: Any = None,
    user_id: Any = None,
) -> models.DeliveryReportPolicySetting:
    """Replace the stored overrides of the account row or a project row.

    The caller validates first and commits.

    Args:
        db: Session.
        account_id: Owning account.
        overrides: The validated overrides; replaces what was stored.
        project_id: A project, or None for the account row.
        user_id: Who saved.

    Returns:
        The row, flushed.
    """
    row = _row(db, account_id=account_id, project_id=project_id)
    if row is None:
        row = models.DeliveryReportPolicySetting(
            account_id=_uuid(account_id),
            project_id=_uuid(project_id) if project_id is not None else None,
        )
        db.add(row)
    row.overrides = dict(overrides)
    row.updated_by = _uuid(user_id) if user_id is not None else None
    db.flush()
    return row


def delete_overrides(db: Session, *, account_id: Any, project_id: Any = None) -> bool:
    """Drop a row so the next layer down applies again. The caller commits.

    Args:
        db: Session.
        account_id: Owning account.
        project_id: A project, or None for the account row.

    Returns:
        True when a row was removed.
    """
    row = _row(db, account_id=account_id, project_id=project_id)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def report_spend_since(db: Session, *, account_id: Any, since: datetime) -> float:
    """What the account's Delivery Report runs cost since ``since``.

    Reads each execution's recorded ``estimated_cost``; a run without one
    counts as zero.

    Args:
        db: Session.
        account_id: Owning account.
        since: Start of the period (the month start for the monthly budget).

    Returns:
        USD spent.
    """
    flows: Sequence[Any] = db.execute(
        select(models.Flow.id, models.Flow.agent_config).where(
            models.Flow.account_id == _uuid(account_id)
        )
    ).all()
    report_flows = [
        flow_id
        for flow_id, config in flows
        if isinstance(config, dict) and config.get("report_kind") == "delivery_report"
    ]
    if not report_flows:
        return 0.0
    since = since.replace(tzinfo=None) if since.tzinfo else since
    total = db.scalar(
        select(func.coalesce(func.sum(models.FlowExecution.estimated_cost), 0)).where(
            models.FlowExecution.flow_id.in_(report_flows),
            models.FlowExecution.start_time >= since,  # type: ignore[arg-type]
        )
    )
    return float(total or Decimal(0))


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _naive(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    )


def pull_request_facts(
    db: Session, *, account_id: Any, start: Optional[datetime], end: datetime
) -> List[Dict[str, Any]]:
    """Merged PRs in the window and open PRs, as report input dicts.

    Each dict carries ``url`` (the canonical PR URL the ledger keys on),
    ``merged_at``, ``state``, ``created_at``, ``head_sha`` and the
    ``linked_issues`` keys from the PR issue link table.

    Args:
        db: Session.
        account_id: Owning account.
        start: Window start, or None for no lower bound.
        end: Window end.

    Returns:
        At most :data:`MAX_FACT_ROWS` merged and as many open PRs.
    """
    pr = models.IssueCostPullRequest
    account = _uuid(account_id)
    merged_query = select(pr).where(
        pr.account_id == account, pr.merged_at.isnot(None), pr.merged_at <= end
    )
    if start is not None:
        merged_query = merged_query.where(pr.merged_at >= start)
    merged = db.scalars(merged_query.order_by(pr.merged_at).limit(MAX_FACT_ROWS)).all()
    still_open = db.scalars(
        select(pr)
        .where(pr.account_id == account, pr.state == "open", pr.merged_at.is_(None))
        .order_by(pr.opened_at)
        .limit(MAX_FACT_ROWS)
    ).all()
    rows = [*merged, *still_open]
    links: Dict[uuid.UUID, List[str]] = {}
    if rows:
        link = models.IssueCostPullRequestIssue
        for pr_id, key in db.execute(
            select(link.pull_request_id, models.Issue.key)
            .join(models.Issue, models.Issue.id == link.issue_id)
            .where(
                link.account_id == account,
                link.pull_request_id.in_([r.id for r in rows]),
            )
        ).all():
            if key:
                links.setdefault(pr_id, []).append(key)
    return [
        {
            "url": row.pr_key,
            "merged_at": _iso(row.merged_at),
            "state": row.state or ("merged" if row.merged_at else None),
            "created_at": _iso(row.opened_at),
            "head_sha": row.head_sha,
            "linked_issues": links.get(row.id, []),
            "origin": row.origin,
            # The detail columns, in the ``get_pull_request`` shape the
            # report's file and brief rules read. None where not observed.
            "title": row.title,
            "description": row.body,
            "author": row.author_login,
            "changes": (
                {
                    "changed_files": list(row.changed_files),
                    "files_changed": row.files_changed,
                }
                if isinstance(row.changed_files, list)
                else None
            ),
        }
        for row in rows
    ]


def pull_requests_missing_files(
    db: Session,
    *,
    account_id: Any,
    start: Optional[datetime],
    end: datetime,
    limit: int,
) -> List[models.IssueCostPullRequest]:
    """Merged PRs in the window whose file list was never read from the forge.

    A row the forge read already tried (``details_observed_at`` set) is not
    returned again, so a PR the forge cannot serve costs one read, not one
    per run.

    Args:
        db: Session.
        account_id: Owning account.
        start: Window start, or None.
        end: Window end.
        limit: Most rows.

    Returns:
        Newest merges first.
    """
    pr = models.IssueCostPullRequest
    query = select(pr).where(
        pr.account_id == _uuid(account_id),
        pr.merged_at.isnot(None),
        pr.merged_at <= end,
        pr.changed_files.is_(None),
        pr.details_observed_at.is_(None),
    )
    if start is not None:
        query = query.where(pr.merged_at >= start)
    return list(db.scalars(query.order_by(pr.merged_at.desc()).limit(limit)).all())


#: Detail columns a writer may set; anything else in ``details`` is ignored.
DETAIL_COLUMNS = ("title", "body", "author_login", "changed_files", "files_changed")


def store_pull_request_details(
    db: Session,
    *,
    pull: models.IssueCostPullRequest,
    details: Dict[str, Any],
    observed_at: Optional[datetime] = None,
) -> models.IssueCostPullRequest:
    """Write the detail columns the caller observed. The caller commits.

    Only keys present in ``details`` change, so a webhook without a body
    does not erase one a forge read stored.

    Args:
        db: Session.
        pull: The ledger row.
        details: Any of :data:`DETAIL_COLUMNS`.
        observed_at: Set for a forge read (attempted, even if it returned
            nothing); None for a webhook.

    Returns:
        The row, flushed.
    """
    for column in DETAIL_COLUMNS:
        if column in details:
            setattr(pull, column, details[column])
    if observed_at is not None:
        pull.details_observed_at = observed_at
    db.flush()
    return pull


def project_for_repository(
    db: Session, *, account_id: Any, repository: str
) -> Optional[tuple[models.Project, models.Organization, models.Tracker]]:
    """The account's synced project whose slug is ``owner/repo``.

    Args:
        db: Session.
        account_id: Owning account.
        repository: ``owner/repo`` from the PR URL.

    Returns:
        ``(project, organization, tracker)``, or None when not synced.
    """
    row = db.execute(
        select(models.Project, models.Organization, models.Tracker)
        .join(
            models.Organization,
            models.Organization.id == models.Project.organization_id,
        )
        .join(models.Tracker, models.Tracker.id == models.Organization.tracker_id)
        .where(
            models.Tracker.account_id == _uuid(account_id),
            func.lower(models.Project.slug) == repository.lower(),
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[0], row[1], row[2]


def report_recipients(
    db: Session,
    *,
    account_id: Any,
    recipient_rule: str,
    sponsoring_lead_user_id: Optional[str],
    opted_out_user_ids: Sequence[str],
) -> List[models.User]:
    """Who receives the report email, opt-outs removed.

    ``account_owner`` is the account's primary user. ``sponsoring_lead``
    is the named user when that user is an active member of the account,
    else the owner, so a departed lead does not silence the report.

    Args:
        db: Session.
        account_id: Owning account.
        recipient_rule: The policy's ``recipients``.
        sponsoring_lead_user_id: The policy's sponsoring lead.
        opted_out_user_ids: Users who opted out.

    Returns:
        Active users with an address; at most one today.
    """
    account = db.get(models.Account, _uuid(account_id))
    if account is None:
        return []
    candidates: List[Any] = []
    if recipient_rule == "sponsoring_lead" and sponsoring_lead_user_id:
        try:
            candidates.append(_uuid(sponsoring_lead_user_id))
        except ValueError:
            pass
    if account.primary_user_id is not None:
        candidates.append(account.primary_user_id)
    opted_out = {str(u) for u in opted_out_user_ids}
    for user_id in candidates:
        user = db.get(models.User, user_id)
        if (
            user is None
            or user.account_id != account.id
            or not user.is_active
            or not user.email
        ):
            continue
        # The first eligible candidate is the recipient. An opted-out lead
        # does not hand the report to the owner: opting out is a choice.
        if str(user.id) in opted_out:
            return []
        return [user]
    return []


def set_user_opt_out(
    db: Session, *, account_id: Any, user_id: Any, opted_out: bool
) -> List[str]:
    """Add or remove one user in the account policy's opt-out list.

    Writes the account row's overrides directly: the opt-out is the
    person's own choice and needs no budget permission. The row is locked
    so a concurrent opt-out or policy save cannot drop this change. The
    caller commits.

    Args:
        db: Session.
        account_id: Owning account.
        user_id: The person.
        opted_out: True to stop their report email.

    Returns:
        The stored opt-out list.
    """
    row = _row(db, account_id=account_id, lock=True)
    overrides = dict(row.overrides or {}) if row is not None else {}
    current = [str(u) for u in overrides.get("opted_out_user_ids") or []]
    key = str(user_id)
    if opted_out and key not in current:
        current.append(key)
    elif not opted_out:
        current = [u for u in current if u != key]
    if current:
        overrides["opted_out_user_ids"] = current
    else:
        overrides.pop("opted_out_user_ids", None)
    if row is None and not overrides:
        return []
    save_overrides(
        db,
        account_id=account_id,
        overrides=overrides,
        user_id=row.updated_by if row is not None else None,
    )
    return current


def issue_facts(
    db: Session, *, account_id: Any, start: Optional[datetime], end: datetime
) -> List[Dict[str, Any]]:
    """Issues closed in the window and open issues, as report input dicts.

    Open issues are read oldest activity first, which is the end the
    inactivity window cares about.

    Args:
        db: Session.
        account_id: Owning account.
        start: Window start, or None for no lower bound.
        end: Window end.

    Returns:
        At most :data:`MAX_FACT_ROWS` closed and as many open issues.
    """
    issue = models.Issue
    base = (
        select(issue, models.Project)
        .join(models.Project, models.Project.id == issue.project_id)
        .join(models.Tracker, models.Tracker.id == issue.tracker_id)
        .where(models.Tracker.account_id == _uuid(account_id))
    )
    touched = func.coalesce(issue.last_updated_external, issue.updated_at)
    closed_query = base.where(
        func.lower(issue.status).in_(_CLOSED_STATES), touched <= _naive(end)
    )
    if start is not None:
        closed_query = closed_query.where(touched >= _naive(start))
    closed = db.execute(closed_query.limit(MAX_FACT_ROWS)).all()
    still_open = db.execute(
        base.where(func.lower(issue.status).notin_(_CLOSED_STATES))
        .order_by(touched)
        .limit(MAX_FACT_ROWS)
    ).all()
    return [
        {
            "key": row.key,
            "title": row.title,
            "url": row.external_url,
            "status": row.status,
            "description": row.description,
            "updated_at": _iso(row.last_updated_external or row.updated_at),
            "project": str(project.slug or project.identifier).lower(),
        }
        for row, project in [*closed, *still_open]
    ]


def credit_available(db: Session, *, account_id: Any) -> Optional[float]:
    """Hosted credit available, or None when the account has no wallet.

    An account without a hosted credit wallet pays its provider directly
    (self-hosted, or its own keys), so credit never blocks its assessments.

    Args:
        db: Session.
        account_id: Owning account.

    Returns:
        Available USD, or None.
    """
    row = db.scalar(
        select(models.HostedCreditAccount).where(
            models.HostedCreditAccount.account_id == _uuid(account_id)
        )
    )
    if row is None:
        return None
    return float(row.balance - row.reserved)


def user_in_account(db: Session, *, account_id: Any, user_id: Any) -> bool:
    """Whether the user row belongs to the account.

    Args:
        db: Session.
        account_id: Account the opt-out link names.
        user_id: User the link names.

    Returns:
        True when the user is the account's.
    """
    user = db.get(models.User, _uuid(user_id))
    return user is not None and user.account_id == _uuid(account_id)
