"""Guard the era boundary before the SDK's header-based HTTP dispatcher."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import IO, Any, cast

import anyio
from fastmcp.server.http import StarletteWithLifespan, StreamableHTTPASGIApp
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Lifespan, Message, Receive, Scope, Send

from preloop.services.artifact_deposit import max_request_bytes

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_SPOOL_MEMORY_BYTES = 1024**2
_CHUNK_BYTES = 64 * 1024


def configure_mcp_request_limit(app: StarletteWithLifespan) -> None:
    """Align SDK 2.3's body limit with the existing artifact request contract.

    FastMCP 4.1.3 does not expose ``max_request_body_size`` in ``http_app`` and
    creates its SDK manager on each lifespan entry. Adjust that manager's
    public limit fields after creation, before requests can be accepted.
    """
    original_lifespan = app.lifespan

    @asynccontextmanager
    async def lifespan(starlette: Starlette) -> AsyncIterator[Mapping[str, Any] | None]:
        async with original_lifespan(starlette) as state:
            transports = [
                endpoint
                for route in app.routes
                if isinstance(
                    endpoint := getattr(route, "endpoint", None), StreamableHTTPASGIApp
                )
            ]
            if len(transports) != 1:
                raise RuntimeError("Expected one FastMCP Streamable HTTP transport")
            manager = transports[0].session_manager
            if manager is None or not isinstance(
                manager.asgi_app, RequestBodyLimitMiddleware
            ):
                raise RuntimeError("Unsupported MCP SDK request body limiter")
            limit = max_request_bytes()
            manager.max_request_body_size = limit
            manager.asgi_app.max_body_size = limit
            yield state

    app.router.lifespan_context = cast(Lifespan[Starlette], lifespan)


def _read_metadata(body: IO[bytes]) -> tuple[bool, str | None, str | int | None]:
    """Parse off the event loop, releasing the full tree before SDK dispatch."""
    try:
        payload = json.load(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False, None, None
    params = payload.get("params") if isinstance(payload, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or PROTOCOL_VERSION_META_KEY not in meta:
        return False, None, None
    version = meta[PROTOCOL_VERSION_META_KEY]
    request_id = payload.get("id")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        request_id = None
    return True, version if isinstance(version, str) else None, request_id


class MCPProtocolMiddleware:
    """Reject modern metadata routed as legacy before any tool can execute.

    SDK 2.3 selects its handler using only MCP-Protocol-Version. Its modern
    handler validates header/body agreement, but cannot see modern requests
    sent with a legacy or absent header. Keep that gap from becoming a bypass.
    All other protocol validation and dispatch remain with the SDK.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        versions = [
            v.decode("latin-1")
            for k, v in scope["headers"]
            if k.lower() == b"mcp-protocol-version"
        ]
        # The SDK's modern path already validates the body/header boundary.
        # Only requests that could reach its legacy handler need inspection.
        if len(versions) == 1 and versions[0] not in HANDSHAKE_PROTOCOL_VERSIONS:
            await self.app(scope, receive, send)
            return

        limit = max_request_bytes()
        async with anyio.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES) as body:
            size = 0
            while True:
                chunk = await receive()
                if chunk["type"] == "http.disconnect":
                    return
                data = chunk.get("body", b"")
                size += len(data)
                if size > limit:
                    await Response(status_code=413)(scope, receive, send)
                    return
                # Bound the spool's memory even when ASGI supplies a big chunk.
                for offset in range(0, len(data), _CHUNK_BYTES):
                    await body.write(memoryview(data)[offset : offset + _CHUNK_BYTES])
                more_body = chunk.get("more_body", False)
                del chunk, data
                if not more_body:
                    break

            await body.seek(0)
            has_version, version, request_id = await anyio.to_thread.run_sync(
                _read_metadata, body.wrapped
            )
            if has_version and (
                len(versions) != 1
                or versions[0] != version
                or version in HANDSHAKE_PROTOCOL_VERSIONS
            ):
                await JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32020,
                            "message": "MCP-Protocol-Version must match modern request metadata",
                        },
                    },
                    status_code=400,
                )(scope, receive, send)
                return

            await body.seek(0)
            remaining = size

            async def replay_body() -> Message:
                nonlocal remaining
                if remaining >= 0:
                    data = await body.read(_CHUNK_BYTES)
                    remaining -= len(data)
                    more = remaining > 0
                    if not more:
                        remaining = -1
                    return {
                        "type": "http.request",
                        "body": data,
                        "more_body": more,
                    }
                return await receive()

            await self.app(scope, replay_body, send)
