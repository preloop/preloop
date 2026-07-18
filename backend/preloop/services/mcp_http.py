"""MCP HTTP Streaming integration for DynamicMCPServer.

This module provides FastAPI integration for the DynamicMCPServer using
StreamableHTTP transport with JWT authentication.
"""

import json
import logging
from typing import Optional

from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.responses import Response
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlalchemy.orm import Session
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.requests import HTTPConnection

from preloop.api.auth.jwt import get_user_from_token_if_valid
from preloop.services.dynamic_mcp_server import (
    DynamicMCPServer,
    has_tracker,
    initialize_dynamic_mcp_server,
)
from preloop.models.db.session import get_db_session as get_db

logger = logging.getLogger(__name__)

# Global server instance
_mcp_server_instance: Optional[DynamicMCPServer] = None


class MCPPathRewriteMiddleware:
    """ASGI middleware: rewrite /mcp → /mcp/v1.

    Starlette's Mount("/mcp", ...) returns a 301 redirect for the exact
    path /mcp (to /mcp/).  MCP clients using StreamableHTTP POST cannot
    follow redirects, so we transparently rewrite the path before the
    router ever sees it.

    Must be added in app.py alongside other middleware to ensure correct
    timing (before the middleware stack is built).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/v1"
            scope["raw_path"] = b"/mcp/v1"
        await self.app(scope, receive, send)


class _MCPRootPathHandler:
    """Wraps the MCP ASGI app to handle / by rewriting to /v1.

    After Starlette's Mount strips the /mcp prefix, requests to /mcp/
    arrive as path=/. The MCP app only serves /v1, so this wrapper
    transparently rewrites / (and empty path) to /v1.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in ("", "/"):
            scope = dict(scope)
            scope["path"] = "/v1"
            scope["raw_path"] = b"/v1"
        await self.app(scope, receive, send)


class PreloopBearerAuthBackend(AuthenticationBackend):
    """
    Authentication backend that validates Bearer tokens using Preloop's existing auth.

    This integrates our API key and JWT authentication with the MCP authentication system.
    """

    async def authenticate(self, conn: HTTPConnection):
        """Extract and validate Bearer token from Authorization header."""
        # Extract Authorization header (case-insensitive)
        auth_header = next(
            (
                conn.headers.get(key)
                for key in conn.headers
                if key.lower() == "authorization"
            ),
            None,
        )

        if not auth_header or not auth_header.lower().startswith("bearer "):
            # Log at INFO to help diagnose MCP clients that don't send headers
            req_path = (
                getattr(conn, "scope", {}).get("path", "?")
                if hasattr(conn, "scope")
                else "?"
            )
            logger.info(
                f"MCP auth: no Bearer token in Authorization header "
                f"(path={req_path}, "
                f"headers_present={[k for k in conn.headers.keys()]})"
            )
            return None

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Get database session
        db = next(get_db())
        try:
            # Validate the token using our existing auth system (API keys / JWTs)
            current_user = await get_user_from_token_if_valid(token, db)

            if current_user:
                # Try to load the API key if this is an API key token (for flow context)
                api_key_obj = None
                if token and "." not in token:  # API keys don't have dots (JWTs do)
                    from preloop.models.crud import crud_api_key

                    api_key_obj = crud_api_key.get_by_key(db, key=token)

                return self._build_auth_result(token, current_user, api_key_obj)

            # Fallback: check OAuth MCP opaque access tokens
            result = self._check_oauth_token(db, token)
            if result:
                return result

            return None
        finally:
            db.close()

    def _check_oauth_token(self, db, token: str):
        """Check the OAuth MCP access token table for an opaque token."""
        import time

        from preloop.models.crud import crud_user
        from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_access_token

        db_token = crud_oauth_mcp_access_token.get_by_token(db, token=token)
        if not db_token:
            return None

        if db_token.is_revoked:
            logger.warning("OAuth MCP access token is revoked")
            return None

        if db_token.expires_at and db_token.expires_at < int(time.time()):
            logger.warning("OAuth MCP access token is expired")
            return None

        # Load the user from the token's user_id
        user = crud_user.get(db, id=str(db_token.user_id))
        if not user:
            logger.warning(f"OAuth MCP token user_id={db_token.user_id} not found")
            return None

        logger.info(f"Authenticated via OAuth MCP token for user {user.username}")
        return self._build_auth_result(token, user, api_key_obj=None)

    @staticmethod
    def _build_auth_result(token: str, current_user, api_key_obj=None):
        """Build the Starlette auth result tuple from a validated user."""
        # Create MCP AccessToken with user info stored for later retrieval
        # Store account ID in the AccessToken so we can retrieve the user later
        access_token = AccessToken(
            token=token,
            client_id=str(current_user.id),  # Use account ID as client_id
            scopes=[],  # We don't use scopes in Phase 1A
            expires_at=None,  # API keys don't expire by default
        )

        # Store user info and API key in AccessToken for later use (avoiding re-validation)
        # Use object.__setattr__() to bypass Pydantic's validation
        object.__setattr__(access_token, "user", current_user)
        if api_key_obj:
            object.__setattr__(access_token, "api_key", api_key_obj)

        # Return authentication credentials and user
        # This will be stored in scope["auth"] and scope["user"]
        return (
            AuthCredentials(scopes=[]),
            AuthenticatedUser(
                access_token
            ),  # Pass access_token as positional arg (auth_info)
        )


def get_mcp_server() -> DynamicMCPServer:
    """Get or create the MCP server instance.

    Returns:
        DynamicMCPServer instance
    """
    global _mcp_server_instance
    if _mcp_server_instance is None:
        _mcp_server_instance = initialize_dynamic_mcp_server()
        logger.info("MCP server initialized")
    return _mcp_server_instance


async def get_user_context_for_mcp(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Extract user context from authenticated request for MCP.

    This function is called by the MCP endpoint after the AuthenticationMiddleware
    has already extracted and validated the Bearer token.

    Args:
        request: FastAPI request (with scope["user"] set by middleware)
        db: Database session

    Returns:
        Dict with user context to inject into MCP request

    Raises:
        HTTPException: If authentication fails
    """
    # Get authenticated user from request scope (set by AuthenticationMiddleware)
    auth_user = request.scope.get("user")

    if not isinstance(auth_user, AuthenticatedUser):
        logger.warning(
            "MCP authentication failed: No authenticated user in request scope"
        )
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Provide Authorization: Bearer <token> header",
        )

    # Extract token and cached account from AuthenticatedUser
    token = auth_user.access_token.token
    current_user = getattr(auth_user.access_token, "account", None)

    if not current_user:
        # Fallback: re-validate if account wasn't cached (shouldn't happen)
        logger.warning(
            f"Account not cached in AccessToken, re-validating token {token[:10]}..."
        )
        current_user = await get_user_from_token_if_valid(token, db)
        if not current_user:
            logger.warning(
                f"MCP authentication failed: Could not load user for token {token[:10]}..."
            )
            raise HTTPException(status_code=401, detail="Invalid authentication token")

    logger.info(
        f"MCP request from user {current_user.username} (token: {token[:10]}...)"
    )

    # Check if user has tracker(s)
    user_has_tracker = has_tracker(current_user, db)

    # For Phase 1A: Return all default tools if user has tracker
    # Phase 1B will implement granular tool configuration from database
    enabled_default_tools = []
    if user_has_tracker:
        # Empty list means "all default tools" in Phase 1A
        enabled_default_tools = []

    enabled_proxied_tools = []

    user_context = {
        "user_id": str(current_user.id),
        "account_id": str(current_user.id),  # For Phase 1A, account_id = user_id
        "username": current_user.username,
        "has_tracker": user_has_tracker,
        "enabled_default_tools": enabled_default_tools,
        "enabled_proxied_tools": enabled_proxied_tools,
    }

    logger.info(
        f"User context loaded for {current_user.username}: "
        f"has_tracker={user_has_tracker}"
    )

    return user_context


async def mcp_http_streaming_endpoint(
    request: Request, user_context: dict = Depends(get_user_context_for_mcp)
) -> Response:
    """MCP HTTP streaming endpoint with authentication.

    This endpoint handles MCP protocol requests over HTTP streaming.
    It integrates with the DynamicMCPServer and injects user context
    into each request for per-user tool filtering.

    All requests require authentication via Bearer token.

    Args:
        request: FastAPI request
        user_context: User context from authentication

    Returns:
        HTTP streaming response with MCP protocol data
    """
    server = get_mcp_server()

    # Read request body
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse request body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")

    # Extract MCP method
    method = body.get("method")

    # Handle initialize method
    if method == "initialize":
        logger.info(f"Handling initialize request for user {user_context['username']}")
        response_data = {
            "jsonrpc": "2.0",
            "id": body.get("id", 1),
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "preloop-mcp",
                    "version": "1.0.0",
                },
            },
        }
        return Response(
            content=json.dumps(response_data),
            media_type="application/json",
        )

    # Handle notifications/initialized
    if method == "notifications/initialized":
        logger.info(
            f"Handling notifications/initialized for user {user_context['username']}"
        )
        # MCP notifications are fire-and-forget, return 204 No Content
        return Response(status_code=204)

    if method == "tools/list":
        # Get tools for user
        # We need to inject user_context into server.request_context
        # This is a simplified implementation for Phase 1A
        # Full implementation will use StreamableHTTPSessionManager

        logger.info(f"Handling tools/list request for user {user_context['username']}")

        try:
            # Get tools with user context
            from preloop.services.dynamic_mcp_server import UserContext

            user_ctx = UserContext(
                user_id=user_context["user_id"],
                account_id=user_context["account_id"],
                username=user_context["username"],
                has_tracker=user_context["has_tracker"],
                enabled_default_tools=user_context["enabled_default_tools"],
                enabled_proxied_tools=user_context["enabled_proxied_tools"],
            )

            tools = server._get_tools_for_user(user_ctx)

            # Convert tools to MCP format
            tools_list = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                }
                for tool in tools
            ]

            response_data = {
                "jsonrpc": "2.0",
                "id": body.get("id", 1),
                "result": {"tools": tools_list},
            }

            logger.info(
                f"Returning {len(tools_list)} tools for user {user_context['username']}"
            )

            return Response(
                content=json.dumps(response_data),
                media_type="application/json",
            )

        except Exception as e:
            logger.error(f"Error handling tools/list: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Internal server error: {str(e)}"
            )

    elif method == "tools/call":
        # Handle tool call
        logger.info(f"Handling tools/call request for user {user_context['username']}")

        tool_name = body.get("params", {}).get("name")
        tool_args = body.get("params", {}).get("arguments", {})

        if not tool_name:
            raise HTTPException(status_code=400, detail="Missing tool name in request")

        try:
            # Get user context object
            from preloop.services.dynamic_mcp_server import UserContext

            user_ctx = UserContext(
                user_id=user_context["user_id"],
                account_id=user_context["account_id"],
                username=user_context["username"],
                has_tracker=user_context["has_tracker"],
                enabled_default_tools=user_context["enabled_default_tools"],
                enabled_proxied_tools=user_context["enabled_proxied_tools"],
            )

            # Check access
            available_tools = server._get_tools_for_user(user_ctx)
            if not any(tool.name == tool_name for tool in available_tools):
                logger.warning(
                    f"User {user_context['username']} attempted to call "
                    f"unauthorized tool: {tool_name}"
                )
                response_data = {
                    "jsonrpc": "2.0",
                    "id": body.get("id", 1),
                    "error": {
                        "code": -32000,
                        "message": f"Access denied: Tool '{tool_name}' is not available",
                    },
                }
                return Response(
                    content=json.dumps(response_data),
                    media_type="application/json",
                )

            # Check if tool requires approval
            approval_required = await server._check_approval_required(
                user_ctx, tool_name
            )

            if approval_required:
                logger.info(
                    f"Tool {tool_name} requires approval - initiating approval flow"
                )
                try:
                    # Wait for approval
                    await server._request_and_wait_for_approval(
                        user_ctx, tool_name, tool_args
                    )
                    logger.info(
                        f"Tool {tool_name} approved - proceeding with execution"
                    )
                except TimeoutError as e:
                    logger.warning(
                        f"Approval timeout for tool {tool_name}: {e}", exc_info=True
                    )
                    response_data = {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "error": {
                            "code": -32000,
                            "message": "Approval timed out",
                        },
                    }
                    return Response(
                        content=json.dumps(response_data),
                        media_type="application/json",
                    )
                except PermissionError as e:
                    logger.warning(
                        f"Approval declined for tool {tool_name}: {e}", exc_info=True
                    )
                    response_data = {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "error": {
                            "code": -32000,
                            "message": "Approval declined",
                        },
                    }
                    return Response(
                        content=json.dumps(response_data),
                        media_type="application/json",
                    )
                except Exception as e:
                    logger.error(
                        f"Approval flow error for tool {tool_name}: {e}", exc_info=True
                    )
                    response_data = {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "error": {
                            "code": -32000,
                            "message": "Approval request failed",
                        },
                    }
                    return Response(
                        content=json.dumps(response_data),
                        media_type="application/json",
                    )

            # Execute tool
            handler = server._tool_handlers.get(tool_name)
            if not handler:
                logger.error(f"No handler found for tool: {tool_name}")
                response_data = {
                    "jsonrpc": "2.0",
                    "id": body.get("id", 1),
                    "error": {
                        "code": -32000,
                        "message": f"Handler not found for tool '{tool_name}'",
                    },
                }
                return Response(
                    content=json.dumps(response_data),
                    media_type="application/json",
                )

            from preloop.utils.redaction import redact_for_log

            logger.info(
                f"Executing tool {tool_name} for user {user_context['username']} "
                f"with args: {redact_for_log(tool_args)}"
            )

            # Call handler
            result = await handler(**tool_args)

            # Convert result to MCP format
            result_text = (
                result.model_dump_json()
                if hasattr(result, "model_dump_json")
                else str(result)
            )

            response_data = {
                "jsonrpc": "2.0",
                "id": body.get("id", 1),
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": result_text,
                        }
                    ]
                },
            }

            return Response(
                content=json.dumps(response_data),
                media_type="application/json",
            )

        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            response_data = {
                "jsonrpc": "2.0",
                "id": body.get("id", 1),
                "error": {
                    "code": -32000,
                    "message": "Tool execution failed",
                },
            }
            return Response(
                content=json.dumps(response_data),
                media_type="application/json",
            )

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")


# Global MCP app and lifespan manager
_mcp_app = None
_mcp_lifespan_manager = None


def setup_mcp_routes(app: FastAPI):
    """Set up MCP routes in the FastAPI application with StreamableHTTP transport.

    This uses DynamicFastMCP (FastMCP extension) with StreamableHTTP transport
    and per-user tool filtering.

    Args:
        app: FastAPI application instance
    """
    global _mcp_app, _mcp_lifespan_manager

    from preloop.services.initialize_mcp import initialize_mcp_with_tools
    from preloop.services.dynamic_fastmcp_http import setup_dynamic_mcp_http

    # Initialize DynamicFastMCP with all tools registered
    mcp = initialize_mcp_with_tools()
    logger.info("DynamicFastMCP initialized with tools")

    # Set up StreamableHTTP transport with authentication
    mcp_app = setup_dynamic_mcp_http(mcp)
    logger.info("StreamableHTTP transport configured")

    # Store the MCP app
    _mcp_app = mcp_app

    # The mcp_app returned by setup_dynamic_mcp_http is wrapped with middleware
    # We need to access the underlying Starlette app's lifespan
    # The base app is stored as context_app.app -> base_app
    # Let's unwrap to find the StarletteWithLifespan
    base_app = mcp_app
    while hasattr(base_app, "app"):
        base_app = base_app.app
        if hasattr(base_app, "lifespan"):
            _mcp_lifespan_manager = base_app.lifespan(base_app)
            logger.info("MCP lifespan manager stored")
            break
    else:
        logger.warning("Could not find MCP app with lifespan")

    # Wrap the MCP app so it also handles / → /v1.
    # After Mount strips /mcp, requests to /mcp/ arrive as path=/.
    # Without this wrapper the MCP app (which only serves /v1) returns 404.
    wrapped_mcp_app = _MCPRootPathHandler(mcp_app)

    # Mount at /mcp — the MCP endpoint is at /mcp/v1,
    # and /mcp/ also works thanks to the wrapper above.
    # For exact /mcp (no slash), MCPPathRewriteMiddleware in app.py
    # rewrites to /mcp/v1 before the router sees it.
    app.mount("/mcp", wrapped_mcp_app)

    logger.info("MCP StreamableHTTP transport mounted at /mcp")


def get_mcp_lifespan_manager():
    """Get the MCP lifespan manager for integration with FastAPI lifespan.

    Returns:
        Async context manager for MCP lifespan, or None if not available
    """
    return _mcp_lifespan_manager
