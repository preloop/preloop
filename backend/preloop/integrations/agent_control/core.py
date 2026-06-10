"""Reusable client primitives for the Preloop agent-control WebSocket.

OpenClaw and Hermes runtime code is not vendored in this repository. This module
is the runtime-side contract they can embed: read the CLI-written
``preloop.control`` block, connect to ``/api/v1/agents/control/ws``, advertise
capabilities, route ``send_message`` commands into runtime hooks, and emit
reply/status envelopes back to Preloop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

import aiohttp

logger = logging.getLogger(__name__)

InputMode = Literal["text", "voice", "voice_transcript"]
SessionMode = Literal["current", "existing", "new"]
OutboundEnvelope = dict[str, Any]
SleepHook = Callable[[float], Any]


@dataclass(frozen=True)
class AgentControlCapabilities:
    """Runtime features advertised to Preloop when the adapter connects."""

    supports_new_session: bool = True
    supports_existing_session: bool = True
    supports_text: bool = True
    supports_voice: bool = False
    supports_interrupt: bool = False

    def to_payload(self) -> dict[str, bool]:
        """Return the wire payload for capability advertisement."""

        return {
            "new_session": self.supports_new_session,
            "existing_session": self.supports_existing_session,
            "text": self.supports_text,
            "voice": self.supports_voice,
            "interrupt": self.supports_interrupt,
        }


@dataclass(frozen=True)
class AgentControlConfig:
    """Runtime-side view of the CLI-written ``preloop.control`` block."""

    control_ws_url: str
    bearer_token: str
    runtime_principal_id: str
    runtime_principal_name: str = ""
    protocol: str = "preloop.agent_control.v1"
    managed_agent_id: str | None = None
    credential_id: str | None = None
    runtime_session_id: str | None = None
    session_source_type: str | None = None
    session_source_id: str | None = None
    session_reference: str | None = None

    @classmethod
    def from_control_block(cls, block: dict[str, Any]) -> "AgentControlConfig":
        """Parse and validate a ``preloop.control`` mapping."""

        control_ws_url = _required_string(block, "control_ws_url")
        bearer_token = _required_string(block, "bearer_token")
        runtime_principal_id = _required_string(block, "runtime_principal_id")
        return cls(
            control_ws_url=control_ws_url,
            bearer_token=bearer_token,
            runtime_principal_id=runtime_principal_id,
            runtime_principal_name=str(block.get("runtime_principal_name") or ""),
            protocol=str(block.get("protocol") or "preloop.agent_control.v1"),
            managed_agent_id=_optional_string(block, "managed_agent_id"),
            credential_id=_optional_string(block, "credential_id"),
            runtime_session_id=_optional_string(block, "runtime_session_id"),
            session_source_type=_optional_string(block, "session_source_type"),
            session_source_id=_optional_string(block, "session_source_id"),
            session_reference=_optional_string(block, "session_reference"),
        )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "AgentControlConfig":
        """Extract ``preloop.control`` from a runtime config document."""

        preloop = document.get("preloop")
        if not isinstance(preloop, dict):
            raise ValueError("missing preloop config block")
        control = preloop.get("control")
        if not isinstance(control, dict):
            raise ValueError("missing preloop.control config block")
        return cls.from_control_block(control)


@dataclass(frozen=True)
class OperatorCommand:
    """A normalized operator command received from Preloop."""

    command_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    session_mode: SessionMode = "current"
    session_reference: str | None = None
    target_session_id: str | None = None
    input_mode: InputMode = "text"
    start_new_session: bool = False
    voice: dict[str, Any] = field(default_factory=dict)
    interrupt: bool = False

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> "OperatorCommand":
        """Normalize a backend ``send_message`` command envelope."""

        if envelope.get("type") != "command" or envelope.get("name") != "send_message":
            raise ValueError("unsupported agent-control command")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("send_message payload must be an object")
        metadata = (
            payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        )
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            raise ValueError("send_message command requires non-empty text")
        session_mode = _normalize_session_mode(
            payload.get("session_mode") or metadata.get("session_mode")
        )
        input_mode = _normalize_input_mode(
            payload.get("input_mode") or metadata.get("input_mode") or "text"
        )
        return cls(
            command_id=str(envelope.get("message_id") or uuid4()),
            text=text,
            metadata=dict(metadata),
            session_mode=session_mode,
            session_reference=_coerce_optional_string(
                payload.get("session_reference")
                or payload.get("target_session_id")
                or payload.get("session_id")
                or metadata.get("session_reference")
                or metadata.get("session_id")
            ),
            target_session_id=_coerce_optional_string(payload.get("target_session_id")),
            input_mode=input_mode,
            start_new_session=bool(payload.get("start_new_session")),
            voice=dict(payload.get("voice") or {})
            if isinstance(payload.get("voice"), dict)
            else {},
            interrupt=bool(payload.get("interrupt") or metadata.get("interrupt")),
        )


@dataclass(frozen=True)
class AgentControlResult:
    """Runtime result returned after handling one operator command."""

    reply_text: str = ""
    status: Literal["completed", "accepted", "failed"] = "completed"
    session_reference: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentControlRuntimeHooks(Protocol):
    """Runtime hooks OpenClaw/Hermes adapters must implement."""

    def capabilities(self) -> AgentControlCapabilities:
        """Return runtime feature support for capability advertisement."""

    async def handle_send_message(self, command: OperatorCommand) -> AgentControlResult:
        """Attach to an existing session or start a new one and send text."""


def _is_permanent_control_connection_error(exc: BaseException) -> bool:
    """Return True when reconnecting would not recover from this failure."""
    status: int | None = None
    if isinstance(exc, aiohttp.ClientResponseError):
        status = exc.status
    elif isinstance(exc, aiohttp.WSServerHandshakeError):
        status = exc.status
    return status in {401, 403}


class AgentControlClient:
    """Maintain the runtime-side WebSocket and dispatch commands to hooks."""

    def __init__(
        self,
        config: AgentControlConfig,
        hooks: AgentControlRuntimeHooks,
        *,
        reconnect_delay_seconds: float = 2.0,
        heartbeat_seconds: float = 30.0,
        session_factory: Callable[[dict[str, str]], Any] | None = None,
        sleep: SleepHook = asyncio.sleep,
    ) -> None:
        self.config = config
        self.hooks = hooks
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._session_factory = session_factory or _default_session_factory
        self._sleep = sleep
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """Request the connection loop to stop after the current iteration."""

        self._stopped.set()

    async def run_forever(self, *, max_attempts: int | None = None) -> None:
        """Connect forever until ``stop`` is called."""

        attempts = 0
        while not self._stopped.is_set():
            if max_attempts is not None and attempts >= max_attempts:
                return
            attempts += 1
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _is_permanent_control_connection_error(exc):
                    logger.warning(
                        "Agent control connection failed with non-retryable error: %s",
                        exc,
                    )
                    raise
                # Transient disconnects are retried on the next loop iteration.
                logger.debug(
                    "Agent control connection dropped; retrying",
                    exc_info=True,
                )
            if not self._stopped.is_set():
                await _maybe_await(self._sleep(self.reconnect_delay_seconds))

    async def run_until_disconnect(self) -> None:
        """Open one WebSocket connection and return when it closes."""

        await self._connect_once()

    async def _connect_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.bearer_token}"}
        async with self._session_factory(headers) as session:
            async with session.ws_connect(self.config.control_ws_url) as websocket:
                await self._send_json(websocket, self.capability_envelope())
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))
                try:
                    async for message in websocket:
                        if self._stopped.is_set():
                            break
                        envelope = _parse_inbound_message(message)
                        if envelope is not None:
                            await self._handle_text_message(websocket, envelope)
                            continue
                        message_type = getattr(message, "type", None)
                        if message_type == aiohttp.WSMsgType.TEXT:
                            await self._send_json(
                                websocket,
                                _error_envelope(
                                    str(uuid4()),
                                    "invalid_json",
                                    {"error": "malformed JSON message"},
                                ),
                            )
                            continue
                        if message_type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        }:
                            break
                finally:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _heartbeat_loop(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        while not self._stopped.is_set():
            await _maybe_await(self._sleep(self.heartbeat_seconds))
            await self._send_json(
                websocket,
                {
                    "type": "heartbeat",
                    "name": "heartbeat",
                    "message_id": str(uuid4()),
                    "payload": {"observed_at": _now_iso()},
                },
            )

    async def _handle_text_message(
        self, websocket: aiohttp.ClientWebSocketResponse, envelope: dict[str, Any]
    ) -> None:
        if envelope.get("type") == "ping":
            await self._send_json(websocket, {"type": "heartbeat", "payload": {}})
            return
        for outbound in await dispatch_operator_command(self.hooks, envelope):
            await self._send_json(websocket, outbound)

    async def _send_json(
        self, websocket: aiohttp.ClientWebSocketResponse, envelope: OutboundEnvelope
    ) -> None:
        await websocket.send_json(envelope)

    def capability_envelope(self) -> OutboundEnvelope:
        """Build the initial capability advertisement envelope."""

        return {
            "type": "presence",
            "name": "capabilities",
            "message_id": str(uuid4()),
            "payload": {
                "status": "online",
                "protocol": self.config.protocol,
                "capabilities": self.hooks.capabilities().to_payload(),
                "runtime_principal_id": self.config.runtime_principal_id,
                "runtime_principal_name": self.config.runtime_principal_name,
                "managed_agent_id": self.config.managed_agent_id,
                "runtime_session_id": self.config.runtime_session_id,
                "session_source_type": self.config.session_source_type,
                "session_source_id": self.config.session_source_id,
            },
        }


async def dispatch_operator_command(
    hooks: AgentControlRuntimeHooks, envelope: dict[str, Any]
) -> list[OutboundEnvelope]:
    """Route one backend envelope to runtime hooks and build outbound events."""

    if envelope.get("type") != "command":
        return []

    try:
        command = OperatorCommand.from_envelope(envelope)
        unsupported = _unsupported_capabilities(hooks.capabilities(), command)
        if unsupported:
            return [
                _error_envelope(
                    command.command_id,
                    "unsupported_capability",
                    {
                        "unsupported": unsupported,
                        "session_mode": command.session_mode,
                        "input_mode": command.input_mode,
                    },
                )
            ]
        result = await hooks.handle_send_message(command)
    except Exception as exc:
        return [
            _error_envelope(
                str(envelope.get("message_id") or uuid4()),
                "command_failed",
                {"error": str(exc)},
            )
        ]

    outbound: list[OutboundEnvelope] = [
        {
            "type": "status",
            "name": "command_status",
            "message_id": command.command_id,
            "payload": {
                "status": result.status,
                "command": "send_message",
                "session_reference": result.session_reference
                or command.session_reference,
                "metadata": result.metadata,
            },
        }
    ]
    if result.reply_text:
        outbound.append(
            {
                "type": "event",
                "name": "agent_reply",
                "message_id": command.command_id,
                "payload": {
                    "text": result.reply_text,
                    "session_reference": result.session_reference
                    or command.session_reference,
                    "metadata": result.metadata,
                },
            }
        )
    return outbound


def _unsupported_capabilities(
    capabilities: AgentControlCapabilities, command: OperatorCommand
) -> list[str]:
    unsupported: list[str] = []
    if (
        command.input_mode in {"voice", "voice_transcript"}
        and not capabilities.supports_voice
    ):
        unsupported.append("voice")
    if command.input_mode == "text" and not capabilities.supports_text:
        unsupported.append("text")
    if command.session_mode == "new" and not capabilities.supports_new_session:
        unsupported.append("new_session")
    if (
        command.session_mode == "existing"
        and not capabilities.supports_existing_session
    ):
        unsupported.append("existing_session")
    if command.interrupt and not capabilities.supports_interrupt:
        unsupported.append("interrupt")
    return unsupported


def _parse_inbound_message(message: Any) -> dict[str, Any] | None:
    """Parse a WebSocket inbound frame into an envelope dict when possible."""

    if isinstance(message, dict):
        return message
    message_type = getattr(message, "type", None)
    if message_type != aiohttp.WSMsgType.TEXT:
        return None
    try:
        parsed = message.json()
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed agent-control JSON message", exc_info=True)
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "Ignoring non-object agent-control JSON message: %s",
            type(parsed).__name__,
        )
        return None
    return parsed


def _error_envelope(
    message_id: str, name: str, payload: dict[str, Any]
) -> OutboundEnvelope:
    return {
        "type": "status",
        "name": name,
        "message_id": message_id,
        "payload": {"status": "error", **payload},
    }


def _required_string(value: dict[str, Any], key: str) -> str:
    resolved = _optional_string(value, key)
    if resolved is None:
        raise ValueError(f"missing required preloop.control field {key!r}")
    return resolved


def _optional_string(value: dict[str, Any], key: str) -> str | None:
    return _coerce_optional_string(value.get(key))


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    resolved = str(value).strip()
    return resolved or None


def _normalize_session_mode(value: Any) -> SessionMode:
    mode = str(value or "current").strip().lower()
    if mode in {"existing", "continue", "attach"}:
        return "existing"
    if mode in {"new", "start"}:
        return "new"
    return "current"


def _normalize_input_mode(value: Any) -> InputMode:
    mode = str(value or "text").strip().lower()
    if mode in {"voice", "voice_transcript"}:
        if mode == "voice_transcript":
            return "voice_transcript"
        return "voice"
    return "text"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_session_factory(headers: dict[str, str]) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers=headers)


async def _maybe_await(result: Any) -> Any:
    if asyncio.iscoroutine(result):
        return await result
    return result
