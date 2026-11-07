"""Account-scoped reads behind the cost per work item report.

A work item is a tracker issue or a pull/merge request. Two sources say which
spend worked on one:

* ``issue_cost_execution``: a flow execution attributed to an issue rollup
  and/or a pull request.
* ``runtime_session_activity`` tool calls carrying a classified ``work_ref``
  (built-in tools, GitHub/GitLab MCP servers, ``gh``/``glab`` through a native
  hook), for flow runs and for agentic sessions outside any flow.

Every read is bounded by the account, a closed ``[since, until]`` window and
a row limit. Naive timestamp columns store UTC.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.engine import Row, RowMapping
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.api_usage import (
    exclude_customer_overhead_condition,
    gateway_action_condition,
)
from preloop.models.crud.fleet_activity import _naive, _totals
from preloop.models.crud.fleet_map import WORK_REF_STATUSES
from preloop.models.crud.fleet_work import ASSIGNED_LINKS


def execution_links(
    db: Session,
    *,
    account_id: uuid.UUID,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Flow executions attributed to an issue or a pull request in the window.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        since: Inclusive window start (execution start time).
        until: Inclusive window end.
        limit: Maximum rows.

    Returns:
        Rows of ``execution_id``, ``pr_key``, ``issue_key``, ``issue_url``
        and ``issue_title``; the issue columns are NULL without a rollup.
    """
    x, r = models.IssueCostExecution, models.IssueCostRollup
    return list(
        db.execute(
            select(
                x.execution_id,
                x.pr_key,
                r.issue_key,
                r.issue_url,
                r.title.label("issue_title"),
            )
            .select_from(x)
            .outerjoin(r, (r.id == x.rollup_id) & (r.account_id == account_id))
            .where(
                x.account_id == account_id,
                x.link.in_(ASSIGNED_LINKS),
                or_(x.rollup_id.is_not(None), x.pr_key.is_not(None)),
                x.start_time >= since,
                x.start_time <= until,
            )
            .order_by(x.start_time.desc())
            .limit(limit)
        ).all()
    )


def work_ref_calls(
    db: Session,
    *,
    account_id: uuid.UUID,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[Row[Any]]:
    """Classified work tool calls in the window, newest first.

    Served by ``ix_runtime_session_activity_work_ref``.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        since: Inclusive window start.
        until: Inclusive window end.
        limit: Maximum rows.

    Returns:
        Rows of ``runtime_session_id``, ``flow_execution_id`` and
        ``work_ref`` (the stored dictionary).
    """
    a = models.RuntimeSessionActivity
    return list(
        db.execute(
            select(
                a.runtime_session_id,
                a.flow_execution_id,
                a.metadata_["work_ref"].label("work_ref"),
            )
            .where(
                a.account_id == account_id,
                a.activity_type == "tool_call",
                a.metadata_.has_key("work_ref"),
                a.status.in_(WORK_REF_STATUSES),
                a.timestamp >= _naive(since),
                a.timestamp <= _naive(until),
            )
            .order_by(a.timestamp.desc())
            .limit(limit)
        ).all()
    )


def contributor_usage(
    db: Session,
    *,
    account_id: uuid.UUID,
    execution_ids: Collection[uuid.UUID],
    session_ids: Collection[uuid.UUID],
    since: datetime,
    until: datetime,
    limit: int,
) -> list[RowMapping]:
    """Gateway spend of the given executions and sessions, one row per slice.

    A slice is one contributor (its flow execution, else its runtime session)
    by flow, model, user and runtime principal, so a caller can split the
    contributor's spend over its work items and still drill down.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        execution_ids: Flow executions whose rows count.
        session_ids: Runtime sessions whose rows outside a flow run count.
        since: Inclusive window start.
        until: Inclusive window end.
        limit: Maximum slices.

    Returns:
        Mappings with ``flow_execution_id``, ``runtime_session_id``,
        ``flow_id``, ``model``, ``user_id``, ``principal_type``,
        ``principal_id``, ``principal_name`` and the ``_totals`` columns
        (``request_count``, token sums, ``estimated_cost``, ``currency``,
        ``partial``).
    """
    if not execution_ids and not session_ids:
        return []
    usage, execution = models.ApiUsage, models.FlowExecution
    owner: list[Any] = []
    if execution_ids:
        owner.append(usage.flow_execution_id.in_(list(execution_ids)))
    if session_ids:
        owner.append(
            usage.flow_execution_id.is_(None)
            & usage.runtime_session_id.in_(list(session_ids))
        )
    flow_id = func.coalesce(usage.flow_id, execution.flow_id)
    model = func.coalesce(usage.model_alias, "unknown")
    keys = [
        usage.flow_execution_id,
        usage.runtime_session_id,
        flow_id,
        model,
        usage.user_id,
        usage.runtime_principal_type,
        usage.runtime_principal_id,
    ]
    statement = (
        select(
            usage.flow_execution_id.label("flow_execution_id"),
            usage.runtime_session_id.label("runtime_session_id"),
            flow_id.label("flow_id"),
            model.label("model"),
            usage.user_id.label("user_id"),
            usage.runtime_principal_type.label("principal_type"),
            usage.runtime_principal_id.label("principal_id"),
            func.max(usage.runtime_principal_name).label("principal_name"),
            func.sum(usage.total_tokens).label("total_tokens"),
            *_totals(usage),
        )
        .select_from(usage)
        .outerjoin(execution, execution.id == usage.flow_execution_id)
        .where(
            usage.account_id == account_id,
            gateway_action_condition(),
            exclude_customer_overhead_condition(),
            # Implied by the owner filter; stated so the planner can use
            # ix_api_usage_customer_session_keys for the account range.
            or_(
                usage.runtime_session_id.is_not(None),
                usage.flow_execution_id.is_not(None),
            ),
            or_(*owner),
            usage.timestamp >= _naive(since),
            usage.timestamp <= _naive(until),
        )
        .group_by(*keys)
        .limit(limit)
    )
    return list(db.execute(statement).mappings())


def pull_request_details(
    db: Session, *, account_id: uuid.UUID, pr_keys: Collection[str]
) -> list[Row[Any]]:
    """Title, state and attributed issue of known pull requests.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        pr_keys: Normalized pull request URLs.

    Returns:
        Rows of ``pr_key``, ``title``, ``state`` and ``issue_key`` (the
        attributed rollup's key, NULL when none).
    """
    if not pr_keys:
        return []
    p, r = models.IssueCostPullRequest, models.IssueCostRollup
    return list(
        db.execute(
            select(p.pr_key, p.title, p.state, r.issue_key)
            .select_from(p)
            .outerjoin(r, (r.id == p.rollup_id) & (r.account_id == account_id))
            .where(p.account_id == account_id, p.pr_key.in_(list(pr_keys)))
        ).all()
    )


def issue_titles(
    db: Session, *, account_id: uuid.UUID, issue_keys: Collection[str]
) -> list[Row[Any]]:
    """Title and URL of issues that have a cost rollup.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        issue_keys: Canonical issue keys (``owner/repo#N`` or ``PROJ-N``).

    Returns:
        Rows of ``issue_key``, ``title`` and ``issue_url``.
    """
    if not issue_keys:
        return []
    r = models.IssueCostRollup
    return list(
        db.execute(
            select(r.issue_key, r.title, r.issue_url).where(
                r.account_id == account_id, r.issue_key.in_(list(issue_keys))
            )
        ).all()
    )


def flow_names(
    db: Session, *, account_id: uuid.UUID, flow_ids: Collection[uuid.UUID]
) -> dict[str, str]:
    """Names of the account's flows by id.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        flow_ids: Flow ids.

    Returns:
        ``{flow id: name}``.
    """
    if not flow_ids:
        return {}
    f = models.Flow
    rows = db.execute(
        select(f.id, f.name).where(f.account_id == account_id, f.id.in_(list(flow_ids)))
    ).all()
    return {str(row.id): str(row.name) for row in rows}


def user_names(
    db: Session, *, account_id: uuid.UUID, user_ids: Collection[uuid.UUID]
) -> dict[str, str]:
    """Display names of the account's users by id.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        user_ids: User ids.

    Returns:
        ``{user id: full name, else username}``.
    """
    if not user_ids:
        return {}
    u = models.User
    rows = db.execute(
        select(u.id, u.full_name, u.username).where(
            u.account_id == account_id, u.id.in_(list(user_ids))
        )
    ).all()
    return {str(row.id): str(row.full_name or row.username) for row in rows}
