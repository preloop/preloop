"""Identity headers Preloop sends to upstream services.

One place for the User-Agent value and the header names Preloop puts on
outbound requests, so the model path and the upstream MCP path identify the
same way. The headers are self-asserted: they let a server operator see that
a request came through Preloop and join their own access record with the
Preloop audit row for the same call. They are not an attestation.
"""

import hashlib
import logging
import os
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Header carrying the Preloop product token.
USER_AGENT_HEADER = "User-Agent"
#: Header carrying the per-call correlation id written to the audit rows.
CORRELATION_ID_HEADER = "X-Preloop-Correlation-Id"
#: Opt-in header carrying a stable, non-secret per-installation id.
INSTANCE_HEADER = "X-Preloop-Instance"

#: ``auth_config`` key; ``False`` removes every identity header for a server.
SEND_IDENTITY_HEADERS_KEY = "send_identity_headers"
#: ``auth_config`` key; ``True`` adds :data:`INSTANCE_HEADER` for a server.
SEND_INSTANCE_ID_KEY = "send_instance_id"

#: Environment override for the instance id (for example, several replicas
#: that should present one id, or an operator-chosen label).
INSTANCE_ID_ENV = "PRELOOP_INSTANCE_ID"

_INSTANCE_ID_LABEL = b"preloop-upstream-instance-id:v1:"
_instance_id_cache: Optional[str] = None


def preloop_user_agent() -> str:
    """Return the Preloop User-Agent, ``Preloop/<version>``.

    Used on model-provider completions and on upstream MCP requests.
    """
    from preloop import __version__

    return f"Preloop/{__version__}"


def derive_instance_id(instance_uuid: str) -> str:
    """Derive the public instance id from the installation UUID.

    The installation UUID is also used by the version check, so it is never
    sent to MCP servers directly. A one-way, domain-separated hash keeps the
    value stable across restarts without exposing the UUID.

    Args:
        instance_uuid: The ``instances.instance_uuid`` of this installation.

    Returns:
        A 32-character hex identifier.
    """
    digest = hashlib.sha256(_INSTANCE_ID_LABEL + instance_uuid.encode("utf-8"))
    return digest.hexdigest()[:32]


def instance_id() -> Optional[str]:
    """Return this installation's stable instance id, or ``None``.

    Reads :data:`INSTANCE_ID_ENV` first, then derives the id from the
    installation record created at first start. The value is cached for the
    process and never logged.

    Returns:
        The instance id, or ``None`` when no installation record is available.
    """
    global _instance_id_cache
    override = os.getenv(INSTANCE_ID_ENV, "").strip()
    if override:
        return override
    if _instance_id_cache is not None:
        return _instance_id_cache
    from preloop.services.instance_service import get_or_create_instance

    instance = get_or_create_instance()
    if instance is None:
        return None
    _instance_id_cache = derive_instance_id(str(instance.instance_uuid))
    return _instance_id_cache


def identity_headers_enabled(auth_config: Optional[Mapping[str, Any]]) -> bool:
    """Whether a server receives identity headers (default ``True``)."""
    return (auth_config or {}).get(SEND_IDENTITY_HEADERS_KEY) is not False


def instance_header_enabled(auth_config: Optional[Mapping[str, Any]]) -> bool:
    """Whether a server opted in to :data:`INSTANCE_HEADER` (default ``False``)."""
    config = auth_config or {}
    return identity_headers_enabled(config) and config.get(SEND_INSTANCE_ID_KEY) is True


def upstream_mcp_identity_headers(
    auth_config: Optional[Mapping[str, Any]],
    *,
    correlation_id: Optional[str] = None,
    instance: Optional[str] = None,
) -> dict[str, str]:
    """Build the identity headers for one upstream MCP session.

    Only these headers are produced; incoming client headers are never
    forwarded.

    Args:
        auth_config: The server's ``auth_config`` (opt-out and opt-in keys).
        correlation_id: The audit correlation id of the call, if any.
        instance: The instance id; sent only when the server opted in.

    Returns:
        Header name to value. Empty when the server opted out.
    """
    if not identity_headers_enabled(auth_config):
        return {}
    headers = {USER_AGENT_HEADER: preloop_user_agent()}
    if correlation_id:
        headers[CORRELATION_ID_HEADER] = correlation_id
    if instance and instance_header_enabled(auth_config):
        headers[INSTANCE_HEADER] = instance
    return headers
