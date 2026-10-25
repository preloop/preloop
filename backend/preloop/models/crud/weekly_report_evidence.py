"""Account-scoped projections for weekly charts and tool trade-offs."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from sqlalchemy import Date, cast, func
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud.api_usage import exclude_customer_overhead_condition


def usage_rows(
    db: Session, *, account_id: UUID, start: datetime, end: datetime
) -> list[Any]:
    """Aggregate requests by day, session, principal, model and tool schema.

    Identical repeated requests become one measurement with a request count.
    Agent attribution uses one account-scoped lookup, never per-request SQL.
    """
    usage, session, flow = models.ApiUsage, models.RuntimeSession, models.Flow
    tools_meta = usage.meta_data["tools_meta"]
    dimensions = (
        usage.runtime_session_id,
        usage.user_id,
        cast(usage.timestamp, Date),
        usage.ai_model_id,
        usage.model_alias,
        usage.cost_source,
        tools_meta,
        session.title,
        session.started_at,
        usage.runtime_principal_type,
        usage.runtime_principal_id,
        usage.runtime_principal_name,
        flow.id,
        flow.name,
    )
    rows = (
        db.query(
            usage.runtime_session_id,
            usage.user_id,
            func.min(usage.timestamp).label("timestamp"),
            func.sum(usage.estimated_cost).label("estimated_cost"),
            func.count(usage.id).label("request_count"),
            usage.ai_model_id,
            usage.model_alias,
            func.sum(usage.prompt_tokens).label("prompt_tokens"),
            usage.cost_source,
            tools_meta.label("tools_meta"),
            session.title,
            session.started_at,
            usage.runtime_principal_type,
            usage.runtime_principal_id,
            usage.runtime_principal_name,
            flow.id.label("flow_id"),
            flow.name.label("flow_name"),
        )
        .outerjoin(
            session,
            (session.id == usage.runtime_session_id)
            & (session.account_id == account_id),
        )
        .outerjoin(flow, (flow.id == usage.flow_id) & (flow.account_id == account_id))
        .filter(
            usage.account_id == account_id,
            usage.action_type == "model_gateway",
            usage.timestamp >= start.astimezone(timezone.utc).replace(tzinfo=None),
            usage.timestamp < end.astimezone(timezone.utc).replace(tzinfo=None),
            exclude_customer_overhead_condition(),
        )
        .group_by(*dimensions)
        .order_by(func.min(usage.timestamp), usage.runtime_session_id)
        .all()
    )
    agents = (
        db.query(models.ManagedAgent)
        .filter(models.ManagedAgent.account_id == account_id)
        .order_by(models.ManagedAgent.id.desc())
        .all()
    )
    by_id = {str(agent.id): agent for agent in agents}
    by_session = {
        agent.runtime_session_id: agent for agent in agents if agent.runtime_session_id
    }
    by_principal = {
        (agent.session_source_type, agent.session_source_id): agent for agent in agents
    }
    result = []
    for row in rows:
        agent = (
            by_id.get(row.runtime_principal_id)
            if row.runtime_principal_type == "managed_agent"
            else None
        )
        agent = agent or by_session.get(row.runtime_session_id)
        agent = agent or by_principal.get(
            (row.runtime_principal_type, row.runtime_principal_id)
        )
        result.append(
            SimpleNamespace(
                **row._mapping,
                agent_id=agent.id if agent else None,
                agent_name=agent.display_name if agent else None,
            )
        )
    return result
