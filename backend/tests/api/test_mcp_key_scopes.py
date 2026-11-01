"""API-key capabilities survive MCP authentication and dispatch."""

import json
from tests.cross_account_keys import legacy_cross_account_runtime_key
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.tools import Tool
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import mcp_scope_allowed
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    create_user_context_from_scope,
)
from preloop.services.dynamic_fastmcp_http import setup_dynamic_mcp_http
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.mcp_http import (
    MCPPathRewriteMiddleware,
    PreloopBearerAuthBackend,
    _MCPRootPathHandler,
)
from preloop.services.oauth_provider import PreloopOAuthProvider


@pytest.mark.parametrize(
    "scopes,allowed",
    [
        (None, True),
        ([], True),
        (["*"], True),
        ([{"device_token": "synthetic-device-reference"}], True),
        (["mcp:read"], True),
        (["mcp:write"], False),
        (["mcp:*"], False),
        (["other"], False),
        ("mcp:read", False),
        ([None], False),
        ([{}], False),
    ],
)
def test_mcp_grants_are_exact_and_preserve_legacy_key_metadata(
    scopes: Any,
    allowed: bool,
) -> None:
    assert mcp_scope_allowed(scopes, "mcp:read") is allowed


def _context(scopes: list[str]) -> UserContext:
    context = UserContext(
        user_id=str(uuid4()), account_id=str(uuid4()), username="jane"
    )
    context.api_key_scopes = tuple(scopes)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["mcp:write"], ["mcp:unknown"], ["other"]])
async def test_discovery_requires_read_even_with_cached_tools(
    scopes: list[str],
) -> None:
    context = _context(scopes)
    context.mcp_tools_cache = [
        Tool(
            name="example",
            description="",
            parameters={"type": "object", "properties": {}},
        )
    ]
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: context)
    assert await server.list_tools() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["mcp:read"], ["mcp:unknown"], ["other"]])
@pytest.mark.parametrize("run_middleware", [True, False])
async def test_invocation_requires_write_before_dispatch(
    scopes: list[str], run_middleware: bool
) -> None:
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: _context(scopes))
    with (
        patch.object(server, "_record_attributed_refusal"),
        patch.object(server, "_audit_refused_tool_call"),
        patch.object(server, "_persist_tool_call_activity"),
        patch("fastmcp.FastMCP.call_tool", new_callable=AsyncMock) as dispatch,
        patch("preloop.services.dynamic_fastmcp.get_db") as database,
    ):
        result = await server.call_tool("example", {}, run_middleware=run_middleware)
    assert result.is_error
    assert "mcp:write" in result.content[0].text
    dispatch.assert_not_awaited()
    database.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["mcp:read"], ["mcp:read", "mcp:write"], [], ["*"]])
async def test_read_scope_lists_permitted_tools(scopes: list[str]) -> None:
    context = _context(scopes)
    context.mcp_tools_cache = [
        Tool(
            name="example",
            description="",
            parameters={"type": "object", "properties": {}},
        )
    ]
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: context)
    assert [tool.name for tool in await server.list_tools()] == ["example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,allowed", [("mcp:read", True), ("mcp:write", False)])
async def test_resource_and_prompt_reads_require_read(
    scope: str, allowed: bool
) -> None:
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: _context([scope]))

    @server.resource("example://document")
    def document() -> str:
        return "Example content"

    @server.prompt
    def example_prompt() -> str:
        return "Example prompt"

    assert bool(await server.list_resources()) is allowed
    assert bool(await server.list_prompts()) is allowed
    if allowed:
        assert await server.read_resource("example://document")
        assert await server.get_prompt("example_prompt")
    else:
        with pytest.raises(PermissionError, match="mcp:read"):
            await server.read_resource("example://document")
        with pytest.raises(PermissionError, match="mcp:read"):
            await server.get_prompt("example_prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes", [["mcp:write"], ["mcp:read", "mcp:write"], [], ["*"]]
)
async def test_write_scope_executes_without_granting_discovery(
    scopes: list[str],
) -> None:
    context = _context(scopes)
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: context)
    calls: list[str] = []

    @server.tool
    def example() -> str:
        calls.append("executed")
        return "ok"

    context.mcp_tools_cache = [
        Tool(
            name="example",
            description="",
            parameters={"type": "object", "properties": {}},
        )
    ]
    async_db = MagicMock()
    async_db.__aenter__ = AsyncMock(return_value=MagicMock())
    async_db.__aexit__ = AsyncMock(return_value=False)
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "preloop.services.dynamic_fastmcp.get_db", lambda: iter([MagicMock()])
            )
        )
        stack.enter_context(
            patch(
                "preloop.services.dynamic_fastmcp.kill_switch_service.tools_halted",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
                return_value=[],
            )
        )
        stack.enter_context(
            patch(
                "preloop.models.db.session.get_async_db_session", return_value=async_db
            )
        )
        stack.enter_context(
            patch(
                "preloop.services.policy_evaluator.evaluate_policy_async",
                new=AsyncMock(return_value=("allow", None, None)),
            )
        )
        stack.enter_context(patch.object(server, "_persist_tool_call_activity"))
        result = await server.call_tool("example", {})
    assert not result.is_error
    assert calls == ["executed"]
    if scopes == ["mcp:write"]:
        assert await server.list_tools() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", ["bearer", "oauth"])
@pytest.mark.parametrize(
    "scopes",
    [
        ["report_discovery"],
        ["telemetry:ingest"],
        ["enrollment:provision"],
        ["enrollment:provision", "mcp:read"],
    ],
)
async def test_single_purpose_keys_never_authenticate_mcp(
    db_session: Session, test_user: models.User, loader: str, scopes: list[str]
) -> None:
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        name="Scoped key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=scopes,
    )
    with patch.object(db_session, "close"):
        if loader == "bearer":
            with patch("preloop.services.mcp_http.get_db", lambda: iter([db_session])):
                result = await PreloopBearerAuthBackend().authenticate(
                    SimpleNamespace(
                        headers={"authorization": f"Bearer {secret}"},
                        scope={"type": "http"},
                    )
                )
        else:
            with patch(
                "preloop.services.oauth_provider._get_db", return_value=db_session
            ):
                result = await PreloopOAuthProvider(
                    base_url="http://localhost:8000"
                ).load_access_token(secret)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes", [["mcp:read"], ["mcp:write"], ["mcp:read", "mcp:write"]]
)
async def test_authenticated_scope_and_account_reach_dispatch_context(
    db_session: Session, test_user: models.User, scopes: list[str]
) -> None:
    key, secret = crud_api_key.create_runtime_key(
        db_session,
        name="Scoped key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=scopes,
    )
    with (
        patch.object(db_session, "close"),
        patch("preloop.services.mcp_http.get_db", lambda: iter([db_session])),
        patch("preloop.services.dynamic_fastmcp.get_db", lambda: iter([db_session])),
    ):
        result = await PreloopBearerAuthBackend().authenticate(
            SimpleNamespace(
                headers={"authorization": f"Bearer {secret}"}, scope={"type": "http"}
            )
        )
        assert result is not None
        context = create_user_context_from_scope({"user": result[1]})
    assert context.account_id == str(test_user.account_id)
    assert context.api_key_id == str(key.id)
    assert context.api_key_scopes == tuple(scopes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes", [["mcp:read"], ["mcp:write"], ["mcp:read", "mcp:write"], ["*"]]
)
@pytest.mark.parametrize("loader", ["bearer", "oauth"])
async def test_mcp_scopes_do_not_override_key_account_binding(
    db_session: Session,
    test_user: models.User,
    scopes: list[str],
    loader: str,
) -> None:
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    other_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "username": "jane",
            "email": "jane@example.com",
            "hashed_password": "unused",
            "is_active": True,
        },
    )
    _, secret = legacy_cross_account_runtime_key(
        db_session,
        name="Mismatched account",
        account_id=test_user.account_id,
        user_id=other_user.id,
        scopes=scopes,
    )
    with patch.object(db_session, "close"):
        if loader == "bearer":
            with patch("preloop.services.mcp_http.get_db", lambda: iter([db_session])):
                result = await PreloopBearerAuthBackend().authenticate(
                    SimpleNamespace(
                        headers={"authorization": f"Bearer {secret}"},
                        scope={"type": "http"},
                    )
                )
        else:
            with patch(
                "preloop.services.oauth_provider._get_db", return_value=db_session
            ):
                result = await PreloopOAuthProvider(
                    base_url="http://localhost:8000"
                ).load_access_token(secret)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["mcp:write"], ["mcp:read", "mcp:write"], ["*"]])
async def test_write_grant_does_not_open_another_accounts_registered_tool(
    scopes: list[str],
) -> None:
    server = DynamicFastMCP("scope-test")
    server.set_user_context_provider(lambda: _context(scopes))
    foreign_name = f"account_{uuid4().hex}_example"
    server._registered_proxied_tools.add(foreign_name)
    with (
        patch.object(server, "_record_attributed_refusal"),
        patch("fastmcp.FastMCP.call_tool", new_callable=AsyncMock) as dispatch,
    ):
        result = await server.call_tool(foreign_name, {})
    assert result.is_error
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/v1"])
@pytest.mark.parametrize(
    "scopes,visible",
    [(["mcp:read"], True), (["mcp:write"], False), (["*"], True), ([], True)],
)
def test_http_transport_keeps_the_presented_key_scopes(
    db_session: Session,
    test_user: models.User,
    path: str,
    scopes: list[str],
    visible: bool,
    read_only: bool,
) -> None:
    """Real authentication, ASGI context and MCP framing on mounted endpoints."""
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        context_data={
            "read_only_review": True,
            "allowed_mcp_tools": [{"name": "example"}],
        }
        if read_only
        else None,
        name="HTTP scope regression",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=scopes,
    )
    server = DynamicFastMCP("scope-http-test")
    transport = setup_dynamic_mcp_http(server)
    base_app = transport
    while not hasattr(base_app, "lifespan"):
        base_app = base_app.app

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with base_app.lifespan(base_app):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(MCPPathRewriteMiddleware)
    app.mount("/mcp", _MCPRootPathHandler(transport))

    def cached_context(scope: dict) -> UserContext | None:
        context = create_user_context_from_scope(scope)
        if read_only:
            assert context is None
            return None
        assert context is not None
        context.mcp_tools_cache = [
            Tool(
                name="example",
                description="",
                parameters={"type": "object", "properties": {}},
            )
        ]
        return context

    with (
        patch.object(db_session, "close"),
        patch("preloop.services.mcp_http.get_db", lambda: iter([db_session])),
        patch("preloop.services.dynamic_fastmcp.get_db", lambda: iter([db_session])),
        patch(
            "preloop.services.dynamic_fastmcp_http.create_user_context_from_scope",
            side_effect=cached_context,
        ),
        patch.object(server, "_record_attributed_refusal"),
        TestClient(app, base_url="http://localhost") as client,
    ):
        headers = {
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        response = client.post(
            path,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        if read_only:
            assert response.status_code == 401, response.text
            response = client.post(
                path,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "example", "arguments": {}},
                },
            )
            assert response.status_code == 401, response.text
            return
        assert response.status_code == 200, response.text
        data = json.loads(
            next(
                line[6:]
                for line in response.text.splitlines()
                if line.startswith("data: ")
            )
        )
        assert bool(data["result"]["tools"]) is (visible and not read_only)
        if scopes == ["mcp:read"] or read_only:
            response = client.post(
                path,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "example", "arguments": {}},
                },
            )
            assert response.status_code == 200, response.text
            data = json.loads(
                next(
                    line[6:]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                )
            )
            assert data["result"]["isError"]
            assert "mcp:write" in data["result"]["content"][0]["text"]
