"""Authenticated, bounded event intake into the existing durable Flow runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow, crud_flow_execution, crud_managed_agent
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.model_routing import prepare_execution_routing
from preloop.services.kill_switch import FlowHaltActiveError, flows_halted
from preloop.services.flow_execution_limits import parse_execution_limits

MAX_EVENT_BYTES = 65536
EVENT_KINDS = {
    "github": {"issue_created", "pull_request_merged"},
    "gitlab": {"issue_created", "merge_request_merged"},
    "jira": {"issue_created"},
    "glitchtip": {"error"},
    "discord": {"member_joined", "channel_message"},
    "slack": {"channel_message"},
    "mattermost": {"channel_message"},
}


class EmployeeEventDenied(ValueError):  # noqa: N818
    """An authenticated event does not match the configured employee scope."""


@dataclass(frozen=True)
class EmployeeEventReceipt:
    """Durable execution receipt; acceptance is not successful completion."""

    execution_id: str
    status: str
    duplicate: bool


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise EmployeeEventDenied(f"Invalid {field}")
    return value


def employee_task_key(
    account_id: str, agent_id: str, source: str, connection_id: str, subject: str
) -> str:
    """Stable tenant/employee/subject key without placing private IDs in logs."""
    return hashlib.sha256(
        json.dumps(
            [account_id, agent_id, source, connection_id, subject],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def ingest_employee_event(
    db: Session,
    *,
    account_id: UUID | str,
    flow_id: UUID | str,
    source: str,
    connection_id: str,
    event_id: str,
    kind: str,
    subject: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> EmployeeEventReceipt:
    """Persist an authenticated event before handing its owned Flow to workers.

    Provider adapters authenticate the connection before calling this function.
    Source/connection/subject bindings are rechecked here on every delivery,
    including replays. Payload fields never supply tenancy or runtime identity.
    The existing execution unique index arbitrates concurrent deliveries; pending
    rows survive a failed publish and are picked up by existing recovery.
    """
    for value, name in (
        (source, "source"),
        (connection_id, "connection_id"),
        (event_id, "event_id"),
        (subject, "subject"),
    ):
        _identifier(value, name)
    if kind not in EVENT_KINDS.get(source, set()):
        raise EmployeeEventDenied("Unsupported source event kind")
    if not isinstance(payload, dict):
        raise EmployeeEventDenied("Payload must be an object")
    try:
        encoded = json.dumps(payload, allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise EmployeeEventDenied("Payload must contain finite JSON values") from exc
    if len(encoded) > MAX_EVENT_BYTES:
        raise EmployeeEventDenied("Event payload is too large")
    account = str(UUID(str(account_id)))
    flow = crud_flow.get(db, id=str(flow_id), account_id=account, refresh=True)
    if flow is None or not flow.is_enabled:
        raise EmployeeEventDenied("Employee flow is unavailable")
    config = flow.trigger_config or {}
    binding = config.get("employee_events", {})
    if not isinstance(binding, dict) or (
        binding.get("source") != source
        or binding.get("connection_id") != connection_id
        or kind not in binding.get("kinds", [])
        or not any(
            subject == scope
            or (
                isinstance(scope, str)
                and len(scope) > 8
                and scope.endswith(":*")
                and subject.startswith(scope[:-1])
            )
            for scope in binding.get("subjects", [])
        )
    ):
        raise EmployeeEventDenied("Event does not match employee source and scope")
    agent_config = flow.agent_config or {}
    agent_id = agent_config.get("target_agent_id")
    agent = (
        crud_managed_agent.get_for_account(
            db,
            account_id=account,
            agent_id=str(agent_id or ""),
        )
        if agent_id
        else None
    )
    if agent_config.get("execution_path") != "persistent":
        raise EmployeeEventDenied(
            "Employee flow must target a persistent managed agent"
        )
    if agent is None or agent.lifecycle_state != "active":
        raise EmployeeEventDenied("Employee identity is unavailable")
    limits = parse_execution_limits(agent_config).as_dict()
    if (
        not flow.timeout_seconds
        or not limits.get("max_turns")
        or not limits.get("max_total_tokens")
        or not limits.get("max_usd")
    ):
        raise EmployeeEventDenied(
            "Employee requires duration, turn, token and cost limits"
        )
    if flows_halted(db, flow.account_id):
        raise FlowHaltActiveError("Employee executions are halted")
    # Adapters suppress their own/bot messages before intake; retain a second
    # guard so a misconfigured bridge cannot produce an event/reply loop.
    if payload.get("bot") is True or payload.get("preloop_origin") is True:
        raise EmployeeEventDenied("Bot-generated employee events are ignored")
    instant = occurred_at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise EmployeeEventDenied("Event timestamp must include a timezone")
    delivery = hashlib.sha256(
        json.dumps(
            [account, source, connection_id, event_id],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    event = {
        "source": source,
        "type": kind,
        "account_id": account,
        "delivery_id": f"employee:{delivery}",
        "payload": payload,
        "occurred_at": instant.isoformat(),
        "employee": {
            "managed_agent_id": str(agent.id),
            "connection_id": connection_id,
            "subject": subject,
            "task_key": employee_task_key(
                account, str(agent.id), source, connection_id, subject
            ),
        },
    }
    event = prepare_execution_routing(db, flow, event)
    execution, duplicate = crud_flow_execution.reserve_employee_event(
        db,
        flow_id=flow.id,
        account_id=account,
        event=event,
        delivery_key=f"delivery:employee:{delivery}",
    )
    if not duplicate:
        # Precreated rows are durable before dispatch. Publishing failure leaves
        # PENDING for existing recovery, never loses the authenticated event.
        await FlowTriggerService(db)._start_flow_execution(
            flow,
            event,
            None,
            precreated_execution=execution,
        )
    return EmployeeEventReceipt(str(execution.id), execution.status, duplicate)
