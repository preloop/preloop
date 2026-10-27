"""An unsuccessful scan must not report cached inventory as a new discovery."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import mcp_servers
from preloop.models import models


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_count", [0, 3])
async def test_failed_scan_reports_bad_gateway(cached_count: int) -> None:
    server = models.MCPServer(id=uuid4(), status="active", auth_type="none")
    user = models.User(account_id=uuid4())
    diagnostic = "MCP server rejected authentication or authorization (HTTP 401)."

    async def scan(*args: Any, **kwargs: Any) -> list[Any]:
        server.status = "error"
        server.last_error = diagnostic
        return [MagicMock()] * cached_count

    with (
        patch.object(mcp_servers, "ensure_permission_in_oss"),
        patch.object(mcp_servers.crud_mcp_server, "get", return_value=server),
        patch.object(mcp_servers, "scan_mcp_server_tools", side_effect=scan),
        patch.object(mcp_servers, "server_warnings", return_value=[]),
    ):
        with pytest.raises(HTTPException) as raised:
            await mcp_servers.scan_mcp_server(
                server.id, current_user=user, db=MagicMock()
            )
    assert raised.value.status_code == 502
    assert "cached" in raised.value.detail.lower()
    assert diagnostic in raised.value.detail
    assert "successfully" not in raised.value.detail


@pytest.mark.asyncio
async def test_successful_empty_scan_remains_successful() -> None:
    server = models.MCPServer(id=uuid4(), status="active", auth_type="none")
    with (
        patch.object(mcp_servers, "ensure_permission_in_oss"),
        patch.object(mcp_servers.crud_mcp_server, "get", return_value=server),
        patch.object(
            mcp_servers, "scan_mcp_server_tools", new=AsyncMock(return_value=[])
        ),
        patch.object(mcp_servers, "server_warnings", return_value=[]),
    ):
        result = await mcp_servers.scan_mcp_server(
            server.id, current_user=models.User(account_id=uuid4()), db=MagicMock()
        )
    assert result["tool_count"] == "0"
    assert "successfully" in result["message"]


@pytest.mark.asyncio
async def test_unconnected_oauth_scan_retains_bad_request() -> None:
    server = models.MCPServer(id=uuid4(), status="active", auth_type="oauth")
    with (
        patch.object(mcp_servers, "ensure_permission_in_oss"),
        patch.object(mcp_servers.crud_mcp_server, "get", return_value=server),
        patch.object(
            mcp_servers.crud_upstream_mcp_oauth, "state", return_value="disconnected"
        ),
        patch.object(
            mcp_servers, "scan_mcp_server_tools", new_callable=AsyncMock
        ) as scan,
    ):
        with pytest.raises(HTTPException) as raised:
            await mcp_servers.scan_mcp_server(
                server.id, current_user=models.User(account_id=uuid4()), db=MagicMock()
            )
    assert raised.value.status_code == 400
    scan.assert_not_awaited()
