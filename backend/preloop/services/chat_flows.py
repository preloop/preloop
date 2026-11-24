"""Explicit chat runs and durable, permission-checked completion replies."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from preloop.api.auth.permissions import has_permission
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_chat, crud_approval_request, crud_flow_execution
from preloop.services.chat_assistant import ChatBroker

TERMINAL = crud_flow_execution.TERMINAL_EXECUTION_STATUSES


def execution_url(execution_id: str) -> str:
    """Link to the complete result in the console."""
    return f"{settings.preloop_url.rstrip('/')}/console/flows/executions/{execution_id}"


def bounded_reply(text: str, url: str, provider: str) -> str:
    """Truncate once, preserving a result link within the provider limit."""
    limit = 2000 if provider == "discord" else 8000
    suffix = f"\n\n{url}"
    available = limit - len(suffix)
    if len(text) > available:
        text = text[: max(0, available - 1)] + "…"
    return text + suffix


def final_assistant_text(logs: list[dict[str, Any]]) -> str | None:
    """Read explicit completed assistant events, never arbitrary log tails."""
    candidate = None
    answer = None
    for row in logs:
        if row.get("type") != "agent_log_line":
            continue
        payload = row.get("payload") or {}
        raw = payload.get("line", payload.get("message"))
        if not isinstance(raw, str):
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in {"turn.started", "turn.failed"}:
            candidate = None
            answer = None
        elif kind == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                candidate = item.get("text")
        elif kind == "turn.completed":
            answer = candidate
            candidate = None
        elif (
            kind == "result"
            and event.get("subtype") == "success"
            and not event.get("is_error")
        ):
            answer = event.get("result")
    return answer if isinstance(answer, str) and answer.strip() else None


def available_flows(broker: ChatBroker, user: models.User) -> list[models.Flow]:
    """Filter the catalog using one resolved principal and loaded resources."""
    from preloop.plugins.account_hooks import (
        AuthorizationContext,
        ACTION_FLOW_RUN,
        VISIBLE_FLOW,
        authorize,
        filter_viewable,
    )
    from preloop.services.read_only_review import read_only_review

    flows = crud_chat.chat_flows(broker.db, broker.connection.account_id)
    ctx = AuthorizationContext(
        account_id=user.account_id,
        db=broker.db,
        user=user,
        attributes={"resource_type": VISIBLE_FLOW},
    )
    return [
        flow
        for flow in filter_viewable(broker.db, user, VISIBLE_FLOW, flows)
        if not read_only_review(flow.git_clone_config)
        and all(
            authorize(ctx, action, flow).allowed
            for action in ("execute_flows", "view_flows", ACTION_FLOW_RUN)
        )
    ]


async def start_or_help(broker: ChatBroker, row: models.ChatWork) -> str:
    """Resolve an explicit command and trigger through the linked user's API."""
    text = str(row.payload.get("text", ""))
    parts = text.split(maxsplit=1)
    verb = parts[0]
    argument = parts[1] if len(parts) > 1 else ""
    user = crud_chat.principal(broker.db, broker.connection, row.external_user_id)
    if user is None:
        raise HTTPException(403, "Identity revoked")
    if not has_permission(user, "execute_flows", broker.db) or not has_permission(
        user, "view_flows", broker.db
    ):
        return "You do not have permission to run and read chat flows."
    if verb == "/help":
        flows = available_flows(broker, user)
        crud_chat.transition(
            broker.db,
            row,
            "processing",
            payload={
                **row.payload,
                "resource_proofs": [
                    {"kind": "flow", "id": str(flow.id), "permission": permission}
                    for flow in flows
                    for permission in ("view_flows", "execute_flows")
                ],
            },
        )
        aliases = ", ".join(flow.chat_alias for flow in flows)
        return "Chat flows: " + (aliases or "none available") + ". Use ask ALIAS TEXT."
    if not argument.strip():
        return "Use ask ALIAS TEXT, or ask TEXT when a default flow is configured."
    parts = argument.split(maxsplit=1)
    alias = parts[0]
    question = parts[1] if len(parts) > 1 else ""
    flow = crud_chat.resolve_flow(broker.db, broker.connection.account_id, alias=alias)
    if flow is None and broker.connection.default_flow_id:
        flow = crud_chat.resolve_flow(
            broker.db,
            broker.connection.account_id,
            flow_id=broker.connection.default_flow_id,
        )
        question = argument.strip()
    from preloop.services.read_only_review import read_only_review

    if flow is None or read_only_review(flow.git_clone_config):
        return "Unknown or unavailable chat flow. Use help to list flows you may run."
    try:
        broker.authorize_resource("flow", str(flow.id), "execute_flows")
        broker.authorize_resource("flow", str(flow.id), "view_flows")
    except HTTPException:
        return "Unknown or unavailable chat flow. Use help to list flows you may run."
    if not question.strip():
        return "Use ask ALIAS TEXT, or ask TEXT when a default flow is configured."
    if ((flow.agent_config or {}).get("interactive") or {}).get("enabled"):
        return await broker.start_conversation(row, flow, question.strip())
    user = crud_chat.principal(broker.db, broker.connection, row.external_user_id)
    if user is None:
        raise HTTPException(403, "Identity revoked")
    revision = flow.config_revision
    denied = crud_chat.reserve_run(broker.db, row, flow.id)
    if denied:
        return denied
    try:
        result = await broker.request(
            "POST",
            f"/api/v1/flows/{flow.id}/trigger",
            "execute_flows",
            {
                "source": "chat",
                "type": "chat_question",
                "payload": {
                    "chat": {
                        "text": question.strip(),
                        "sender_display_name": user.full_name or user.username,
                        "connection_name": broker.connection.name,
                    }
                },
                "_chat_origin": {
                    "connection_id": str(broker.connection.id),
                    "request_id": str(row.id),
                },
            },
            revision=revision,
        )
    except HTTPException:
        # A definitive refusal is safe to report. Ambiguous POST failures escape
        # to the worker's uncertain-send policy and are never retriggered.
        crud_chat.transition(broker.db, row, "processing", run_flow_id=None)
        return "You cannot start this chat flow right now. Check your permissions, flow settings, and account limits."
    execution_id = str(UUID(result["id"]))
    crud_chat.transition(
        broker.db, row, "processing", run_execution_id=UUID(execution_id)
    )
    return f"Flow started. {execution_url(execution_id)}"


async def watch_run(broker: ChatBroker, row: models.ChatWork) -> bool:
    """Poll without a long-lived lease; return true when a reply is ready."""
    db = broker.db
    parent = crud_chat.work.get(
        db, UUID(row.payload["flow_watch"]), account_id=str(row.account_id)
    )
    if parent is None:
        crud_chat.transition(db, row, "cancelled", last_error="Run receipt unavailable")
        return False
    execution = crud_chat.find_run(db, parent)
    if execution is None:
        if parent.status in {"failed", "cancelled", "delivered"} or (
            parent.run_reserved_at
            and parent.run_reserved_at < datetime.utcnow() - timedelta(minutes=10)
        ):
            crud_chat.transition(
                db, row, "cancelled", last_error="No execution created"
            )
            return False
        defer_watch(broker, row)
        return False
    execution_id = str(execution.id)
    data = await broker.request(
        "GET", f"/api/v1/flows/executions/{execution_id}", "view_flows"
    )
    status = data["status"]
    url = execution_url(execution_id)
    waiting = status == "WAITING_FOR_HUMAN"
    if status not in TERMINAL and not waiting:
        waiting = bool(
            crud_approval_request.get_multi_by_execution(
                db,
                execution_id=execution_id,
                account_id=str(row.account_id),
                status="pending",
                limit=1,
            )
        )
    if waiting and not row.payload.get("approval_notified"):
        # The unique event key prevents duplicate notices after a crash. Sending
        # this separate outbox item does not interrupt completion observation.
        crud_chat.receive(
            db,
            connection=broker.connection,
            event_id=f"flow-approval:{parent.id}",
            external_user_id=row.external_user_id,
            payload={
                "actor_id": str(row.user_id),
                "flow_notice": execution_id,
                "text": f"Flow is waiting for approval. {url}",
                "channel_id": row.payload.get("channel_id"),
                "thread_id": row.payload.get("thread_id"),
                "private": row.payload.get("private"),
                "transport": row.payload.get("transport"),
            },
        )
        crud_chat.transition(
            db, row, "processing", payload={**row.payload, "approval_notified": True}
        )
    if status not in TERMINAL:
        defer_watch(broker, row)
        return False
    from preloop.services.review_completion_evidence import result_availability

    if result_availability(execution) == "pending":
        defer_watch(broker, row)
        return False
    if status != "SUCCEEDED":
        text = f"Flow {status.lower()}."
    elif data.get("result_schema_validation") is not None:
        validation = data["result_schema_validation"]
        result = data.get("result") or {}
        text = result.get("text") if validation.get("valid") is True else None
        if not isinstance(text, str) or not text.strip():
            text = "Flow completed without a validated text answer. Open the result for details."
    else:
        logs = await broker.request(
            "GET",
            f"/api/v1/flows/executions/{execution_id}/logs?tail=1000&exclude_model_calls=true",
            "view_flows",
        )
        text = (
            final_assistant_text(logs.get("logs", []))
            or "Flow completed without a final text answer. Open the result for details."
        )
    crud_chat.transition(
        db,
        row,
        "reply_ready",
        reply=bounded_reply(text, url, broker.connection.provider),
        payload={**row.payload, "flow_notice": execution_id},
    )
    return True


def defer_watch(broker: ChatBroker, row: models.ChatWork) -> None:
    """Release the lease until the next bounded status read."""
    crud_chat.transition(
        broker.db,
        row,
        "pending",
        attempts=0,
        lease_until=None,
        next_attempt_at=datetime.utcnow() + timedelta(seconds=10),
    )
