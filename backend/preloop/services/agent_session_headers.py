"""Agent-native session-id resolution for the model gateways.

Preloop's runtime sessions are keyed on the credential's runtime principal,
which for every CLI agent Preloop enrolls is **durable and static** — one
machine-scoped id minted once at ``preloop agents enroll`` time. The per-run
split therefore depends entirely on a per-conversation id supplied with the
request. Historically the only such id was Preloop's own
``X-Preloop-Session-Id``, which no third-party agent sends, so every
conversation on a machine collapsed onto a single runtime session row that never
ended.

Several agents *do* identify their conversation on the wire; Preloop simply was
not reading it. This module centralises that reading for the OpenAI-shaped
ingress, where two different agents use two different (and, in one case,
dangerously generic) header names:

* **Codex** — ``Session-Id`` and ``Thread-Id`` (both the conversation uuid; it
  is the rollout filename Codex writes and the ``session id:`` it prints).
  ``x-client-request-id`` looks similar but is per REQUEST, and
  ``installation_id`` inside ``x-codex-turn-metadata`` is per INSTALL — keying
  on either would reproduce the very bug this fixes, so neither is read.
* **OpenCode** — ``X-Session-Id`` (and the identical ``x-session-affinity``).

Security: unlike ``X-Claude-Code-Session-Id``, the header names above are
**generic**. ``Session-Id``/``X-Session-Id`` is a name any reverse proxy, load
balancer, CDN, or unrelated client may stamp for its own purposes. Reading them
unconditionally would let an intermediary silently drive Preloop's session
identity — and, because session boundaries are never re-derivable after the
fact, a wrong boundary is permanent. So every agent-native header is **gated on
the credential's own runtime-principal type**: we trust ``Session-Id`` only when
the credential says the caller is Codex, and ``X-Session-Id`` only when it says
OpenCode. A credential cannot be forged, and the gate fails closed to the
pre-existing source-keyed behavior.

Precedence, highest first:

1. ``X-Preloop-Session-Id`` — explicit, ours, always wins (back-compat).
2. Vendor session header, gated on the principal type (this module).
3. Body-level conversation id — ``prompt_cache_key`` (OpenAI) /
   ``metadata.user_id`` (Anthropic); applied later, in the gateway service.
4. Nothing: source keying, bounded by the inactivity closer.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# Header names each agent uses for its own conversation id, keyed by the
# ``runtime_principal.type`` recorded on the credential. Order within a tuple is
# the precedence used when an agent sends more than one.
_NATIVE_SESSION_HEADERS: Dict[str, tuple[str, ...]] = {
    # Codex sends the same uuid on both; Session-Id is its primary name.
    "codex": ("session-id", "thread-id"),
    # OpenCode sends x-session-id and x-session-affinity with identical values.
    "opencode": ("x-session-id", "x-session-affinity"),
}


def runtime_principal_type(auth_context: Any) -> Optional[str]:
    """Return the runtime-principal type recorded on the request credential.

    Args:
        auth_context: The authenticated model-gateway context.

    Returns:
        The lowercased principal type (e.g. ``"codex"``), or ``None`` when the
        request is not on a runtime credential.
    """
    api_key = getattr(auth_context, "api_key", None)
    if api_key is None:
        return None
    context_data = getattr(api_key, "context_data", None)
    if not isinstance(context_data, dict):
        return None
    runtime_principal = context_data.get("runtime_principal")
    if not isinstance(runtime_principal, dict):
        return None
    principal_type = runtime_principal.get("type")
    if not isinstance(principal_type, str):
        return None
    return principal_type.strip().lower() or None


def native_session_id_from_headers(
    headers: Optional[Mapping[str, str]],
    *,
    auth_context: Any,
) -> Optional[str]:
    """Read the agent's own conversation id from the request headers.

    Only headers belonging to the agent the *credential* identifies are read, so
    an unrelated intermediary that stamps a generic ``X-Session-Id`` can never
    drive session identity (see the module docstring).

    Args:
        headers: The incoming request headers (case-insensitive mapping).
        auth_context: The authenticated model-gateway context, used to resolve
            which agent this credential belongs to.

    Returns:
        The raw header value to use as the per-run session id, or ``None``. The
        value is NOT validated here; the gateway service normalizes it through
        the same charset/length rules as ``X-Preloop-Session-Id``, so a hostile
        value degrades to source keying rather than reaching the database.
    """
    if headers is None:
        return None
    principal_type = runtime_principal_type(auth_context)
    if not principal_type:
        return None
    for header_name in _NATIVE_SESSION_HEADERS.get(principal_type, ()):
        value = headers.get(header_name)
        if isinstance(value, str) and value.strip():
            return value
    return None
