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
    "nanobot",
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
    history_session_id: Optional[Union[UUID, str]] = None


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
    consuming_account_id: Optional[Union[UUID, str]] = None,
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
    account_id = consuming_account_id or agent.account_id
    shared = str(account_id) != str(agent.account_id)
    if shared and not start_new_session and target_session_id is None:
        raise AgentControlDispatchError(
            "Shared agents require a new or consumer-owned session"
        )
    if target_session_id is not None:
        return crud_runtime_session.get_account_session(
            db,
            account_id=str(account_id),
            runtime_session_id=str(target_session_id),
        )
    if not start_new_session:
        if agent.runtime_session_id is None:
            return None
        return crud_runtime_session.get_account_session(
            db,
            account_id=str(account_id),
            runtime_session_id=str(agent.runtime_session_id),
        )

    now = datetime.now(UTC)
    command_session_id = f"{agent.session_source_id}-{uuid.uuid4()}"
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
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
    consuming_account_id: Optional[Union[UUID, str]] = None,
    history_session_id: Optional[Union[UUID, str]] = None,
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
        runtime_session_id=history_session_id or agent.runtime_session_id,
        consuming_account_id=consuming_account_id,
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
        from preloop.utils.control_credentials import protect_control_credentials

        protected = AgentControlEnvelope.model_validate(
            protect_control_credentials(envelope.model_dump(mode="json"))
        )
        subject = await _publish_command(protected)
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
        history_session_id=history_session_id,
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
    consuming_account_id: Optional[Union[UUID, str]] = None,
    history_session_id: Optional[Union[UUID, str]] = None,
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
    if consuming_account_id is not None and str(consuming_account_id) != str(
        managed_agent.account_id
    ):
        from preloop.models.crud.resource_share import crud_resource_share

        visible = crud_resource_share.visible_resource(
            db,
            account_id=consuming_account_id,
            resource_type="managed_agent",
            resource_id=managed_agent.id,
        )
        if visible is None:
            raise AgentControlDispatchError("Managed agent not found", status_code=404)
        if not start_new_session and target_session_id is None:
            raise AgentControlDispatchError(
                "Shared agents require a consumer-owned session"
            )
        if history_session_id is None:
            history = create_command_history_session(
                db,
                agent=managed_agent,
                start_new_session=start_new_session,
                target_session_id=target_session_id,
                consuming_account_id=consuming_account_id,
            )
            if history is None:
                raise AgentControlDispatchError(
                    "Consumer runtime session not found", status_code=404
                )
            history_session_id = history.id
    resolved_metadata = dict(metadata or {})
    if history_session_id is not None:
        resolved_metadata["runtime_session_id"] = str(history_session_id)
        if consuming_account_id is not None and str(consuming_account_id) != str(
            managed_agent.account_id
        ):
            from preloop.models.crud import crud_api_key
            from preloop.models.crud.resource_share import crud_resource_share

            gateway = dict(resolved_metadata.get("gateway") or {})
            token = gateway.get("api_key")
            if not token:
                if created_by_user_id is None:
                    raise AgentControlDispatchError(
                        "Shared targets require a consumer runtime credential"
                    )
                _, token = crud_api_key.create_runtime_key(
                    db,
                    name="Shared agent session",
                    account_id=consuming_account_id,
                    user_id=created_by_user_id,
                    scopes=["mcp:read", "mcp:write"],
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                    commit=False,
                )
            crud_resource_share.bind_runtime_key(
                db,
                account_id=consuming_account_id,
                agent=managed_agent,
                token=token,
                runtime_session_id=history_session_id,
            )
            gateway.update(
                api_key=token,
                api_url=settings.preloop_url,
                base_url=f"{settings.preloop_url.rstrip('/')}/api/v1/gateway",
            )
            resolved_metadata["gateway"] = gateway
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
        consuming_account_id=consuming_account_id,
        history_session_id=history_session_id,
    )


# --- terminal attach (#1150) -------------------------------------------------

#: Delivery states a client shows for one operator command, in order. The
#: command row stores pending|delivered|acked|failed|expired|cancelled; a
#: successful result leaves the row ``acked`` and stores the result in the
#: envelope, so "finished" is derived from that.
COMMAND_DELIVERY_STATES = (
    "queued",
    "delivered",
    "started",
    "finished",
    "failed",
    "expired",
    "cancelled",
)
TERMINAL_COMMAND_DELIVERY_STATES = frozenset(
    {"finished", "failed", "expired", "cancelled"}
)


def command_delivery_state(record: Any) -> tuple[str, Optional[str]]:
    """Map a stored command row to the state an operator sees.

    Args:
        record: ``AgentControlCommand`` row.

    Returns:
        ``(delivery_state, result_status)``. ``result_status`` is the
        runtime's own status for a finished command (``completed`` when it
        did not say), else None.
    """
    from preloop.models.crud.agent_control_command import (
        COMMAND_RESULT_ENVELOPE_KEY,
    )

    envelope = record.envelope if isinstance(record.envelope, dict) else {}
    result = envelope.get(COMMAND_RESULT_ENVELOPE_KEY)
    result_status: Optional[str] = None
    if isinstance(result, dict):
        result_status = str(result.get("status") or "completed")
    status = str(record.status or "pending")
    if status == "failed":
        return "failed", result_status or "failed"
    if status in {"expired", "cancelled"}:
        return status, result_status
    if result_status is not None:
        if result_status in {"failed", "error"}:
            return "failed", result_status
        return "finished", result_status
    if status == "acked":
        return "started", None
    if status == "delivered":
        return "delivered", None
    return "queued", None


@dataclass(frozen=True)
class SessionControlMode:
    """Whether a typed line on an attached session can start a new turn.

    ``mode`` is ``command`` when the session belongs to an active managed
    agent with a verified Agent Control plugin and a live control
    connection, and ``note`` otherwise. ``reason_code`` and ``reason`` say
    why a session is in note mode, in words an operator can act on.
    """

    mode: str
    reason_code: Optional[str]
    reason: Optional[str]
    agent: Any = None


def session_managed_agent(db: Session, *, account_id: str, session: Any) -> Any:
    """Return the managed agent that owns ``session``, or None.

    Uses the same two identities the command endpoint accepts for
    ``target_session_id``: the session's own source, then its runtime
    principal. Anything this returns therefore passes that check.
    """
    from preloop.models.crud import crud_managed_agent

    pairs = (
        (session.session_source_type, session.session_source_id),
        (
            getattr(session, "runtime_principal_type", None),
            getattr(session, "runtime_principal_id", None),
        ),
    )
    for source_type, source_id in pairs:
        if not source_type or not source_id:
            continue
        agent = crud_managed_agent.get_by_source(
            db,
            account_id=account_id,
            session_source_type=str(source_type),
            session_source_id=str(source_id),
        )
        if agent is not None:
            return agent
    return None


def resolve_session_control_mode(
    db: Session,
    *,
    account_id: str,
    session: Any,
    now: Optional[datetime] = None,
) -> SessionControlMode:
    """Decide command or note mode for one attached session.

    Args:
        db: Database session.
        account_id: Caller's account.
        session: ``RuntimeSession`` row already scoped to ``account_id``.
        now: Override for tests.

    Returns:
        The mode, the reason for note mode, and the managed agent if any.
    """
    from preloop.services.agent_control_presence import (
        AGENT_CONTROL_PRESENCE_TTL,
        control_heartbeat_is_fresh,
    )

    if session.ended_at is not None:
        return SessionControlMode(
            "note", "session_ended", "the session has ended; nothing can be sent"
        )
    agent = session_managed_agent(db, account_id=account_id, session=session)
    if agent is None:
        return SessionControlMode(
            "note",
            "not_managed",
            "this session is governed through hooks or the gateway, not run by "
            "an Agent Control agent; a line is a note read at its next tool or "
            "model call",
        )
    kind = str(agent.agent_kind or agent.session_source_type or "").lower()
    name = agent.display_name or kind or "the agent"
    if kind not in SUPPORTED_CONTROL_AGENT_KINDS:
        return SessionControlMode(
            "note",
            "unsupported_kind",
            f"{name} ({kind}) does not take commands through Agent Control; "
            "a line is a note read at its next tool or model call",
            agent,
        )
    if agent.lifecycle_state != "active":
        return SessionControlMode(
            "note",
            "agent_inactive",
            f"{name} is {agent.lifecycle_state}, so it cannot take commands",
            agent,
        )
    if not agent_has_control_config(db, account_id=account_id, agent=agent):
        return SessionControlMode(
            "note",
            "no_control_plugin",
            f"{name} is governed through hooks or the gateway and has no "
            "verified Agent Control plugin; a line is a note read at its next "
            "tool or model call ('preloop agents install-plugin' adds command "
            "mode)",
            agent,
        )
    if agent.runtime_session_id is None or not control_heartbeat_is_fresh(
        agent.control_last_heartbeat_at, now=now
    ):
        seconds = int(AGENT_CONTROL_PRESENCE_TTL.total_seconds())
        return SessionControlMode(
            "note",
            "control_offline",
            f"{name} has no live Agent Control connection (no heartbeat in the "
            f"last {seconds}s); a line is a note until it reconnects",
            agent,
        )
    return SessionControlMode("command", None, None, agent)
