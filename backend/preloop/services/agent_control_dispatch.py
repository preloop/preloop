"""Persist-then-deliver dispatcher for Agent Control operator commands.

Operator prompts, flow executions on a persistent managed agent, and
in-session notices all need the same audited ``send_message`` path: persist
the envelope in ``agent_control_command`` before any WebSocket or NATS
attempt, then deliver at-least-once. This module is that shared core so the
endpoint does not own a second copy of the delivery contract.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Optional, Union
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.config import settings
from preloop.models.crud import agent_control_connection as control_connection
from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent_enrollment,
    crud_runtime_session,
)
from preloop.schemas.agent_control import AgentControlEnvelope

logger = logging.getLogger(__name__)

SUPPORTED_CONTROL_AGENT_KINDS = {
    "hermes",
    "openclaw",
    "claude_code",
    "opencode",
    "pi",
    "deepseek",
    "codex",
}
# Pi and DeepSeek accept text on an already-open session only. Shared so the
# operator endpoint and persistent executor refuse start_new_session together.
CONTROL_NEW_SESSION_UNSUPPORTED_KINDS = {"pi", "deepseek"}

_UNAVAILABLE_DETAIL = "Managed agent command channel is unavailable"


class AgentControlDispatchError(Exception):
    """A managed-agent command could not be persisted or delivered.

    Attributes:
        status_code: HTTP status the operator endpoint maps this to.
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of persisting and attempting delivery of one command."""

    command_id: str
    envelope: AgentControlEnvelope
    local_delivery: bool
    subject: Optional[str]
    command_status: str
    expires_at: datetime
    command_ttl_seconds: int


def agent_has_control_config(db: Session, *, account_id: str, agent: Any) -> bool:
    """Return True when the agent is an allow-listed kind with verified config.

    Args:
        db: Database session.
        account_id: Owning account id.
        agent: Managed-agent ORM row.

    Returns:
        True when the kind is supported and an enrollment proved the control
        channel is configured. False for other kinds, even if a runtime is
        connected.
    """
    agent_kind = str(agent.agent_kind or agent.session_source_type or "").lower()
    if agent_kind not in SUPPORTED_CONTROL_AGENT_KINDS:
        return False

    enrollments = (
        crud_managed_agent_enrollment.get_latest_for_agent_by_type(
            db,
            account_id=account_id,
            agent_id=str(agent.id),
            enrollment_type="cli_managed_config",
        )
        or crud_managed_agent_enrollment.get_latest_for_agent(
            db, account_id=account_id, agent_id=str(agent.id)
        ),
        crud_managed_agent_enrollment.get_latest_for_agent_by_type(
            db,
            account_id=account_id,
            agent_id=str(agent.id),
            enrollment_type="runtime_plugin_control",
        ),
    )
    for enrollment in enrollments:
        if enrollment is None:
            continue
        validation = (
            enrollment.validation_result
            if isinstance(enrollment.validation_result, dict)
            else {}
        )
        validation_control_ready = bool(
            validation.get("control_channel_configured")
            or (
                validation.get("control_plugin_verified")
                and validation.get("control_ws_url_ok")
                and validation.get("control_bearer_token_ok")
            )
        )
        if validation_control_ready:
            return True
    return False


def command_source(metadata: dict[str, Any]) -> Optional[str]:
    """Best-effort originating surface (console|mobile|watch|api) for audit."""
    for key in ("source", "surface", "via", "device"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:32]
    return "api"


def build_operator_envelope(
    agent: Any,
    *,
    name: str,
    payload: dict[str, Any],
) -> AgentControlEnvelope:
    """Build a typed command envelope for a live managed agent.

    Args:
        agent: Managed-agent ORM row. Must currently hold a runtime session.
        name: Command name (``send_message``, ``request_takeover``, ...).
        payload: Command payload the runtime plugin already accepts.

    Returns:
        Envelope ready to persist and deliver.

    Raises:
        AgentControlDispatchError: When the agent has no live runtime session.
    """
    if agent.runtime_session_id is None:
        raise AgentControlDispatchError(
            "Managed agent is not online",
            status_code=409,
        )
    return AgentControlEnvelope(
        type="command",
        name=name,
        message_id=str(uuid.uuid4()),
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=agent.runtime_session_id,
        session_source_type=agent.session_source_type,
        session_source_id=agent.session_source_id,
        timestamp=datetime.now(UTC),
        payload=payload,
    )


def create_command_history_session(
    db: Session,
    *,
    agent: Any,
    start_new_session: bool,
    target_session_id: Optional[Union[UUID, str]] = None,
) -> Any:
    """Return the runtime session that should record this command's history.

    A ``start_new_session`` command mints a tracking session so later interrupt
    and log reads have a stable id. Targeting an existing session or the
    agent's current binding reuses that row.

    Args:
        db: Database session.
        agent: Managed-agent ORM row.
        start_new_session: Whether the command asked for a new session.
        target_session_id: Existing session the operator named, if any.

    Returns:
        Runtime session row, or None when there is no session to record on.
    """
    if target_session_id is not None:
        return crud_runtime_session.get_account_session(
            db,
            account_id=str(agent.account_id),
            runtime_session_id=str(target_session_id),
        )
    if not start_new_session:
        if agent.runtime_session_id is None:
            return None
        return crud_runtime_session.get_account_session(
            db,
            account_id=str(agent.account_id),
            runtime_session_id=str(agent.runtime_session_id),
        )

    now = datetime.now(UTC)
    command_session_id = f"{agent.session_source_id}-{uuid.uuid4()}"
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=agent.account_id,
        session_source_type=agent.session_source_type,
        session_source_id=command_session_id,
        session_reference="Agent Control new session",
        runtime_principal_type=agent.session_source_type,
        runtime_principal_id=agent.session_source_id,
        runtime_principal_name=agent.display_name,
        started_at=now,
        last_activity_at=now,
    )


async def persist_and_deliver_command(
    db: Session,
    *,
    agent: Any,
    envelope: AgentControlEnvelope,
    source: Optional[str],
    created_by_user_id: Any = None,
    expires_at: Optional[datetime] = None,
    require_delivery: bool = True,
) -> DispatchResult:
    """Persist one command then deliver it locally or via NATS.

    Args:
        db: Database session.
        agent: Managed-agent ORM row the envelope addresses.
        envelope: Fully built command envelope.
        source: Audit source stored on the command row.
        created_by_user_id: Optional operator user id.
        expires_at: Optional expiry; defaults to the configured TTL.
        require_delivery: When True, raise if neither local WS nor NATS
            accepted the envelope. When False, leave the row pending.

    Returns:
        DispatchResult with command id, delivery flags, and expiry.

    Raises:
        AgentControlDispatchError: When ``require_delivery`` is True and no
            delivery channel was available.
    """
    from preloop.api.endpoints.agent_control import (
        _publish_command,
        agent_control_manager,
    )

    now = datetime.now(UTC)
    command_ttl_seconds = int(settings.agent_control_command_ttl_seconds)
    command_expires_at = expires_at or (now + timedelta(seconds=command_ttl_seconds))
    crud_agent_control_command.create_command(
        db,
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=agent.runtime_session_id,
        command_id=envelope.message_id,
        envelope=envelope.model_dump(mode="json"),
        source=source,
        created_by_user_id=created_by_user_id,
        expires_at=command_expires_at,
    )
    delivery_agent_id = str(agent.id)
    await run_db_off_loop(partial(control_connection.release_read_transaction, db))
    command_status = "pending"
    local_delivery = await agent_control_manager.send_to_agent(
        managed_agent_id=delivery_agent_id,
        envelope=envelope,
    )
    if local_delivery:
        command_status = "delivered"
    subject: Optional[str] = None
    if not local_delivery:
        subject = await _publish_command(envelope)
        if subject is None and require_delivery:
            try:
                with db.begin_nested():
                    crud_agent_control_command.mark_failed(
                        db,
                        account_id=str(agent.account_id),
                        managed_agent_id=str(agent.id),
                        command_id=envelope.message_id,
                        error=_UNAVAILABLE_DETAIL,
                        commit=False,
                    )
                db.commit()
            except SQLAlchemyError:
                logger.exception(
                    "Failed to mark Agent Control command %s failed",
                    envelope.message_id,
                )
            raise AgentControlDispatchError(
                _UNAVAILABLE_DETAIL,
                status_code=503,
            )
    return DispatchResult(
        command_id=envelope.message_id,
        envelope=envelope,
        local_delivery=local_delivery,
        subject=subject,
        command_status=command_status,
        expires_at=command_expires_at,
        command_ttl_seconds=command_ttl_seconds,
    )


async def dispatch_operator_message(
    db: Session,
    *,
    managed_agent: Any,
    text: str,
    metadata: Optional[dict[str, Any]] = None,
    start_new_session: bool = False,
    target_session_id: Optional[Union[UUID, str]] = None,
    source: Optional[str] = None,
    input_mode: str = "text",
    interrupt: bool = False,
    spawn_worktree: bool = False,
    voice: Optional[dict[str, Any]] = None,
    session_mode: Optional[str] = None,
    session_identity: Optional[dict[str, Any]] = None,
    created_by_user_id: Any = None,
    require_delivery: bool = True,
    expires_at: Optional[datetime] = None,
) -> DispatchResult:
    """Build, persist, and deliver one ``send_message`` command.

    Args:
        db: Database session.
        managed_agent: Target managed-agent ORM row.
        text: Operator or flow prompt text.
        metadata: Envelope metadata the runtime already accepts.
        start_new_session: Ask the runtime to open a new session.
        target_session_id: Existing session to address, if any.
        source: Audit source for the command row.
        input_mode: ``text`` or ``voice_transcript``.
        interrupt: When True, the runtime should interrupt the target session.
        spawn_worktree: Optional worktree flag forwarded in the payload.
        voice: Optional voice payload.
        session_mode: ``new``, ``existing``, or ``current``. Derived when omitted.
        session_identity: Extra payload keys for an existing session.
        created_by_user_id: Optional operator user id.
        require_delivery: See :func:`persist_and_deliver_command`.
        expires_at: Optional command expiry.

    Returns:
        DispatchResult for the persisted command.

    Raises:
        AgentControlDispatchError: When the agent is offline or delivery
            is required and no channel was available.
    """
    resolved_metadata = dict(metadata or {})
    if session_mode is None:
        if start_new_session:
            session_mode = "new"
        elif target_session_id is not None:
            session_mode = "existing"
        else:
            session_mode = "current"
    payload: dict[str, Any] = {
        "text": text,
        "metadata": resolved_metadata,
        "input_mode": input_mode,
        "session_mode": session_mode,
        "target_session_id": str(target_session_id) if target_session_id else None,
        "start_new_session": start_new_session,
        "voice": voice or {},
        "spawn_worktree": spawn_worktree,
        "interrupt": interrupt,
    }
    if session_identity:
        payload.update(session_identity)
    envelope = build_operator_envelope(
        managed_agent,
        name="send_message",
        payload=payload,
    )
    return await persist_and_deliver_command(
        db,
        agent=managed_agent,
        envelope=envelope,
        source=source or command_source(resolved_metadata),
        created_by_user_id=created_by_user_id,
        expires_at=expires_at,
        require_delivery=require_delivery,
    )
