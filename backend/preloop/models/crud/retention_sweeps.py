"""Account-scoped eligibility predicates for high-volume retention sweeps."""

from datetime import datetime
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.sql.elements import ColumnElement

from preloop.models import models
from preloop.models.crud.legal_hold import execution_in_account


def execution_log_filters(
    *, account_id: Any, cutoff: datetime, hold_filters: list[Any]
) -> tuple[ColumnElement[bool], ...]:
    """Limit execution age and hold checks to this account's flows.

    Both the execution and the line must be old enough. The flow/date index
    bounds the execution lookup before the log's execution-id index is used.
    """
    return (
        models.FlowExecutionLog.timestamp < cutoff,
        models.FlowExecutionLog.execution_id.in_(
            select(models.FlowExecution.id).where(
                execution_in_account(account_id),
                models.FlowExecution.created_at < cutoff,
                *hold_filters,
            )
        ),
    )


def event_filters(
    *, account_id: Any, cutoff: datetime
) -> tuple[ColumnElement[bool], ...]:
    """Keep the event timeline of held executions in the same account.

    Compare JSON ids as text: missing, null or malformed ids must not abort a
    batch or prevent unrelated telemetry from expiring. Only the account's
    held executions enter the exclusion set.
    """
    execution_id = models.Event.event_data["execution_id"].astext
    held_ids = select(cast(models.FlowExecution.id, String)).where(
        execution_in_account(account_id), models.FlowExecution.legal_hold.is_(True)
    )
    return (
        models.Event.account_id == account_id,
        models.Event.timestamp < cutoff,
        or_(execution_id.is_(None), execution_id.not_in(held_ids)),
    )
