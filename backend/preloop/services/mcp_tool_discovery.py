"""MCP Tool Discovery Service.

This module provides functionality to discover and cache tools from external MCP servers.
"""

import logging
from datetime import datetime
from typing import Any, List
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.services.mcp_client_pool import get_mcp_client_pool
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.models.crud import crud_mcp_server, crud_mcp_tool, crud_tool_configuration
from preloop.services.mcp_tool_collisions import (
    first_wins,
    is_valid_mcp_tool_name,
    recompute_and_audit,
)

logger = logging.getLogger(__name__)


async def scan_mcp_server_tools(
    mcp_server_id: UUID, db: Session, user: Any = None
) -> List[MCPTool]:
    """Scan an MCP server and cache its available tools.

    After the scan, same-named tools across the account's servers are
    recomputed (#1135): the newer server's tools are marked shadowed, and
    one ``configuration_change`` audit event is written per collision set
    when ``user`` is given.

    Args:
        mcp_server_id: ID of the MCP server to scan
        db: Database session
        user: Acting user for the collision audit events (optional)

    Returns:
        List of discovered tools

    Raises:
        ValueError: If server not found
    """
    # Get MCP server from database using CRUD layer
    mcp_server = crud_mcp_server.get(db, id=mcp_server_id)
    if not mcp_server:
        raise ValueError(f"MCP server not found: {mcp_server_id}")

    logger.info(f"Scanning MCP server: {mcp_server.name} ({mcp_server.url})")

    try:
        # Get client from pool
        client_pool = get_mcp_client_pool()
        client = await client_pool.get_client(
            server_id=str(mcp_server_id),
            url=mcp_server.url,
            auth_type=mcp_server.auth_type,
            auth_config=mcp_server.auth_config,
            transport=mcp_server.transport,
        )

        # List tools from the server
        discovered_tools = await client.list_tools()
        logger.info(
            f"Discovered {len(discovered_tools)} tools from server {mcp_server.name}"
        )

        # Get existing tools for this server using CRUD layer
        existing_tools = crud_mcp_tool.get_by_server(db, server_id=mcp_server_id)
        existing_tool_names = {tool.name for tool in existing_tools}

        # Track new and updated tools
        new_tools = []
        updated_count = 0

        discovered_at = datetime.utcnow().isoformat()

        for tool in discovered_tools:
            if tool.name in existing_tool_names:
                # Update existing tool
                existing_tool = next(t for t in existing_tools if t.name == tool.name)
                existing_tool.description = tool.description
                existing_tool.input_schema = tool.inputSchema
                existing_tool.discovered_at = discovered_at
                updated_count += 1
            else:
                # Create new tool
                new_tool = MCPTool(
                    mcp_server_id=mcp_server_id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.inputSchema,
                    discovered_at=discovered_at,
                )
                db.add(new_tool)
                new_tools.append(new_tool)

        # Update server scan timestamp and status
        mcp_server.last_scan_at = discovered_at
        mcp_server.status = "active"
        mcp_server.last_error = None

        # Commit all changes
        db.commit()

        # Same-named tools across the account's servers: mark the newer
        # server's tools shadowed, or un-shadow them when the owner is gone.
        recompute_and_audit(db, str(mcp_server.account_id), user)

        logger.info(
            f"Scan complete for {mcp_server.name}: "
            f"{len(new_tools)} new tools, {updated_count} updated tools"
        )

        # Return all tools for this server using CRUD layer
        all_tools = crud_mcp_tool.get_by_server(db, server_id=mcp_server_id)
        return all_tools

    except Exception as e:
        # Update server with error status
        was_healthy = mcp_server.status != "error"
        mcp_server.status = "error"
        mcp_server.last_error = str(e)
        db.commit()
        # An owner that turned unhealthy no longer owns its names.
        recompute_and_audit(db, str(mcp_server.account_id), user)

        logger.warning("Failed to scan MCP server %s: %s", mcp_server.name, e)
        try:
            from preloop.sync.tasks import notify_admins

            if not was_healthy:
                # Already known-unhealthy; avoid re-notifying on every rescan.
                return crud_mcp_tool.get_by_server(db, server_id=mcp_server_id)
            notify_admins(
                subject=f"MCP server scan failed: {mcp_server.name}",
                message=(
                    "Preloop could not scan configured MCP server "
                    f"{mcp_server.name} ({mcp_server.url}). "
                    f"The server was marked unhealthy and cached tools will be used. "
                    f"Error: {e}"
                ),
            )
        except Exception as notify_error:
            logger.debug(
                "Failed to notify admins about MCP scan failure: %s",
                notify_error,
            )

        return crud_mcp_tool.get_by_server(db, server_id=mcp_server_id)


async def get_cached_tools_for_server(
    mcp_server_id: UUID, db: Session
) -> List[MCPTool]:
    """Get cached tools for an MCP server without scanning.

    Args:
        mcp_server_id: ID of the MCP server
        db: Database session

    Returns:
        List of cached tools
    """
    tools = crud_mcp_tool.get_by_server(db, server_id=mcp_server_id)
    return tools


def _get_proxied_tools_sync(
    account_id: str, db: Session
) -> List[tuple[MCPServer, MCPTool]]:
    """Synchronous implementation of proxied tool discovery.

    Used by run_in_executor() to avoid blocking the event loop.
    """

    # Get all active MCP servers for this account using CRUD layer
    # Own servers plus any another account shares here (account hook H3).
    mcp_servers = crud_mcp_server.get_active_visible_by_account(
        db, account_id=account_id
    )

    # Get all tool configurations for this account (for filtering) using CRUD layer
    tool_configs = crud_tool_configuration.get_by_source(
        db, account_id=account_id, tool_source="mcp"
    )

    # Build a map of (tool_name, server_id) -> is_enabled
    config_map = {
        (tc.tool_name, str(tc.mcp_server_id)): tc.is_enabled for tc in tool_configs
    }

    # First-wins per exposed name (#1135): servers come own-first, each by
    # created_at then id, so the oldest active server owns a name and newer
    # servers' same-named tools are shadowed (not listed, not callable).
    pairs = []
    for server in mcp_servers:
        tools = sorted(
            crud_mcp_tool.get_by_server(db, server_id=server.id),
            key=lambda tool: tool.name,
        )
        pairs.extend((server, tool) for tool in tools)

    proxied_tools = []
    for server, tool, exposed_name in first_wins(pairs):
        if not is_valid_mcp_tool_name(exposed_name):
            logger.warning(
                "Skipping MCP tool %r from server %s: not a valid MCP tool name",
                exposed_name,
                server.name,
            )
            continue
        # Configuration is keyed by the exposed name and the server.
        is_enabled = config_map.get((exposed_name, str(server.id)), True)
        if is_enabled:
            proxied_tools.append((server, tool))
        else:
            logger.debug(
                f"Skipping disabled tool {exposed_name} from server {server.name}"
            )

    return proxied_tools


async def get_all_enabled_proxied_tools(
    account_id: str, db: Session
) -> List[tuple[MCPServer, MCPTool]]:
    """Get all enabled proxied tools for an account.

    This checks tool_configuration to see if tools have been explicitly disabled.
    By default, tools are enabled unless explicitly configured otherwise.

    Args:
        account_id: Account ID
        db: Database session

    Returns:
        List of (MCPServer, MCPTool) tuples for enabled tools
    """
    return _get_proxied_tools_sync(account_id, db)
