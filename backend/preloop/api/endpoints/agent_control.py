"""Managed-agent control-plane WebSocket and operator command endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Awaitable, Callable, Optional, TypeVar

import nats.errors
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError
from starlette import status

from preloop.api.loop_safety import run_db_off_loop
from preloop.api.auth import get_current_active_user
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from preloop.api.auth.jwt import (
    RuntimeBearerAuthContext,
    authenticate_runtime_bearer_token,
)
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_agent_control_command,
    crud_audit_log,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.crud import agent_control_connection as control_connection
from preloop.models.crud.agent_control_connection import AgentControlConnectionContext
from preloop.models.db.session import get_db_session
from preloop.utils.redaction import redact_dict
from preloop.schemas.agent_control import (
    AgentControlCommandResponse,
    AgentControlCommandStatusResponse,
    AgentControlEnvelope,
    AgentControlEnvelopeType,
    AgentControlInboundEnvelope,
    AgentControlSendMessageRequest,
    AgentControlSessionActionRequest,
    AgentControlSessionMode,
    AgentControlVoiceTranscriptRequest,
    RuntimeSessionControlResponse,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AGENT_CONTROL,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    ACCOUNT_TOPIC_SESSION_PROGRESS,
    build_account_event,
    emit_account_event,
)
from preloop.sync.services.event_bus import get_nats_client
from preloop.services.agent_control_dispatch import (
    CONTROL_NEW_SESSION_UNSUPPORTED_KINDS,
    SUPPORTED_CONTROL_AGENT_KINDS,
    TERMINAL_COMMAND_DELIVERY_STATES,
    AgentControlDispatchError,
    agent_has_control_config,
    build_operator_envelope,
    command_delivery_state,
    command_source,
    create_command_history_session,
    dispatch_operator_message,
    persist_and_deliver_command,
    resolve_session_control_mode,
    TERMINAL_SESSION_REASON,
    TERMINAL_SESSION_REASON_CODE,
    session_reachable_by_control,
    session_managed_agent,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Close code sent to an evicted WebSocket so clients can distinguish eviction
# from other closures and skip immediate reconnection.  The Python client
# already defines the same value as ``EVICTION_CLOSE_CODE`` in
# ``integrations/agent_control/core.py``; keep them in sync.
EVICTION_CLOSE_CODE: int = 4000
# Registration is serialized through the claim/registry handoff. A stalled
# evicted socket must not hold that lock while unrelated agents try to connect.
EVICTION_CLOSE_TIMEOUT_SECONDS: float = 1.0

# How long the control WebSocket waits for an inbound frame before it pings the
# agent, and how many of those windows may pass in silence before the socket is
# closed.
#
# Presence itself is retired by the ~90s heartbeat TTL, independent of this
# close. Two windows is 120s: that is how long a half-open socket (a closed
# laptop lid, a network that dropped without sending a FIN) can occupy this
# replica. Closing frees the dead connection so a wedged plugin can reconnect,
# and the disconnect path can drop the registry entry and clear the heartbeat.
AGENT_CONTROL_RECEIVE_TIMEOUT_SECONDS: float = 60.0
AGENT_CONTROL_MAX_SILENT_RECEIVES: int = 2

# Operational failures on the delivery path (NATS / DB / WS). Catch these
# instead of bare ``Exception`` so programming bugs (AttributeError, TypeError,
# etc.) still surface.
_NATS_DELIVERY_ERRORS = (
    nats.errors.Error,
    OSError,
    TimeoutError,
    ConnectionError,
    asyncio.TimeoutError,
)
_DB_DELIVERY_ERRORS = (SQLAlchemyError,)
# Agent/session vanished under the live WebSocket (deleted row or concurrent
# modification). Distinct from transient DB failures so logs stay accurate.
_AGENT_GONE_ERRORS = (ObjectDeletedError, StaleDataError)
_WS_DELIVERY_ERRORS = (
    WebSocketDisconnect,
    OSError,
    ConnectionError,
    RuntimeError,
    ValueError,
    json.JSONDecodeError,
    UnicodeDecodeError,
)
_SERIALIZATION_ERRORS = (
    TypeError,
    ValueError,
    OverflowError,
    PydanticSerializationError,
)

HEARTBEAT_TOUCH_INTERVAL = timedelta(seconds=15)


def _connection_context_from_auth(
    context: RuntimeBearerAuthContext,
) -> AgentControlConnectionContext:
    """Capture connection identifiers before ORM rows can be deleted."""
    return AgentControlConnectionContext(
        api_key_id=str(context.api_key.id),
        user_id=str(context.user.id),
        agent_kind=context.managed_agent.agent_kind,
        session_reference=context.runtime_session.session_reference,
        runtime_principal_id=context.runtime_session.runtime_principal_id,
        connection_id=str(uuid.uuid4()),
        account_id=str(context.runtime_session.account_id),
        managed_agent_id=str(context.managed_agent.id),
        runtime_session_id=str(context.runtime_session.id),
        session_source_type=context.runtime_session.session_source_type,
        session_source_id=context.runtime_session.session_source_id,
        managed_agent_session_source_type=context.managed_agent.session_source_type,
        managed_agent_session_source_id=context.managed_agent.session_source_id,
    )


T = TypeVar("T")


class _ControlDatabase:
    """Serialize short DB phases without pinning a connection to the socket."""

    def __init__(self, db: Session) -> None:
        # Resolving the bind does not check out a connection. The dependency's
        # empty session remains unused; each worker owns and closes its session.
        self._bind = db.get_bind()
        self._lock = db.info.setdefault("agent_control_db_lock", asyncio.Lock())

    async def run(self, operation: Callable[[Session], T]) -> T:
        """Finish/close a worker session before yielding to network operations."""

        def execute() -> T:
            # create_savepoint also respects externally managed test transactions.
            with Session(
                bind=self._bind, join_transaction_mode="create_savepoint"
            ) as db:
                control_connection.configure_transaction(db)
                return operation(db)

        async with self._lock:
            # Cancellation drains the worker before releasing this lock.
            return await run_db_off_loop(execute)


def _load_control_identity(db: Session, token: str) -> AgentControlConnectionContext:
    """Authenticate in the worker and return scalar identity only."""
    context = authenticate_runtime_bearer_token(
        db, token, enforce_current_binding=False
    )
    return _connection_context_from_auth(context)


def _agent_has_control_config(db: Session, *, account_id: str, agent: Any) -> bool:
    """True when the agent kind is allow-listed and control config is verified."""
    return agent_has_control_config(db, account_id=account_id, agent=agent)


class AgentControlConnectionManager:
    """In-process registry for currently connected managed agents."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._agent_connections: dict[str, str] = {}
        self._connection_agents: dict[str, str] = {}
        self._presence: dict[str, dict[str, Any]] = {}
        self._senders: dict[str, Callable[[dict[str, Any]], Awaitable[bool]]] = {}
        self._lock = asyncio.Lock()
        self.registration_lock = asyncio.Lock()

    async def connect(
        self,
        *,
        managed_agent_id: str,
        websocket: WebSocket,
        sender: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> str:
        """Register one accepted managed-agent WebSocket.

        If another connection is already registered for the same agent,
        the previous connection is evicted: it receives a close frame
        with code EVICTION_CLOSE_CODE so the client can distinguish eviction from other
        closures and avoid an immediate reconnect storm.
        """
        connection_id = str(uuid.uuid4())
        evicted_ws: WebSocket | None = None
        evicted_id: str | None = None
        async with self._lock:
            previous_connection_id = self._agent_connections.get(managed_agent_id)
            if previous_connection_id:
                evicted_ws = self._connections.pop(previous_connection_id, None)
                evicted_id = previous_connection_id
                self._connection_agents.pop(previous_connection_id, None)
                self._senders.pop(previous_connection_id, None)
            self._connections[connection_id] = websocket
            if sender is not None:
                self._senders[connection_id] = sender
            self._agent_connections[managed_agent_id] = connection_id
            self._connection_agents[connection_id] = managed_agent_id
        if evicted_ws is not None:
            logger.warning(
                "Evicting agent-control connection for managed_agent_id=%s: "
                "connection %s superseded by %s",
                managed_agent_id,
                evicted_id,
                connection_id,
            )
            try:
                await asyncio.wait_for(
                    evicted_ws.close(
                        code=EVICTION_CLOSE_CODE,
                        reason="Superseded by a newer connection for this agent",
                    ),
                    timeout=EVICTION_CLOSE_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.debug(
                    "Failed to send close frame to evicted WebSocket for agent %s",
                    managed_agent_id,
                    exc_info=True,
                )
        return connection_id

    async def disconnect(self, connection_id: str) -> bool:
        """Remove one connection if it is still the active binding."""
        async with self._lock:
            managed_agent_id = self._connection_agents.pop(connection_id, None)
            self._connections.pop(connection_id, None)
            self._senders.pop(connection_id, None)
            if managed_agent_id is None:
                return False
            if self._agent_connections.get(managed_agent_id) == connection_id:
                self._agent_connections.pop(managed_agent_id, None)
                self._presence.pop(managed_agent_id, None)
                return True
            return False

    def snapshot(self, managed_agent_id: str) -> dict[str, Any]:
        """Return WS liveness and last advertised capabilities for one agent."""
        online = managed_agent_id in self._agent_connections
        presence = self._presence.get(managed_agent_id, {})
        capabilities = presence.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        session_mode = presence.get("session_mode")
        if not online:
            session_mode = "offline"
        elif session_mode not in {"local", "remote", "queued"}:
            session_mode = "remote"
        queued_count = presence.get("queued_count") or 0
        try:
            queued_count = int(queued_count)
        except (TypeError, ValueError):
            queued_count = 0
        if queued_count > 0:
            session_mode = "queued"
        raw_desktop = capabilities.get("desktop")
        desktop = raw_desktop if raw_desktop in ("vnc", "rdp") else "none"
        desktop_display = capabilities.get("desktop_display")
        if desktop == "none" or not isinstance(desktop_display, str):
            desktop_display = None
        return {
            "online": online,
            "supports_interrupt": bool(capabilities.get("interrupt")),
            "session_mode": session_mode,
            "capabilities": capabilities,
            "queued_count": queued_count,
            "desktop": desktop,
            "desktop_display": desktop_display,
        }

    def record_presence(
        self,
        managed_agent_id: str,
        payload: dict[str, Any] | None,
        *,
        connection_id: str | None = None,
    ) -> None:
        """Remember the last capabilities/session_mode envelope from the plugin."""
        if (
            connection_id is not None
            and self._agent_connections.get(managed_agent_id) != connection_id
        ):
            return
        incoming = dict(payload or {})
        # Heartbeat and status frames omit capabilities. Replacing the whole
        # entry would drop desktop (and interrupt) about 30s after connect.
        if "capabilities" not in incoming:
            previous = self._presence.get(managed_agent_id, {})
            previous_capabilities = previous.get("capabilities")
            if isinstance(previous_capabilities, dict):
                incoming["capabilities"] = previous_capabilities
        self._presence[managed_agent_id] = incoming

    async def send_to_agent(
        self, *, managed_agent_id: str, envelope: AgentControlEnvelope
    ) -> bool:
        """Send one envelope to a locally connected managed agent."""
        async with self._lock:
            connection_id = self._agent_connections.get(managed_agent_id)
            websocket = self._connections.get(connection_id or "")
            sender = self._senders.get(connection_id or "")
        if websocket is None:
            return False
        try:
            payload = envelope.model_dump(mode="json")
        except _SERIALIZATION_ERRORS:
            logger.exception(
                "Failed to serialize Agent Control envelope for agent %s; "
                "sending minimal fallback",
                managed_agent_id,
            )
            payload = {
                "type": getattr(envelope, "type", "error"),
                "name": "serialization_error",
                "message_id": getattr(envelope, "message_id", None),
                "managed_agent_id": managed_agent_id,
                "payload": {"error": "envelope_serialization_failed"},
            }
        try:
            if sender is not None:
                return bool(await sender(payload))
            await websocket.send_json(payload)
        except _WS_DELIVERY_ERRORS:
            logger.exception(
                "Failed to send Agent Control envelope to agent %s",
                managed_agent_id,
            )
            return False
        return True


agent_control_manager = AgentControlConnectionManager()


def agent_control_snapshot(managed_agent_id: str) -> dict[str, Any]:
    """Process-local Agent Control WS snapshot for account enrichment."""
    return agent_control_manager.snapshot(managed_agent_id)


def _extract_bearer_token(websocket: WebSocket) -> Optional[str]:
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    query_token = websocket.query_params.get("token")
    if not query_token:
        return None
    if not settings.agent_control_allow_query_token:
        logger.warning(
            "Agent Control WebSocket ?token= query auth rejected "
            "(set AGENT_CONTROL_ALLOW_QUERY_TOKEN=true to enable; prefer "
            "Authorization: Bearer)"
        )
        return None
    logger.warning(
        "Agent Control WebSocket authenticated via ?token= query parameter; "
        "prefer Authorization: Bearer and redact token query params from "
        "reverse-proxy access logs"
    )
    return query_token


def _connection_envelope(
    connection: AgentControlConnectionContext,
    *,
    envelope_type: AgentControlEnvelopeType,
    name: str,
    message_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> AgentControlEnvelope:
    return AgentControlEnvelope(
        type=envelope_type,
        name=name,
        message_id=message_id or str(uuid.uuid4()),
        account_id=connection.account_id,
        managed_agent_id=connection.managed_agent_id,
        runtime_session_id=connection.runtime_session_id,
        session_source_type=connection.session_source_type,
        session_source_id=connection.session_source_id,
        timestamp=datetime.now(UTC),
        payload=payload or {},
    )


def _command_source(metadata: dict[str, Any]) -> Optional[str]:
    """Best-effort originating surface (console|mobile|watch|api) for audit."""
    return command_source(metadata)


def _operator_command_envelope(
    agent: Any,
    *,
    name: str,
    payload: dict[str, Any],
) -> AgentControlEnvelope:
    try:
        return build_operator_envelope(agent, name=name, payload=payload)
    except AgentControlDispatchError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        ) from exc


def _touch_presence(
    db: Session,
    context: AgentControlConnectionContext,
    *,
    observed_at: datetime,
    session_mode: Optional[str] = None,
    reachable_session_ids: Optional[list[Any]] = None,
    commit: bool = True,
) -> None:
    # Match operator lifecycle transactions: managed agent before runtime.
    # Reversing this order lets heartbeat and decommission wait on each other.
    crud_managed_agent.touch_last_seen_for_principal(
        db,
        account_id=context.account_id,
        session_source_type=context.managed_agent_session_source_type,
        session_source_id=context.managed_agent_session_source_id,
        observed_at=observed_at,
        control_session_mode=session_mode,
        # This function is only reachable from the Agent Control WebSocket, so
        # every call here is proof the plugin is connected right now. It is the
        # only presence signal the other api replicas can read.
        control_heartbeat_at=observed_at,
        control_reachable_session_ids=reachable_session_ids,
        commit=False,
    )
    crud_runtime_session.touch_activity(
        db,
        account_id=context.account_id,
        runtime_session_id=context.runtime_session_id,
        observed_at=observed_at,
        min_update_interval=HEARTBEAT_TOUCH_INTERVAL,
        commit=False,
    )
    if commit:
        control_connection.commit(db)


def _mark_control_verified_from_capabilities(
    db: Session,
    context: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
    *,
    commit: bool = True,
) -> None:
    """Treat a live capabilities envelope as runtime-plugin verification."""
    if inbound.type != "presence" or inbound.name != "capabilities":
        return

    agent_kind = str(
        context.agent_kind
        or context.managed_agent_session_source_type
        or context.session_source_type
        or ""
    ).lower()
    if agent_kind not in SUPPORTED_CONTROL_AGENT_KINDS:
        return

    latest_enrollment = crud_managed_agent_enrollment.get_latest_for_agent_by_type(
        db,
        account_id=str(context.account_id),
        agent_id=str(context.managed_agent_id),
        enrollment_type="cli_managed_config",
    ) or crud_managed_agent_enrollment.get_latest_for_agent_by_type(
        db,
        account_id=str(context.account_id),
        agent_id=str(context.managed_agent_id),
        enrollment_type="runtime_plugin_control",
    )
    validation_result = {
        **(
            latest_enrollment.validation_result
            if latest_enrollment is not None
            and isinstance(latest_enrollment.validation_result, dict)
            else {}
        ),
        "control_channel_configured": True,
        "control_plugin_installed": True,
        "control_plugin_verified": True,
        "control_plugin_verification": "verified_by_runtime_connection",
        "control_ws_url_ok": True,
        "control_bearer_token_ok": True,
        "control_runtime_principal_id_ok": True,
        "control_runtime_session_id_present": True,
    }
    if latest_enrollment is not None:
        crud_managed_agent_enrollment.mark_validated(
            db,
            account_id=context.account_id,
            agent_id=context.managed_agent_id,
            enrollment_id=str(latest_enrollment.id),
            validation_result=validation_result,
            commit=commit,
        )
        return

    crud_managed_agent_enrollment.create_for_agent(
        db,
        account_id=context.account_id,
        agent_id=context.managed_agent_id,
        created_by_user_id=context.user_id,
        enrollment_type="runtime_plugin_control",
        adapter_key=agent_kind,
        status="validated",
        target_config_path=context.session_reference,
        discovered_config={
            "session_source_type": context.session_source_type,
            "session_source_id": context.session_source_id,
            "runtime_principal_id": context.runtime_principal_id,
        },
        managed_config={
            "preloop": {
                "control": {
                    "enabled": True,
                    "runtime": agent_kind,
                    "control_ws_url": "/api/v1/agents/control/ws",
                    "managed_agent_id": str(context.managed_agent_id),
                    "runtime_session_id": str(context.runtime_session_id),
                    "runtime_principal_id": context.runtime_principal_id,
                }
            }
        },
        validation_result=validation_result,
        restore_available=False,
        last_applied_at=datetime.now(UTC),
        last_validated_at=datetime.now(UTC),
        commit=commit,
    )


async def _publish_command(
    envelope: AgentControlEnvelope,
) -> Optional[str]:
    subject = f"agent-control.commands.{envelope.managed_agent_id}"
    try:
        nats_client = await get_nats_client()
        if not nats_client or not nats_client.is_connected:
            return None
        await nats_client.publish(
            subject,
            json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        )
        return subject
    except _NATS_DELIVERY_ERRORS:
        logger.exception("Failed to publish managed-agent command")
        return None


def _safe_mark_command_delivered(
    db: Session,
    *,
    account_id: str,
    command_id: str,
    managed_agent_id: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Mark a persisted command delivered; DB errors never break delivery.

    Uses a savepoint so a failed mark cannot roll back unrelated outer
    transaction work on the shared WebSocket session.
    """
    try:
        with db.begin_nested():
            crud_agent_control_command.mark_delivered(
                db,
                account_id=account_id,
                command_id=command_id,
                managed_agent_id=managed_agent_id,
                delivered_at=datetime.now(UTC),
                commit=False,
            )
        if commit:
            db.commit()
    except _DB_DELIVERY_ERRORS:
        logger.exception(
            "Failed to mark Agent Control command %s delivered", command_id
        )


async def _send_control_command(
    database: _ControlDatabase,
    connection: AgentControlConnectionContext,
    websocket: WebSocket,
    payload: dict[str, Any],
) -> bool:
    """Fence database effects around at-least-once socket delivery.

    Use the persisted account/agent-scoped envelope, never broker-supplied
    command content. Replacement can happen after the read and before the
    send; that old socket can no longer acknowledge or mark delivery.
    """
    command_id = payload.get("message_id")
    if payload.get("type") != "command" or not isinstance(command_id, str):
        return False

    def load(db: Session) -> dict[str, Any] | None:
        if control_connection.authorize(db, connection) is None:
            return None
        record = crud_agent_control_command.get_by_command_id(
            db,
            account_id=connection.account_id,
            managed_agent_id=connection.managed_agent_id,
            command_id=command_id,
        )
        if (
            record is None
            or record.kind != "command"
            or record.status not in {"pending", "delivered"}
        ):
            return None
        if record.expires_at is not None and record.expires_at.replace(
            tzinfo=UTC
        ) <= datetime.now(UTC):
            return None
        from preloop.utils.control_credentials import hydrate_control_credentials

        return hydrate_control_credentials(record.envelope)

    def mark(db: Session) -> None:
        if control_connection.authorize(db, connection) is not None:
            _safe_mark_command_delivered(
                db,
                account_id=connection.account_id,
                managed_agent_id=connection.managed_agent_id,
                command_id=command_id,
            )

    try:
        envelope = await database.run(load)
        if envelope is None:
            return False
        await websocket.send_json(envelope)
        await database.run(mark)
        return True
    except _DB_DELIVERY_ERRORS + _WS_DELIVERY_ERRORS:
        logger.warning("Control command delivery unavailable", exc_info=True)
        return False


async def _subscribe_to_commands(
    *,
    managed_agent_id: str,
    websocket: WebSocket,
    database: Optional[_ControlDatabase] = None,
    account_id: Optional[str] = None,
    connection: AgentControlConnectionContext | None = None,
) -> Any:
    subject = f"agent-control.commands.{managed_agent_id}"
    try:
        nats_client = await get_nats_client()
        if not nats_client or not nats_client.is_connected:
            return None

        async def forward_command(msg: Any) -> None:
            try:
                payload = json.loads(msg.data.decode())
            except _WS_DELIVERY_ERRORS:
                logger.exception("Invalid managed-agent command notification")
                return
            if not isinstance(payload, dict) or database is None or connection is None:
                return
            await _send_control_command(database, connection, websocket, payload)

        return await nats_client.subscribe(subject, cb=forward_command)
    except _NATS_DELIVERY_ERRORS:
        logger.debug("Managed-agent command subscription unavailable", exc_info=True)
        return None


async def _redeliver_pending_commands(
    database: _ControlDatabase,
    connection: AgentControlConnectionContext,
    websocket: WebSocket,
) -> None:
    """Load plain command envelopes, release the connection, then redeliver."""
    now = datetime.now(UTC)

    def load(db: Session) -> list[tuple[str, dict[str, Any]]]:
        if control_connection.authorize(db, connection) is None:
            return []
        with db.begin_nested():
            crud_agent_control_command.expire_stale(
                db,
                now=now,
                account_id=connection.account_id,
                managed_agent_id=connection.managed_agent_id,
                commit=False,
            )
            pending = crud_agent_control_command.get_undelivered_for_agent(
                db,
                managed_agent_id=connection.managed_agent_id,
                account_id=connection.account_id,
                now=now,
            )
            snapshots = [
                (record.command_id, dict(record.envelope)) for record in pending
            ]
        db.commit()
        return snapshots

    try:
        pending = await database.run(load)
    except _DB_DELIVERY_ERRORS:
        logger.exception(
            "Failed to load undelivered Agent Control commands for agent %s",
            connection.managed_agent_id,
        )
        return

    for _, envelope in pending:
        if not await _send_control_command(database, connection, websocket, envelope):
            break


_COMMAND_ACK_NAMES = {
    "ack",
    "command_ack",
    "command_result",
    "command_error",
    "command_progress",
}


def _handle_command_ack(
    db: Session,
    connection: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
    *,
    commit: bool = True,
) -> None:
    """Record an end-to-end command acknowledgement from the agent.

    Runtime plugins ack by sending an inbound envelope named ``ack`` or
    ``command_ack`` (a ``command_result``/``command_error`` also proves
    receipt) carrying the original command id. The ack is scoped to the
    connected ``managed_agent_id`` so a peer agent cannot spoof another
    agent's delivery state. Unknown ids are logged, not errors, and DB
    failures never break the WebSocket loop.
    """
    if inbound.name not in _COMMAND_ACK_NAMES:
        return
    command_id = inbound.payload.get("command_id")
    if not isinstance(command_id, str) or not command_id.strip():
        return
    try:
        with db.begin_nested():
            record = crud_agent_control_command.mark_acked(
                db,
                account_id=connection.account_id,
                managed_agent_id=connection.managed_agent_id,
                command_id=command_id.strip(),
                acked_at=datetime.now(UTC),
                commit=False,
            )
        if commit:
            control_connection.commit(db)
        if record is None:
            logger.info(
                "Received ack for unknown or cross-agent Agent Control command %s",
                command_id,
            )
    except _DB_DELIVERY_ERRORS:
        if not commit:
            raise
        logger.exception("Failed to mark Agent Control command %s acked", command_id)


_AGENT_CONTROL_TRUNCATE_KEYS = frozenset(
    {"result", "reply_text", "error", "message", "text", "output"}
)
_MAX_AGENT_CONTROL_PAYLOAD_CHARS = 4096
_MAX_AGENT_CONTROL_RESULT_CHARS = 1_048_576
_AGENT_CONTROL_TRUNCATION_MARKER = "...[truncated]"
_AGENT_CONTROL_OMISSION_MARKERS = frozenset({"result_too_large", "structured_result"})


def _truncate_agent_control_text(value: str, max_chars: int) -> str:
    """Cap one string field, keeping a marker when the tail is dropped."""
    if len(value) > max_chars:
        return value[:max_chars] + _AGENT_CONTROL_TRUNCATION_MARKER
    return value


def _is_omission_marker(value: Any) -> bool:
    """True for a marker this sanitizer wrote, not an agent-supplied dict."""
    return (
        isinstance(value, dict)
        and set(value) <= {"_omitted", "type"}
        and value.get("_omitted") in _AGENT_CONTROL_OMISSION_MARKERS
    )


def _payload_within_budget(payload: dict[str, Any], max_chars: int) -> bool:
    """True when the JSON form of ``payload`` fits in ``max_chars``."""
    try:
        return len(json.dumps(payload, default=str)) <= max_chars
    except (TypeError, ValueError):
        return False


def _single_truncated_field_fits(payload: dict[str, Any], max_chars: int) -> bool:
    """Allow one truncated string to carry its marker past the serialized budget.

    The slack is the marker only, measured on ``json.dumps`` so key names
    and punctuation count. A second bulky field, or a key long enough to
    blow the envelope, does not qualify.
    """
    texts = [value for value in payload.values() if isinstance(value, str)]
    if len(texts) != 1 or not texts[0].endswith(_AGENT_CONTROL_TRUNCATION_MARKER):
        return False
    if not all(
        isinstance(value, (str, int, float, bool, type(None)))
        or _is_omission_marker(value)
        for value in payload.values()
    ):
        return False
    try:
        serialized = len(json.dumps(payload, default=str))
    except (TypeError, ValueError):
        return False
    return serialized - len(_AGENT_CONTROL_TRUNCATION_MARKER) <= max_chars


def _refit_string_field(payload: dict[str, Any], key: str, max_chars: int) -> bool:
    """Shorten ``payload[key]`` until the envelope fits, keeping a prefix.

    Returns:
        True when the shortened field meets the hard budget or the
        single-truncated-field slack. False when even a marker-only value
        cannot fit; the caller should omit the field.
    """
    text = payload.get(key)
    if not isinstance(text, str):
        return False
    original = text
    lo = 0
    hi = len(text)
    best: str | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid >= len(text):
            candidate = text
        else:
            candidate = text[:mid] + _AGENT_CONTROL_TRUNCATION_MARKER
        payload[key] = candidate
        if _payload_within_budget(payload, max_chars) or _single_truncated_field_fits(
            payload, max_chars
        ):
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        payload[key] = original
        return False
    payload[key] = best
    return True


def _cap_persisted_result_payload(
    sanitized: dict[str, Any], max_chars: int
) -> dict[str, Any]:
    """Drop or shorten values until the persisted envelope fits its budget.

    Each string is already truncated to ``max_chars``. Structured values
    become an omission marker. A string that still blows the serialized
    envelope is shortened so the JSON fits, marker included, before it is
    omitted. One truncated string may exceed the budget by the marker only.
    Key names count toward the serialized size.
    """
    if _payload_within_budget(sanitized, max_chars) or _single_truncated_field_fits(
        sanitized, max_chars
    ):
        return sanitized
    capped = dict(sanitized)
    if "result" in capped and not _is_omission_marker(capped["result"]):
        capped["result"] = {"_omitted": "result_too_large"}
        if _payload_within_budget(capped, max_chars):
            return capped
    bounded: dict[str, Any] = {}
    for key, value in capped.items():
        if isinstance(value, (str, int, float, bool, type(None))):
            bounded[key] = value
        elif _is_omission_marker(value):
            bounded[key] = value
        else:
            bounded[key] = {
                "_omitted": "result_too_large",
                "type": type(value).__name__,
            }
    while not _payload_within_budget(
        bounded, max_chars
    ) and not _single_truncated_field_fits(bounded, max_chars):
        strings = [
            (key, value) for key, value in bounded.items() if isinstance(value, str)
        ]
        if not strings:
            return {"_omitted": "result_too_large"}
        longest = max(strings, key=lambda item: len(item[1]))[0]
        if _refit_string_field(bounded, longest, max_chars):
            continue
        bounded[longest] = {"_omitted": "result_too_large"}
    return bounded


def _sanitize_agent_control_payload(
    payload: dict[str, Any],
    *,
    max_chars: int = _MAX_AGENT_CONTROL_PAYLOAD_CHARS,
    drop_structured_result: bool = True,
) -> dict[str, Any]:
    """Redact secrets and bound bulky agent output before emit or persist.

    Args:
        payload: Inbound agent-control envelope fields.
        max_chars: Character budget for each string and, when structured
            results are kept, for the serialized payload.
        drop_structured_result: When True, replace a non-scalar ``result``
            with a type marker. Live console and audit logs use this.
            Persistent command rows pass False so a later step can read a
            structured result that still fits in ``max_chars``.

    Returns:
        A JSON-safe dict. Secrets are redacted. Oversized values are
        truncated or replaced with an omission marker.
    """
    from preloop.utils.redaction import redact_dict

    safe = redact_dict(payload)
    if not isinstance(safe, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in safe.items():
        if (
            drop_structured_result
            and key == "result"
            and not isinstance(value, (str, int, float, bool, type(None)))
        ):
            sanitized[key] = {
                "_omitted": "structured_result",
                "type": type(value).__name__,
            }
        elif isinstance(value, str) and (
            key in _AGENT_CONTROL_TRUNCATE_KEYS or not drop_structured_result
        ):
            sanitized[key] = _truncate_agent_control_text(value, max_chars)
        else:
            sanitized[key] = value
    if drop_structured_result:
        return sanitized
    return _cap_persisted_result_payload(sanitized, max_chars)


def _sanitize_agent_control_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and bound execution output persisted on the command row."""
    return _sanitize_agent_control_payload(
        payload,
        max_chars=_MAX_AGENT_CONTROL_RESULT_CHARS,
        drop_structured_result=False,
    )


def _persist_agent_control_result(
    db: Session,
    context: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
    *,
    commit: bool = True,
) -> None:
    if inbound.type != "status" or inbound.name not in {
        "command_result",
        "command_error",
    }:
        return

    command_id = inbound.payload.get("command_id")
    if not isinstance(command_id, str) or not command_id.strip():
        return

    command = crud_agent_control_command.get_by_command_id(
        db,
        account_id=context.account_id,
        managed_agent_id=context.managed_agent_id,
        command_id=command_id.strip(),
    )
    if command is None or command.kind != "command":
        return
    # Results are recorded after ack. Errors may land on a still-pending
    # command if the runtime never acknowledged delivery.
    if inbound.name == "command_result" and command.status != "acked":
        return

    default_status = "failed" if inbound.name == "command_error" else "completed"
    result_status = str(inbound.payload.get("status") or default_status)
    reply_text = inbound.payload.get("reply_text")
    error_text = inbound.payload.get("error")
    message = None
    if isinstance(reply_text, str) and reply_text.strip():
        message = reply_text.strip()
    elif isinstance(error_text, str) and error_text.strip():
        message = error_text.strip()

    sanitized = _sanitize_agent_control_payload(inbound.payload)
    crud_runtime_session_activity.log_agent_control_result(
        db,
        account_id=command.consuming_account_id or context.account_id,
        command_id=command_id.strip(),
        fallback_runtime_session_id=command.runtime_session_id
        or context.runtime_session_id,
        status=result_status,
        message=message,
        metadata=sanitized,
        commit=False,
    )
    failed = inbound.name == "command_error" or result_status in {
        "failed",
        "error",
    }
    error_text = inbound.payload.get("error")
    full_result_payload = _sanitize_agent_control_result_payload(inbound.payload)
    crud_agent_control_command.mark_terminal_result(
        db,
        account_id=context.account_id,
        managed_agent_id=context.managed_agent_id,
        command_id=command_id.strip(),
        result_payload=full_result_payload,
        failed=failed,
        error=error_text if isinstance(error_text, str) else None,
        commit=commit,
    )


def _persist_agent_control_progress(
    db: Session,
    context: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
) -> None:
    """Accept progress only for a command dispatched to this authenticated agent."""
    if inbound.type != "event" or inbound.name != "command_progress":
        return
    command_id = inbound.payload.get("command_id")
    summary = inbound.payload.get("summary")
    if (
        not isinstance(command_id, str)
        or not isinstance(summary, str)
        or not isinstance(inbound.message_id, str)
        or not inbound.message_id
    ):
        return
    command = crud_agent_control_command.get_by_command_id(
        db,
        account_id=context.account_id,
        managed_agent_id=context.managed_agent_id,
        command_id=command_id,
    )
    if command is None or command.kind != "command" or command.status != "acked":
        return
    kind = inbound.payload.get("kind")
    call_id = inbound.payload.get("call_id")
    tool_name = inbound.payload.get("tool_name")
    if not isinstance(kind, str) or kind not in {"assistant", "tool", "summary"}:
        return
    if kind == "tool" and not (
        isinstance(call_id, str)
        and 0 < len(call_id) <= 200
        and isinstance(tool_name, str)
        and 0 < len(tool_name) <= 100
    ):
        return
    consuming_account_id = command.consuming_account_id or context.account_id
    activity = crud_runtime_session_activity.log_agent_control_progress(
        db,
        account_id=consuming_account_id,
        command_id=command_id,
        event_id=str(inbound.message_id),
        kind=kind,
        summary=summary[:2000],
        call_id=call_id if isinstance(call_id, str) else None,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        commit=False,
    )
    _broadcast_command_progress(
        consuming_account_id,
        activity,
        command_id=command_id,
        kind=kind,
        call_id=call_id if isinstance(call_id, str) else None,
        tool_name=tool_name if isinstance(tool_name, str) else None,
    )


def _broadcast_command_progress(
    account_id: Any,
    activity: Any,
    *,
    command_id: str,
    kind: str,
    call_id: Optional[str],
    tool_name: Optional[str],
) -> None:
    """Tell the consuming session's viewers that an SDK turn moved.

    Scoped to the consuming account and the progress row's runtime session,
    like ``runtime_session_turn_ended``, on the ``session_progress`` topic so
    views that reload on any ``agent_control`` event are not woken by every
    step. The payload says what kind of step it was and which tool, never the
    summary text: viewers read text from the stored activity stream, which
    applies its own access checks.
    """
    runtime_session_id = getattr(activity, "runtime_session_id", None)
    if activity is None or runtime_session_id is None:
        return
    try:
        emit_account_event(
            build_account_event(
                account_id=str(account_id),
                topic=ACCOUNT_TOPIC_SESSION_PROGRESS,
                event_type="managed_agent_command_progress",
                payload={
                    "runtime_session_id": str(runtime_session_id),
                    "command_id": command_id,
                    "kind": kind,
                    "call_id": call_id,
                    "tool_name": tool_name,
                },
                runtime_session_id=str(runtime_session_id),
            )
        )
    except Exception:
        logger.warning("Failed to emit command progress", exc_info=True)


#: Live events a sidecar forwards for a session it can drive but did not
#: start through a command (a Codex TUI thread on the shared app-server
#: daemon). They are broadcast on their own topic, not stored; the sidecar
#: merges per-token deltas into at most four events a second.
SESSION_PROGRESS_KINDS = frozenset(
    {
        "turn_started",
        "turn_completed",
        "assistant_delta",
        "status_title",
        "tool_start",
        "tool_end",
        "usage",
    }
)
_SESSION_PROGRESS_TEXT_FIELDS = {
    "turn_id": 200,
    "item_id": 200,
    "call_id": 200,
    "tool_name": 100,
    "status": 40,
    "delta": 2000,
    "command": 400,
}
_SESSION_PROGRESS_INT_FIELDS = ("exit_code", "duration_ms")
_SESSION_PROGRESS_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _session_progress_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Keep only the known, bounded fields of a ``session_progress`` event.

    Args:
        payload: Inbound envelope payload from the sidecar.

    Returns:
        The allow-listed, redacted payload, or None when the kind is unknown.
    """
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in SESSION_PROGRESS_KINDS:
        return None
    clean: dict[str, Any] = {"kind": kind}
    for field, limit in _SESSION_PROGRESS_TEXT_FIELDS.items():
        value = payload.get(field)
        if isinstance(value, str):
            clean[field] = value[:limit]
    for field in _SESSION_PROGRESS_INT_FIELDS:
        value = payload.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            clean[field] = value
    seq = payload.get("seq")
    # Per-session event counter: a viewer that sees a jump knows it missed
    # events (dropped by the throttle or the socket) and re-reads the thread.
    if isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < 2**31:
        clean["seq"] = seq
    clean = _sanitize_agent_control_payload(clean)
    usage = payload.get("usage")
    if kind == "usage" and isinstance(usage, dict):
        # Integers only, added after redaction: the redactor masks any key
        # that contains "token", which would blank the token counters.
        clean["usage"] = {
            key: usage[key]
            for key in _SESSION_PROGRESS_USAGE_FIELDS
            if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
        }
    return clean


#: How long a native id to runtime session mapping is trusted. Reply deltas
#: arrive several times a second; the mapping does not change within a turn.
_SESSION_PROGRESS_TARGET_TTL = timedelta(seconds=30)
#: Unknown or foreign ids are re-checked sooner, so a session that registers
#: a moment after its first event is picked up quickly.
_SESSION_PROGRESS_MISS_TTL = timedelta(seconds=5)
_SESSION_PROGRESS_TARGET_CACHE_MAX = 2048
#: Where one session's progress goes: (viewer account id, runtime session id).
_ProgressTarget = tuple[str, str]
_session_progress_targets: dict[
    tuple[str, str, str], tuple[Optional[_ProgressTarget], datetime]
] = {}


def _cached_session_progress_target(
    connection: AgentControlConnectionContext, session_source_id: str
) -> tuple[bool, Optional[_ProgressTarget]]:
    """Return ``(hit, target)`` from the per-agent cache."""
    key = (
        str(connection.account_id),
        str(connection.managed_agent_id),
        session_source_id,
    )
    entry = _session_progress_targets.get(key)
    if entry is None or entry[1] <= datetime.now(UTC):
        _session_progress_targets.pop(key, None)
        return False, None
    return True, entry[0]


def _remember_session_progress_target(
    connection: AgentControlConnectionContext,
    session_source_id: str,
    target: Optional[_ProgressTarget],
) -> None:
    if len(_session_progress_targets) >= _SESSION_PROGRESS_TARGET_CACHE_MAX:
        _session_progress_targets.clear()
    ttl = _SESSION_PROGRESS_TARGET_TTL if target else _SESSION_PROGRESS_MISS_TTL
    _session_progress_targets[
        (
            str(connection.account_id),
            str(connection.managed_agent_id),
            session_source_id,
        )
    ] = (target, datetime.now(UTC) + ttl)


def _resolve_session_progress_target(
    db: Session,
    connection: AgentControlConnectionContext,
    session_source_id: str,
) -> Optional[_ProgressTarget]:
    """Map a native session id to a runtime session this agent owns.

    Args:
        db: Database session.
        connection: The authenticated sidecar connection.
        session_source_id: Native id the sidecar reported (a Codex thread id).

    Returns:
        ``(account id, runtime session id)``, or None when the session is unknown, ended or
        belongs to another agent. Account isolation comes from the
        connection's account; agent isolation from the ownership check.
    """
    session = crud_runtime_session.get_open_by_source_id(
        db,
        account_id=connection.account_id,
        session_source_id=session_source_id,
    )
    if session is None:
        return None
    agent = session_managed_agent(db, account_id=connection.account_id, session=session)
    if agent is None or str(agent.id) != str(connection.managed_agent_id):
        return None
    return str(connection.account_id), str(session.id)


def _resolve_command_progress_target(
    db: Session,
    connection: AgentControlConnectionContext,
    command_id: str,
) -> Optional[_ProgressTarget]:
    """Map a command this agent is running to the session it was sent for.

    A Talk turn (New session included) is recorded on the command's tracking
    session, whose source id is not the native id the sidecar knows, and a
    shared agent's turn belongs to the consuming account, not the agent owner.
    The command row holds both, so live progress for a command turn goes
    exactly where its stored reply goes.

    Args:
        db: Database session.
        connection: The authenticated sidecar connection.
        command_id: Envelope id of the command whose turn is streaming.

    Returns:
        ``(consuming account id, runtime session id)``, or None when the
        command is unknown, not this agent's, finished or has no session.
    """
    command = crud_agent_control_command.get_by_command_id(
        db,
        account_id=connection.account_id,
        managed_agent_id=connection.managed_agent_id,
        command_id=command_id,
    )
    if (
        command is None
        or command.kind != "command"
        or command.status not in {"delivered", "acked"}
        or command.runtime_session_id is None
    ):
        return None
    account_id = command.consuming_account_id or connection.account_id
    return str(account_id), str(command.runtime_session_id)


#: Reply and reasoning deltas one session may broadcast: a burst of
#: ``_SESSION_PROGRESS_DELTA_BURST`` refilled at ``_SESSION_PROGRESS_DELTA_RATE``
#: a second. Sidecars already merge per-token deltas to four a second; this
#: bounds a sidecar that does not, so a chatty agent cannot flood the account's
#: realtime channel. A dropped delta leaves a ``seq`` gap and the viewer
#: re-reads the thread. Turn bounds, tools and usage are never throttled.
_SESSION_PROGRESS_DELTA_RATE = 8.0
_SESSION_PROGRESS_DELTA_BURST = 16
#: Only reply text is throttled. Reasoning titles are accumulated by the
#: status line, which has no re-read on a gap, so they are never dropped.
_SESSION_PROGRESS_THROTTLED_KINDS = frozenset({"assistant_delta"})
_session_progress_buckets: dict[tuple[str, str, str], tuple[float, float]] = {}


def _allow_session_progress_delta(
    connection: AgentControlConnectionContext, session_source_id: str
) -> bool:
    """Take one token from the session's delta bucket; False when it is empty."""
    key = (
        str(connection.account_id),
        str(connection.managed_agent_id),
        session_source_id,
    )
    now = time.monotonic()
    tokens, last = _session_progress_buckets.get(
        key, (float(_SESSION_PROGRESS_DELTA_BURST), now)
    )
    tokens = min(
        float(_SESSION_PROGRESS_DELTA_BURST),
        tokens + (now - last) * _SESSION_PROGRESS_DELTA_RATE,
    )
    if len(_session_progress_buckets) >= _SESSION_PROGRESS_TARGET_CACHE_MAX:
        _session_progress_buckets.clear()
    if tokens < 1.0:
        _session_progress_buckets[key] = (tokens, now)
        return False
    _session_progress_buckets[key] = (tokens - 1.0, now)
    return True


async def _emit_session_progress(
    database: Any,
    connection: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
) -> None:
    """Broadcast one ``session_progress`` event scoped to its runtime session."""
    source_id = inbound.payload.get("session_source_id")
    if not isinstance(source_id, str) or not 0 < len(source_id) <= 200:
        return
    payload = _session_progress_payload(inbound.payload)
    if payload is None:
        return
    command_id = inbound.payload.get("command_id")
    if command_id is not None and (
        not isinstance(command_id, str) or not 0 < len(command_id) <= 200
    ):
        return
    # A command turn is scoped by its command, never by the native id: the
    # two can name different sessions (or accounts, for a shared agent).
    cache_key = f"command:{command_id}" if command_id else source_id
    hit, target = _cached_session_progress_target(connection, cache_key)
    if not hit:
        try:
            if command_id:
                target = await database.run(
                    partial(
                        _resolve_command_progress_target,
                        connection=connection,
                        command_id=command_id,
                    )
                )
            else:
                target = await database.run(
                    partial(
                        _resolve_session_progress_target,
                        connection=connection,
                        session_source_id=source_id,
                    )
                )
        except _DB_DELIVERY_ERRORS:
            # Live progress is best effort; never drop the control socket.
            logger.warning("Could not scope session progress", exc_info=True)
            return
        _remember_session_progress_target(connection, cache_key, target)
    if target is None:
        return
    account_id, runtime_session_id = target
    if payload[
        "kind"
    ] in _SESSION_PROGRESS_THROTTLED_KINDS and not _allow_session_progress_delta(
        connection, source_id
    ):
        return
    emit_account_event(
        build_account_event(
            account_id=account_id,
            topic=ACCOUNT_TOPIC_SESSION_PROGRESS,
            event_type="managed_agent_session_progress",
            payload={**payload, "message_id": inbound.message_id},
            managed_agent_id=connection.managed_agent_id,
            runtime_session_id=runtime_session_id,
            session_source_type=connection.session_source_type,
            session_source_id=source_id,
        )
    )


async def _emit_agent_message(
    connection: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
) -> None:
    # Progress is delivered through the consuming execution/session's stored
    # activity stream. The generic topic belongs to the agent owner, which can
    # be a different account for a shared agent, and must not receive its text.
    # Session progress is emitted scoped to its own session instead.
    if inbound.type == "heartbeat" or inbound.name in {
        "command_progress",
        "session_progress",
    }:
        return
    event_type = f"agent_control_{inbound.type}"
    emit_account_event(
        build_account_event(
            account_id=connection.account_id,
            topic=ACCOUNT_TOPIC_AGENT_CONTROL,
            event_type=event_type,
            payload={
                "name": inbound.name or inbound.type,
                "message_id": inbound.message_id,
                "agent_payload": _sanitize_agent_control_payload(inbound.payload),
            },
            managed_agent_id=connection.managed_agent_id,
            runtime_session_id=connection.runtime_session_id,
            session_source_type=connection.session_source_type,
            session_source_id=connection.session_source_id,
        )
    )


def _process_control_message(
    db: Session,
    connection: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
    observed_at: datetime,
) -> bool:
    """Persist one inbound frame using one isolated worker session."""
    if control_connection.authorize(db, connection) is None:
        return False
    inbound_mode = inbound.payload.get("session_mode")
    if inbound_mode not in {"local", "remote", "queued"}:
        inbound_mode = None
    # Native ids the plugin can drive. Absent on older plugins and on frames
    # that do not carry presence, which leaves the stored list unchanged.
    reachable = inbound.payload.get("reachable_session_ids")
    if not isinstance(reachable, list):
        reachable = None
    _touch_presence(
        db,
        connection,
        observed_at=observed_at,
        session_mode=inbound_mode,
        reachable_session_ids=reachable,
        commit=False,
    )
    _mark_control_verified_from_capabilities(db, connection, inbound, commit=False)
    _handle_command_ack(db, connection, inbound, commit=False)
    _persist_agent_control_result(db, connection, inbound, commit=False)
    _persist_agent_control_progress(db, connection, inbound)
    control_connection.commit(db)
    return True


def _retire_control_presence(
    db: Session,
    connection: AgentControlConnectionContext,
) -> bool:
    """Retire presence atomically using the persisted generation."""
    return control_connection.retire(db, connection)


def _claim_control_connection(
    db: Session, connection: AgentControlConnectionContext, observed_at: datetime
) -> bool:
    if control_connection.authorize(db, connection, claim=True) is None:
        return False
    _touch_presence(db, connection, observed_at=observed_at)
    return True


@router.websocket("/agents/control/ws")
async def managed_agent_control_websocket(
    websocket: WebSocket,
    db: Session = Depends(get_db_session),
) -> None:
    """Keep one managed agent online for low-latency operator commands."""
    manager = agent_control_manager
    token = _extract_bearer_token(websocket)
    if token is None:
        await websocket.close(code=1008, reason="Runtime bearer token required")
        return

    database = _ControlDatabase(db)
    try:
        connection = await database.run(
            lambda session: _load_control_identity(session, token)
        )
    except HTTPException as exc:
        # Starlette collapses a pre-accept close into a bare 403 handshake
        # failure, so the reason never reaches the agent's logs. Log it here or
        # the operator has no way to tell an expired credential from a revoked
        # one.
        logger.warning(
            "Agent control websocket rejected: %s (token prefix=%s)",
            exc.detail,
            token[:12],
        )
        await websocket.close(code=1008, reason=str(exc.detail))
        return

    await websocket.accept()
    connection_id = ""
    command_subscription = None
    now = datetime.now(UTC)
    try:
        async with manager.registration_lock:
            if not await database.run(
                lambda session: _claim_control_connection(session, connection, now)
            ):
                await websocket.close(
                    code=1008, reason="Control credential is no longer active"
                )
                return
            connection_id = await manager.connect(
                managed_agent_id=connection.managed_agent_id,
                websocket=websocket,
                sender=partial(_send_control_command, database, connection, websocket),
            )
        command_subscription = await _subscribe_to_commands(
            managed_agent_id=connection.managed_agent_id,
            websocket=websocket,
            database=database,
            account_id=connection.account_id,
            connection=connection,
        )
        # The last heartbeat this connection wrote, so a clean close can retire
        # its own presence without stealing a newer connection's.
        connected = _connection_envelope(
            connection,
            envelope_type="presence",
            name="connected",
            payload={"status": "online"},
        )
        await websocket.send_json(connected.model_dump(mode="json"))
        emit_account_event(
            build_account_event(
                account_id=connection.account_id,
                topic=ACCOUNT_TOPIC_AGENT_CONTROL,
                event_type="managed_agent_online",
                payload=connected.model_dump(mode="json"),
                managed_agent_id=connection.managed_agent_id,
                runtime_session_id=connection.runtime_session_id,
            )
        )
        # Recover commands persisted while the agent was offline. Runs right
        # after the connection handshake so reconnecting agents catch up before
        # (or alongside) sending their capabilities envelope.
        await _redeliver_pending_commands(database, connection, websocket)

        # Consecutive receive timeouts. A live plugin beats every 30s, so one
        # timeout is a slow agent and two is a socket nobody is on the other end of.
        silent_receives = 0
        while True:
            try:
                raw_message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=AGENT_CONTROL_RECEIVE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                silent_receives += 1
                if silent_receives >= AGENT_CONTROL_MAX_SILENT_RECEIVES:
                    logger.info(
                        "Managed agent %s control websocket silent for %ss; "
                        "closing so presence can be retired",
                        connection.managed_agent_id,
                        int(AGENT_CONTROL_RECEIVE_TIMEOUT_SECONDS * silent_receives),
                    )
                    # Close explicitly: the client has to learn the socket is
                    # gone so a plugin that is merely wedged can reconnect.
                    try:
                        await websocket.close(code=status.WS_1001_GOING_AWAY)
                    except _WS_DELIVERY_ERRORS:
                        logger.debug(
                            "Silent control websocket already gone", exc_info=True
                        )
                    break
                await websocket.send_json({"type": "ping"})
                continue
            silent_receives = 0

            try:
                inbound = AgentControlInboundEnvelope.model_validate(raw_message)
            except ValidationError as exc:
                await websocket.send_json(
                    {
                        "type": "error",
                        "name": "invalid_envelope",
                        "error": exc.errors(),
                    }
                )
                continue

            last_presence_at = datetime.now(UTC)
            try:
                active = await database.run(
                    partial(
                        _process_control_message,
                        connection=connection,
                        inbound=inbound,
                        observed_at=last_presence_at,
                    )
                )
                if not active:
                    logger.info(
                        "Managed agent %s deleted or inactive during control websocket; closing",
                        connection.managed_agent_id,
                    )
                    await websocket.close(
                        code=EVICTION_CLOSE_CODE,
                        reason="Control ownership or credential changed",
                    )
                    break
                if inbound.type in {"presence", "heartbeat", "status"}:
                    manager.record_presence(
                        connection.managed_agent_id,
                        inbound.payload,
                        connection_id=connection_id,
                    )
            except _AGENT_GONE_ERRORS:
                logger.info(
                    "Managed agent %s deleted during control websocket; closing",
                    connection.managed_agent_id,
                )
                break
            except _DB_DELIVERY_ERRORS:
                logger.warning(
                    "Managed agent %s DB update failed; closing control websocket",
                    connection.managed_agent_id,
                    exc_info=True,
                )
                await websocket.close(code=1013, reason="Database busy; reconnect")
                break
            await _emit_agent_message(connection, inbound)
            if inbound.type == "event" and inbound.name == "session_progress":
                await _emit_session_progress(database, connection, inbound)
            if inbound.type == "heartbeat":
                ack = _connection_envelope(
                    connection,
                    envelope_type="ack",
                    name="heartbeat",
                    message_id=inbound.message_id,
                    payload={"status": "ok"},
                )
                await websocket.send_json(ack.model_dump(mode="json"))
    except _WS_DELIVERY_ERRORS:
        logger.info("Managed-agent control WebSocket disconnected")
    except _DB_DELIVERY_ERRORS:
        logger.warning("Control database phase failed", exc_info=True)
        await websocket.close(code=1013, reason="Database busy; reconnect")
    finally:
        if command_subscription is not None:
            try:
                await command_subscription.unsubscribe()
            except _NATS_DELIVERY_ERRORS:
                logger.debug(
                    "Failed to unsubscribe agent command subscription",
                    exc_info=True,
                )
        removed_active = await manager.disconnect(connection_id)
        if removed_active or not connection_id:
            # A clean close is presence information every replica can read, so
            # retire the heartbeat instead of waiting out the window. This runs
            # in a finally block: a session that already failed must not stop
            # the disconnect bookkeeping below.
            retired = False
            try:
                retired = await database.run(
                    lambda session: _retire_control_presence(session, connection)
                )
            except _DB_DELIVERY_ERRORS:
                logger.warning(
                    "Failed to retire control presence for managed agent %s",
                    connection.managed_agent_id,
                    exc_info=True,
                )
            if retired:
                emit_account_event(
                    build_account_event(
                        account_id=connection.account_id,
                        topic=ACCOUNT_TOPIC_AGENT_CONTROL,
                        event_type="managed_agent_offline",
                        payload={
                            "managed_agent_id": connection.managed_agent_id,
                            "runtime_session_id": connection.runtime_session_id,
                        },
                        managed_agent_id=connection.managed_agent_id,
                        runtime_session_id=connection.runtime_session_id,
                    )
                )


def _resolve_session_mode(
    db: Session,
    *,
    account_id: str,
    agent: Any,
    request: AgentControlSendMessageRequest,
) -> tuple[AgentControlSessionMode, Optional[models.RuntimeSession]]:
    if request.start_new_session:
        return "new", None
    if request.target_session_id is None:
        return "current", None

    target_session = crud_runtime_session.get_account_session(
        db,
        account_id=account_id,
        runtime_session_id=str(request.target_session_id),
    )
    if target_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target runtime session not found",
        )
    source_matches = (
        target_session.session_source_type == agent.session_source_type
        and target_session.session_source_id == agent.session_source_id
    )
    principal_matches = (
        target_session.runtime_principal_type == agent.session_source_type
        and target_session.runtime_principal_id == agent.session_source_id
    )
    if not source_matches and not principal_matches:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Target runtime session does not belong to this managed agent",
        )
    return "existing", target_session


def _existing_session_identity(
    target_session: Optional[models.RuntimeSession],
) -> dict[str, Any]:
    """Native resume fields for a targeted existing session.

    Clients address sessions by Preloop UUID. Sidecars such as Claude Code
    resume by ``session_source_id``. Attach the stored identity so plugins
    do not have to map UUIDs themselves.
    """
    if target_session is None:
        return {}
    return {
        "session_source_id": target_session.session_source_id,
        "session_reference": target_session.session_reference,
    }


def _command_history_session(
    db: Session,
    *,
    agent: Any,
    request: AgentControlSendMessageRequest,
) -> Optional[models.RuntimeSession]:
    return create_command_history_session(
        db,
        agent=agent,
        start_new_session=request.start_new_session,
        target_session_id=request.target_session_id,
    )


#: Live events carry a preview of the operator's message; the timeline row
#: keeps the stored summary.
_MAX_LIVE_COMMAND_CHARS = 2000


def _emit_operator_command_activity(
    *,
    account_id: str,
    runtime_session_id: str,
    message: str,
    status: str,
    command_id: str,
    author_display: Optional[str],
    agent: Any,
) -> None:
    """Put an operator command on the session's live stream (#1150).

    The command is already a timeline row (``agent_control_message``); this
    is the same fact as the ``runtime_session_updated`` event notes and tool
    calls use, so the console's session view and ``preloop sessions attach``
    show the new turn as soon as it is sent instead of at the agent's first
    model or tool call. Never raises.
    """
    try:
        emit_account_event(
            build_account_event(
                account_id=account_id,
                topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                event_type="runtime_session_updated",
                payload={
                    "runtime_session_id": runtime_session_id,
                    "last_activity_at": datetime.now(UTC).isoformat(),
                    "activity_type": "agent_control_message",
                    "status": status,
                    "summary": message[:_MAX_LIVE_COMMAND_CHARS],
                    "metadata": {
                        "kind": "operator_command",
                        "command_id": command_id,
                        "sent_by": author_display,
                        "managed_agent_id": str(agent.id),
                        "agent_name": agent.display_name,
                    },
                },
                managed_agent_id=str(agent.id),
                runtime_session_id=runtime_session_id,
            )
        )
    except Exception:  # pragma: no cover - live delivery is best effort
        logger.debug("Live operator command event failed", exc_info=True)


def _public_shared_command_envelope(
    envelope: AgentControlEnvelope,
) -> AgentControlEnvelope:
    """Recipient command responses never include runtime credentials or metadata."""
    fields = {
        "text",
        "session_mode",
        "start_new_session",
        "input_mode",
        "target_session_id",
        "interrupt",
        "spawn_worktree",
    }
    return envelope.model_copy(
        update={
            "payload": {
                key: value for key, value in envelope.payload.items() if key in fields
            }
        }
    )


async def _require_session_start_consent(
    db: Session,
    *,
    agent: Any,
    request: AgentControlSendMessageRequest,
    current_user: models.User,
) -> None:
    """Authorize a new session before any command or history row is created."""
    starts_session = request.start_new_session or request.spawn_worktree
    if not starts_session and request.target_session_id is None:
        current = (
            crud_runtime_session.get_account_session(
                db,
                account_id=str(agent.account_id),
                runtime_session_id=str(agent.runtime_session_id),
            )
            if agent.runtime_session_id is not None
            else None
        )
        starts_session = current is None or current.ended_at is not None
    if not starts_session:
        return

    mode = agent.others_may_start_sessions
    is_owner = agent.owner_user_id is not None and str(agent.owner_user_id) == str(
        current_user.id
    )
    if is_owner or mode == "allow":
        crud_audit_log.log_action(
            db,
            account_id=agent.account_id,
            user_id=current_user.id,
            action="agent_session_consent",
            resource_type="managed_agent",
            resource_id=str(agent.id),
            status="success",
            details={"decision": "owner" if is_owner else "allow"},
        )
        return
    if mode != "ask_me" or agent.owner_user_id is None:
        crud_audit_log.log_action(
            db,
            account_id=agent.account_id,
            user_id=current_user.id,
            action="agent_session_consent",
            resource_type="managed_agent",
            resource_id=str(agent.id),
            status="denied",
            details={"decision": "deny"},
        )
        raise HTTPException(403, "The agent owner does not permit new sessions")
    if request.consent_request_id is None:
        raise HTTPException(
            422,
            "A consent_request_id is required to request owner approval",
        )
    from preloop.services.owner_consent import require_owner_consent

    intent = {
        "agent_id": str(agent.id),
        "requester_id": str(current_user.id),
        "account_id": str(current_user.account_id),
        "request": request.model_dump(mode="json"),
    }
    request_key = hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await require_owner_consent(
        db,
        account_id=agent.account_id,
        owner_user_id=agent.owner_user_id,
        requester_user_id=current_user.id,
        subject_id=str(agent.id),
        action="agent_session_start",
        request_key=request_key,
        details={
            "agent_name": agent.display_name,
            "consent_request_id": str(request.consent_request_id),
        },
    )


async def _route_managed_agent_prompt(
    *,
    agent_id: str,
    request: AgentControlSendMessageRequest,
    current_user: models.User,
    db: Session,
) -> AgentControlCommandResponse:
    agent = crud_managed_agent.get_visible_target(
        db,
        account_id=str(current_user.account_id),
        agent_id=agent_id,
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed agent not found",
        )
    if agent.lifecycle_state != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed agent is not active",
        )
    if getattr(agent, "agent_kind", None) in CONTROL_NEW_SESSION_UNSUPPORTED_KINDS and (
        request.start_new_session
        or request.spawn_worktree
        or request.input_mode != "text"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This harness supports text messages to active sessions only",
        )
    if not _agent_has_control_config(db, account_id=str(agent.account_id), agent=agent):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed agent does not have an Agent Control plugin configured",
        )

    if (
        str(agent.account_id) != str(current_user.account_id)
        and not request.start_new_session
        and request.target_session_id is None
    ):
        raise HTTPException(400, "Shared agents require a consumer-owned session")
    session_mode, target_session = _resolve_session_mode(
        db,
        account_id=str(current_user.account_id),
        agent=agent,
        request=request,
    )
    if (
        session_mode == "existing"
        and target_session is not None
        and not session_reachable_by_control(agent, target_session)
    ):
        # The plugin cannot type into this terminal; a command would resume
        # the same native session in a second process and fork it. The
        # operator sends a note instead (POST /operator-notes).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": TERMINAL_SESSION_REASON_CODE,
                "message": TERMINAL_SESSION_REASON,
            },
        )
    await _require_session_start_consent(
        db, agent=agent, request=request, current_user=current_user
    )
    try:
        dispatched = await dispatch_operator_message(
            db,
            managed_agent=agent,
            text=request.message,
            metadata=request.metadata,
            start_new_session=request.start_new_session,
            target_session_id=request.target_session_id,
            source=_command_source(request.metadata),
            input_mode=request.input_mode,
            interrupt=request.interrupt,
            spawn_worktree=request.spawn_worktree,
            voice=request.voice,
            session_mode=session_mode,
            session_identity=_existing_session_identity(target_session),
            created_by_user_id=current_user.id,
            require_delivery=True,
            consuming_account_id=current_user.account_id,
        )
    except AgentControlDispatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    envelope = dispatched.envelope
    local_delivery = dispatched.local_delivery
    subject = dispatched.subject
    command_status = dispatched.command_status
    expires_at = dispatched.expires_at
    command_ttl_seconds = dispatched.command_ttl_seconds

    if getattr(dispatched, "history_session_id", None) is not None:
        history_session = crud_runtime_session.get_account_session(
            db,
            account_id=str(current_user.account_id),
            runtime_session_id=str(dispatched.history_session_id),
        )
    else:
        history_session = _command_history_session(db, agent=agent, request=request)
    author_display = (
        getattr(current_user, "full_name", None)
        or getattr(current_user, "username", None)
        or getattr(current_user, "email", None)
    )
    if history_session is not None:
        crud_runtime_session_activity.log_agent_control_message(
            db,
            account_id=current_user.account_id,
            runtime_session_id=history_session.id,
            message=request.message,
            status="delivered" if local_delivery else "queued",
            metadata={
                "kind": "operator_command",
                # Not "author_*": the activity redactor masks any key that
                # contains "auth".
                "sent_by": author_display,
                "command_id": envelope.message_id,
                "managed_agent_id": str(agent.id),
                "agent_name": agent.display_name,
                "input_mode": request.input_mode,
                "session_mode": session_mode,
                "target_session_id": str(request.target_session_id)
                if request.target_session_id
                else None,
                "start_new_session": request.start_new_session,
                "source_metadata": request.metadata,
                "local_delivery": local_delivery,
                "published": subject is not None,
                "subject": subject,
            },
        )

        _emit_operator_command_activity(
            account_id=str(current_user.account_id),
            runtime_session_id=str(history_session.id),
            message=request.message,
            status="delivered" if local_delivery else "queued",
            command_id=envelope.message_id,
            author_display=author_display,
            agent=agent,
        )

    public_envelope = (
        _public_shared_command_envelope(envelope)
        if str(agent.account_id) != str(current_user.account_id)
        else envelope
    )
    emit_account_event(
        build_account_event(
            account_id=str(current_user.account_id),
            topic=ACCOUNT_TOPIC_AGENT_CONTROL,
            event_type="managed_agent_command_sent",
            payload=redact_dict(
                {
                    **public_envelope.model_dump(mode="json"),
                    "runtime_session_id": str(history_session.id)
                    if history_session
                    else None,
                }
            ),
            managed_agent_id=str(agent.id),
            runtime_session_id=str(history_session.id) if history_session else None,
        )
    )
    identity_session = target_session
    if identity_session is None and request.start_new_session:
        identity_session = history_session
    return AgentControlCommandResponse(
        command_id=envelope.message_id,
        managed_agent_id=agent.id,
        runtime_session_id=history_session.id
        if history_session and str(agent.account_id) != str(current_user.account_id)
        else envelope.runtime_session_id,
        target_session_id=(
            history_session.id
            if request.start_new_session and history_session is not None
            else request.target_session_id
        ),
        session_source_id=(
            identity_session.session_source_id if identity_session is not None else None
        ),
        session_reference=(
            identity_session.session_reference if identity_session is not None else None
        ),
        session_mode=session_mode,
        subject=subject,
        local_delivery=local_delivery,
        published=subject is not None,
        command_status=command_status,
        expires_at=expires_at,
        command_ttl_seconds=command_ttl_seconds,
        command_envelope=public_envelope,
    )


@router.post(
    "/agents/{agent_id}/control/commands",
    response_model=AgentControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("control_managed_agent")
async def send_managed_agent_command(
    agent_id: str,
    request: AgentControlSendMessageRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandResponse:
    """Backward-compatible route for sending text commands to an agent."""
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "control_managed_agent")
    )
    return await _route_managed_agent_prompt(
        agent_id=agent_id,
        request=request,
        current_user=current_user,
        db=db,
    )


@router.post(
    "/agents/{agent_id}/control/prompts",
    response_model=AgentControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("control_managed_agent")
async def send_managed_agent_prompt(
    agent_id: str,
    request: AgentControlSendMessageRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandResponse:
    """Route a text prompt to the current, existing, or next agent session."""
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "control_managed_agent")
    )
    return await _route_managed_agent_prompt(
        agent_id=agent_id,
        request=request,
        current_user=current_user,
        db=db,
    )


@router.get(
    "/agents/{agent_id}/control/commands/{command_id}",
    response_model=AgentControlCommandStatusResponse,
)
@require_permission("control_managed_agent")
def get_managed_agent_command_status(
    agent_id: str,
    command_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandStatusResponse:
    """Delivery state of one operator command, for clients that follow it.

    Returns delivery state for a command belonging to the requested agent
    in the current account. A command belonging to another agent returns
    404. FastAPI runs this sync handler in the threadpool, off the event loop.
    """
    try:
        agent_uuid = uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Command not found"
        ) from None
    record = crud_agent_control_command.get_for_consumer(
        db,
        account_id=current_user.account_id,
        managed_agent_id=agent_uuid,
        command_id=command_id.strip(),
    )
    if record is None or record.kind != "command":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Command not found"
        )
    delivery_state, result_status = command_delivery_state(record)
    return AgentControlCommandStatusResponse(
        command_id=record.command_id,
        managed_agent_id=record.managed_agent_id,
        status=record.status,
        delivery_state=delivery_state,
        terminal=delivery_state in TERMINAL_COMMAND_DELIVERY_STATES,
        result_status=result_status,
        last_error=record.last_error,
        created_at=getattr(record, "created_at", None),
        delivered_at=record.delivered_at,
        acked_at=record.acked_at,
        expires_at=record.expires_at,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/control",
    response_model=RuntimeSessionControlResponse,
)
@require_permission("view_runtime_sessions")
def get_runtime_session_control_mode(
    runtime_session_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RuntimeSessionControlResponse:
    """Say whether a line typed on this session can start a new agent turn.

    ``preloop sessions attach`` reads this once on attach and again when a
    command is refused. Sending still goes through the command endpoint and
    its ``control_managed_agent`` check; this only picks the input mode. A
    sync handler: FastAPI runs it in the threadpool, off the event loop.
    """
    try:
        uuid.UUID(runtime_session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime session not found",
        ) from None
    session = crud_runtime_session.get_account_session(
        db,
        account_id=str(current_user.account_id),
        runtime_session_id=runtime_session_id,
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime session not found",
        )
    decision = resolve_session_control_mode(
        db, account_id=str(current_user.account_id), session=session
    )
    agent = decision.agent
    return RuntimeSessionControlResponse(
        runtime_session_id=session.id,
        mode=decision.mode,
        reason_code=decision.reason_code,
        reason=decision.reason,
        delivery=decision.delivery,
        send_path=decision.send_path,
        managed_agent_id=agent.id if agent is not None else None,
        agent_name=agent.display_name if agent is not None else None,
        agent_kind=(agent.agent_kind or agent.session_source_type)
        if agent is not None
        else decision.kind,
    )


@router.post(
    "/agents/{agent_id}/control/voice-transcripts",
    response_model=AgentControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("control_managed_agent")
async def send_managed_agent_voice_transcript(
    agent_id: str,
    request: AgentControlVoiceTranscriptRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandResponse:
    """Mobile-friendly alias that routes a voice transcript as a prompt."""
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "control_managed_agent")
    )
    prompt_request = AgentControlSendMessageRequest(
        message=request.transcript,
        consent_request_id=request.consent_request_id,
        metadata=request.metadata,
        target_session_id=request.target_session_id,
        start_new_session=request.start_new_session,
        input_mode="voice_transcript",
        voice=request.voice,
    )
    return await _route_managed_agent_prompt(
        agent_id=agent_id,
        request=prompt_request,
        current_user=current_user,
        db=db,
    )


async def _route_session_action(
    *,
    agent_id: str,
    name: str,
    request: AgentControlSessionActionRequest,
    current_user: models.User,
    db: Session,
) -> AgentControlCommandResponse:
    """Persist and deliver request_takeover or release."""
    prompt = AgentControlSendMessageRequest(
        message=name,
        consent_request_id=request.consent_request_id,
        metadata={**request.metadata, "command": name},
        target_session_id=request.target_session_id,
        start_new_session=request.start_new_session,
        spawn_worktree=request.spawn_worktree,
    )
    agent = crud_managed_agent.get_for_account(
        db,
        account_id=str(current_user.account_id),
        agent_id=agent_id,
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Managed agent not found",
        )
    if agent.lifecycle_state != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed agent is not active",
        )
    if not _agent_has_control_config(
        db, account_id=str(current_user.account_id), agent=agent
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed agent does not have an Agent Control plugin configured",
        )
    session_mode, target_session = _resolve_session_mode(
        db,
        account_id=str(current_user.account_id),
        agent=agent,
        request=prompt,
    )
    await _require_session_start_consent(
        db, agent=agent, request=prompt, current_user=current_user
    )
    payload: dict[str, Any] = {
        "metadata": prompt.metadata,
        "session_mode": session_mode,
        "target_session_id": (
            str(request.target_session_id) if request.target_session_id else None
        ),
        "spawn_worktree": request.spawn_worktree,
    }
    payload.update(_existing_session_identity(target_session))
    envelope = _operator_command_envelope(agent, name=name, payload=payload)
    try:
        dispatched = await persist_and_deliver_command(
            db,
            agent=agent,
            envelope=envelope,
            source=_command_source(prompt.metadata),
            created_by_user_id=current_user.id,
            require_delivery=True,
        )
    except AgentControlDispatchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    command_status = dispatched.command_status
    subject = dispatched.subject
    local_delivery = dispatched.local_delivery
    expires_at = dispatched.expires_at
    command_ttl_seconds = dispatched.command_ttl_seconds
    emit_account_event(
        build_account_event(
            account_id=str(current_user.account_id),
            topic=ACCOUNT_TOPIC_AGENT_CONTROL,
            event_type=f"managed_agent_{name}",
            payload=envelope.model_dump(mode="json"),
            managed_agent_id=str(agent.id),
            runtime_session_id=str(agent.runtime_session_id)
            if agent.runtime_session_id
            else None,
        )
    )
    return AgentControlCommandResponse(
        command_id=envelope.message_id,
        managed_agent_id=agent.id,
        runtime_session_id=envelope.runtime_session_id,
        target_session_id=request.target_session_id,
        session_source_id=(
            target_session.session_source_id if target_session is not None else None
        ),
        session_reference=(
            target_session.session_reference if target_session is not None else None
        ),
        session_mode=session_mode,
        subject=subject,
        local_delivery=local_delivery,
        published=subject is not None,
        command_status=command_status,
        expires_at=expires_at,
        command_ttl_seconds=command_ttl_seconds,
        command_envelope=envelope,
    )


@router.post(
    "/agents/{agent_id}/control/takeover",
    response_model=AgentControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("control_managed_agent")
async def request_managed_agent_takeover(
    agent_id: str,
    request: AgentControlSessionActionRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandResponse:
    """Ask the sidecar to switch a local TUI session into remote SDK mode."""
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "control_managed_agent")
    )
    return await _route_session_action(
        agent_id=agent_id,
        name="request_takeover",
        request=request,
        current_user=current_user,
        db=db,
    )


@router.post(
    "/agents/{agent_id}/control/release",
    response_model=AgentControlCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("control_managed_agent")
async def release_managed_agent_session(
    agent_id: str,
    request: AgentControlSessionActionRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AgentControlCommandResponse:
    """Release a remote SDK session back to the local `preloop claude` TUI."""
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "control_managed_agent")
    )
    return await _route_session_action(
        agent_id=agent_id,
        name="release",
        request=request,
        current_user=current_user,
        db=db,
    )
