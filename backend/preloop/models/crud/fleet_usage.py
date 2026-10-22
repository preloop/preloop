"""Account-scoped, SQL-grouped usage over a bounded visible model mapping."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import String, Uuid, and_, case, column, func, or_, select, values
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.api_usage import (
    exclude_replay_usage_condition,
    gateway_action_condition,
)


def usage_by_node(
    db: Session,
    *,
    account_id: UUID,
    model_nodes: list[tuple[UUID, str, str]],
    grouping: Literal["model", "provider"],
    since: datetime,
    until: datetime,
    principals: list[tuple[str, str]] | None = None,
    user_ids: list[str] | None = None,
) -> list[RowMapping]:
    """Aggregate gateway rows once per grouping, never loading ledger rows.

    Args:
        db: Request database session.
        account_id: Tenant whose gateway usage is being read.
        model_nodes: Bounded, authorized model id to model/provider node mapping.
        grouping: Which graph node identifier to group by.
        since: Inclusive window start in UTC.
        until: Inclusive window end in UTC.
        principals: When set (a people scope), count only rows outside flow
            runs made by these ``(type, source id)`` principals, including
            their per-conversation ``<id>:<run>`` principals.
        user_ids: With ``principals``, also count these users' direct rows
            (no principal, no flow).

    Returns:
        SQL aggregates keyed by graph node id, with missing-data indicators.

    Notes:
        Currency and explicit pricing markers are not in the covering gateway
        indexes. This bounded window read uses indexed heap access, rather than
        claiming an index-only scan or adding wide pricing metadata to indexes.
    """
    if not model_nodes:
        return []
    scoped: list[Any] = []
    if principals is not None or user_ids is not None:
        condition = _scope_condition(principals or [], user_ids or [])
        if condition is None:
            return []
        scoped.append(condition)
    mapping = values(
        column("model_id", Uuid),
        column("model_node", String),
        column("provider_node", String),
        name="visible_models",
    ).data(model_nodes)
    node = mapping.c.model_node if grouping == "model" else mapping.c.provider_node
    usage = models.ApiUsage
    currency = func.coalesce(usage.currency, "USD")
    one_currency = func.count(func.distinct(currency)) == 1
    missing_cost = or_(
        usage.estimated_cost.is_(None),
        usage.cost_source.is_not_distinct_from("unpriced"),
        # Budget metadata is a preflight estimate. A subsequently resolved
        # cost source (including a legitimate zero) or positive legacy cost
        # takes precedence over that earlier inability to price the request.
        and_(
            usage.cost_source.is_(None),
            usage.estimated_cost == 0,
            usage.meta_data["budget"]["pricing_available"].astext.is_not_distinct_from(
                "false"
            ),
        ),
    )
    known_cost = case((missing_cost, None), else_=usage.estimated_cost)
    statement = (
        select(
            node.label("node_id"),
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
        )
        .select_from(usage)
        .join(mapping, usage.ai_model_id == mapping.c.model_id)
        .where(
            usage.account_id == account_id,
            gateway_action_condition(),
            exclude_replay_usage_condition(),
            usage.timestamp >= since.replace(tzinfo=None),
            usage.timestamp <= until.replace(tzinfo=None),
            *scoped,
        )
        .group_by(node)
        .order_by(node)
    )
    return list(db.execute(statement).mappings())


def _scope_condition(
    principals: list[tuple[str, str]], user_ids: list[str]
) -> Any | None:
    """Ledger rows made by the scoped agents or directly by the scoped users."""
    usage = models.ApiUsage
    outside_flows = and_(usage.flow_execution_id.is_(None), usage.flow_id.is_(None))
    options: list[Any] = []
    for principal_type, principal_id in principals:
        options.append(
            and_(
                usage.runtime_principal_type == principal_type,
                or_(
                    usage.runtime_principal_id == principal_id,
                    usage.runtime_principal_id.startswith(
                        f"{principal_id}:", autoescape=True
                    ),
                ),
            )
        )
    users: list[UUID] = []
    for value in user_ids:
        try:
            users.append(UUID(str(value)))
        except ValueError:
            continue
    if users:
        options.append(
            and_(usage.runtime_principal_id.is_(None), usage.user_id.in_(users))
        )
    if not options:
        return None
    return and_(outside_flows, or_(*options))
