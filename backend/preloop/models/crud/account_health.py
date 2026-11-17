"""Read-only aggregates for the operator account health report.

Every query is bounded by one account and a time window and returns
categories, counts, ids and timestamps only: never prompts, outputs, raw
error text or provider error bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution

#: Statuses of an execution that ended without succeeding.
FAILED_EXECUTION_STATUSES = ("FAILED", "TIMEOUT", "TIMED_OUT", "ABORTED")

#: Sample ids returned per signal.
SAMPLE_LIMIT = 20


def _account_executions(account_id: UUID, since: datetime, until: datetime) -> Any:
    """Window filter on executions of an account's flows.

    ``flow_id IN (account flows)`` plus ``created_at`` uses
    ``ix_flow_execution_flow_created``.
    """
    flow_ids = select(Flow.id).where(Flow.account_id == account_id)
    return and_(
        FlowExecution.flow_id.in_(flow_ids),
        FlowExecution.created_at >= since.replace(tzinfo=None),
        FlowExecution.created_at < until.replace(tzinfo=None),
    )


class CRUDAccountHealth:
    """Aggregates over executions and gateway usage for one account."""

    def failed_executions_by_category(
        self, db: Session, *, account_id: UUID, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """Failed executions per failure category, with the first of each.

        Returns:
            Entries with ``category``, ``count``, ``first_execution_id`` and
            ``first_at``, largest count first.
        """
        category = func.coalesce(FlowExecution.failure_category, "uncategorized")
        count = func.count().label("count")
        first_id = func.array_agg(
            aggregate_order_by(FlowExecution.id, FlowExecution.created_at)
        )
        rows = db.execute(
            select(
                category.label("category"),
                count,
                first_id[1].label("first_execution_id"),
                func.min(FlowExecution.created_at).label("first_at"),
            )
            .where(
                _account_executions(account_id, since, until),
                FlowExecution.status.in_(FAILED_EXECUTION_STATUSES),
            )
            .group_by(category)
            .order_by(count.desc())
        ).all()
        return [dict(row._mapping) for row in rows]

    def hollow_successes(
        self, db: Session, *, account_id: UUID, since: datetime, until: datetime
    ) -> dict[str, Any]:
        """Successful executions with no tool calls or no model requests.

        A model request is any gateway row linked to the execution, failed
        or not: a run whose every model call failed made model requests.
        ``tool_calls_count`` can undercount native shell activity on older
        runs, so a zero there is an observation, not proof of no work.

        Returns:
            ``zero_tool_calls`` and ``zero_model_requests``, each with a
            ``count`` and up to ``SAMPLE_LIMIT`` execution ids.
        """
        succeeded = and_(
            _account_executions(account_id, since, until),
            FlowExecution.status == "SUCCEEDED",
        )
        no_model = ~exists().where(ApiUsage.flow_execution_id == FlowExecution.id)
        no_tools = func.coalesce(FlowExecution.tool_calls_count, 0) == 0
        result: dict[str, Any] = {}
        for name, condition in (
            ("zero_tool_calls", no_tools),
            ("zero_model_requests", no_model),
        ):
            where = and_(succeeded, condition)
            total = db.scalar(select(func.count()).where(where)) or 0
            ids = db.scalars(
                select(FlowExecution.id)
                .where(where)
                .order_by(FlowExecution.created_at.desc())
                .limit(SAMPLE_LIMIT)
            ).all()
            result[name] = {"count": total, "sample_execution_ids": list(ids)}
        return result

    def gateway_upstream_errors(
        self, db: Session, *, account_id: UUID, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """Gateway requests that failed upstream, per provider and error class.

        Served by ``ix_api_usage_account_action_ts``.
        """
        count = func.count().label("count")
        rows = db.execute(
            select(
                func.coalesce(ApiUsage.provider_name, "unknown").label("provider"),
                ApiUsage.error_class,
                count,
                func.min(ApiUsage.timestamp).label("first_at"),
                func.max(ApiUsage.timestamp).label("last_at"),
            )
            .where(
                ApiUsage.account_id == account_id,
                ApiUsage.action_type == "model_gateway",
                ApiUsage.timestamp >= since.replace(tzinfo=None),
                ApiUsage.timestamp < until.replace(tzinfo=None),
                ApiUsage.error_class.is_not(None),
            )
            .group_by("provider", ApiUsage.error_class)
            .order_by(count.desc())
        ).all()
        return [dict(row._mapping) for row in rows]


crud_account_health = CRUDAccountHealth()
