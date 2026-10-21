"""Account-scoped, grouped activity reads for Fleet Map windows and totals.

Every function issues one grouped query bounded by the account, a closed
``[since, until]`` window and a row limit. Naive timestamp columns store UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    null,
    or_,
    select,
)
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.sql.elements import Null
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.api_usage import (
    exclude_replay_usage_condition,
    gateway_action_condition,
)
from preloop.models.crud.fleet_work import ASSIGNED_LINKS

#: Statuses of a tool call that was allowed to run. Native hook rows are
#: recorded at PreToolUse with the decision's status.
RAN_STATUSES = ("succeeded", "allowed", "approved")


def _naive(value: datetime) -> datetime:
    """UTC wall time for a ``timestamp without time zone`` column."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _gateway_window(since: datetime, until: datetime) -> list[Any]:
    usage = models.ApiUsage
    return [
        gateway_action_condition(),
        exclude_replay_usage_condition(),
        usage.timestamp >= _naive(since),
        usage.timestamp <= _naive(until),
    ]


def _usage_flow_id() -> Any:
    """A ledger row's flow: its own column, else its execution's flow."""
    return func.coalesce(models.ApiUsage.flow_id, models.FlowExecution.flow_id)


def gateway_edges(
    db: Session,
    *,
    account_id: Any,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Distinct (model, principal, flow, user) combinations seen in the window.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        since: Inclusive window start.
        until: Inclusive window end.
        limit: Maximum groups, including a truncation sentinel.

    Returns:
        Rows of ``ai_model_id``, ``principal_type``, ``principal_id``,
        ``flow_id``, ``user_id``, ``requests`` and ``last_at`` (aware UTC).
    """
    usage, execution = models.ApiUsage, models.FlowExecution
    flow_id = _usage_flow_id()
    return list(
        db.execute(
            select(
                usage.ai_model_id,
                usage.runtime_principal_type.label("principal_type"),
                usage.runtime_principal_id.label("principal_id"),
                flow_id.label("flow_id"),
                usage.user_id,
                func.count().label("requests"),
                func.max(usage.timestamp).label("last_at"),
            )
            .select_from(usage)
            .outerjoin(execution, execution.id == usage.flow_execution_id)
            .where(usage.account_id == account_id, *_gateway_window(since, until))
            .group_by(
                usage.ai_model_id,
                usage.runtime_principal_type,
                usage.runtime_principal_id,
                flow_id,
                usage.user_id,
            )
            .order_by(func.max(usage.timestamp).desc())
            .limit(limit)
        ).all()
    )


def principal_sessions(
    db: Session,
    *,
    account_id: Any,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Latest runtime-session activity per runtime principal in the window."""
    s = models.RuntimeSession
    at: Any = func.max(func.coalesce(s.last_activity_at, s.started_at))
    return list(
        db.execute(
            select(
                s.runtime_principal_type.label("principal_type"),
                s.runtime_principal_id.label("principal_id"),
                at.label("last_at"),
            )
            .where(
                s.account_id == account_id,
                s.runtime_principal_type.is_not(None),
                func.coalesce(s.last_activity_at, s.started_at) >= _naive(since),
                s.started_at <= _naive(until),
            )
            .group_by(s.runtime_principal_type, s.runtime_principal_id)
            .order_by(at.desc())
            .limit(limit)
        ).all()
    )


def flow_runs(
    db: Session,
    *,
    account_id: Any,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Flows with a run started in the window: id, name, model, runs, last start."""
    e, f = models.FlowExecution, models.Flow
    last: Any = func.max(e.start_time)
    return list(
        db.execute(
            select(
                f.id.label("flow_id"),
                f.name,
                f.ai_model_id,
                func.count(e.id).label("runs"),
                last.label("last_at"),
            )
            .select_from(e)
            .join(f, (f.id == e.flow_id) & (f.account_id == account_id))
            .where(e.start_time.between(_naive(since), _naive(until)))
            .group_by(f.id, f.name, f.ai_model_id)
            .order_by(last.desc())
            .limit(limit)
        ).all()
    )


def _totals(usage: Any) -> list[Any]:
    """The ``FleetUsageTotals`` aggregate columns (see ``fleet_usage``)."""
    currency = func.coalesce(usage.currency, "USD")
    one_currency = func.count(func.distinct(currency)) == 1
    missing_cost = or_(
        usage.estimated_cost.is_(None),
        usage.cost_source.is_not_distinct_from("unpriced"),
        and_(
            usage.cost_source.is_(None),
            usage.estimated_cost == 0,
            usage.meta_data["budget"]["pricing_available"].astext.is_not_distinct_from(
                "false"
            ),
        ),
    )
    known_cost = case((missing_cost, None), else_=usage.estimated_cost)
    return [
        func.count().label("request_count"),
        func.sum(usage.prompt_tokens).label("input_tokens"),
        func.sum(usage.completion_tokens).label("output_tokens"),
        func.sum(usage.cache_read_tokens).label("cache_read_tokens"),
        func.sum(usage.cache_creation_tokens).label("cache_write_tokens"),
        case((one_currency, func.sum(known_cost))).label("estimated_cost"),
        case((one_currency, func.min(currency))).label("currency"),
        (
            func.bool_or(
                or_(
                    usage.prompt_tokens.is_(None),
                    usage.completion_tokens.is_(None),
                    missing_cost,
                )
            )
            | ~one_currency
        ).label("partial"),
    ]


UsageGrouping = Literal["principal", "flow", "user", "pull_request", "issue"]


def usage_totals(
    db: Session,
    *,
    account_id: Any,
    grouping: UsageGrouping,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[RowMapping]:
    """Gateway totals grouped by one attribution, never loading ledger rows.

    ``principal``: rows outside a flow run, by runtime principal type/id.
    ``flow``: rows of a flow run, by flow. ``user``: direct rows with no
    principal and no flow, by user. ``pull_request``/``issue``: rows of flow
    runs explicitly linked to that object (issue_cost_execution).

    Returns:
        Mappings with ``key_a``, ``key_b`` and the totals columns.
    """
    usage, execution = models.ApiUsage, models.FlowExecution
    x, r = models.IssueCostExecution, models.IssueCostRollup
    flow_id = _usage_flow_id()
    keys: list[Any]
    statement = select().select_from(usage)
    conditions: list[Any] = [usage.account_id == account_id]
    if grouping == "principal":
        keys = [usage.runtime_principal_type, usage.runtime_principal_id]
        conditions += [
            usage.runtime_principal_id.is_not(None),
            usage.flow_execution_id.is_(None),
            usage.flow_id.is_(None),
        ]
    elif grouping == "flow":
        statement = statement.outerjoin(
            execution, execution.id == usage.flow_execution_id
        )
        keys = [flow_id, null()]
        conditions.append(flow_id.is_not(None))
    elif grouping == "user":
        keys = [usage.user_id, null()]
        conditions += [
            usage.user_id.is_not(None),
            usage.runtime_principal_id.is_(None),
            usage.flow_execution_id.is_(None),
            usage.flow_id.is_(None),
        ]
    else:
        statement = statement.join(
            x,
            (x.execution_id == usage.flow_execution_id)
            & (x.account_id == account_id)
            & x.link.in_(ASSIGNED_LINKS),
        )
        if grouping == "pull_request":
            keys = [x.pr_key, null()]
            conditions.append(x.pr_key.is_not(None))
        else:
            statement = statement.join(
                r, (r.id == x.rollup_id) & (r.account_id == account_id)
            )
            keys = [r.issue_key, r.issue_url]
    statement = (
        statement.add_columns(
            cast(keys[0], String).label("key_a"),
            cast(keys[1], String).label("key_b"),
            *_totals(usage),
        )
        .where(*conditions, *_gateway_window(since, until))
        # Only real columns: a NULL literal in GROUP BY is a syntax error
        # once inlined (psycopg2).
        .group_by(*(key for key in keys if not isinstance(key, Null)))
        .order_by(func.count().desc())
        .limit(limit)
    )
    return list(db.execute(statement).mappings())


def tool_call_counts(
    db: Session,
    *,
    account_id: Any,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Tool calls allowed to run, by session principal (outside runs) or flow."""
    a, s, e = (
        models.RuntimeSessionActivity,
        models.RuntimeSession,
        models.FlowExecution,
    )
    principal_type = case((a.flow_execution_id.is_(None), s.runtime_principal_type))
    principal_id = case((a.flow_execution_id.is_(None), s.runtime_principal_id))
    return list(
        db.execute(
            select(
                principal_type.label("principal_type"),
                principal_id.label("principal_id"),
                e.flow_id,
                func.count().label("tool_calls"),
            )
            .select_from(a)
            .join(s, (s.id == a.runtime_session_id) & (s.account_id == account_id))
            .outerjoin(e, e.id == a.flow_execution_id)
            .where(
                a.account_id == account_id,
                a.activity_type == "tool_call",
                a.status.in_(RAN_STATUSES),
                a.timestamp.between(since, until),
            )
            .group_by(principal_type, principal_id, e.flow_id)
            .limit(limit)
        ).all()
    )


def approval_counts(
    db: Session,
    *,
    account_id: Any,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Approval requests raised in the window, by agent or flow and status."""
    q, e = models.ApprovalRequest, models.FlowExecution
    flow_id: Any = e.flow_id
    return list(
        db.execute(
            select(
                q.managed_agent_id,
                flow_id.label("flow_id"),
                cast(q.status, String).label("status"),
                func.count().label("count"),
            )
            .select_from(q)
            .outerjoin(e, cast(e.id, String) == q.execution_id)
            .where(
                q.account_id == account_id,
                q.requested_at.between(_naive(since), _naive(until)),
            )
            .group_by(q.managed_agent_id, flow_id, q.status)
            .limit(limit)
        ).all()
    )
