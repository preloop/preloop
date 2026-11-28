"""Bounded, account-scoped discovery facts projected without session content."""

from datetime import UTC, datetime
from typing import Any, Collection
from uuid import UUID

from sqlalchemy import Row, and_, case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from preloop.models import models
from preloop.models.crud.fleet_map import WORK_REF_STATUSES

# Per input, plus a sentinel so callers can report partial coverage.
FACT_LIMIT = 500


def _independent_session(account_id: UUID) -> ColumnElement[bool]:
    """Exclude flow-owned timelines, linked hook conversations and examples."""
    s, a = models.RuntimeSession, models.RuntimeSessionActivity
    usage = models.ApiUsage
    return and_(
        s.account_id == account_id,
        s.session_source_type.not_in(("flow_execution", "example", "api_key")),
        s.runtime_principal_type.is_distinct_from("flow_execution"),
        ~select(a.id)
        .where(
            a.runtime_session_id == s.id,
            a.account_id == account_id,
            a.flow_execution_id.is_not(None),
        )
        .correlate(s)
        .exists(),
        ~select(usage.id)
        .where(
            usage.runtime_session_id == s.id,
            usage.account_id == account_id,
            usage.flow_execution_id.is_not(None),
        )
        .correlate(s)
        .exists(),
    )


def sessions(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[Row[Any]]:
    """Read original launch or last activity time and any runner checkout."""
    s, r = models.RuntimeSession, models.RunnerRemoteSession
    start, end = (t.astimezone(UTC).replace(tzinfo=None) for t in (start, end))
    at = case((s.last_activity_at <= end, s.last_activity_at), else_=s.started_at)
    return list(
        db.execute(
            select(
                s.id,
                s.desktop_endpoint_id,
                at.label("at"),
                func.coalesce(s.start_directory, s.cwd).label("directory"),
                r.workspace["repository"].astext.label("repository"),
                r.workspace["tracker_id"].astext.label("tracker_id"),
            )
            .outerjoin(
                r,
                and_(
                    r.runtime_session_id == s.id,
                    r.account_id == account_id,
                    r.workspace_kind == "tracker_checkout",
                ),
            )
            .where(_independent_session(account_id), at.between(start, end))
            .order_by(at.desc(), s.id)
            .limit(FACT_LIMIT + 1)
        )
    )


def work_items(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[Row[Any]]:
    """Read classified work attempts, including sessions still active after end."""
    s, a = models.RuntimeSession, models.RuntimeSessionActivity
    return list(
        db.execute(
            select(
                a.id,
                a.runtime_session_id,
                a.timestamp,
                a.status,
                a.metadata_["work_ref"].label("work"),
            )
            .join(s, s.id == a.runtime_session_id)
            .where(
                _independent_session(account_id),
                a.account_id == account_id,
                a.activity_type == "tool_call",
                a.status.in_(WORK_REF_STATUSES),
                a.metadata_.has_key("work_ref"),
                a.timestamp.between(start, end),
            )
            .order_by(a.timestamp.desc(), a.id)
            .limit(FACT_LIMIT + 1)
        )
    )


def issues(
    db: Session,
    *,
    account_id: UUID,
    project_ids: Collection[UUID],
    start: datetime,
    end: datetime,
) -> list[Row[Any]]:
    """Read provider updates on visible projects, including transferred issues."""
    i, t = models.Issue, models.Tracker
    return list(
        db.execute(
            select(i.id, i.project_id, i.external_url, i.key, i.last_updated_external)
            .join(t, t.id == i.tracker_id)
            .where(
                t.account_id == account_id,
                i.project_id.in_(project_ids),
                i.last_updated_external.between(
                    start.astimezone(UTC).replace(tzinfo=None),
                    end.astimezone(UTC).replace(tzinfo=None),
                ),
            )
            .order_by(i.last_updated_external.desc(), i.id)
            .limit(FACT_LIMIT + 1)
        )
    )


def merges(
    db: Session,
    *,
    account_id: UUID,
    repositories: Collection[str],
    start: datetime,
    end: datetime,
) -> list[Row[Any]]:
    """Read original merge times from the Delivery Report's attribution ledger."""
    pr = models.IssueCostPullRequest
    # The ledger keys on canonical forge URLs. Scope before taking the newest
    # rows so a busy excluded repository cannot crowd out a visible merge.
    base_url = func.regexp_replace(
        func.lower(pr.pr_key),
        r"(/-)?/(pulls?|merge_requests|pull-requests)/[0-9]+/?$",
        "",
    )
    visible = [
        f"{scheme}://{name}" for name in repositories for scheme in ("http", "https")
    ]
    return list(
        db.execute(
            select(pr.id, pr.pr_key, pr.merged_at)
            .where(
                pr.account_id == account_id,
                pr.merged_at.between(start, end),
                pr.origin != "preloop_flow",
                base_url.in_(visible),
            )
            .order_by(pr.merged_at.desc(), pr.id)
            .limit(FACT_LIMIT + 1)
        )
    )
