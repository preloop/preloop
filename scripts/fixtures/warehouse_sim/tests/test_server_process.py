"""Start the server as a process and talk to it with the official MCP client."""

import os
import socket
import subprocess
import sys
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .conftest import REPO_ROOT

EXPECTED_TOOLS = {
    "get_transcript",
    "list_workflows",
    "propose_workflow_change",
    "create_task",
    "get_audio",
    "transcribe_audio",
}
MODULE = "scripts.fixtures.warehouse_sim"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env():
    return {**os.environ, "PRELOOP_DISABLE_TELEMETRY": "1"}


@pytest.fixture
def http_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", MODULE, "--http", f"127.0.0.1:{port}"],
        cwd=REPO_ROOT,
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited: {proc.stderr.read().decode()[-2000:]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start listening")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _exercise(session):
    await session.initialize()
    tools = await session.list_tools()
    assert {t.name for t in tools.tools} == EXPECTED_TOOLS

    tr = await session.call_tool("get_transcript", {"site": "nord", "shift": "late"})
    assert not tr.isError
    blocks = [b.model_dump(mode="json", by_alias=True) for b in tr.content]
    assert [b["type"] for b in blocks] == ["text", "resource"]
    assert blocks[1]["resource"]["uri"] == "warehouse-sim://transcripts/nord/late.vtt"
    assert blocks[1]["resource"]["mimeType"] == "text/vtt"
    assert blocks[1]["resource"]["text"].startswith("WEBVTT")

    au = await session.call_tool("get_audio", {"site": "nord", "shift": "late"})
    (audio,) = [b.model_dump(mode="json", by_alias=True) for b in au.content]
    assert audio["type"] == "audio" and audio["mimeType"] == "audio/wav"

    tx = await session.call_tool("transcribe_audio", {"audio_ref": audio["data"]})
    assert [b.model_dump(mode="json", by_alias=True) for b in tx.content] == blocks

    bad = await session.call_tool("get_transcript", {"site": "west", "shift": "late"})
    assert bad.isError


@pytest.mark.asyncio
async def test_http_server_lists_six_tools_and_serves_content(http_server):
    async with streamablehttp_client(http_server) as (read, write, _):
        async with ClientSession(read, write) as session:
            await _exercise(session)


@pytest.mark.asyncio
async def test_stdio_server_lists_six_tools_and_serves_content():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", MODULE], cwd=str(REPO_ROOT), env=_env()
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await _exercise(session)
