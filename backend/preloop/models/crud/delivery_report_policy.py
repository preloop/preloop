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
    db: Session, *, account_id: Any, project_id: Any = None
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
    return db.scalar(query)


def get_overrides(
    db: Session, *, account_id: Any, project_id: Any = None
) -> Optional[Dict[str, Any]]:
    """The stored overrides of the account row, or of one project row.

    Args:
        db: Session.
        account_id: Owning account.
        project_id: A project, or None for the account row.

    Returns:
        The overrides, or None when no row exists.
    """
    row = _row(db, account_id=account_id, project_id=project_id)
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
        }
        for row in rows
    ]


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
