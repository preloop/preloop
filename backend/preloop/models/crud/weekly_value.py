"""Account-scoped inputs and forge cache for the weekly outcome report.

No transcript walk: read compact results, stored attribution facts and only
successful publication/review tool activity. All windows are half-open UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models


def candidates(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Read executions completed and session tool activity observed in a window."""
    e, f = models.FlowExecution, models.Flow
    rows = db.execute(
        select(
            e.id,
            e.flow_id,
            e.result,
            e.trigger_event_details,
            e.estimated_cost,
            e.start_time,
            e.end_time,
            f.name,
        )
        .join(f, f.id == e.flow_id)
        .where(
            f.account_id == account_id,
            e.end_time >= start.astimezone(UTC).replace(tzinfo=None),
            e.end_time < end.astimezone(UTC).replace(tzinfo=None),
        )
    ).all()
    result = [
        {
            "kind": "flow",
            "id": str(execution.id),
            "title": execution.name,
            "cost": float(execution.estimated_cost)
            if execution.estimated_cost is not None
            else None,
            "payload": execution.result if isinstance(execution.result, dict) else {},
            "trigger": execution.trigger_event_details
            if isinstance(execution.trigger_event_details, dict)
            else {},
            "started_at": execution.start_time.replace(tzinfo=UTC).isoformat(),
            "ended_at": execution.end_time.replace(tzinfo=UTC).isoformat(),
        }
        for execution in rows
    ]
    # Execution.estimated_cost historically defaults to zero. A default with
    # no priced traffic is unknown, not a free run competing at the top.
    u = models.ApiUsage
    usage = (
        db.execute(
            select(
                u.flow_execution_id,
                func.sum(u.estimated_cost),
                func.count(u.id),
                func.count(u.estimated_cost),
            )
            .where(
                u.account_id == account_id,
                u.flow_execution_id.in_([execution.id for execution in rows]),
            )
            .group_by(u.flow_execution_id)
        ).all()
        if rows
        else []
    )
    known = {
        str(eid): float(cost)
        for eid, cost, total, priced in usage
        if total == priced and cost is not None
    }
    for item in result:
        if not item["cost"]:
            item["cost"] = known.get(item["id"])
    # Stable execution-to-PR facts survive incomplete result artifacts.
    facts = (
        db.scalars(
            select(models.IssueCostExecution).where(
                models.IssueCostExecution.account_id == account_id,
                models.IssueCostExecution.execution_id.in_(
                    [execution.id for execution in rows]
                ),
            )
        ).all()
        if rows
        else []
    )
    by_id = {str(fact.execution_id): fact for fact in facts}
    for item in result:
        fact = by_id.get(item["id"])
        item["attributed_pr"] = fact.pr_key if fact else None
    a, s, u = models.RuntimeSessionActivity, models.RuntimeSession, models.ApiUsage
    session_rows = db.execute(
        select(s.id, s.title, s.session_source_id).where(
            s.account_id == account_id,
            s.session_source_type != "flow_execution",
            or_(
                and_(s.started_at >= start, s.started_at < end),
                and_(s.last_activity_at >= start, s.last_activity_at < end),
            ),
        )
    ).all()
    sessions = {
        str(sid): {
            "kind": "session",
            "id": str(sid),
            "source_id": source_id,
            "title": title or f"Session {str(sid)[:8]}",
            "cost": None,
            "activities": [],
        }
        for sid, title, source_id in session_rows
    }
    # Keep the payload walk narrow, even for accounts with millions of tool calls.
    relevant = or_(
        *(
            a.tool_name.ilike(f"%{name}%")
            for name in (
                "create_pull_request",
                "create_merge_request",
                "review",
                "comment",
                "discussion",
            )
        )
    )
    activity = db.execute(
        select(
            a.runtime_session_id, a.flow_execution_id, a.tool_name, a.metadata_
        ).where(
            a.account_id == account_id,
            a.timestamp >= start,
            a.timestamp < end,
            a.activity_type == "tool_call",
            a.status.in_(["success", "succeeded", "completed"]),
            relevant,
        )
    ).all()
    flow_rows = {row["id"]: row for row in result}
    for sid, execution_id, tool, payload in activity:
        row = (
            flow_rows.get(str(execution_id)) if execution_id else sessions.get(str(sid))
        )
        if row is not None:
            row.setdefault("activities", []).append(
                {"tool": tool, "payload": payload or {}}
            )
    if sessions:
        costs = db.execute(
            select(
                u.runtime_session_id,
                func.sum(u.estimated_cost),
                func.count(u.id),
                func.count(u.estimated_cost),
            )
            .where(
                u.account_id == account_id,
                u.runtime_session_id.in_([sid for sid, _, _ in session_rows]),
            )
            .group_by(u.runtime_session_id)
        ).all()
        for sid, cost, total, priced in costs:
            if total == priced and cost is not None:
                sessions[str(sid)]["cost"] = float(cost)
    return result + list(sessions.values())


def cached(
    db: Session, *, account_id: UUID, pr_keys: list[str]
) -> dict[str, models.IssueCostPullRequest]:
    """Fetch all cached PRs in one account-scoped query."""
    p = models.IssueCostPullRequest
    return {
        row.pr_key: row
        for row in db.scalars(
            select(p).where(p.account_id == account_id, p.pr_key.in_(pr_keys))
        )
    }


def store(
    db: Session,
    *,
    account_id: UUID,
    pr_key: str,
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    """Upsert without overwriting issue attribution or committing the caller."""
    p = models.IssueCostPullRequest
    stmt = insert(p).values(
        account_id=account_id,
        pr_key=pr_key,
        value_evidence=evidence,
        value_observed_at=now,
    )
    db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_issue_cost_pull_request",
            set_={
                "value_evidence": stmt.excluded.value_evidence,
                "value_observed_at": stmt.excluded.value_observed_at,
            },
        )
    )
    db.flush()


def repository_contexts(db: Session, *, account_id: UUID) -> list[tuple[Any, Any, Any]]:
    """Only repositories synced through this account's live trackers."""
    p, o, t = models.Project, models.Organization, models.Tracker
    return [
        (row[0], row[1], row[2])
        for row in db.execute(
            select(p, o, t)
            .join(o, p.organization_id == o.id)
            .join(t, o.tracker_id == t.id)
            .where(
                t.account_id == account_id,
                t.is_deleted.is_(False),
                p.is_active.is_(True),
            )
        ).all()
    ]


def recorded_prs(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[str]:
    """Known pull requests with publication or merge evidence in this window."""
    p = models.IssueCostPullRequest

    return list(
        db.scalars(
            select(p.pr_key).where(
                p.account_id == account_id,
                or_(
                    and_(p.opened_at >= start, p.opened_at < end),
                    and_(p.merged_at >= start, p.merged_at < end),
                ),
            )
        )
    )


def commit_evidence(db: Session) -> None:
    """Persist the digest's cache after its account savepoint has been released."""
    db.commit()
