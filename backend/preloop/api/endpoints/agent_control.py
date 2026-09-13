"""Managed-agent control-plane WebSocket and operator command endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
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
from preloop.utils.permissions import require_permission
from preloop.api.auth.jwt import (
    RuntimeBearerAuthContext,
    authenticate_runtime_bearer_token,
)
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_agent_control_command,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.crud import agent_control_connection as control_connection
from preloop.models.crud.agent_control_connection import AgentControlConnectionContext
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_control import (
    AgentControlCommandResponse,
    AgentControlEnvelope,
    AgentControlEnvelopeType,
    AgentControlInboundEnvelope,
    AgentControlSendMessageRequest,
    AgentControlSessionActionRequest,
    AgentControlSessionMode,
    AgentControlVoiceTranscriptRequest,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AGENT_CONTROL,
    build_account_event,
    emit_account_event,
)
from preloop.sync.services.event_bus import get_nats_client

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
SUPPORTED_CONTROL_AGENT_KINDS = {"hermes", "openclaw", "claude_code", "opencode"}


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

    # A CLI-written config without validation means the install is intentional
    # but the runtime plugin has not been proven available yet. Keep command
    # routing closed until the adapter reports the validation flags above.
    return False


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
        return {
            "online": online,
            "supports_interrupt": bool(capabilities.get("interrupt")),
            "session_mode": session_mode,
            "capabilities": capabilities,
            "queued_count": queued_count,
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
        self._presence[managed_agent_id] = payload or {}

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
    for key in ("source", "surface", "via", "device"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:32]
    return "api"


def _operator_command_envelope(
    agent: Any,
    *,
    name: str,
    payload: dict[str, Any],
) -> AgentControlEnvelope:
    if agent.runtime_session_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed agent is not online",
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


def _touch_presence(
    db: Session,
    context: AgentControlConnectionContext,
    *,
    observed_at: datetime,
    session_mode: Optional[str] = None,
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
        return dict(record.envelope)

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


_COMMAND_ACK_NAMES = {"ack", "command_ack", "command_result", "command_error"}


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


def _sanitize_agent_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets and truncate bulky agent output before emit/persist."""
    from preloop.utils.redaction import redact_dict

    safe = redact_dict(payload)
    if not isinstance(safe, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in safe.items():
        if key in _AGENT_CONTROL_TRUNCATE_KEYS and isinstance(value, str):
            if len(value) > _MAX_AGENT_CONTROL_PAYLOAD_CHARS:
                sanitized[key] = (
                    value[:_MAX_AGENT_CONTROL_PAYLOAD_CHARS] + "...[truncated]"
                )
            else:
                sanitized[key] = value
        elif key == "result" and not isinstance(
            value, (str, int, float, bool, type(None))
        ):
            # Drop bulky structured results from realtime/audit surfaces.
            sanitized[key] = {
                "_omitted": "structured_result",
                "type": type(value).__name__,
            }
        else:
            sanitized[key] = value
    return sanitized


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
    if command is None or command.kind != "command" or command.status != "acked":
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

    crud_runtime_session_activity.log_agent_control_result(
        db,
        account_id=context.account_id,
        command_id=command_id.strip(),
        fallback_runtime_session_id=context.runtime_session_id,
        status=result_status,
        message=message,
        metadata=_sanitize_agent_control_payload(inbound.payload),
        commit=commit,
    )


async def _emit_agent_message(
    connection: AgentControlConnectionContext,
    inbound: AgentControlInboundEnvelope,
) -> None:
    if inbound.type == "heartbeat":
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
    _touch_presence(
        db, connection, observed_at=observed_at, session_mode=inbound_mode, commit=False
    )
    _mark_control_verified_from_capabilities(db, connection, inbound, commit=False)
    _handle_command_ack(db, connection, inbound, commit=False)
    _persist_agent_control_result(db, connection, inbound, commit=False)
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
    last_presence_at = now
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
    if request.target_session_id is not None:
        return crud_runtime_session.get_account_session(
            db,
            account_id=str(agent.account_id),
            runtime_session_id=str(request.target_session_id),
        )
    if not request.start_new_session:
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


async def _route_managed_agent_prompt(
    *,
    agent_id: str,
    request: AgentControlSendMessageRequest,
    current_user: models.User,
    db: Session,
) -> AgentControlCommandResponse:
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
        request=request,
    )
    payload: dict[str, Any] = {
        "text": request.message,
        "metadata": request.metadata,
        "input_mode": request.input_mode,
        "session_mode": session_mode,
        "target_session_id": str(request.target_session_id)
        if request.target_session_id
        else None,
        "start_new_session": request.start_new_session,
        "voice": request.voice,
        "spawn_worktree": request.spawn_worktree,
        "interrupt": request.interrupt,
    }
    payload.update(_existing_session_identity(target_session))
    envelope = _operator_command_envelope(
        agent,
        name="send_message",
        payload=payload,
    )
    # Persist the command durably BEFORE any delivery attempt so agents that
    # reconnect after downtime can recover missed instructions.
    now = datetime.now(UTC)
    command_ttl_seconds = int(settings.agent_control_command_ttl_seconds)
    expires_at = now + timedelta(seconds=command_ttl_seconds)
    crud_agent_control_command.create_command(
        db,
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=agent.runtime_session_id,
        command_id=envelope.message_id,
        envelope=envelope.model_dump(mode="json"),
        source=_command_source(request.metadata),
        created_by_user_id=current_user.id,
        expires_at=expires_at,
    )

    # create_command refreshes its row after committing. End that read
    # transaction before the guarded sender checks out its own connection.
    # Snapshot before rollback; reading an expired agent here would reacquire it.
    delivery_agent_id = str(agent.id)
    await run_db_off_loop(partial(control_connection.release_read_transaction, db))
    command_status = "pending"
    local_delivery = await agent_control_manager.send_to_agent(
        managed_agent_id=delivery_agent_id,
        envelope=envelope,
    )
    if local_delivery:
        command_status = "delivered"
    subject = None
    if not local_delivery:
        # NATS publish is best-effort fan-out to other pods; the pod holding
        # the WebSocket marks the command delivered when it actually sends,
        # so the row stays pending here.
        subject = await _publish_command(envelope)
        if subject is None:
            # Savepoint so a failed mark_failed cannot roll back the already
            # committed create_command (or other outer session work).
            try:
                with db.begin_nested():
                    crud_agent_control_command.mark_failed(
                        db,
                        account_id=str(agent.account_id),
                        managed_agent_id=str(agent.id),
                        command_id=envelope.message_id,
                        error="Managed agent command channel is unavailable",
                        commit=False,
                    )
                db.commit()
            except _DB_DELIVERY_ERRORS:
                logger.exception(
                    "Failed to mark Agent Control command %s failed",
                    envelope.message_id,
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Managed agent command channel is unavailable",
            )

    history_session = _command_history_session(db, agent=agent, request=request)
    if history_session is not None:
        crud_runtime_session_activity.log_agent_control_message(
            db,
            account_id=current_user.account_id,
            runtime_session_id=history_session.id,
            message=request.message,
            status="delivered" if local_delivery else "queued",
            metadata={
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

    emit_account_event(
        build_account_event(
            account_id=str(current_user.account_id),
            topic=ACCOUNT_TOPIC_AGENT_CONTROL,
            event_type="managed_agent_command_sent",
            payload=envelope.model_dump(mode="json"),
            managed_agent_id=str(agent.id),
            runtime_session_id=str(agent.runtime_session_id)
            if agent.runtime_session_id
            else None,
        )
    )
    identity_session = target_session
    if identity_session is None and request.start_new_session:
        identity_session = history_session
    return AgentControlCommandResponse(
        command_id=envelope.message_id,
        managed_agent_id=agent.id,
        runtime_session_id=envelope.runtime_session_id,
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
        command_envelope=envelope,
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
    return await _route_managed_agent_prompt(
        agent_id=agent_id,
        request=request,
        current_user=current_user,
        db=db,
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
    prompt_request = AgentControlSendMessageRequest(
        message=request.transcript,
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
    now = datetime.now(UTC)
    command_ttl_seconds = int(settings.agent_control_command_ttl_seconds)
    expires_at = now + timedelta(seconds=command_ttl_seconds)
    crud_agent_control_command.create_command(
        db,
        account_id=agent.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=agent.runtime_session_id,
        command_id=envelope.message_id,
        envelope=envelope.model_dump(mode="json"),
        source=_command_source(prompt.metadata),
        created_by_user_id=current_user.id,
        expires_at=expires_at,
    )
    # create_command refreshes its row after committing. End that read
    # transaction before the guarded sender checks out its own connection.
    # Snapshot before rollback; reading an expired agent here would reacquire it.
    delivery_agent_id = str(agent.id)
    await run_db_off_loop(partial(control_connection.release_read_transaction, db))
    command_status = "pending"
    local_delivery = await agent_control_manager.send_to_agent(
        managed_agent_id=delivery_agent_id,
        envelope=envelope,
    )
    if local_delivery:
        command_status = "delivered"
    subject = None
    if not local_delivery:
        subject = await _publish_command(envelope)
        if subject is None:
            try:
                with db.begin_nested():
                    crud_agent_control_command.mark_failed(
                        db,
                        account_id=str(agent.account_id),
                        managed_agent_id=str(agent.id),
                        command_id=envelope.message_id,
                        error="Managed agent command channel is unavailable",
                        commit=False,
                    )
                db.commit()
            except _DB_DELIVERY_ERRORS:
                logger.exception(
                    "Failed to mark Agent Control command %s failed",
                    envelope.message_id,
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Managed agent command channel is unavailable",
            )
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
    return await _route_session_action(
        agent_id=agent_id,
        name="release",
        request=request,
        current_user=current_user,
        db=db,
    )
