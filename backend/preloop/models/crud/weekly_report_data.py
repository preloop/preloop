"""SQL-only report projections, scoped before grouping and percentile calculation."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Float, and_, case, cast, func, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.models.crud.api_usage import (
    cache_split_columns,
    cache_split_from_row,
    exclude_replay_usage_condition,
)
from preloop.models.crud.weekly_signals import approval_signals


def usage_snapshot(
    db: Session,
    *,
    account_id: UUID,
    start: datetime,
    end: datetime,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Read the same metrics for either window, optionally for one member.

    A user's gateway attribution is the stored ApiUsage.user_id. Unattributed
    traffic is never assigned to a member by guessing from a shared resource.
    All joins constrain account ownership, including legacy malformed links.
    """
    usage = models.ApiUsage
    equivalent = usage.meta_data["api_equivalent_cost"]
    covered = and_(
        usage.cost_source == "subscription", func.jsonb_typeof(equivalent) == "number"
    )
    columns = [
        func.count(usage.id).label("request_count"),
        func.coalesce(func.sum(usage.estimated_cost), 0).label(
            "total_gateway_cost_usd"
        ),
        func.coalesce(
            func.sum(case((covered, cast(equivalent.astext, Float)), else_=None)), 0
        ).label("subscription_absorbed_usd"),
        func.count(usage.id).filter(usage.status_code >= 400).label("error_count"),
        func.percentile_cont(0.95).within_group(usage.duration).label("p95_duration"),
        func.count(func.distinct(usage.runtime_session_id)).label("sessions"),
        func.count(func.distinct(usage.user_id)).label("developers"),
        func.count(usage.id)
        .filter(and_(usage.estimated_cost.is_(None), usage.total_tokens > 0))
        .label("unpriced_request_count"),
        *cache_split_columns(),
    ]
    predicates = [
        usage.account_id == account_id,
        usage.action_type == "model_gateway",
        usage.timestamp >= start.astimezone(timezone.utc).replace(tzinfo=None),
        usage.timestamp < end.astimezone(timezone.utc).replace(tzinfo=None),
        exclude_replay_usage_condition(),
    ]
    if user_id is not None:
        predicates.append(usage.user_id == user_id)
    row = db.query(*columns).filter(*predicates).one()
    result = {
        key: float(value or 0)
        if key
        in {"total_gateway_cost_usd", "subscription_absorbed_usd", "p95_duration"}
        else int(value or 0)
        for key, value in row._mapping.items()
    }
    result.update(cache_split_from_row(row))
    result["error_rate"] = (
        result["error_count"] / result["request_count"]
        if result["request_count"]
        else None
    )
    result["cache_hit_share"] = (
        result["cache_read_tokens"]
        / (result["cache_read_tokens"] + result["uncached_input_tokens"])
        if result["cache_read_tokens"] + result["uncached_input_tokens"]
        else None
    )
    # Every entity list is complete. Taking top five happens only in rendering,
    # so a ranking change is never mistaken for a newly active identity.
    dimensions = {
        "models": (usage.ai_model_id, usage.model_alias),
        "flows": (usage.flow_id, models.Flow.name),
    }
    for kind, keys in dimensions.items():
        query = db.query(
            *keys,
            func.count(usage.id).label("request_count"),
            func.coalesce(func.sum(usage.estimated_cost), 0).label("estimated_cost"),
        ).select_from(usage)
        if kind == "flows":
            query = query.outerjoin(
                models.Flow,
                and_(
                    models.Flow.id == usage.flow_id,
                    models.Flow.account_id == account_id,
                ),
            )
        rows = (
            query.filter(*predicates)
            .group_by(*keys)
            .order_by(func.count(usage.id).desc())
            .all()
        )
        items = []
        for item in rows:
            parts = item[: len(keys)]
            if not any(parts):
                continue
            identity = ":".join(str(part or "") for part in parts[:-1]) or str(
                parts[-1] or ""
            )
            name = parts[-1] or "Unknown"
            value = {
                "identity": identity,
                "name": str(name),
                "request_count": item.request_count,
                "estimated_cost": float(item.estimated_cost),
            }
            if kind == "models":
                value.update(
                    model_alias=name, ai_model_id=str(parts[0]) if parts[0] else None
                )
            elif kind == "flows":
                value.update(
                    flow_name=name, flow_id=str(parts[0]) if parts[0] else None
                )
            else:
                value.update(
                    agent_id=str(parts[1])
                    if parts[0] == "managed_agent" and parts[1]
                    else None
                )
            items.append(value)
        result[kind] = items
    result["agents"] = [
        row
        for row in crud_api_usage.get_gateway_usage_by_agent(
            db,
            account_id=str(account_id),
            start_date=start,
            end_date=end,
            user_id=str(user_id) if user_id else None,
            limit=None,
        )
        if row.get("name")
    ]
    return result


def report_approvals(
    db: Session,
    *,
    account_id: UUID,
    start: datetime,
    end: datetime,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Count approvals decided by or triggered by the member, once per request."""
    return approval_signals(
        db, account_id=account_id, start=start, end=end, user_id=user_id
    )._asdict()


def member_findings(
    db: Session,
    *,
    account_id: UUID,
    user_id: UUID,
    start: datetime,
    end: datetime,
    include_policy: bool,
) -> list[dict[str, Any]]:
    """Only explicitly attributed findings from the latest completed window."""
    if not include_policy:
        return []
    finding, run = models.GovernanceFinding, models.GovernanceReviewRun
    run_id = (
        select(run.id)
        .where(
            run.account_id == account_id,
            run.window_start < end,
            run.window_end > start,
            run.window_end <= end,
            run.started_at <= end,
        )
        .order_by(run.window_end.desc(), run.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = db.query(finding).filter(
        finding.account_id == account_id,
        finding.user_id == user_id,
        finding.run_id == run_id,
        finding.dismissed_at.is_(None),
    )
    # Evidence can reference other members in a shared session. Personal views
    # use the attributed recommendation only, never raw cross-user evidence.
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "category": row.category,
            "severity": row.severity,
            "title": row.title,
            "suggestion": row.suggestion,
            "user_id": str(user_id),
            "developer": "You",
            "evidence": [],
            "estimated_weekly_savings": row.estimated_weekly_savings,
            "rule_ref": row.rule_ref,
        }
        for row in query.order_by(finding.id).all()
    ]


def team_usage(
    db: Session,
    *,
    account_id: UUID,
    start: datetime,
    end: datetime,
) -> list[dict[str, float]]:
    """Unnamed per-member aggregates, including active members with no traffic."""
    user, usage = models.User, models.ApiUsage
    rows = (
        db.query(
            func.count(usage.id),
            func.coalesce(func.sum(usage.estimated_cost), 0),
            func.count(func.distinct(usage.runtime_session_id)),
        )
        .select_from(user)
        .outerjoin(
            usage,
            and_(
                usage.user_id == user.id,
                usage.account_id == account_id,
                usage.action_type == "model_gateway",
                usage.timestamp >= start.replace(tzinfo=None),
                usage.timestamp < end.replace(tzinfo=None),
                exclude_replay_usage_condition(),
            ),
        )
        .filter(user.account_id == account_id, user.is_active.is_(True))
        .group_by(user.id)
        .all()
    )
    return [
        {
            "request_count": float(row[0]),
            "total_gateway_cost_usd": float(row[1]),
            "sessions": float(row[2]),
        }
        for row in rows
    ]
