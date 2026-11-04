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
    assert _is_permanent_control_connection_error(ConnectionEvictedError("superseded"))


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
            "name": "command_error",
            "message_id": "command-3",
            "payload": {
                "command_id": "command-3",
                "status": "failed",
                "error": "unsupported_capability",
                "unsupported": ["voice"],
                "session_mode": "current",
                "input_mode": "voice",
            },
        }
    ]


@pytest.mark.asyncio
async def test_runtime_failure_emits_correlated_terminal_error() -> None:
    async def fail(command: OperatorCommand) -> AgentControlResult:
        raise RuntimeError("native runtime failed")

    outbound = await dispatch_operator_command(
        OpenClawAgentControlAdapter.with_hooks(send_message=fail),
        {
            "type": "command",
            "name": "send_message",
            "message_id": "example-command",
            "payload": {"text": "hello"},
        },
    )
    assert outbound[0]["name"] == "command_error"
    assert outbound[0]["payload"]["command_id"] == "example-command"
    assert outbound[0]["payload"]["status"] == "failed"
    assert outbound[0]["payload"]["error"] == "native runtime failed"


@pytest.mark.asyncio
async def test_async_acceptance_does_not_emit_terminal_result() -> None:
    async def accept(command: OperatorCommand) -> AgentControlResult:
        return AgentControlResult(status="accepted")

    outbound = await dispatch_operator_command(
        OpenClawAgentControlAdapter.with_hooks(send_message=accept),
        {
            "type": "command",
            "name": "send_message",
            "message_id": "example-command",
            "payload": {"text": "hello"},
        },
    )
    assert outbound[0]["name"] == "command_ack"
    assert outbound[0]["payload"]["command_id"] == "example-command"
    assert not any(
        item["name"] in {"command_result", "command_error"} for item in outbound
    )


# --- desktop.install (#1566) -------------------------------------------------


def _install_envelope(**payload: Any) -> dict[str, Any]:
    return {
        "type": "command",
        "name": "desktop.install",
        "message_id": "req-1",
        "payload": {"request_id": "req-1", "managed_agent_id": "agent-1", **payload},
    }


@pytest.mark.asyncio
async def test_desktop_install_runs_the_cli_and_reports_ok() -> None:
    from preloop.integrations.agent_control.core import (
        HookedDesktopInstaller,
        run_desktop_enable,
    )

    calls: list[list[str]] = []

    async def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, "Enabled preloop-desktop-tunnel.service"

    async def send_message(command: OperatorCommand) -> AgentControlResult:
        raise AssertionError("not a message")

    adapter = OpenClawAgentControlAdapter.with_hooks(send_message=send_message)
    adapter.desktop_installer = HookedDesktopInstaller(
        lambda agent_id: run_desktop_enable(agent_id, run=run)
    )
    outbound = await dispatch_operator_command(adapter, _install_envelope())

    assert calls == [["preloop", "desktop", "enable", "--agent", "agent-1"]]
    assert outbound == [
        {
            "type": "status",
            "name": "command_result",
            "message_id": "req-1",
            "payload": {
                "command_id": "req-1",
                "status": "completed",
                "command": "desktop.install",
                "desktop_install": {"ok": True},
            },
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("Error: desktop_unsupported_distro", "desktop_unsupported_distro"),
        ("Error: desktop_privilege_required", "desktop_privilege_required"),
        ("Error: desktop_unsupported_os", "desktop_unsupported_os"),
        ("apt-get exploded", "desktop_failed"),
    ],
)
async def test_desktop_install_maps_cli_failures(output: str, error: str) -> None:
    from preloop.integrations.agent_control.core import run_desktop_enable

    async def run(argv: list[str]) -> tuple[int, str]:
        return 1, output

    assert await run_desktop_enable("agent-1", run=run) == {"ok": False, "error": error}


@pytest.mark.asyncio
async def test_desktop_install_without_an_installer_is_unsupported() -> None:
    async def send_message(command: OperatorCommand) -> AgentControlResult:
        raise AssertionError("not a message")

    adapter = OpenClawAgentControlAdapter.with_hooks(send_message=send_message)
    outbound = await dispatch_operator_command(adapter, _install_envelope())
    assert outbound[0]["name"] == "command_error"
    assert outbound[0]["payload"]["error"] == "unsupported agent-control command"


def test_hermes_client_answers_desktop_install(tmp_path: Path) -> None:
    from preloop.integrations.agent_control import create_hermes_agent_control_client

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "preloop": {
                    "control": {
                        "control_ws_url": "ws://localhost:8000/api/v1/agents/control/ws",
                        "bearer_token": "agt_local",
                        "runtime_principal_id": "hermes-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def send_message(command: OperatorCommand) -> AgentControlResult:
        raise AssertionError("not a message")

    client = create_hermes_agent_control_client(config_path, send_message=send_message)
    assert client.hooks.desktop_installer is not None
