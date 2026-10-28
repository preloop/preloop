"""Real SDK sessions against deterministic modern and legacy HTTP peers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
import pytest
from mcp import MCPError, types

from preloop.services import mcp_client_pool
from preloop.services.mcp_client_pool import (
    MCPClient,
    MCPToolOutcomeUnknownError,
    MCPAuthenticationError,
)

MODERN = "2026-07-28"
LEGACY = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


@dataclass
class Peer:
    version: str = MODERN
    probe_status: int | None = None
    probe_error: int | None = None
    supported: list[str] = field(default_factory=lambda: [MODERN])
    call_status: int = 200
    call_error: int | None = None
    calls: list[tuple[str, dict, dict]] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            self.calls.append((request.method, {}, dict(request.headers)))
            return httpx.Response(405 if request.method == "GET" else 200)
        body = json.loads(request.content)
        method = body["method"]
        self.calls.append((method, body, dict(request.headers)))
        if method == "server/discover":
            status = self.probe_status or (200 if self.version == MODERN else 400)
            if status != 200 or self.probe_error is not None:
                if self.probe_error is None:
                    return httpx.Response(status, text="Initialization required")
                return httpx.Response(
                    status,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": self.probe_error,
                            "message": "Probe rejected",
                            "data": {"requested": MODERN, "supported": self.supported},
                        },
                    },
                )
            result = types.DiscoverResult(
                supported_versions=[MODERN],
                capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
            ).model_dump(by_alias=True, exclude_none=True)
        elif method == "initialize":
            result = {
                "protocolVersion": self.version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "legacy-peer", "version": "1"},
            }
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "synthetic-session"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            )
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "tools/list":
            result = types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="echo",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "region": {"type": "string", "x-mcp-header": "Region"}
                            },
                        },
                    )
                ]
            ).model_dump(by_alias=True, exclude_none=True)
        elif method == "tools/call":
            if self.call_status != 200:
                return httpx.Response(
                    self.call_status,
                    headers={"Location": "http://example.com/redirect"},
                )
            if self.call_error is not None:
                return httpx.Response(
                    400,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": self.call_error,
                            "message": "Header mismatch",
                        },
                    },
                )
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text="done")],
                structured_content={"done": True},
            ).model_dump(by_alias=True, exclude_none=True)
        else:
            raise AssertionError(method)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    @property
    def methods(self) -> list[str]:
        return [method for method, _, _ in self.calls]


def install_peer(monkeypatch: pytest.MonkeyPatch, peer: Peer) -> MCPClient:
    def create_client(
        *,
        headers: dict,
        timeout: httpx.Timeout,
        auth: httpx.Auth | None,
        response_policy: Any,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(peer.respond),
            headers=headers,
            timeout=timeout,
            auth=auth,
            event_hooks={"response": [response_policy.on_response]},
        )

    monkeypatch.setattr(
        mcp_client_pool, "create_streamable_mcp_http_client", create_client
    )
    return MCPClient(
        "http://example.com/mcp",
        auth_type="bearer",
        auth_config={"token": "synthetic-token"},
    )


@pytest.mark.asyncio
async def test_modern_peer_uses_discovery_and_per_request_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer()
    client = install_peer(monkeypatch, peer)
    await client.connect()
    tools = await client.list_tools()
    assert tools[0].input_schema["properties"]["region"]["x-mcp-header"] == "Region"
    result = await client.call_tool("echo", {"region": "north"})
    assert result[0].text == "done"
    assert result.structured_content == {"done": True}
    assert peer.methods.count("tools/call") == 1
    assert not {
        "initialize",
        "GET",
        "DELETE",
        "notifications/initialized",
    }.intersection(peer.methods)
    for method, body, request_headers in peer.calls:
        assert request_headers["authorization"] == "Bearer synthetic-token"
        assert request_headers["mcp-protocol-version"] == MODERN
        assert request_headers["mcp-method"] == method
        assert (
            body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN
        )
        if method == "tools/call":
            assert request_headers["mcp-name"] == "echo"
            assert request_headers["mcp-param-region"] == "north"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", LEGACY)
@pytest.mark.parametrize("probe_status,probe_error", [(400, None), (200, -32601)])
async def test_legacy_fallback_initialization_and_session_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    probe_status: int,
    probe_error: int | None,
) -> None:
    peer = Peer(version=version, probe_status=probe_status, probe_error=probe_error)
    client = install_peer(monkeypatch, peer)
    await client.connect()
    await client.list_tools()
    await client.call_tool("echo", {})
    assert peer.methods.count("server/discover") == 1
    assert peer.methods.count("initialize") == 3
    assert peer.methods.count("DELETE") == 3
    assert peer.methods.count("tools/call") == 1
    for method, _, request_headers in peer.calls:
        if method in {"tools/list", "tools/call"}:
            assert request_headers["mcp-session-id"] == "synthetic-session"
            assert request_headers["mcp-protocol-version"] == version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (400, -32020),
        (400, -32021),
        (400, -32022),
        (400, -32601),
        (404, -32601),
        (200, -32020),
        (200, -32021),
        (200, -32022),
    ],
)
async def test_recognized_modern_errors_never_downgrade(
    monkeypatch: pytest.MonkeyPatch, status: int, code: int
) -> None:
    peer = Peer(probe_status=status, probe_error=code, supported=["2099-01-01"])
    client = install_peer(monkeypatch, peer)
    with pytest.raises(MCPError) as raised:
        await client.connect()
    assert raised.value.code == code
    assert peer.methods == ["server/discover"]


@pytest.mark.asyncio
async def test_mutual_modern_version_retry_is_only_a_discovery_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer(probe_status=400, probe_error=-32022)
    client = install_peer(monkeypatch, peer)
    with pytest.raises(MCPError):
        await client.connect()
    assert peer.methods == ["server/discover", "server/discover"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 405, 429, 500, 502])
async def test_http_failures_never_become_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    peer = Peer(probe_status=status)
    client = install_peer(monkeypatch, peer)
    with pytest.raises(
        MCPAuthenticationError
        if status in {401, 403}
        else (httpx.HTTPStatusError, MCPError)
    ):
        await client.connect()
    assert peer.methods == ["server/discover"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code", [(307, None), (401, None), (403, None), (500, None), (200, -32020)]
)
async def test_dispatched_tool_is_never_replayed(
    monkeypatch: pytest.MonkeyPatch, status: int, code: int | None
) -> None:
    peer = Peer(call_status=status, call_error=code)
    client = install_peer(monkeypatch, peer)
    await client.connect()
    if status in {401, 403}:
        # An auth rejection is reported as such so the user can reconnect.
        with pytest.raises(PermissionError, match=f"HTTP {status}") as raised:
            await client.call_tool("echo", {})
        assert not isinstance(raised.value, MCPToolOutcomeUnknownError)
        assert raised.value.status_code == status
        assert f"HTTP {status}" in mcp_client_pool.describe_mcp_connection_error(
            raised.value
        )
    else:
        with pytest.raises(MCPToolOutcomeUnknownError, match="may have executed"):
            await client.call_tool("echo", {})
    assert peer.methods.count("tools/call") == 1
    assert "initialize" not in peer.methods


@pytest.mark.asyncio
async def test_malformed_error_body_is_not_modern_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer(version="2025-11-25", probe_status=400)
    respond = peer.respond

    def malformed(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        if request.headers.get("mcp-method") == "server/discover":
            return httpx.Response(400, json={"error": {"code": {"invalid": True}}})
        return response

    monkeypatch.setattr(peer, "respond", malformed)
    client = install_peer(monkeypatch, peer)
    await client.connect()
    assert peer.methods[:2] == ["server/discover", "initialize"]


@pytest.mark.asyncio
async def test_endpoint_change_reprobes_cached_legacy_era(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer(version="2025-11-25")
    client = install_peer(monkeypatch, peer)
    await client.connect()
    client.url = "http://example.com/modern-mcp"
    peer.version = MODERN
    await client.connect()
    assert client._protocol_version == MODERN
    assert peer.methods.count("server/discover") == 2


@pytest.mark.asyncio
async def test_modern_response_header_prevents_method_not_found_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = Peer(probe_status=200, probe_error=-32601)
    respond = peer.respond

    def modern_error(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        response.headers["Mcp-Protocol-Version"] = MODERN
        return response

    monkeypatch.setattr(peer, "respond", modern_error)
    client = install_peer(monkeypatch, peer)
    with pytest.raises(MCPError):
        await client.connect()
    assert peer.methods == ["server/discover"]
