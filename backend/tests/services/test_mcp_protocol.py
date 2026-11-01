"""Wire-level interoperability against the released FastMCP/SDK transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from unittest.mock import ANY, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

from preloop.services import dynamic_fastmcp_http
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_http import MCPPathRewriteMiddleware

MODERN = "2026-07-28"
LEGACY = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"


def message(method: str, *, version: str = MODERN, **params: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {
                VERSION_KEY: version,
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            **params,
        },
    }


def headers(method: str, *, version: str = MODERN, name: str | None = None) -> dict:
    result = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name is not None:
        result["Mcp-Name"] = name
    return result


def rpc(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        return next(
            item for item in reversed(data) if "result" in item or "error" in item
        )
    return response.json()


@pytest_asyncio.fixture
async def wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    server = FastMCP("protocol-fixture")
    monkeypatch.setattr(server, "set_user_context_provider", MagicMock(), raising=False)

    @server.tool
    def echo(value: str) -> str:
        return value

    context = UserContext(
        user_id="test-user",
        account_id="test-account",
        username="test",
        has_tracker=False,
        enabled_default_tools=[],
        enabled_proxied_tools=[],
    )
    monkeypatch.setattr(
        dynamic_fastmcp_http, "create_user_context_from_scope", lambda scope: context
    )
    app = dynamic_fastmcp_http.setup_dynamic_mcp_http(server)
    base = app.app.app.app
    outer = MCPPathRewriteMiddleware(Starlette(routes=[Mount("/mcp", app=app)]))
    started, stopped = asyncio.Event(), asyncio.Event()

    async def serve_lifespan() -> None:
        async with base.lifespan(base):
            started.set()
            await stopped.wait()

    task = asyncio.create_task(serve_lifespan())
    await asyncio.wait_for(started.wait(), 10)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=outer), base_url="http://localhost"
        ) as client:
            yield client
    finally:
        stopped.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
@pytest.mark.parametrize("header_version", [None, "2025-11-25", "2025-03-26"])
async def test_modern_metadata_cannot_enter_legacy_handler(
    wire: httpx.AsyncClient, path: str, header_version: str | None
) -> None:
    request_headers = headers("tools/list")
    if header_version is None:
        request_headers.pop("MCP-Protocol-Version")
    else:
        request_headers["MCP-Protocol-Version"] = header_version
    response = await wire.post(
        path, headers=request_headers, json=message("tools/list")
    )
    assert response.status_code == 400
    assert rpc(response)["error"]["code"] == -32020


@pytest.mark.asyncio
@pytest.mark.parametrize("header_version", [None, "2025-11-25"])
async def test_large_body_with_trailing_modern_metadata_cannot_dispatch(
    wire: httpx.AsyncClient, header_version: str | None
) -> None:
    """Spooling must inspect metadata even after a large artifact argument."""
    body = message("tools/call", name="echo", arguments={"value": "x" * (4 * 1024**2)})
    meta = body["params"].pop("_meta")
    body["params"]["_meta"] = meta
    request_headers = headers("tools/call", name="echo")
    if header_version is None:
        request_headers.pop("MCP-Protocol-Version")
    else:
        request_headers["MCP-Protocol-Version"] = header_version
    response = await wire.post("/mcp", headers=request_headers, json=body)
    assert response.status_code == 400
    assert rpc(response)["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_chunked_legacy_request_obeys_artifact_request_ceiling(
    wire: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import mcp_protocol

    monkeypatch.setattr(mcp_protocol, "max_request_bytes", lambda: 1024)

    async def chunks() -> AsyncIterator[bytes]:
        yield b" " * 512
        yield b" " * 513

    response = await wire.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        content=chunks(),
    )
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
async def test_modern_discovery_list_and_call_without_handshake(
    wire: httpx.AsyncClient, path: str
) -> None:
    response = await wire.post(
        path, headers=headers("server/discover"), json=message("server/discover")
    )
    assert response.status_code == 200
    assert MODERN in rpc(response)["result"]["supportedVersions"]
    assert "mcp-session-id" not in response.headers
    response = await wire.post(
        path, headers=headers("tools/list"), json=message("tools/list")
    )
    assert [tool["name"] for tool in rpc(response)["result"]["tools"]] == ["echo"]
    response = await wire.post(
        path,
        headers=headers("tools/call", name="echo"),
        json=message("tools/call", name="echo", arguments={"value": "hello"}),
    )
    assert rpc(response)["result"]["content"][0]["text"] == "hello"
    assert "location" not in response.headers
    assert "mcp-session-id" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("version", LEGACY)
@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
async def test_legacy_initialize_and_call(
    wire: httpx.AsyncClient, path: str, version: str
) -> None:
    base_headers = {"Accept": "application/json, text/event-stream"}
    response = await wire.post(
        path,
        headers=base_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "legacy-fixture", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    assert rpc(response)["result"]["protocolVersion"] == version
    assert (
        "mcp-session-id" not in response.headers
    )  # Preloop's existing stateless configuration.
    base_headers["MCP-Protocol-Version"] = version
    response = await wire.post(
        path,
        headers=base_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202
    response = await wire.post(
        path,
        headers=base_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert [tool["name"] for tool in rpc(response)["result"]["tools"]] == ["echo"]
    response = await wire.post(
        path,
        headers=base_headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": version}},
        },
    )
    assert rpc(response)["result"]["content"][0]["text"] == version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,name,changed_header,value",
    [
        ("tools/list", None, "Mcp-Method", None),
        ("tools/list", None, "Mcp-Method", "tools/call"),
        ("tools/call", "echo", "Mcp-Name", None),
        ("tools/call", "echo", "Mcp-Name", "other"),
        ("tools/list", None, "MCP-Protocol-Version", "2099-01-01"),
    ],
)
async def test_modern_header_mismatches(
    wire: httpx.AsyncClient,
    method: str,
    name: str | None,
    changed_header: str,
    value: str | None,
) -> None:
    request_headers = headers(method, name=name)
    if value is None:
        request_headers.pop(changed_header)
    else:
        request_headers[changed_header] = value
    params = {"name": name, "arguments": {"value": "ignored"}} if name else {}
    response = await wire.post(
        "/mcp", headers=request_headers, json=message(method, **params)
    )
    assert response.status_code == 400
    assert rpc(response)["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_unsupported_version_and_unknown_method(wire: httpx.AsyncClient) -> None:
    response = await wire.post(
        "/mcp",
        headers=headers("server/discover", version="2099-01-01"),
        json=message("server/discover", version="2099-01-01"),
    )
    assert response.status_code == 400
    error = rpc(response)["error"]
    assert error["code"] == -32022
    assert error["data"]["requested"] == "2099-01-01"
    assert MODERN in error["data"]["supported"]
    response = await wire.post(
        "/mcp", headers=headers("unknown/method"), json=message("unknown/method")
    )
    assert response.status_code == 404
    assert rpc(response)["error"]["code"] == -32601


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_modern_has_no_session_endpoint(
    wire: httpx.AsyncClient, method: str
) -> None:
    response = await wire.request(method, "/mcp", headers=headers("tools/list"))
    assert response.status_code == 405
    response = await wire.post(
        "/mcp/v2", headers=headers("tools/list"), json=message("tools/list")
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_modern_ignores_session_and_resumption_headers(
    wire: httpx.AsyncClient,
) -> None:
    request_headers = headers("tools/list") | {
        "Mcp-Session-Id": "old-session",
        "Last-Event-ID": "old-event",
    }
    response = await wire.post(
        "/mcp", headers=request_headers, json=message("tools/list")
    )
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers


@pytest.mark.asyncio
async def test_unauthenticated_discovery_challenges_oauth(
    wire: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dynamic_fastmcp_http, "create_user_context_from_scope", lambda scope: None
    )
    response = await wire.post(
        "/mcp", headers=headers("server/discover"), json=message("server/discover")
    )
    assert response.status_code == 401
    assert (
        '/.well-known/oauth-protected-resource/mcp"'
        in response.headers["www-authenticate"]
    )


@pytest.mark.asyncio
async def test_untrusted_origin_rejected(wire: httpx.AsyncClient) -> None:
    response = await wire.post(
        "/mcp",
        headers=headers("tools/list") | {"Origin": "https://untrusted.example.com"},
        json=message("tools/list"),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [MODERN, "2025-11-25"])
@pytest.mark.parametrize(
    "decision,approved",
    [("allow", True), ("deny", True), ("allow", False), ("require_approval", False)],
)
async def test_wire_calls_preserve_policy_and_approval_gates(
    monkeypatch: pytest.MonkeyPatch, version: str, decision: str, approved: bool
) -> None:
    from unittest.mock import AsyncMock
    from uuid import UUID
    from mcp.types import TextContent
    from tests.services.test_proxied_tool_error_audit import _setup

    context = UserContext(
        user_id="00000000-0000-4000-8000-000000000001",
        account_id="00000000-0000-4000-8000-000000000002",
        username="test",
    )
    server, upstream, _, _ = _setup(
        monkeypatch,
        context,
        upstream=[TextContent(type="text", text="executed")],
        approve=approved,
    )
    policy = AsyncMock(
        return_value=(
            decision,
            UUID("00000000-0000-4000-8000-000000000003")
            if decision == "require_approval"
            else None,
            "test policy",
        )
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.evaluate_policy_async", policy
    )
    monkeypatch.setattr(
        dynamic_fastmcp_http, "create_user_context_from_scope", lambda scope: context
    )
    app = dynamic_fastmcp_http.setup_dynamic_mcp_http(server)
    base = app.app.app.app
    async with base.lifespan(base):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            body = message("tools/call", name="safe_tool", arguments={"ok": "yes"})
            if version != MODERN:
                body["params"].pop("_meta")
            response = await client.post(
                "/v1",
                headers=headers("tools/call", name="safe_tool", version=version),
                json=body,
            )
    result = rpc(response)["result"]
    if decision == "deny" or not approved:
        assert result["isError"] is True
        upstream.call_tool.assert_not_awaited()
    else:
        assert result.get("isError", False) is False
        upstream.call_tool.assert_awaited_once_with(
            "safe_tool", {"ok": "yes"}, correlation_id=ANY
        )
    policy.assert_awaited_once()


@pytest.mark.asyncio
async def test_modern_sse_progress_and_disconnect_cancels_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp import Context

    server = FastMCP("streaming-fixture")
    monkeypatch.setattr(server, "set_user_context_provider", MagicMock(), raising=False)
    cancelled = asyncio.Event()
    progress = asyncio.Event()

    async def wait_for_approval(ctx: Context) -> str:
        try:
            await ctx.report_progress(
                progress=0, total=1, message="Waiting for approval"
            )
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "unreachable"

    # Explicitly resolve the local Context type for the tool schema builder.
    wait_for_approval.__annotations__["ctx"] = Context
    server.tool()(wait_for_approval)
    context = UserContext(
        user_id="test-user", account_id="test-account", username="test"
    )
    monkeypatch.setattr(
        dynamic_fastmcp_http, "create_user_context_from_scope", lambda scope: context
    )
    app = dynamic_fastmcp_http.setup_dynamic_mcp_http(server)
    base = app.app.app.app
    incoming: asyncio.Queue = asyncio.Queue()
    request = message("tools/call", name="wait_for_approval", arguments={})
    request["params"]["_meta"]["progressToken"] = "synthetic-progress"
    await incoming.put(
        {
            "type": "http.request",
            "body": json.dumps(request).encode(),
            "more_body": False,
        }
    )
    output: list[dict] = []

    async def send(event: dict) -> None:
        output.append(event)
        if b"notifications/progress" in event.get("body", b""):
            progress.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1",
        "raw_path": b"/v1",
        "root_path": "",
        "query_string": b"",
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (
                headers("tools/call", name="wait_for_approval")
                | {"Host": "localhost", "Content-Type": "application/json"}
            ).items()
        ],
    }
    async with base.lifespan(base):
        task = asyncio.create_task(app(scope, incoming.get, send))
        try:
            await asyncio.wait_for(progress.wait(), 5)
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(cancelled.wait(), 5)
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    start = next(event for event in output if event["type"] == "http.response.start")
    assert start["status"] == 200
    assert b"text/event-stream" in dict(start["headers"])[b"content-type"]
    assert b"Waiting for approval" in b"".join(
        event.get("body", b"") for event in output
    )


@pytest.mark.asyncio
async def test_lowlevel_adapter_cannot_authenticate_from_client_metadata() -> None:
    from mcp import Client, types
    from preloop.services.dynamic_mcp_server import DynamicMCPServer

    server = DynamicMCPServer()
    async with Client(server.server, mode="legacy") as client:
        result = await client.session.list_tools(
            params=types.PaginatedRequestParams(
                _meta={"user_id": "forged", "account_id": "forged", "has_tracker": True}
            )
        )
    assert result.tools == []
