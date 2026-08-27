from __future__ import annotations

import json
from pathlib import Path

import pytest

import aiohttp

from preloop.integrations.agent_control import (
    AgentControlCapabilities,
    AgentControlConfig,
    AgentControlResult,
    OpenClawAgentControlAdapter,
    OperatorCommand,
    dispatch_operator_command,
    load_openclaw_control_config,
)
from preloop.integrations.agent_control.core import (
    ConnectionEvictedError,
    _is_permanent_control_connection_error,
    _parse_inbound_message,
)


def test_parse_inbound_message_accepts_dict_and_rejects_invalid_json() -> None:
    assert _parse_inbound_message({"type": "ping"}) == {"type": "ping"}

    class InvalidJsonTextMessage:
        type = aiohttp.WSMsgType.TEXT

        def json(self) -> dict[str, object]:
            raise json.JSONDecodeError("Expecting value", "{bad", 0)

    assert _parse_inbound_message(InvalidJsonTextMessage()) is None


def test_permanent_control_connection_error_detects_auth_failures() -> None:
    assert _is_permanent_control_connection_error(
        aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=401,
            message="Unauthorized",
        )
    )
    assert _is_permanent_control_connection_error(
        aiohttp.WSServerHandshakeError(
            request_info=None,
            history=(),
            status=403,
            message="Forbidden",
        )
    )
    assert not _is_permanent_control_connection_error(RuntimeError("temporary"))


def test_connection_evicted_error_is_permanent() -> None:
    assert _is_permanent_control_connection_error(
        ConnectionEvictedError("superseded")
    )


def test_control_config_reads_cli_document() -> None:
    config = AgentControlConfig.from_document(
        {
            "preloop": {
                "control": {
                    "control_ws_url": "wss://preloop.example/api/v1/agents/control/ws",
                    "bearer_token": "agt_secret",
                    "runtime_principal_id": "octavia-123",
                    "managed_agent_id": "agent-123",
                    "credential_id": "cred-123",
                    "runtime_session_id": "session-123",
                    "session_source_type": "openclaw",
                    "session_source_id": "octavia-session",
                }
            }
        }
    )

    assert config.control_ws_url == "wss://preloop.example/api/v1/agents/control/ws"
    assert config.bearer_token == "agt_secret"
    assert config.runtime_principal_id == "octavia-123"
    assert config.managed_agent_id == "agent-123"


def test_openclaw_control_config_loader(tmp_path: Path) -> None:
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "preloop": {
                    "control": {
                        "control_ws_url": "ws://localhost:8000/api/v1/agents/control/ws",
                        "bearer_token": "agt_local",
                        "runtime_principal_id": "local-agent",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_openclaw_control_config(config_path)

    assert config.control_ws_url == "ws://localhost:8000/api/v1/agents/control/ws"
    assert config.bearer_token == "agt_local"


def test_operator_command_normalizes_session_routing() -> None:
    command = OperatorCommand.from_envelope(
        {
            "type": "command",
            "name": "send_message",
            "message_id": "command-1",
            "payload": {
                "text": "continue the refactor",
                "session_mode": "attach",
                "session_id": "session-a",
                "metadata": {"input_mode": "voice", "interrupt": True},
            },
        }
    )

    assert command.command_id == "command-1"
    assert command.text == "continue the refactor"
    assert command.session_mode == "existing"
    assert command.session_reference == "session-a"
    assert command.input_mode == "voice"
    assert command.interrupt is True


@pytest.mark.asyncio
async def test_dispatch_send_message_to_existing_session() -> None:
    seen: list[OperatorCommand] = []

    async def send_message(command: OperatorCommand) -> AgentControlResult:
        seen.append(command)
        return AgentControlResult(
            reply_text="done",
            session_reference=command.session_reference,
            metadata={"runtime": "openclaw"},
        )

    adapter = OpenClawAgentControlAdapter.with_hooks(send_message=send_message)

    outbound = await dispatch_operator_command(
        adapter,
        {
            "type": "command",
            "name": "send_message",
            "message_id": "command-2",
            "payload": {
                "text": "what is your status?",
                "session_mode": "existing",
                "session_reference": "openclaw-session",
            },
        },
    )

    assert seen[0].session_mode == "existing"
    assert seen[0].session_reference == "openclaw-session"
    assert outbound[0]["type"] == "status"
    assert outbound[0]["payload"]["status"] == "completed"
    assert outbound[1]["name"] == "agent_reply"
    assert outbound[1]["payload"]["text"] == "done"


@pytest.mark.asyncio
async def test_dispatch_ignores_backend_presence() -> None:
    async def send_message(command: OperatorCommand) -> AgentControlResult:
        raise AssertionError("presence envelopes should not reach runtime hooks")

    adapter = OpenClawAgentControlAdapter.with_hooks(send_message=send_message)

    outbound = await dispatch_operator_command(
        adapter,
        {
            "type": "presence",
            "name": "connected",
            "message_id": "presence-1",
            "payload": {"status": "online"},
        },
    )

    assert outbound == []


@pytest.mark.asyncio
async def test_dispatch_rejects_unsupported_voice() -> None:
    async def send_message(command: OperatorCommand) -> AgentControlResult:
        raise AssertionError("unsupported commands should not reach runtime hooks")

    class TextOnlyAdapter:
        def capabilities(self) -> AgentControlCapabilities:
            return AgentControlCapabilities(supports_voice=False)

        async def handle_send_message(
            self, command: OperatorCommand
        ) -> AgentControlResult:
            return await send_message(command)

    outbound = await dispatch_operator_command(
        TextOnlyAdapter(),
        {
            "type": "command",
            "name": "send_message",
            "message_id": "command-3",
            "payload": {
                "text": "voice-originated prompt",
                "input_mode": "voice",
            },
        },
    )

    assert outbound == [
        {
            "type": "status",
            "name": "unsupported_capability",
            "message_id": "command-3",
            "payload": {
                "status": "error",
                "unsupported": ["voice"],
                "session_mode": "current",
                "input_mode": "voice",
            },
        }
    ]
