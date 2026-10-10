"""Route limits for API keys whose scopes are MCP scopes only.

Flow execution credentials (``create_flow_runtime_token``), runtime session
tokens and managed-agent credentials are minted with ``mcp:read`` and
``mcp:write``. Such a key is meant for the MCP endpoint and for the runtime
routes that verify their own credentials (model gateway, agent control
WebSocket, native permission checks, operator note pull, browser steps and
artifact capabilities). None of those go through the generic REST
dependency, so on that dependency an MCP-only key is denied unless the route
is on :data:`MCP_ONLY_KEY_ALLOWED_PATH_PREFIXES`.

A device-scoped key is the same idea for opt-in discovery reporting: a key
whose only scope is ``report_discovery`` may reach the discovery salt and
report routes and nothing else, so an MDM job holding it cannot read or
change the account. The MCP bearer backend and the model gateway do not
use this REST dependency, so they refuse a device-scoped key themselves.

A telemetry ingest key (scopes exactly ``telemetry:ingest``, issue #1412) is
the same idea for the OTLP/HTTP receiver: it is the credential a Claude apps
gateway's ``telemetry.forward_to`` destination carries, so it may post OTLP
exports under ``/api/v1/telemetry/otlp/`` and reach nothing else. The MCP
backend and the model gateway refuse it through
:func:`is_single_purpose_api_key`.

Keys without scopes (personal API keys) and keys with any other scope keep
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

#: Scope (and RBAC permission name) for opt-in discovery reporting.
DISCOVERY_REPORT_SCOPE = "report_discovery"

#: Exact REST paths a device-scoped (``report_discovery`` only) key may call.
DEVICE_KEY_ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/agents/discovery-salt",
        "/api/v1/agents/discovery-reports",
    }
)


#: Scope for OTLP/HTTP telemetry ingest (issue #1412). Admin-only to grant.
TELEMETRY_INGEST_SCOPE = "telemetry:ingest"

#: REST path prefix a telemetry ingest key may call.
TELEMETRY_KEY_ALLOWED_PATH_PREFIX = "/api/v1/telemetry/otlp/"


def is_telemetry_ingest_api_key(api_key: Any) -> bool:
    """Return True when the key's scopes are exactly the telemetry scope.

    Args:
        api_key: API key record (or any object with a ``scopes`` attribute).

    Returns:
        True for a non-empty scope list made only of ``telemetry:ingest``.
    """
    scopes = getattr(api_key, "scopes", None)
    if not isinstance(scopes, (list, tuple)) or not scopes:
        return False
    return all(scope == TELEMETRY_INGEST_SCOPE for scope in scopes)


def is_single_purpose_api_key(api_key: Any) -> bool:
    """Return True for keys limited to one narrow REST surface.

    Device-scoped (``report_discovery``) and telemetry ingest keys. Surfaces
    that do not use the REST dependency (MCP, model gateway) refuse them.

    Args:
        api_key: API key record, or None.

    Returns:
        True when the key must be refused outside its own routes.
    """
    return is_device_scoped_api_key(api_key) or is_telemetry_ingest_api_key(api_key)


def is_device_scoped_api_key(api_key: Any) -> bool:
    """Return True when the key's scopes are exactly the discovery scope.

    Args:
        api_key: API key record (or any object with a ``scopes`` attribute).

    Returns:
        True for a non-empty scope list made only of ``report_discovery``.
    """
    scopes = getattr(api_key, "scopes", None)
    if not isinstance(scopes, (list, tuple)) or not scopes:
        return False
    return all(scope == DISCOVERY_REPORT_SCOPE for scope in scopes)


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
    if is_device_scoped_api_key(api_key):
        _enforce_device_key_route(api_key, request)
        return
    if is_telemetry_ingest_api_key(api_key):
        _enforce_telemetry_key_route(api_key, request)
        return
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


def _enforce_device_key_route(api_key: Any, request: Optional[Request]) -> None:
    """Deny a device-scoped key anywhere but the discovery reporting routes.

    Not subject to the MCP enforcement mode: the device scope is new, so
    there is no existing integration that audit mode would protect.

    Raises:
        HTTPException: 403 with ``detail.code`` ``api_key_scope_denied``.
    """
    path = request.url.path.rstrip("/") if request is not None else None
    if path in DEVICE_KEY_ALLOWED_PATHS:
        return
    logger.info(
        "Denied device-scoped API key %s on %s",
        getattr(api_key, "id", None),
        path or "a call without request",
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": API_KEY_SCOPE_DENIED,
            "message": (
                "This API key is limited to discovery reporting and cannot "
                "call this endpoint."
            ),
        },
    )


def _enforce_telemetry_key_route(api_key: Any, request: Optional[Request]) -> None:
    """Deny a telemetry ingest key anywhere but the OTLP receiver.

    Raises:
        HTTPException: 403 with ``detail.code`` ``api_key_scope_denied``.
    """
    path = request.url.path if request is not None else None
    if path is not None and path.startswith(TELEMETRY_KEY_ALLOWED_PATH_PREFIX):
        return
    logger.info(
        "Denied telemetry ingest API key %s on %s",
        getattr(api_key, "id", None),
        path or "a call without request",
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": API_KEY_SCOPE_DENIED,
            "message": (
                "This API key is limited to telemetry ingest and cannot call "
                "this endpoint."
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
    if is_single_purpose_api_key(api_key):
        return False
    return not _mode_denies(api_key, channel)
