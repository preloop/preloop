"""Stable, tenant-visible source windows for the Fleet Map."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Row, and_, false, or_, select, tuple_
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.fleet_work import ASSIGNED_LINKS

Source = Literal["agents", "models", "runners", "users"]
Resource = models.ManagedAgent | models.AIModel | models.FlowRunner | models.User


def list_resources(
    db: Session,
    *,
    source: Source,
    account_id: str,
    limit: int,
    include_built_in: bool = False,
) -> list[Resource]:
    """Read one source window without relationships or shared-account expansion.

    Args:
        db: Request database session.
        source: Allowlisted resource kind.
        account_id: Selected membership's owning tenant.
        limit: Maximum rows including a truncation sentinel.
        include_built_in: Include the available hosted model when enabled.

    Returns:
        Tenant-visible rows in stable ID order, without loading relationships.
        Decommissioned agents are never returned.
    """
    model = {
        "agents": models.ManagedAgent,
        "models": models.AIModel,
        "runners": models.FlowRunner,
        "users": models.User,
    }[source]
    visible = model.account_id == account_id
    if source == "models" and include_built_in:
        built_in = (
            models.AIModel.account_id.is_(None)
            & (models.AIModel.meta_data["gateway"]["enabled"].astext == "true")
            & models.AIModel.meta_data["retired"].astext.is_distinct_from("true")
        )
        visible = or_(visible, built_in)
    if source == "agents":
        # Decommissioning is terminal; those rows are history, not fleet.
        visible = visible & (models.ManagedAgent.lifecycle_state != "decommissioned")
    return db.query(model).filter(visible).order_by(model.id).limit(limit).all()


def list_decommissioned_agents(
    db: Session, *, account_id: str, limit: int
) -> list[models.ManagedAgent]:
    """List the account's decommissioned agents that the map leaves out.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        limit: Maximum rows to read.

    Returns:
        Decommissioned managed agents in stable ID order, for counting after
        the caller's visibility filter.
    """
    return (
        db.query(models.ManagedAgent)
        .filter(
            models.ManagedAgent.account_id == account_id,
            models.ManagedAgent.lifecycle_state == "decommissioned",
        )
        .order_by(models.ManagedAgent.id)
        .limit(limit)
        .all()
    )


#: Tool names that open, review, comment on or merge a pull or merge request.
#: The partial index ``ix_runtime_session_activity_code_tools`` carries the
#: same list; keep the two in step so the planner can use it.
CODE_ACTIVITY_TOOLS: tuple[str, ...] = (
    "add_comment",
    "add_comment_to_pending_review",
    "approve_merge_request",
    "create_merge_request",
    "create_merge_request_note",
    "create_pull_request",
    "create_pull_request_review",
    "merge_merge_request",
    "merge_pull_request",
    "pull_request_review_write",
    "submit_pending_pull_request_review",
    "update_pull_request",
)

# Work history also includes issue edits and PR edits without a review.
WORK_ACTIVITY_TOOLS = (
    *CODE_ACTIVITY_TOOLS,
    "create_issue",
    "update_issue",
    "update_merge_request",
)


#: A classified call that was allowed to run. Native hook rows are recorded at
#: PreToolUse, with the decision's status; the partial index uses this list.
WORK_REF_STATUSES = ("succeeded", "allowed", "approved")


def list_code_tool_calls(
    db: Session,
    *,
    account_id: str,
    since: datetime,
    limit: int,
    until: datetime | None = None,
    include_work: bool = False,
    principals: list[tuple[str, str]] | None = None,
) -> list[Row[Any]]:
    """Read successful pull and merge request tool calls since ``since``.

    Bounded by the account, the window and ``limit``, newest first, and
    served by the partial index on code tool calls.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        since: Oldest call to read.
        limit: Maximum rows, including a truncation sentinel.
        until: Optional inclusive upper bound.
        include_work: Include issue tools for the work-item projection.
        principals: When set (a people scope), only calls outside flow runs by
            these ``(type, source id)`` principals or their per-conversation
            ``<id>:<run>`` principals, so the cap holds in-scope rows only.

    Returns:
        Rows of timestamp, tool name, arguments metadata, flow execution id and
        the session's runtime principal type and id.
    """
    a, s = models.RuntimeSessionActivity, models.RuntimeSession
    return list(
        db.execute(
            select(
                a.id,
                a.timestamp,
                a.tool_name,
                a.metadata_,
                a.flow_execution_id,
                s.runtime_principal_type,
                s.runtime_principal_id,
            )
            .join(s, s.id == a.runtime_session_id)
            .where(
                a.account_id == account_id,
                a.activity_type == "tool_call",
                or_(
                    and_(
                        a.tool_name.in_(
                            WORK_ACTIVITY_TOOLS if include_work else CODE_ACTIVITY_TOOLS
                        ),
                        a.status == "succeeded",
                    ),
                    # Classified calls (built-in, GitHub/GitLab MCP, gh/glab
                    # through a native hook), served by the work_ref index.
                    and_(
                        a.metadata_.has_key("work_ref"),
                        a.status.in_(WORK_REF_STATUSES),
                    ),
                ),
                a.timestamp >= since,
                *([a.timestamp <= until] if until is not None else []),
                s.account_id == account_id,
                *_principal_filter(s, a, principals),
            )
            .order_by(a.timestamp.desc(), a.id)
            .limit(limit)
        ).all()
    )


def _principal_filter(
    s: Any, a: Any, principals: list[tuple[str, str]] | None
) -> list[Any]:
    """Session principal conditions for a people scope (none: no filter)."""
    if principals is None:
        return []
    options = [
        and_(
            s.runtime_principal_type == kind,
            or_(
                s.runtime_principal_id == identity,
                s.runtime_principal_id.startswith(f"{identity}:", autoescape=True),
            ),
        )
        for kind, identity in principals
    ]
    if not options:
        return [false()]
    return [a.flow_execution_id.is_(None), or_(*options)]


def agents_for_principals(
    db: Session, *, account_id: str, principals: set[tuple[str, str]]
) -> list[models.ManagedAgent]:
    """Managed agents keyed by the runtime principals that made the calls.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        principals: ``(session_source_type, session_source_id)`` pairs.

    Returns:
        Non-decommissioned agents matching any pair.
    """
    if not principals:
        return []
    m = models.ManagedAgent
    # Per-conversation principals (``<base>:<run>``) belong to the base agent;
    # a ``managed_agent`` principal is the agent id itself.
    pairs: set[tuple[str, str]] = set()
    agent_ids: set[UUID] = set()
    for principal_type, principal_id in principals:
        if not principal_type or not principal_id:
            continue
        pairs.add((principal_type, principal_id))
        pairs.add((principal_type, principal_id.split(":", 1)[0]))
        if principal_type == "managed_agent":
            try:
                agent_ids.add(UUID(principal_id))
            except ValueError:
                pass
    matches = [tuple_(m.session_source_type, m.session_source_id).in_(sorted(pairs))]
    if agent_ids:
        matches.append(m.id.in_(sorted(agent_ids)))
    return (
        db.query(m)
        .filter(
            m.account_id == account_id,
            m.lifecycle_state != "decommissioned",
            or_(*matches),
        )
        .all()
    )


def flows_for_executions(
    db: Session, *, account_id: str, execution_ids: set[Any]
) -> list[Row[Any]]:
    """Map flow executions to their account's flows.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        execution_ids: Flow execution ids, read by primary key.

    Returns:
        Rows of execution id, flow id, flow name and the flow's model id.
    """
    if not execution_ids:
        return []
    e, f = models.FlowExecution, models.Flow
    return list(
        db.execute(
            select(e.id, f.id, f.name, f.ai_model_id)
            .join(f, f.id == e.flow_id)
            .where(e.id.in_(sorted(execution_ids, key=str)), f.account_id == account_id)
        ).all()
    )


def flows_by_id(db: Session, *, account_id: str, flow_ids: set[Any]) -> list[Row[Any]]:
    """Read flow names and model ids by primary key, within the account."""
    if not flow_ids:
        return []
    f = models.Flow
    return list(
        db.execute(
            select(f.id, f.name, f.ai_model_id).where(
                f.id.in_(sorted(flow_ids, key=str)), f.account_id == account_id
            )
        ).all()
    )


def list_merged_pull_requests(
    db: Session, *, account_id: str, since: datetime, limit: int
) -> list[Row[Any]]:
    """Pull requests merged since ``since`` and the flows that worked on them.

    One row per pull request and contributing flow (flow id NULL when no flow
    run is linked), newest merge first.

    Args:
        db: Request database session.
        account_id: Selected membership's owning tenant.
        since: Oldest merge to read.
        limit: Maximum pull requests read.

    Returns:
        Rows of pr key, merged at and flow id.
    """
    p, x = models.IssueCostPullRequest, models.IssueCostExecution
    merged = (
        select(p.pr_key, p.merged_at, p.ambiguous)
        .where(p.account_id == account_id, p.merged_at >= since)
        .order_by(p.merged_at.desc())
        .limit(limit)
        .subquery()
    )
    return list(
        db.execute(
            select(merged.c.pr_key, merged.c.merged_at, x.flow_id)
            .distinct()
            .outerjoin(
                x,
                (x.account_id == account_id)
                & (x.pr_key == merged.c.pr_key)
                & x.link.in_(ASSIGNED_LINKS)
                & merged.c.ambiguous.is_(False),
            )
            .order_by(merged.c.merged_at.desc())
        ).all()
    )


def models_by_id(
    db: Session, *, account_id: str, model_ids: set[Any]
) -> list[models.AIModel]:
    """Read AI model rows by id: the account's own and global built-ins."""
    if not model_ids:
        return []
    m = models.AIModel
    return (
        db.query(m)
        .filter(
            m.id.in_(sorted(model_ids, key=str)),
            or_(m.account_id == account_id, m.account_id.is_(None)),
        )
        .all()
    )
