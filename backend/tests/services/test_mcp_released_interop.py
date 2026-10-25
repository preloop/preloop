"""Optional cross-major checks with an isolated MCP 1.30 / FastMCP 3.4 install.

Set MCP_LEGACY_PYTHON to that environment's Python; see mcp-protocol-compatibility.md.
No external servers or credentials are used.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

from preloop.services import dynamic_fastmcp_http
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_client_pool import MCPClient
from preloop.services.mcp_http import MCPPathRewriteMiddleware

LEGACY_PYTHON = os.environ.get("MCP_LEGACY_PYTHON")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not LEGACY_PYTHON, reason="Set MCP_LEGACY_PYTHON to the isolated SDK 1.x Python"
    ),
]

LEGACY_SERVER = """
import sys
from fastmcp import FastMCP
mcp = FastMCP("legacy-peer")
@mcp.tool
def echo(value: str) -> str:
    return value
mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[1]), show_banner=False)
"""
LEGACY_CLIENT = """
import asyncio, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main() -> None:
    async with streamablehttp_client(sys.argv[1]) as (read, write, _):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert result.protocolVersion == "2025-11-25"
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["echo"]
            result = await session.call_tool("echo", {"value": "old-client"})
            assert result.content[0].text == "old-client"
asyncio.run(main())
"""


@asynccontextmanager
async def serve(app: object) -> AsyncIterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)


async def test_sdk2_pool_against_released_legacy_server() -> None:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        LEGACY_PYTHON,
        "-c",
        LEGACY_SERVER,
        str(port),
        env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            while True:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
        client = MCPClient(f"http://127.0.0.1:{port}/mcp")
        await client.connect()
        assert client._protocol_version == "2025-11-25"
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
        result = await client.call_tool("echo", {"value": "new-client"})
        assert result[0].text == "new-client"
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)


@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
async def test_released_sdk1_client_against_preloop_transport(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    mcp = FastMCP("new-peer")
    monkeypatch.setattr(mcp, "set_user_context_provider", MagicMock(), raising=False)

    @mcp.tool
    def echo(value: str) -> str:
        return value

    context = UserContext(
        user_id="test-user", account_id="test-account", username="test"
    )
    monkeypatch.setattr(
        dynamic_fastmcp_http, "create_user_context_from_scope", lambda scope: context
    )
    app = dynamic_fastmcp_http.setup_dynamic_mcp_http(mcp)
    base = app.app.app.app
    outer = MCPPathRewriteMiddleware(
        Starlette(routes=[Mount("/mcp", app=app)], lifespan=base.lifespan)
    )
    async with serve(outer) as url:
        process = await asyncio.create_subprocess_exec(
            LEGACY_PYTHON,
            "-c",
            LEGACY_CLIENT,
            url + path,
            env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 20)
            assert process.returncode == 0, stderr.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
