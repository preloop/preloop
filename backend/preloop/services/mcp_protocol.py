"""Guard the era boundary before the SDK's header-based HTTP dispatcher."""

from __future__ import annotations

import json

from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
MAX_MCP_BODY_BYTES = 4 * 1024 * 1024


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

        body = bytearray()
        while True:
            chunk = await receive()
            if chunk["type"] == "http.disconnect":
                return
            body.extend(chunk.get("body", b""))
            if len(body) > MAX_MCP_BODY_BYTES:
                await Response(status_code=413)(scope, receive, send)
                return
            if not chunk.get("more_body", False):
                break

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        params = payload.get("params") if isinstance(payload, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        versions = [
            v.decode("latin-1")
            for k, v in scope["headers"]
            if k.lower() == b"mcp-protocol-version"
        ]
        if isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta:
            version = meta[PROTOCOL_VERSION_META_KEY]
            if (
                len(versions) != 1
                or versions[0] != version
                or version in HANDSHAKE_PROTOCOL_VERSIONS
            ):
                request_id = payload.get("id")
                if not isinstance(request_id, (str, int)) or isinstance(
                    request_id, bool
                ):
                    request_id = None
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

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)
