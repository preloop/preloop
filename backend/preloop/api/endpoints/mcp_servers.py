"""MCP Servers router for managing external MCP server connections."""

from preloop.schemas.resource_share import SharedResourceRead

import logging
import os
import secrets
from typing import Any, Dict, List
from uuid import UUID

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import crud_mcp_server
from preloop.models.db.session import get_db_session
from preloop.models import models
from preloop.models.crud.upstream_mcp_oauth import (
    crud_upstream_mcp_oauth,
    OAuthContextError,
    digest,
)
from preloop.services.upstream_mcp_oauth import (
    OAuthError,
    OAuthHTTP,
    discover,
    configure_client,
    authorization_url,
    request_tokens,
)

from preloop.models.schemas.mcp_server import (
    MCPServerCreate,
    MCPServerResponse,
    MCPServerUpdate,
    merge_auth_config,
)
from preloop.models.schemas.mcp_tool import MCPToolResponse
from preloop.plugins.account_hooks import VISIBLE_MCP_SERVER, filter_viewable
from preloop.services.mcp_tool_discovery import (
    get_cached_tools_for_server,
    scan_mcp_server_tools,
)
from preloop.services.mcp_tool_collisions import (
    exposed_tool_name,
    recompute_and_audit,
    server_warnings,
    server_warnings_map,
    tool_warnings_by_id,
)
from preloop.utils.audit import log_config_change
from preloop.utils.mcp_oauth_privacy import install_mcp_oauth_privacy
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

MCPServer = models.MCPServer
User = models.User

logger = logging.getLogger(__name__)
router = APIRouter()

# Public router for OAuth callback (no auth required — user is redirected here)
oauth_callback_router = APIRouter(tags=["MCP Servers OAuth"])
install_mcp_oauth_privacy()


# Claim marking a short-lived token minted solely for the browser OAuth-authorize
# redirect. Scoped to one server; NOT a general access token.
MCP_OAUTH_AUTHORIZE_PURPOSE = "mcp_oauth_authorize"
MCP_OAUTH_AUTHORIZE_TTL_SECONDS = 120


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
@require_permission("create_mcp_servers")
async def create_mcp_server(
    server_data: MCPServerCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> MCPServerResponse:
    """Create a new external MCP server configuration.

    Args:
        server_data: MCP server configuration data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Created MCP server

    Raises:
        HTTPException: If creation fails or validation fails
    """
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "create_mcp_servers")
    )
    logger.info(
        f"Account {current_user.account_id} creating MCP server: {server_data.name} "
        f"(auth_type={server_data.auth_type!r})"
    )

    # Check if server with same name already exists for this account
    existing_server = crud_mcp_server.get_by_name(
        db, account_id=str(current_user.account_id), name=server_data.name
    )

    if existing_server:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"MCP server with name '{server_data.name}' already exists",
        )

    # Redaction markers carry no secret on create; drop them.
    auth_config = merge_auth_config(server_data.auth_config, None)

    # Validate connection to the MCP server before saving
    # Skip validation for OAuth — no credentials until the OAuth flow completes
    from preloop.services.mcp_client_pool import (
        MCPClient,
        describe_mcp_connection_error,
    )

    validation_error = None
    if server_data.auth_type == "oauth":
        logger.info(
            f"Skipping connection validation for OAuth MCP server at {server_data.url} "
            "(credentials will be obtained after OAuth flow)"
        )
    else:
        try:
            logger.info(f"Validating connection to MCP server at {server_data.url}")
            test_client = MCPClient(
                url=server_data.url,
                auth_type=server_data.auth_type or "none",
                auth_config=auth_config,
                transport=server_data.transport or "http-streaming",
            )

            # Try to connect and initialize
            await test_client.connect()
            logger.info(f"✓ Successfully validated MCP server at {server_data.url}")
            await test_client.close()

        except Exception as e:
            validation_error = describe_mcp_connection_error(e)
            logger.warning("MCP server validation failed: %s", validation_error)
            # Don't raise here - we'll store the error and mark status as 'error'

    # Create new MCP server
    try:
        new_server = MCPServer(
            account_id=str(current_user.account_id),
            name=server_data.name,
            url=server_data.url,
            transport=server_data.transport or "http-streaming",
            auth_type=server_data.auth_type or "none",
            auth_config=auth_config,
            status="error" if validation_error else "active",
            last_error=validation_error if validation_error else None,
            tool_prefix=server_data.tool_prefix,
        )

        new_server = crud_mcp_server.create(
            db,
            obj_in={
                "account_id": current_user.account_id,
                "name": new_server.name,
                "url": new_server.url,
                "transport": new_server.transport,
                "auth_type": new_server.auth_type,
                "auth_config": new_server.auth_config,
                "tool_prefix": new_server.tool_prefix,
                "status": new_server.status,
                "last_error": new_server.last_error,
            },
        )

        log_config_change(
            db,
            user=current_user,
            config_type="mcp_server",
            action="created",
            new_value={
                "id": str(new_server.id),
                "name": new_server.name,
                "url": new_server.url,
                "transport": new_server.transport,
                "tool_prefix": new_server.tool_prefix,
            },
        )

        if validation_error:
            logger.warning(
                f"Created MCP server {new_server.id} with error status: {validation_error}"
            )
        else:
            logger.info(
                f"Created MCP server {new_server.id} for account {current_user.account_id}"
            )

            # Automatically scan for tools on successful creation
            # Skip for OAuth — no credentials until the OAuth flow completes
            if server_data.auth_type == "oauth":
                logger.info(
                    f"Skipping auto-scan for OAuth MCP server {new_server.id} "
                    "(connect OAuth account first)"
                )
            else:
                try:
                    logger.info(f"Auto-scanning MCP server {new_server.id} for tools")
                    tools = await scan_mcp_server_tools(
                        new_server.id, db, user=current_user
                    )
                    logger.info(
                        f"Auto-scan complete: discovered {len(tools)} tools for {new_server.name}"
                    )
                except Exception as e:
                    logger.warning(f"Auto-scan failed for {new_server.id}: {e}")
                    # Don't fail the creation if scan fails - user can manually rescan

        return _server_response(db, new_server)

    except Exception as e:
        db.rollback()
        logger.error(f"Error creating MCP server: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating MCP server: {str(e)}",
        )


@router.get("/mcp-servers", response_model=List[SharedResourceRead | MCPServerResponse])
@require_permission("view_mcp_servers")
def list_mcp_servers(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[SharedResourceRead | MCPServerResponse]:
    """List all MCP servers for the current user.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of MCP servers
    """
    try:
        logger.info(f"Listing MCP servers for account {current_user.account_id}")
        servers = crud_mcp_server.get_multi_by_account(
            db, account_id=str(current_user.account_id)
        )
        servers = filter_viewable(db, current_user, VISIBLE_MCP_SERVER, servers)
        logger.info(f"Found {len(servers)} MCP servers")

        try:
            warnings_by_server = server_warnings_map(db, str(current_user.account_id))
        except Exception:
            logger.warning("Could not compute MCP tool warnings", exc_info=True)
            warnings_by_server = {}
        from preloop.models.crud.resource_share import crud_resource_share

        shared = crud_resource_share.public_list(
            db, account_id=current_user.account_id, resource_type="mcp_server"
        )
        states = crud_upstream_mcp_oauth.states_for_servers(db, servers)
        return [
            *[
                _server_response(db, server, warnings_by_server, states)
                for server in servers
            ],
            *filter_viewable(db, current_user, VISIBLE_MCP_SERVER, shared),
        ]
    except Exception as e:
        logger.error(f"Error listing MCP servers: {e}", exc_info=True)
        raise


@router.get(
    "/mcp-servers/{server_id}", response_model=SharedResourceRead | MCPServerResponse
)
@require_permission("view_mcp_servers")
async def get_mcp_server(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SharedResourceRead | MCPServerResponse:
    """Get a specific MCP server by ID.

    Args:
        server_id: MCP server ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        MCP server details

    Raises:
        HTTPException: If server not found or access denied
    """
    from preloop.models.crud.resource_share import crud_resource_share

    shared = crud_resource_share.public_read(
        db,
        account_id=current_user.account_id,
        resource_type="mcp_server",
        resource_id=server_id,
    )
    if shared is not None:
        if not filter_viewable(db, current_user, VISIBLE_MCP_SERVER, [shared]):
            raise HTTPException(404, "Resource not found")
        return shared
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    if not filter_viewable(db, current_user, VISIBLE_MCP_SERVER, [server]):
        raise HTTPException(404, "MCP server not found or access denied")
    return _server_response(db, server)


def _server_response(
    db: Session,
    server: MCPServer,
    warnings_by_server: Dict[str, List[str]] | None = None,
    states: dict[UUID, str | None] | None = None,
) -> SharedResourceRead | MCPServerResponse:
    """Server response with its tool-collision warnings (#1135).

    Lists batch warnings and states and use the stored public OAuth config.
    Only individual reads load encrypted config to provide redacted edit markers.
    """
    response = MCPServerResponse.model_validate(server)
    response.connection_state = (
        states[server.id]
        if states is not None
        else crud_upstream_mcp_oauth.state(db, server)
    )
    if server.auth_type == "oauth" and states is None:
        response.auth_config = crud_upstream_mcp_oauth.config_for_edit(db, server)
    try:
        if warnings_by_server is not None:
            response.warnings = warnings_by_server.get(str(server.id), [])
        else:
            response.warnings = server_warnings(db, server)
    except Exception:
        logger.warning("Could not compute MCP tool warnings", exc_info=True)
    return response


async def _evict_mcp_client(server_id: UUID) -> None:
    """Drop this process's pooled client so the next call uses the new row.

    Other pods rebuild on their next call because the pool compares the
    connection config it is handed with the cached one.
    """
    from preloop.services.mcp_client_pool import get_mcp_client_pool

    try:
        await get_mcp_client_pool().close_client(str(server_id))
    except Exception:
        logger.warning("Could not evict pooled MCP client", exc_info=True)


def _unregister_proxied_tools(
    account_id: str, server_id: UUID, tool_names: List[str]
) -> None:
    """Remove this process's MCP wrappers for a deleted server's tools."""
    if not tool_names:
        return
    from preloop.services import mcp_http

    mcp = getattr(mcp_http, "_mcp_server_instance", None)
    if mcp is None or not hasattr(mcp, "unregister_proxied_tools"):
        return
    try:
        mcp.unregister_proxied_tools(account_id, str(server_id), tool_names)
    except Exception:
        logger.warning("Could not unregister proxied MCP tools", exc_info=True)


@router.put("/mcp-servers/{server_id}", response_model=MCPServerResponse)
@require_permission("edit_mcp_servers")
async def update_mcp_server(
    server_id: UUID,
    server_update: MCPServerUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> MCPServerResponse:
    """Update an existing MCP server configuration.

    Args:
        server_id: MCP server ID
        server_update: Updated server data
        current_user: Current authenticated user
        db: Database session

    Returns:
        Updated MCP server

    Raises:
        HTTPException: If server not found or update fails
    """
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "edit_mcp_servers")
    )
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    # Update fields
    update_data = server_update.model_dump(exclude_unset=True)

    try:
        old_snapshot = {
            "id": str(server.id),
            "name": server.name,
            "url": server.url,
            "transport": server.transport,
            "tool_prefix": server.tool_prefix,
        }
        old_exposed = [
            exposed_tool_name(server.tool_prefix, tool.name) for tool in server.tools
        ]

        server = crud_mcp_server.update(db, db_obj=server, obj_in=update_data)
        await _evict_mcp_client(server_id)

        log_config_change(
            db,
            user=current_user,
            config_type="mcp_server",
            action="updated",
            old_value=old_snapshot,
            new_value={
                "id": str(server.id),
                "name": server.name,
                "url": server.url,
                "transport": server.transport,
                "tool_prefix": server.tool_prefix,
            },
        )

        # A new prefix or status changes which names this server exposes or
        # owns: recompute shadowing and drop wrappers for retired names.
        recompute_and_audit(db, str(current_user.account_id), current_user)
        if old_snapshot["tool_prefix"] != server.tool_prefix:
            _unregister_proxied_tools(
                str(current_user.account_id), server_id, old_exposed
            )

        logger.info(
            f"Updated MCP server {server_id} for account {current_user.account_id}"
        )

        return _server_response(db, server)

    except Exception as e:
        db.rollback()
        logger.error(f"Error updating MCP server {server_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating MCP server: {str(e)}",
        )


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_200_OK)
@require_permission("delete_mcp_servers")
async def delete_mcp_server(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Delete an MCP server.

    Args:
        server_id: MCP server ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If server not found or deletion fails
    """
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "delete_mcp_servers")
    )
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    try:
        # Delete associated tool configurations first (CASCADE should handle this,
        # but we do it explicitly for better logging and to avoid orphaned configs)
        from preloop.models.crud import crud_tool_configuration

        tool_configs = crud_tool_configuration.get_by_mcp_server(
            db, mcp_server_id=server_id
        )
        if tool_configs:
            logger.info(
                f"Deleting {len(tool_configs)} tool configurations for MCP server {server_id}"
            )

        server_name = server.name  # capture before delete
        from preloop.models.crud import crud_mcp_tool

        tool_names = [
            exposed_tool_name(server.tool_prefix, tool.name)
            for tool in crud_mcp_tool.get_by_server(db, server_id=server_id)
        ]

        # Delete the MCP server
        crud_mcp_server.remove(
            db, id=server_id, account_id=str(current_user.account_id)
        )
        await _evict_mcp_client(server_id)
        _unregister_proxied_tools(str(current_user.account_id), server_id, tool_names)

        log_config_change(
            db,
            user=current_user,
            config_type="mcp_server",
            action="deleted",
            old_value={"id": str(server_id), "name": server_name},
        )
        # The next oldest server takes over names this server owned.
        recompute_and_audit(db, str(current_user.account_id), current_user)

        logger.info(
            f"Deleted MCP server {server_id} and {len(tool_configs) if tool_configs else 0} "
            f"tool configurations for account {current_user.account_id}"
        )

        return {"message": "MCP server deleted successfully"}

    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting MCP server {server_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting MCP server: {str(e)}",
        )


@router.post("/mcp-servers/{server_id}/scan", status_code=status.HTTP_200_OK)
@require_permission("manage_mcp_servers")
async def scan_mcp_server(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, Any]:
    """Trigger a tool discovery scan for an MCP server.

    Args:
        server_id: MCP server ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message with tool count; failed scans return HTTP 502.

    Raises:
        HTTPException: If server not found or scan fails
    """
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "manage_mcp_servers")
    )
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    try:
        logger.info(
            f"Account {current_user.account_id} triggering scan for MCP server {server_id}"
        )

        # Guard: OAuth servers without tokens can't be scanned
        if (
            server.auth_type == "oauth"
            and crud_upstream_mcp_oauth.state(db, server) != "connected"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OAuth account not connected yet. Click 'Connect OAuth Account' to authorize first.",
            )

        tools = await scan_mcp_server_tools(server_id, db, user=current_user)

        if server.status == "error":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Scan failed. Cached tools were retained. "
                    + (server.last_error or "Check the server connection and rescan.")
                ),
            )

        logger.info(
            f"Scan complete for MCP server {server_id}: {len(tools)} tools discovered"
        )

        return {
            "message": f"Scan completed successfully. Discovered {len(tools)} tools.",
            "tool_count": str(len(tools)),
            "warnings": server_warnings(db, server),
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error scanning MCP server {server_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error scanning MCP server: {str(e)}",
        )


@router.get("/mcp-servers/{server_id}/tools", response_model=List[MCPToolResponse])
@require_permission("view_mcp_servers")
async def list_mcp_server_tools(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[MCPToolResponse]:
    """List discovered tools for an MCP server.

    Args:
        server_id: MCP server ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of discovered tools

    Raises:
        HTTPException: If server not found or access denied
    """
    server = crud_mcp_server.get_visible(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server or not filter_viewable(
        db, current_user, VISIBLE_MCP_SERVER, [server]
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    try:
        tools = await get_cached_tools_for_server(server_id, db)
        warnings = tool_warnings_by_id(db, server)

        responses = []
        for tool in tools:
            item = MCPToolResponse.model_validate(tool)
            item.exposed_name = exposed_tool_name(server.tool_prefix, tool.name)
            item.warnings = warnings.get(str(tool.id), [])
            responses.append(item)
        return responses

    except Exception as e:
        logger.error(
            f"Error listing tools for MCP server {server_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing tools: {str(e)}",
        )


# ---------------------------------------------------------------------------
# OAuth flow endpoints for external MCP servers
# ---------------------------------------------------------------------------


@router.post("/mcp-servers/{server_id}/oauth/authorize-token")
@require_permission("edit_mcp_servers")
async def create_mcp_oauth_authorize_token(
    server_id: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> dict:
    """Mint a short-lived, single-purpose token for the browser OAuth redirect.

    The browser navigates to the authorize endpoint via a full-page redirect
    and cannot attach an Authorization header, so the token must ride in the
    URL. Rather than exposing the reusable access token there (where it would
    land in browser history, server access logs, and the Referer sent onward
    to the external OAuth provider), this endpoint — authenticated normally via
    the Authorization header — returns a token scoped to this one server and
    purpose and expiring in ~2 minutes.
    """
    from preloop.api.loop_safety import run_db_off_loop

    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "edit_mcp_servers")
    )
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    from datetime import timedelta

    from preloop.api.auth.jwt import create_access_token, user_auth_generation

    authorize_token = create_access_token(
        {
            "sub": str(current_user.id),
            "purpose": MCP_OAUTH_AUTHORIZE_PURPOSE,
            "server_id": server_id,
            "permission": "edit_mcp_servers",
        },
        expires_delta=timedelta(seconds=MCP_OAUTH_AUTHORIZE_TTL_SECONDS),
        auth_generation=user_auth_generation(current_user),
    )
    return {"authorize_token": authorize_token}


@oauth_callback_router.get("/api/v1/mcp-servers/{server_id}/oauth/authorize")
async def mcp_server_oauth_authorize(
    server_id: str,
    code: str = Query(
        ..., description="Short-lived authorize token from /oauth/authorize-token"
    ),
    db: Session = Depends(get_db_session),
):
    """Initiate the MCP OAuth client flow for an external MCP server.

    This endpoint is on the public router because the browser navigates
    to it directly (302 redirect flow). Auth is via the short-lived,
    server-scoped ``code`` minted by ``POST .../oauth/authorize-token`` —
    never the reusable access token.

    Discovers the protected resource and selected issuer, configures a public
    or pre-registered client, and stores a durable browser-bound PKCE
    transaction before redirecting to provider consent.
    """
    # Validate the short-lived, purpose- and server-scoped authorize token.
    import jwt
    from jwt import PyJWTError

    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY
    from preloop.models.crud import crud_user

    try:
        payload = jwt.decode(code, SECRET_KEY, algorithms=[ALGORITHM])
    except PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authorization code",
        )
    if (
        payload.get("purpose") != MCP_OAUTH_AUTHORIZE_PURPOSE
        or payload.get("server_id") != server_id
        or payload.get("permission") != "edit_mcp_servers"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization code is not valid for this request",
        )
    current_user = crud_user.get(db, id=payload.get("sub"))
    if not current_user or not getattr(current_user, "is_active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authorization code",
        )

    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    if server.auth_type != "oauth":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server auth_type is not 'oauth'",
        )

    from preloop.api.auth.jwt import user_auth_generation

    if payload.get("gen", 0) != user_auth_generation(current_user):
        raise HTTPException(401, "Authorization session is no longer valid")
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "edit_mcp_servers")
    )
    try:
        snapshot = await run_db_off_loop(
            lambda: crud_upstream_mcp_oauth.setup(
                db, UUID(server_id), current_user.account_id
            )
        )
        http = OAuthHTTP()
        metadata = await discover(snapshot.url, snapshot.config, http)
        config = await configure_client(
            snapshot.config, metadata, _oauth_callback_uri(), http
        )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        await run_db_off_loop(
            lambda: crud_upstream_mcp_oauth.begin(
                db,
                snapshot,
                user_id=current_user.id,
                state=state,
                browser_nonce=nonce,
                verifier=verifier,
                config=config,
            )
        )
        response = RedirectResponse(
            authorization_url(config, state, verifier), status_code=302
        )
        response.set_cookie(
            _oauth_cookie(state),
            nonce,
            max_age=600,
            httponly=True,
            secure=_oauth_callback_uri().startswith("https:"),
            samesite="lax",
            path="/api/v1/mcp-servers/oauth",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    except (OAuthError, OAuthContextError) as error:
        raise HTTPException(400, str(error)) from None


def _oauth_callback_uri() -> str:
    return (
        os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
        + "/api/v1/mcp-servers/oauth/callback"
    )


def _oauth_cookie(state: str) -> str:
    return "mcp_oauth_" + digest(state)[:16]


@oauth_callback_router.get("/api/v1/mcp-servers/oauth/callback")
async def mcp_server_oauth_callback(
    request: Request,
    state: str = Query(..., max_length=512),
    code: str | None = Query(None),
    error: str | None = Query(None, max_length=256),
    iss: str | None = Query(None, max_length=2048),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    """Consume a durable browser-bound transaction before any token exchange."""
    try:
        snapshot = await run_db_off_loop(
            lambda: crud_upstream_mcp_oauth.consume(
                db,
                state=state,
                browser_nonce=request.cookies.get(_oauth_cookie(state), ""),
                redirect_uri=_oauth_callback_uri(),
                issuer=iss,
            )
        )
    except OAuthContextError:
        raise HTTPException(
            400, "Invalid or expired OAuth state. Start authorization again."
        ) from None
    success = False
    try:
        if error is not None or not code:
            raise OAuthError("OAuth authorization was not completed.")
        config = await request_tokens(
            snapshot.config, OAuthHTTP(), code=code, verifier=snapshot.verifier
        )
        success = await run_db_off_loop(
            lambda: crud_upstream_mcp_oauth.complete(db, snapshot, config)
        )
    except (OAuthError, OAuthContextError):
        try:
            await run_db_off_loop(lambda: crud_upstream_mcp_oauth.fail(db, snapshot))
        except OAuthContextError:
            pass  # Deletion/replacement already invalidated this transaction.
    await _evict_mcp_client(snapshot.server_id)
    if success:
        try:
            await scan_mcp_server_tools(snapshot.server_id, db)
        except Exception:
            logger.warning(
                "Post-OAuth scan failed; credentials saved. Rescan from Tools setup."
            )
    frontend = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
    response = RedirectResponse(
        f"{frontend}/console/tools#setup_mcp={'success' if success else 'error'}",
        status_code=302,
    )
    response.delete_cookie(_oauth_cookie(state), path="/api/v1/mcp-servers/oauth")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post(
    "/mcp-servers/{server_id}/oauth/disconnect", response_model=MCPServerResponse
)
@require_permission("edit_mcp_servers")
def disconnect_mcp_oauth(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> MCPServerResponse:
    """Remove local grants and pending transactions. No upstream revocation."""
    # A sync handler, so the session is only used on the worker thread.
    ensure_permission_in_oss(db, current_user, "edit_mcp_servers")
    try:
        crud_upstream_mcp_oauth.disconnect(db, server_id, current_user.account_id)
    except OAuthContextError:
        raise HTTPException(404, "OAuth server not found or unavailable") from None
    from_thread.run(_evict_mcp_client, server_id)
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )
    return _server_response(db, server)
