"""Audited terminal transport selected only by the central Talk resolver."""

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.crud import herdr_command
from preloop.schemas.agent_control import AgentControlEnvelope
from preloop.services.herdr_inventory import _emit_changed

MAX_PROMPT_BYTES = 16 * 1024
REASONS = {
    "device_busy": "The device is busy. Try again after the current command finishes.",
    "command_conflict": "The command identity conflicts with a prior request. Send a new command.",
    "invalid_command": "The device rejected an invalid command envelope.",
    "command_expired": "The command expired before device submission.",
    "journal_unavailable": "The device could not safely record this command. Check its local storage.",
    "journal_full": "The device command journal is full. Check local storage before retrying.",
    "delivered": "Prompt submitted; replies remain in the governed session record.",
    "prompt_disabled": "Enable herdr prompting locally on this device before sending a prompt.",
    "agent_blocked": "The agent is waiting at a terminal dialog. Handle it locally.",
    "agent_prompt_stalled": "No activity was confirmed after submission. Check the pane before retrying.",
    "timeout": "Delivery is uncertain after the timeout. Check the session before retrying.",
    "delivery_unknown": "Delivery is uncertain. Check the session before retrying.",
    "pane_gone": "The pane disappeared. Check the session before sending another prompt.",
    "session_changed": "The pane identity changed. Select the current session before prompting.",
    "note_required": "The agent is working. Send a note through Talk instead.",
    "agent_not_ready": "The agent is not ready for interactive input. Check the pane locally.",
    "invalid_prompt": "The prompt is empty or exceeds the 16 KiB limit.",
    "invalid_target": "The terminal session identity is not valid.",
    "herdr_protocol_unsupported": "Update the adapter to support this herdr protocol.",
    "snapshot_unavailable": "The device could not verify the pane. Check herdr locally.",
    "invalid_response": "Herdr did not confirm submission. Check the session before retrying.",
    "expired": "The command expired. Check the session before sending another prompt.",
    "unavailable": "The selected device or session is no longer eligible for prompting.",
    "halted": "Agent commands are halted for this account.",
}


def dispatch_prompt(
    db: Session,
    *,
    agent: models.ManagedAgent,
    session: models.RuntimeSession,
    target: dict[str, str],
    text: str,
    user_id: Any,
    source: str | None = None,
) -> Any:
    """Persist the existing command/audit identity without touching plugin transport."""
    from preloop.services.agent_control_dispatch import (
        AgentControlDispatchError,
        DispatchResult,
    )

    if (
        herdr_command.eligible_target(
            db, account_id=agent.account_id, session=session, agent=agent
        )
        != target
    ):
        raise AgentControlDispatchError(REASONS["unavailable"])
    if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AgentControlDispatchError(REASONS["invalid_prompt"], status_code=422)
    text = "".join(char for char in text if char.isprintable())
    if not text.strip():
        raise AgentControlDispatchError(REASONS["invalid_prompt"], status_code=422)
    now = datetime.now(UTC)
    ttl = min(max(1, int(settings.agent_control_command_ttl_seconds)), 120)
    expires = now + timedelta(seconds=ttl)
    envelope = AgentControlEnvelope(
        type="command",
        name="send_message",
        message_id=str(uuid.uuid4()),
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        session_source_type=agent.session_source_type,
        session_source_id=agent.session_source_id,
        timestamp=now,
        expires_at=expires,
        payload={
            "text": text,
            "session_mode": "existing",
            "target_session_id": str(session.id),
            "delivery_route": herdr_command.ROUTE,
            "herdr_target": target,
        },
    )
    crud_agent_control_command.create_command(
        db,
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        command_id=envelope.message_id,
        envelope=envelope.model_dump(mode="json"),
        source=source,
        created_by_user_id=user_id,
        expires_at=expires,
    )
    return DispatchResult(
        command_id=envelope.message_id,
        envelope=envelope,
        local_delivery=False,
        subject=None,
        command_status="pending",
        expires_at=expires,
        command_ttl_seconds=ttl,
        history_session_id=session.id,
    )


def _finish(
    db: Session,
    command: models.AgentControlCommand,
    *,
    code: str,
    delivery: str,
    state: str = "",
) -> bool:
    """Store one bounded terminal outcome and its normal governed activity row."""
    command = herdr_command.lock_command(db, command)
    if (
        command.status in {"failed", "expired", "cancelled"}
        or crud_agent_control_command.command_result_payload(command) is not None
    ):
        return False
    if code in {"expired", "halted"}:
        delivery = "not_sent" if command.status == "pending" else "unknown"
    failed = delivery != "sent" or code == "agent_blocked"
    payload = {
        "status": "failed" if failed else "completed",
        "code": code,
        "delivery": delivery,
        "reason": REASONS[code],
        "state": state,
        "delivery_route": herdr_command.ROUTE,
    }
    if delivery == "sent":
        crud_agent_control_command.mark_acked(
            db,
            account_id=command.account_id,
            managed_agent_id=command.managed_agent_id,
            command_id=command.command_id,
            acked_at=datetime.now(UTC),
            commit=False,
        )
    crud_agent_control_command.mark_terminal_result(
        db,
        account_id=command.account_id,
        managed_agent_id=command.managed_agent_id,
        command_id=command.command_id,
        result_payload=payload,
        failed=failed,
        error=REASONS[code],
        commit=False,
    )
    crud_runtime_session_activity.log_agent_control_result(
        db,
        account_id=command.account_id,
        command_id=command.command_id,
        fallback_runtime_session_id=command.runtime_session_id,
        status=payload["status"],
        message=REASONS[code],
        metadata=payload,
        commit=False,
    )
    if code in {"expired", "command_expired"}:
        herdr_command.mark_expired(db, command)
    herdr_command.commit(db)
    # Metadata invalidation only; command result activities retain their normal timestamps.
    from types import SimpleNamespace

    _emit_changed(
        SimpleNamespace(account_id=command.account_id),
        {str(command.runtime_session_id)},
    )
    return True


def expire_commands(db: Session) -> int:
    """Finish abandoned pending/delivered commands even if the device stays offline."""
    total = 0
    for command in herdr_command.outstanding(db, expired=True):
        total += _finish(
            db,
            command,
            code="expired",
            delivery="not_sent" if command.status == "pending" else "unknown",
        )
    return total


def pending_messages(db: Session, runner: models.FlowRunner) -> list[dict[str, Any]]:
    """Replay the same bounded frame on reconnect; the device durably deduplicates."""
    from preloop.services.runner_sessions import flows_or_tools_halted

    messages = []
    if not (getattr(runner, "harness_inventory", None) or {}).get("herdr"):
        return messages
    halted = flows_or_tools_halted(db, runner.account_id)
    for command in herdr_command.outstanding(db, runner=runner):
        if command.expires_at <= datetime.now(UTC):
            _finish(
                db,
                command,
                code="expired",
                delivery="not_sent" if command.status == "pending" else "unknown",
            )
            continue
        if halted:
            _finish(
                db,
                command,
                code="halted",
                delivery="not_sent" if command.status == "pending" else "unknown",
            )
            continue
        target = herdr_command.route(command)
        # Delivered commands are replayed verbatim to recover the device's journaled
        # result, even if the pane changed after submission. They cannot run twice.
        if command.status == "pending":
            session = crud_runtime_session.get_account_session(
                db,
                account_id=str(runner.account_id),
                runtime_session_id=str(command.runtime_session_id),
            )
            agent = crud_managed_agent.get(db, id=command.managed_agent_id)
            if (
                session is None
                or agent is None
                or herdr_command.eligible_target(
                    db, account_id=runner.account_id, session=session, agent=agent
                )
                != target
            ):
                _finish(db, command, code="unavailable", delivery="not_sent")
                continue
        payload = command.envelope["payload"]
        messages.append(
            {
                "type": "herdr_prompt",
                "command_id": command.command_id,
                **{
                    key: target[key]
                    for key in (
                        "runtime_session_id",
                        "pane_id",
                        "harness_kind",
                        "harness_session_id",
                    )
                },
                "text": payload["text"],
                "expires_at": command.expires_at.isoformat(),
            }
        )
        crud_agent_control_command.mark_delivered(
            db,
            account_id=runner.account_id,
            managed_agent_id=command.managed_agent_id,
            command_id=command.command_id,
            delivered_at=datetime.now(UTC),
            commit=False,
        )
    if messages:
        herdr_command.commit(db)
    return messages


def apply_result(db: Session, runner: models.FlowRunner, raw: dict[str, Any]) -> bool:
    """Accept only bounded outcomes for this authenticated runner's own command."""
    command_id = raw.get("command_id")
    code, delivery = raw.get("code"), raw.get("delivery")
    if (
        not isinstance(command_id, str)
        or len(command_id) > 64
        or not isinstance(code, str)
        or not isinstance(delivery, str)
        or code not in REASONS
        or delivery not in {"not_sent", "sent", "unknown"}
    ):
        return False
    state = raw.get("state", "")
    if not isinstance(state, str) or state not in {
        "",
        "idle",
        "done",
        "blocked",
        "working",
        "unknown",
    }:
        return False
    if delivery == "sent" and code not in {"delivered", "agent_blocked"}:
        return False
    if code == "delivered" and delivery != "sent":
        return False
    command = herdr_command.bound_command(db, runner=runner, command_id=command_id)
    if command is None or command.status == "pending":
        return False
    return _finish(db, command, code=code, delivery=delivery, state=state)
