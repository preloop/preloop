"""MCP Servers router for managing external MCP server connections."""

import base64
import hashlib
import logging
import os
import secrets
from typing import Dict, List
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.services.mcp_tool_discovery import (
    get_cached_tools_for_server,
    scan_mcp_server_tools,
)
from preloop.models.crud import crud_mcp_server
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.models.models.mcp_server import MCPServer
from preloop.models.schemas.mcp_server import (
    MCPServerCreate,
    MCPServerResponse,
    MCPServerUpdate,
)
from preloop.models.schemas.mcp_tool import MCPToolResponse
from preloop.utils.audit import log_config_change
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

# Public router for OAuth callback (no auth required — user is redirected here)
oauth_callback_router = APIRouter(tags=["MCP Servers OAuth"])

# In-memory store for pending OAuth states (maps state -> context dict)
_pending_oauth_states: dict[str, dict] = {}

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

    # Validate connection to the MCP server before saving
    # Skip validation for OAuth — no credentials until the OAuth flow completes
    from preloop.services.mcp_client_pool import MCPClient

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
                auth_config=server_data.auth_config,
                transport=server_data.transport or "http-streaming",
            )

            # Try to connect and initialize
            await test_client.connect()
            logger.info(f"✓ Successfully validated MCP server at {server_data.url}")
            await test_client.close()

        except Exception as e:
            validation_error = str(e)
            logger.warning(f"Failed to validate MCP server at {server_data.url}: {e}")
            # Don't raise here - we'll store the error and mark status as 'error'

    # Create new MCP server
    try:
        new_server = MCPServer(
            account_id=str(current_user.account_id),
            name=server_data.name,
            url=server_data.url,
            transport=server_data.transport or "http-streaming",
            auth_type=server_data.auth_type or "none",
            auth_config=server_data.auth_config,
            status="error" if validation_error else "active",
            last_error=validation_error if validation_error else None,
        )

        db.add(new_server)
        db.commit()
        db.refresh(new_server)

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
                    tools = await scan_mcp_server_tools(new_server.id, db)
                    logger.info(
                        f"Auto-scan complete: discovered {len(tools)} tools for {new_server.name}"
                    )
                except BaseException as e:
                    logger.warning(f"Auto-scan failed for {new_server.id}: {e}")
                    # Don't fail the creation if scan fails - user can manually rescan

        return MCPServerResponse.model_validate(new_server)

    except Exception as e:
        db.rollback()
        logger.error(f"Error creating MCP server: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating MCP server: {str(e)}",
        )


@router.get("/mcp-servers", response_model=List[MCPServerResponse])
@require_permission("view_mcp_servers")
def list_mcp_servers(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[MCPServerResponse]:
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
        logger.info(f"Found {len(servers)} MCP servers")

        return [MCPServerResponse.model_validate(server) for server in servers]
    except Exception as e:
        logger.error(f"Error listing MCP servers: {e}", exc_info=True)
        raise


@router.get("/mcp-servers/{server_id}", response_model=MCPServerResponse)
@require_permission("view_mcp_servers")
async def get_mcp_server(
    server_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> MCPServerResponse:
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
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    return MCPServerResponse.model_validate(server)


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
        }

        for field, value in update_data.items():
            setattr(server, field, value)

        db.commit()
        db.refresh(server)

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
            },
        )

        logger.info(
            f"Updated MCP server {server_id} for account {current_user.account_id}"
        )

        return MCPServerResponse.model_validate(server)

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
            for config in tool_configs:
                db.delete(config)

        server_name = server.name  # capture before delete

        # Delete the MCP server
        db.delete(server)
        db.commit()

        log_config_change(
            db,
            user=current_user,
            config_type="mcp_server",
            action="deleted",
            old_value={"id": str(server_id), "name": server_name},
        )

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
) -> Dict[str, str]:
    """Trigger a tool discovery scan for an MCP server.

    Args:
        server_id: MCP server ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Success message with tool count

    Raises:
        HTTPException: If server not found or scan fails
    """
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
        if server.auth_type == "oauth" and not (server.auth_config or {}).get(
            "access_token"
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OAuth account not connected yet. Click 'Connect OAuth Account' to authorize first.",
            )

        tools = await scan_mcp_server_tools(server_id, db)

        logger.info(
            f"Scan complete for MCP server {server_id}: {len(tools)} tools discovered"
        )

        return {
            "message": f"Scan completed successfully. Discovered {len(tools)} tools.",
            "tool_count": str(len(tools)),
        }

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
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )

    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    try:
        tools = await get_cached_tools_for_server(server_id, db)

        return [MCPToolResponse.model_validate(tool) for tool in tools]

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
    server = crud_mcp_server.get(
        db, id=server_id, account_id=str(current_user.account_id)
    )
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="MCP server not found or access denied",
        )

    from datetime import timedelta

    from preloop.api.auth.jwt import create_access_token

    authorize_token = create_access_token(
        {
            "sub": str(current_user.id),
            "purpose": MCP_OAUTH_AUTHORIZE_PURPOSE,
            "server_id": server_id,
        },
        expires_delta=timedelta(seconds=MCP_OAUTH_AUTHORIZE_TTL_SECONDS),
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

    1. Fetches the server's /.well-known/oauth-authorization-server metadata.
    2. Dynamically registers as an OAuth client (RFC 7591) if needed.
    3. Generates PKCE code_challenge / code_verifier.
    4. Redirects the user to the external server's authorization endpoint.
    """
    # Validate the short-lived, purpose- and server-scoped authorize token.
    from jose import JWTError
    from jose import jwt as jose_jwt

    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY
    from preloop.models.crud import crud_user

    try:
        payload = jose_jwt.decode(code, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authorization code",
        )
    if (
        payload.get("purpose") != MCP_OAUTH_AUTHORIZE_PURPOSE
        or payload.get("server_id") != server_id
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

    server_url = server.url.rstrip("/")

    # Step 1: Discover OAuth metadata
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Try the path-aware metadata URL first (RFC 9728 / MCP spec)
        from urllib.parse import urlparse

        parsed = urlparse(server_url)
        base_origin = f"{parsed.scheme}://{parsed.netloc}"
        server_path = parsed.path.rstrip("/")

        metadata = None
        for meta_url in [
            f"{base_origin}/.well-known/oauth-authorization-server{server_path}",
            f"{base_origin}/.well-known/oauth-authorization-server",
        ]:
            try:
                resp = await client.get(meta_url)
                if resp.status_code == 200:
                    metadata = resp.json()
                    break
            except Exception:
                continue

        if not metadata:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"Could not discover OAuth metadata at {server_url}. "
                    "The server may not support MCP OAuth."
                ),
            )

        authorization_endpoint = metadata.get("authorization_endpoint")
        token_endpoint = metadata.get("token_endpoint")
        registration_endpoint = metadata.get("registration_endpoint")

        if not authorization_endpoint or not token_endpoint:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="OAuth metadata is missing authorization_endpoint or token_endpoint",
            )

        # Step 2: Dynamic Client Registration (if we don't have a client_id yet)
        auth_config = dict(server.auth_config or {})
        client_id = auth_config.get("client_id")

        preloop_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
        callback_url = f"{preloop_url}/api/v1/mcp-servers/oauth/callback"

        if not client_id and registration_endpoint:
            reg_body = {
                "redirect_uris": [callback_url],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_name": "Preloop MCP Proxy",
            }
            try:
                reg_resp = await client.post(registration_endpoint, json=reg_body)
                if reg_resp.status_code in (200, 201):
                    reg_data = reg_resp.json()
                    client_id = reg_data.get("client_id")
                    client_secret = reg_data.get("client_secret")
                    auth_config["client_id"] = client_id
                    if client_secret:
                        auth_config["client_secret"] = client_secret
                    logger.info(
                        f"Dynamically registered OAuth client: {client_id} "
                        f"for MCP server {server.name}"
                    )
                else:
                    logger.warning(
                        f"Dynamic registration failed ({reg_resp.status_code}): "
                        f"{reg_resp.text}"
                    )
            except Exception as e:
                logger.warning(f"Dynamic registration error: {e}")

        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "No client_id available. The external server does not support "
                    "dynamic registration. Please set client_id in auth_config."
                ),
            )

    # Step 3: Generate PKCE
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # Store both PKCE verifier and server context
    _pending_oauth_states[state] = {
        "server_id": str(server.id),
        "account_id": str(server.account_id),
        "code_verifier": code_verifier,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret": auth_config.get("client_secret", ""),
        "callback_url": callback_url,
    }

    # Persist the token_endpoint and client_id in auth_config for future use
    auth_config["token_endpoint"] = token_endpoint
    auth_config["authorization_endpoint"] = authorization_endpoint
    if registration_endpoint:
        auth_config["registration_endpoint"] = registration_endpoint
    crud_mcp_server.update(db, db_obj=server, obj_in={"auth_config": auth_config})

    # Step 4: Redirect to authorization endpoint
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{authorization_endpoint}?{urlencode(params)}"

    logger.info(
        f"Redirecting user to OAuth authorization for MCP server {server.name} "
        f"at {authorization_endpoint}"
    )
    return RedirectResponse(url=auth_url, status_code=302)


@oauth_callback_router.get("/api/v1/mcp-servers/oauth/callback")
async def mcp_server_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    """Handle the OAuth callback from an external MCP server.

    Exchanges the authorization code for access/refresh tokens using PKCE,
    saves them in the MCP server's auth_config, and redirects to the frontend.
    """
    # Validate state (CSRF protection)
    state_data = _pending_oauth_states.pop(state, None)
    if not state_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state. Please try again.",
        )

    server_id = state_data["server_id"]
    token_endpoint = state_data["token_endpoint"]
    client_id = state_data["client_id"]
    client_secret = state_data.get("client_secret", "")
    code_verifier = state_data["code_verifier"]
    callback_url = state_data["callback_url"]

    # Exchange authorization code for tokens
    token_body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback_url,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        token_body["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                token_endpoint,
                data=token_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                logger.error(
                    f"OAuth token exchange failed ({resp.status_code}): {resp.text}"
                )
                frontend_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip(
                    "/"
                )
                return RedirectResponse(
                    url=f"{frontend_url}/console/tools#setup_mcp=error",
                    status_code=302,
                )

            token_data = resp.json()
        except Exception as e:
            logger.error(f"OAuth token exchange error: {e}", exc_info=True)
            frontend_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
            return RedirectResponse(
                url=f"{frontend_url}/console/tools#setup_mcp=error",
                status_code=302,
            )

    # Save tokens in the server's auth_config
    db = next(get_db_session())
    try:
        server = crud_mcp_server.get(
            db, id=server_id, account_id=state_data["account_id"]
        )
        if server:
            auth_config = dict(server.auth_config or {})
            auth_config["access_token"] = token_data.get("access_token")
            if token_data.get("refresh_token"):
                auth_config["refresh_token"] = token_data["refresh_token"]
            if token_data.get("expires_in"):
                import time

                auth_config["expires_at"] = int(time.time()) + token_data["expires_in"]
            auth_config["token_type"] = token_data.get("token_type", "Bearer")

            crud_mcp_server.update(
                db,
                db_obj=server,
                obj_in={
                    "auth_config": auth_config,
                    "status": "active",
                    "last_error": None,
                },
            )
            logger.info(
                f"OAuth tokens saved for MCP server {server.name} (id={server_id})"
            )

            # Auto-scan for tools now that we have credentials
            try:
                tools = await scan_mcp_server_tools(server_id, db)
                logger.info(
                    f"Post-OAuth auto-scan: discovered {len(tools)} tools for {server.name}"
                )
            except BaseException as e:
                logger.warning(f"Post-OAuth auto-scan failed for {server_id}: {e}")
                # Don't fail the callback — tokens are saved, user can rescan manually
        else:
            logger.warning(f"MCP server {server_id} not found after OAuth callback")
    finally:
        db.close()

    # Redirect to the frontend tools page with success indicator
    frontend_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
    return RedirectResponse(
        url=f"{frontend_url}/console/tools#setup_mcp=success",
        status_code=302,
    )
