"""Gateway reconnect must not acknowledge an event before durable intake."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from preloop.integrations import discord_employee_bridge as bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disconnect", "timeout", "rejected"])
async def test_resume_uses_last_acknowledged_sequence(monkeypatch, tmp_path, failure):
    sent = []
    attempts = []

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            return False

    class Socket:
        async def receive_json(self):
            return {"d": {"heartbeat_interval": 60000}}

        async def send_json(self, data):
            sent.append(data)
            if data["op"] == 6:
                raise asyncio.CancelledError

        def __aiter__(self):
            async def frames():
                for packet in [
                    {"s": 41, "t": "READY", "d": {"session_id": "example-session"}},
                    {
                        "s": 42,
                        "t": "MESSAGE_CREATE",
                        "d": {
                            "id": "example-message",
                            "guild_id": "example-guild",
                            "channel_id": "example-channel",
                            "author": {"id": "example-user"},
                            "content": "help",
                        },
                    },
                ]:
                    yield SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT, data=json.dumps(packet)
                    )

            return frames()

    class Client(Context):
        def __init__(self, **kwargs):
            super().__init__(self)

        def ws_connect(self, *args, **kwargs):
            return Context(Socket())

        def post(self, *args, **kwargs):
            attempts.append(kwargs)
            if failure == "disconnect":
                raise aiohttp.ClientConnectionError("synthetic disconnect")
            if failure == "timeout":
                raise asyncio.TimeoutError
            return Context(SimpleNamespace(status=503))

    async def immediate_sleep(seconds):
        # Allow the heartbeat to be cancelled without starting a busy loop.
        if seconds == 60:
            await asyncio.Future()

    monkeypatch.setattr(bridge.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(bridge.asyncio, "sleep", immediate_sleep)
    state = tmp_path / "checkpoint.json"
    with pytest.raises(asyncio.CancelledError):
        await bridge.run_bridge(
            token="synthetic-token",
            endpoint="https://example.com/events",
            secret="synthetic-secret-at-least-32-characters",
            guild_id="example-guild",
            channel_ids=frozenset({"example-channel"}),
            state_path=state,
        )
    assert attempts
    assert sent[-1]["op"] == 6 and sent[-1]["d"]["seq"] == 41
    assert json.loads(state.read_text())["sequence"] == 41


@pytest.mark.parametrize(
    "name,data",
    [
        ("MESSAGE_CREATE", {"guild_id": "example", "author": None}),
        ("GUILD_MEMBER_ADD", {"guild_id": "example", "user": []}),
        ("MESSAGE_CREATE", []),
    ],
)
def test_malformed_provider_objects_are_ignored(name, data):
    assert (
        bridge.normalize_discord_event(
            name, data, guild_id="example", channel_ids=frozenset({"channel"})
        )
        is None
    )
