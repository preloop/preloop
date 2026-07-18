"""Unit tests for runtime Agent Control adapter client behavior."""

import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from preloop.integrations.agent_control import (
    AgentControlClient,
    AgentControlConfig,
    AgentControlResult,
    HookedAgentControlAdapter,
    OperatorCommand,
    load_openclaw_control_config,
)

pytestmark = pytest.mark.asyncio


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._messages = list(messages or [])

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def __aenter__(self) -> "FakeWebSocket":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def __aiter__(self) -> "FakeWebSocket":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeSession:
    def __init__(self, websocket: FakeWebSocket, headers: dict[str, str]) -> None:
        self.websocket = websocket
        self.headers = headers
        self.connect_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def ws_connect(
        self,
        url: str,
    ) -> FakeWebSocket:
        self.connect_calls.append({"url": url, "headers": self.headers})
        return self.websocket


class InvalidJsonTextMessage:
    type = aiohttp.WSMsgType.TEXT

    def json(self) -> dict[str, Any]:
        raise json.JSONDecodeError("Expecting value", "{not-json", 0)


class FailingSession:
    def __init__(self, _headers: dict[str, str]) -> None:
        pass

    async def __aenter__(self) -> "FailingSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def ws_connect(self, *_args: object, **_kwargs: object) -> FakeWebSocket:
        raise RuntimeError("network down")


async def test_load_openclaw_control_config_reads_runtime_websocket_contract(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "preloop": {
                    "control": {
                        "control_ws_url": "wss://preloop.example/control/ws",
                        "bearer_token": "durable-runtime-token",
                        "runtime_principal_id": "openclaw-live",
                        "managed_agent_id": "agent-1",
                        "runtime_session_id": "session-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_openclaw_control_config(config_path)

    assert config.control_ws_url == "wss://preloop.example/control/ws"
    assert config.bearer_token == "durable-runtime-token"
    assert config.runtime_principal_id == "openclaw-live"
    assert config.managed_agent_id == "agent-1"
    assert config.runtime_session_id == "session-1"


async def test_client_connects_with_bearer_and_advertises_capabilities() -> None:
    websocket = FakeWebSocket()
    sessions: list[FakeSession] = []

    def session_factory(headers: dict[str, str]) -> FakeSession:
        session = FakeSession(websocket, headers)
        sessions.append(session)
        return session

    adapter = HookedAgentControlAdapter.with_hooks(send_message=lambda _: "ok")
    client = AgentControlClient(
        AgentControlConfig(
            control_ws_url="wss://preloop.example/api/v1/agents/control/ws",
            bearer_token="runtime-token",
            runtime_principal_id="openclaw-live",
        ),
        adapter,
        session_factory=session_factory,
    )

    await client.run_until_disconnect()

    assert sessions[0].connect_calls == [
        {
            "url": "wss://preloop.example/api/v1/agents/control/ws",
            "headers": {"Authorization": "Bearer runtime-token"},
        }
    ]
    assert websocket.sent[0]["type"] == "presence"
    assert websocket.sent[0]["name"] == "capabilities"
    assert websocket.sent[0]["payload"]["protocol"] == "preloop.agent_control.v1"
    assert websocket.sent[0]["payload"]["capabilities"] == {
        "new_session": True,
        "existing_session": True,
        "text": True,
        "voice": False,
        "interrupt": False,
        "tool_approval": False,
    }


async def test_client_dispatches_send_message_and_reports_result() -> None:
    handled: list[OperatorCommand] = []

    async def send_message(command: OperatorCommand) -> AgentControlResult:
        handled.append(command)
        return AgentControlResult(
            reply_text="Done",
            session_reference="session-42",
            metadata={"runtime": "hermes"},
        )

    websocket = FakeWebSocket(
        [
            {
                "type": "command",
                "name": "send_message",
                "message_id": "cmd-1",
                "payload": {
                    "text": "Inspect the failing test",
                    "metadata": {"source": "mobile"},
                    "input_mode": "voice_transcript",
                    "session_mode": "existing",
                    "target_session_id": "session-42",
                    "start_new_session": False,
                    "voice": {"locale": "en-US"},
                },
            }
        ]
    )
    adapter = HookedAgentControlAdapter.with_hooks(
        send_message=send_message,
        supports_voice=True,
    )
    client = AgentControlClient(
        AgentControlConfig(
            control_ws_url="wss://preloop.example/api/v1/agents/control/ws",
            bearer_token="runtime-token",
            runtime_principal_id="hermes-live",
        ),
        adapter,
        session_factory=lambda headers: FakeSession(websocket, headers),
    )

    await client.run_until_disconnect()

    assert handled == [
        OperatorCommand(
            command_id="cmd-1",
            text="Inspect the failing test",
            metadata={"source": "mobile"},
            input_mode="voice_transcript",
            session_mode="existing",
            target_session_id="session-42",
            start_new_session=False,
            voice={"locale": "en-US"},
            session_reference="session-42",
        )
    ]
    statuses = [
        sent
        for sent in websocket.sent
        if sent["type"] == "status" and sent["name"] == "command_status"
    ]
    assert [status["payload"]["status"] for status in statuses] == [
        "completed",
    ]
    replies = [sent for sent in websocket.sent if sent["name"] == "agent_reply"]
    assert replies[0]["payload"]["text"] == "Done"
    assert replies[0]["payload"]["session_reference"] == "session-42"


async def test_client_survives_malformed_json_without_disconnecting() -> None:
    handled: list[str] = []

    async def send_message(command: OperatorCommand) -> AgentControlResult:
        handled.append(command.command_id)
        return AgentControlResult(reply_text="Recovered")

    websocket = FakeWebSocket(
        [
            InvalidJsonTextMessage(),
            {
                "type": "command",
                "name": "send_message",
                "message_id": "cmd-after-bad-json",
                "payload": {"text": "continue"},
            },
        ]
    )
    client = AgentControlClient(
        AgentControlConfig(
            control_ws_url="wss://preloop.example/api/v1/agents/control/ws",
            bearer_token="runtime-token",
            runtime_principal_id="openclaw-live",
        ),
        HookedAgentControlAdapter.with_hooks(send_message=send_message),
        session_factory=lambda headers: FakeSession(websocket, headers),
    )

    await client.run_until_disconnect()

    invalid_json = [
        sent for sent in websocket.sent if sent.get("name") == "invalid_json"
    ]
    assert len(invalid_json) == 1
    assert invalid_json[0]["payload"]["status"] == "error"
    assert handled == ["cmd-after-bad-json"]


async def test_client_reconnects_after_connection_failure() -> None:
    websocket = FakeWebSocket()
    sessions = [
        FailingSession,
        lambda headers: FakeSession(websocket, headers),
    ]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    client = AgentControlClient(
        AgentControlConfig(
            control_ws_url="wss://preloop.example/api/v1/agents/control/ws",
            bearer_token="runtime-token",
            runtime_principal_id="openclaw-live",
        ),
        HookedAgentControlAdapter.with_hooks(send_message=lambda _: "ok"),
        reconnect_delay_seconds=0.5,
        session_factory=lambda headers: sessions.pop(0)(headers),
        sleep=sleep,
    )

    await client.run_forever(max_attempts=2)

    assert sleeps == [0.5, 0.5]
    assert websocket.sent[0]["name"] == "capabilities"
