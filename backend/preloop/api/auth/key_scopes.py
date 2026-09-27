"""Route limits for API keys whose scopes are MCP scopes only.

Flow execution credentials (``create_flow_runtime_token``), runtime session
tokens and managed-agent credentials are minted with ``mcp:read`` and
``mcp:write``. Such a key is meant for the MCP endpoint and for the runtime
routes that verify their own credentials (model gateway, agent control
WebSocket, native permission checks, operator note pull, browser steps and
artifact capabilities). None of those go through the generic REST
dependency, so on that dependency an MCP-only key is denied unless the route
is on :data:`MCP_ONLY_KEY_ALLOWED_PATH_PREFIXES`.

Keys without scopes (personal API keys) and keys with any non-MCP scope keep
their previous behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException, Request, status

from preloop.config import settings

logger = logging.getLogger(__name__)

MCP_SCOPE_PREFIX = "mcp:"

#: Error code returned in ``detail.code`` when a key's scopes do not cover
#: the route.
API_KEY_SCOPE_DENIED = "api_key_scope_denied"

#: REST path prefixes an MCP-only key may reach through the generic
#: dependency. The MCP transport under ``/mcp/`` authenticates itself today;
#: the prefix is listed so a FastAPI route added under it stays reachable.
MCP_ONLY_KEY_ALLOWED_PATH_PREFIXES: tuple[str, ...] = ("/mcp/",)


def is_mcp_only_api_key(api_key: Any) -> bool:
    """Return True when every scope on the key is an MCP scope.

    Args:
        api_key: API key record (or any object with a ``scopes`` attribute).

    Returns:
        True for a non-empty scope list made only of ``mcp:*`` strings.
    """
    scopes = getattr(api_key, "scopes", None)
    if not isinstance(scopes, (list, tuple)) or not scopes:
        return False
    return all(
        isinstance(scope, str) and scope.startswith(MCP_SCOPE_PREFIX)
        for scope in scopes
    )


def _route_allowed(path: Optional[str]) -> bool:
    if not path:
        return False
    return any(path.startswith(prefix) for prefix in MCP_ONLY_KEY_ALLOWED_PATH_PREFIXES)


def _mode_denies(api_key: Any, where: str) -> bool:
    """Apply the enforcement mode to an MCP-only key on a surface it may not use.

    This is the one place that reads ``api_key_scope_enforcement``, so the
    REST dependency and the console channels cannot drift apart.

    Args:
        api_key: The authenticated API key, or None for JWT sessions.
        where: Surface description for the log line (method and path, or a
            channel name).

    Returns:
        True when the caller must refuse the key.
    """
    mode = getattr(settings, "api_key_scope_enforcement", "enforce")
    if mode == "off" or not is_mcp_only_api_key(api_key):
        return False
    if mode == "audit":
        logger.warning(
            "MCP-scoped API key %s (%s) used on %s; allowed by audit mode",
            getattr(api_key, "id", None),
            getattr(api_key, "name", None),
            where,
        )
        return False
    logger.info(
        "Denied MCP-scoped API key %s on %s", getattr(api_key, "id", None), where
    )
    return True


def enforce_api_key_route_scope(api_key: Any, request: Optional[Request]) -> None:
    """Deny an MCP-only key on a REST route its scopes do not cover.

    Callers that authenticate without a request (optional identity lookups)
    cannot name the route, so they are denied too: the key then reads as an
    invalid credential to them, which is the safe outcome.

    Args:
        api_key: The authenticated API key.
        request: The current HTTP request, if the caller has one.

    Raises:
        HTTPException: 403 with ``detail.code`` ``api_key_scope_denied``.
    """
    if not is_mcp_only_api_key(api_key):
        # Checked before touching the request, so other keys pay nothing.
        return
    path = request.url.path if request is not None else None
    if _route_allowed(path):
        return
    where = (
        f"{request.method} {path}" if request is not None else "a call without request"
    )
    if not _mode_denies(api_key, where):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": API_KEY_SCOPE_DENIED,
            "message": (
                "This API key is limited to MCP scopes and cannot call this "
                "endpoint. Use a personal API key or a console session."
            ),
        },
    )


def api_key_allowed_on_channel(api_key: Any, channel: str) -> bool:
    """Return whether a key may open a console channel that is not REST.

    Console WebSockets stream account events. MCP-only keys have no business
    there, so they get the same treatment as on a REST route outside the
    allow list: refused under ``enforce``, logged and allowed under ``audit``.

    Args:
        api_key: The authenticated API key, or None for JWT sessions.
        channel: Channel name used in the log line.

    Returns:
        False when the key must be refused.
    """
    return not _mode_denies(api_key, channel)
