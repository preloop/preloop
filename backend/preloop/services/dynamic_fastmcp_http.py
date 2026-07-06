"""HTTP transport setup for DynamicFastMCP with authentication and context injection.

This module sets up FastMCP's StreamableHTTP transport with middleware that injects
authenticated user context for per-request tool filtering.
"""

import inspect
import json
import logging
import os
from contextvars import ContextVar
from typing import Optional
from urllib.parse import urlparse

from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    UserContext,
    create_user_context_from_scope,
)
from preloop.services.mcp_http import PreloopBearerAuthBackend

logger = logging.getLogger(__name__)

# Context variable to store user context for the current request
_current_user_context: ContextVar[Optional[UserContext]] = ContextVar(
    "mcp_user_context", default=None
)


class UserContextMiddleware:
    """Middleware that extracts user context and stores it in a context variable.

    This middleware runs for each request and extracts the authenticated user's
    context, storing it in a context variable that the DynamicFastMCP server
    can access during tool listing and execution.

    Returns 401 Unauthorized when no valid auth token is provided, which
    triggers OAuth discovery in MCP clients (e.g. Claude Desktop).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        logger.info(f"UserContextMiddleware: Processing request to {scope.get('path')}")

        # Extract user context from authenticated scope
        user_context = create_user_context_from_scope(scope)

        if user_context:
            logger.info(
                f"Setting user context for {user_context.username}, "
                f"has_tracker={user_context.has_tracker}"
            )
        else:
            logger.warning("No user context available, returning 401")
            # Return 401 to trigger OAuth discovery in MCP clients
            base_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")
            resource_metadata_url = (
                f"{base_url}/.well-known/oauth-protected-resource/mcp"
            )
            response = Response(
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32001,
                            "message": "Authentication required. Provide Authorization: Bearer <token> header.",
                        },
                    }
                ),
                status_code=401,
                media_type="application/json",
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{resource_metadata_url}"'
                    ),
                },
            )
            await response(scope, receive, send)
            return

        # Store in context variable for this request's async context
        token = _current_user_context.set(user_context)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user_context.reset(token)


def mcp_http_allowed_hosts(preloop_url: str | None = None) -> list[str]:
    """Return Host header patterns trusted by FastMCP's request guard.

    FastMCP 3.4+ enables host/origin protection by default and only allows
    loopback hosts plus the ASGI server host. External requests proxied through
    nginx with ``Host: $host`` must be explicitly allowlisted.

    Args:
        preloop_url: Public Preloop base URL. Defaults to ``PRELOOP_URL`` env.

    Returns:
        Host patterns passed to ``http_app(allowed_hosts=...)``.
    """
    url = (
        preloop_url if preloop_url is not None else os.getenv("PRELOOP_URL", "")
    ).strip()
    if not url:
        return []

    hostname = urlparse(url).hostname
    if not hostname:
        return []

    hosts = [hostname]
    if hostname.endswith(".test.preloop.ai"):
        hosts.append("*.test.preloop.ai")
    return hosts


def mcp_http_allowed_origins(preloop_url: str | None = None) -> list[str]:
    """Return browser Origin values trusted by FastMCP's request guard."""
    url = (
        preloop_url if preloop_url is not None else os.getenv("PRELOOP_URL", "")
    ).strip()
    if not url:
        return []
    return [url.rstrip("/")]


def get_current_user_context() -> Optional[UserContext]:
    """Get the current user context from the context variable.

    This function is registered with DynamicFastMCP as the user context provider.
    It retrieves the user context that was set by UserContextMiddleware.

    Returns:
        UserContext if available, None otherwise
    """
    context = _current_user_context.get()
    if context:
        logger.debug(f"Retrieved user context for {context.username}")
    return context


def setup_dynamic_mcp_http(mcp: DynamicFastMCP):
    """Set up StreamableHTTP transport for DynamicFastMCP with authentication.

    This wraps FastMCP's built-in streamable_http_app with:
    1. Authentication middleware (validates Bearer tokens)
    2. User context middleware (extracts and stores user context)

    Args:
        mcp: DynamicFastMCP instance to set up

    Returns:
        ASGI app ready to handle MCP StreamableHTTP requests
    """
    # Register the user context provider with the MCP server
    mcp.set_user_context_provider(get_current_user_context)
    logger.info("Registered user context provider with DynamicFastMCP")

    # Get FastMCP's built-in HTTP app with StreamableHTTP transport
    # path="/v1" means it will serve on /v1 within the mounted app
    # So mounting at /mcp makes it available at /mcp/v1
    # NOTE: json_response must be None (not True) to allow SSE streaming for progress
    # NOTE: stateless_http=True prevents session state issues on server restart
    http_app_kwargs: dict[str, object] = {
        "path": "/v1",
        "transport": "streamable-http",
        "json_response": None,  # Allow SSE streaming for progress notifications
        "stateless_http": True,  # Don't maintain session state (allows clean reconnections)
    }
    http_app_params = inspect.signature(mcp.http_app).parameters
    allowed_hosts = mcp_http_allowed_hosts()
    if "allowed_hosts" in http_app_params and allowed_hosts:
        http_app_kwargs["allowed_hosts"] = allowed_hosts
    allowed_origins = mcp_http_allowed_origins()
    if "allowed_origins" in http_app_params and allowed_origins:
        http_app_kwargs["allowed_origins"] = allowed_origins

    base_app = mcp.http_app(**http_app_kwargs)
    logger.info(
        "Got FastMCP's http_app with streamable-http transport for path /v1 "
        f"(allowed_hosts={allowed_hosts or 'default'})"
    )

    # Wrap with our middleware layers
    # Layer 1: User context middleware (extracts and stores user context)
    context_app = UserContextMiddleware(base_app)

    # Layer 2: Authentication middleware (validates Bearer tokens)
    auth_app = AuthenticationMiddleware(
        context_app,
        backend=PreloopBearerAuthBackend(),
    )

    logger.info("DynamicFastMCP HTTP transport configured with authentication")
    return auth_app
