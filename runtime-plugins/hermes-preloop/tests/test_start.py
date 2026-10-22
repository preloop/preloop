"""Exercise the standalone entrypoint wiring and actual presence envelope."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from preloop.integrations.agent_control import (
    AgentControlCapabilities,
    AgentControlClient,
    AgentControlResult,
)  # noqa: E402
from preloop_hermes_plugin.plugin import HermesPreloopPlugin  # noqa: E402


@pytest.mark.asyncio
async def test_start_wires_native_commands_and_preserves_manifest(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    config.write_text(
        "preloop:\n  control:\n    runtime: hermes\n    control_ws_url: wss://example.com/api/v1/agents/control/ws\n    bearer_token: example-token\n    runtime_principal_id: example-principal\n"
    )
    desktop = tmp_path / "desktop.json"
    desktop.write_text(json.dumps({"vnc": {"host": "127.0.0.1"}, "display": ":99"}))
    monkeypatch.setenv("PRELOOP_DESKTOP_FILE", str(desktop))
    native = SimpleNamespace(
        capabilities=lambda: AgentControlCapabilities(
            supports_existing_session=False, supports_tool_approval=True
        ),
        send_command=AsyncMock(
            return_value=AgentControlResult(
                reply_text="example reply", session_reference="example-session"
            )
        ),
    )
    runtime_module = importlib.import_module("preloop_hermes_plugin.runtime")
    monkeypatch.setattr(runtime_module, "NativeHermesRuntime", lambda plugin: native)
    sent = []

    async def send_json(envelope):
        sent.append(envelope)

    async def run(client):
        sent.append(client.capability_envelope())
        for mode in ("new", "existing"):
            await client._handle_text_message(
                SimpleNamespace(send_json=send_json),
                {
                    "type": "command",
                    "name": "send_message",
                    "message_id": f"example-{mode}",
                    "payload": {"text": "hello", "session_mode": mode},
                },
            )

    monkeypatch.setattr(AgentControlClient, "run_forever", run)
    plugin = HermesPreloopPlugin(config)
    await plugin.start()
    caps = sent[0]["payload"]["capabilities"]
    assert caps["new_session"] and caps["text"]
    assert not caps["existing_session"] and not caps["interrupt"] and not caps["voice"]
    assert caps["desktop"] == "vnc" and caps["desktop_display"] == ":99"
    native.send_command.assert_awaited_once()
    assert native.send_command.call_args.args[0].session_mode == "new"
    assert [e["name"] for e in sent[1:] if e["name"] != "heartbeat"] == [
        "command_ack",
        "command_result",
        "agent_reply",
        "command_ack",
        "command_error",
    ]
    assert any(e["name"] == "heartbeat" for e in sent[1:])
    error = next(e for e in sent if e["name"] == "command_error")
    assert error["payload"]["command_id"] == "example-existing"
