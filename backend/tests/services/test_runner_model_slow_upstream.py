"""Slow HTTP upstream through the gateway transport and socket bridge.

Only NATS and the websocket are in-memory; the fake runner makes a real HTTP
request to a local server and obeys the start/next/cancel protocol.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from preloop.services import runner_model_proxy as proxy


@pytest.mark.asyncio
async def test_slow_http_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keepalives permit slow headers and chunks without becoming model output."""
    monkeypatch.setattr(proxy.settings, "runner_model_idle_seconds", 0.04)
    monkeypatch.setattr(proxy.settings, "runner_model_silent_seconds", 0.3)
    monkeypatch.setattr(proxy.settings, "runner_model_total_seconds", 1)

    async def upstream(request: web.Request) -> web.StreamResponse:
        await request.json()
        await asyncio.sleep(0.1)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for text in ("Hello", " world"):
            await asyncio.sleep(0.08)
            chunk = {"choices": [{"index": 0, "delta": {"content": text}}]}
            await response.write(("data: " + json.dumps(chunk) + "\n").encode())
        await response.write(b"data: [DONE]\n")
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base_url = f"http://127.0.0.1:{port}/v1"

    class Socket:
        def __init__(self) -> None:
            self.credit: asyncio.Queue[None] = asyncio.Queue()
            self.task: asyncio.Task[None] | None = None
            self.connection = proxy.RunnerModelConnection(self, "a", "r")
            monkeypatch.setattr(self.connection, "_authorized", lambda raw: True)

        async def send_json(self, raw: dict[str, Any]) -> None:
            if raw["operation"] == "cancel":
                if self.task is not None:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
            elif raw["operation"] == "start":
                self.task = asyncio.create_task(self.generate(raw))
            else:
                self.credit.put_nowait(None)

        async def generate(self, raw: dict[str, Any]) -> None:
            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(0.01)
                    self.connection.response(
                        {"request_id": raw["request_id"], "keepalive": True}
                    )

            heartbeat = asyncio.create_task(keepalive())
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        base_url + "/chat/completions", json=raw["body"]
                    ) as response:
                        async for line in response.content:
                            done = line.strip() == b"data: [DONE]"
                            self.connection.response(
                                {
                                    "request_id": raw["request_id"],
                                    "data": line.decode().strip(),
                                    "done": done,
                                }
                            )
                            if done:
                                return
                            await self.credit.get()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    class Broker:
        socket: Socket | None = None

        async def request(self, subject: str, data: bytes, **kwargs: Any) -> Any:
            if self.socket is None:
                self.socket = Socket()
            respond = AsyncMock()
            await self.socket.connection._request(
                SimpleNamespace(data=data, reply="inbox", respond=respond)
            )
            return SimpleNamespace(data=respond.call_args.args[0])

        async def publish(self, subject: str, data: bytes) -> None:
            if self.socket:
                await self.socket.connection._request(
                    SimpleNamespace(data=data, reply="", respond=AsyncMock())
                )

        async def flush(self, **kwargs: Any) -> None:
            pass

        async def close(self) -> None:
            if self.socket:
                await self.socket.connection.close()

    broker = Broker()
    monkeypatch.setattr(proxy.nats, "connect", AsyncMock(return_value=broker))
    model = SimpleNamespace(
        id="m", account_id="a", runner_id="r", api_endpoint=base_url
    )
    stream = proxy.runner_completion(
        model, {"model": "example", "stream": True}, stream=True
    )
    try:
        chunks = await asyncio.to_thread(list, stream)
        assert (
            "".join(chunk.choices[0].delta.content for chunk in chunks) == "Hello world"
        )
        assert broker.socket is not None and not broker.socket.connection.active
        assert stream.closed and not stream.thread.is_alive()
    finally:
        await asyncio.to_thread(stream.close)
        await runner.cleanup()
