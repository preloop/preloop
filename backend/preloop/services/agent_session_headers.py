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

1. ``X-Preloop-Session-Id`` — explicit, ours, always wins (back-compat). On
   a plain API key it is also the *only* signal that opts the request into a
   runtime session at all: the vendor and body-level signals below are gated
   on the credential's principal type, which a plain key does not carry.
2. Vendor session header, gated on the principal type (this module).
3. Body-level conversation id — ``prompt_cache_key`` (OpenAI) /
   ``metadata.user_id`` (Anthropic); applied later, in the gateway service.
4. Nothing: source keying, bounded by the inactivity closer. For a plain API
   key with no explicit header this is the historical answer, and it now means
   no runtime session row is created.

Parent sessions
---------------

Some harnesses also say, on the wire, that a turn belongs to a subagent one of
their own sessions spawned. ``docs/guide/subagent-session-identity.md`` records
what each harness was observed to send; two of them are usable today:

* **OpenCode** gives the subagent its own ``X-Session-Id`` and names the parent
  explicitly in ``X-Parent-Session-Id``. The child's own id is unchanged, so
  only the parent has to be read.
* **Claude Code** keeps sending the *parent's* ``X-Claude-Code-Session-Id`` on a
  subagent turn and adds ``X-Claude-Code-Agent-Id``, which is stable for that
  subagent's lifetime and absent on the parent's own turns. The subagent
  therefore needs a composite id (``<session>:<agent>``) to get a row of its
  own, and its parent is the session the header already names.

Every other harness (Gemini CLI, and everything Preloop reads no per
conversation id for at all) produces no parent. That is a first-class answer
meaning "lineage unknown", not an error, and it is silent: nothing is logged
for a harness that simply has nothing to say.

``X-Parent-Session-Id`` is as generic a name as ``X-Session-Id``, so it gets
the same principal-type gate. ``X-Claude-Code-Agent-Id`` is vendor-namespaced
like ``X-Claude-Code-Session-Id``, which the Anthropic ingress already reads
ungated, so it is read on the same terms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

# Header naming the session that spawned this one, same gating rule as above.
# Codex is deliberately absent: its subagent path (`agent_name` inside
# X-Codex-Turn-Metadata) is a canonical task path that has never been observed
# for an actual child, so there is nothing here to key on yet.
_NATIVE_PARENT_SESSION_HEADERS: Dict[str, tuple[str, ...]] = {
    "opencode": ("x-parent-session-id",),
}

#: Vendor-namespaced header Claude Code stamps on a subagent's turns only.
CLAUDE_CODE_AGENT_ID_HEADER = "x-claude-code-agent-id"

# Per-run session id validation, shared by every path that can put a
# client-supplied value into a session key. The id maps to a LangGraph
# ``thread_id`` / wizard-emitted run id, so we accept a conservative,
# URL/identifier-safe charset and cap the length; anything else is ignored, so
# a malformed or hostile header never errors and simply falls back to source
# keying (and, for a parent, to no lineage at all).
SESSION_ID_MAX_LEN = 200
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")


@dataclass(frozen=True)
class SessionLineage:
    """A turn's own session id and, when derivable, its parent's.

    Attributes:
        session_id: The id this turn's runtime session should be keyed by, or
            ``None`` to fall back to source keying.
        parent_session_id: The id of the session that spawned it, or ``None``
            when the harness does not say (the normal case).
    """

    session_id: Optional[str] = None
    parent_session_id: Optional[str] = None


def normalize_session_id(raw: Optional[str]) -> Optional[str]:
    """Validate and normalize a client-supplied session id.

    Args:
        raw: Raw header or body value (may be ``None``).

    Returns:
        The trimmed id when it is non-empty, within the length cap, and uses
        only the safe charset; otherwise ``None``.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > SESSION_ID_MAX_LEN:
        return None
    if not _SESSION_ID_RE.match(candidate):
        return None
    return candidate


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


def native_parent_session_id_from_headers(
    headers: Optional[Mapping[str, str]],
    *,
    auth_context: Any,
) -> Optional[str]:
    """Read the id of the session that spawned this one, when the agent sends it.

    Gated exactly like :func:`native_session_id_from_headers`: the header is a
    generic name an intermediary could stamp, so it is trusted only for the
    agent the *credential* identifies. A harness that says nothing about
    lineage returns ``None``, which is the normal case and not an error.

    Args:
        headers: The incoming request headers (case-insensitive mapping).
        auth_context: The authenticated model-gateway context.

    Returns:
        The raw parent session id, or ``None``. Like the session id itself the
        value is normalized later, so a hostile value degrades to no lineage
        rather than reaching the database.
    """
    if headers is None:
        return None
    principal_type = runtime_principal_type(auth_context)
    if not principal_type:
        return None
    for header_name in _NATIVE_PARENT_SESSION_HEADERS.get(principal_type, ()):
        value = headers.get(header_name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def claude_code_session_lineage(
    session_id: Optional[str],
    agent_id: Optional[str],
) -> SessionLineage:
    """Split a Claude Code turn into its own session id and its parent's.

    Claude Code sends the *parent's* ``X-Claude-Code-Session-Id`` on a
    subagent's turns too, and distinguishes them only by adding
    ``X-Claude-Code-Agent-Id``. Keying the subagent on the session header alone
    would append its traffic to the parent's row; keying it on the agent id
    alone would lose which conversation it belongs to. So a subagent turn is
    keyed by both, and the parent is the session the header already names.

    A missing, malformed or oversized agent id is treated as "this is a parent
    turn": the session id is untouched and there is no lineage. That keeps a
    hostile header from costing a request its session identity.

    Args:
        session_id: Raw ``X-Claude-Code-Session-Id`` value (may be ``None``).
        agent_id: Raw ``X-Claude-Code-Agent-Id`` value (may be ``None``).

    Returns:
        The turn's :class:`SessionLineage`. Both fields are ``None`` when the
        session header itself is absent or unusable.
    """
    normalized_session_id = normalize_session_id(session_id)
    if normalized_session_id is None:
        return SessionLineage()
    normalized_agent_id = normalize_session_id(agent_id)
    if normalized_agent_id is None:
        return SessionLineage(session_id=normalized_session_id)
    subagent_session_id = normalize_session_id(
        f"{normalized_session_id}:{normalized_agent_id}"
    )
    if subagent_session_id is None:
        # Only the combined length cap can fail here, and a subagent whose key
        # does not fit stays on its parent's row rather than losing the run.
        return SessionLineage(session_id=normalized_session_id)
    return SessionLineage(
        session_id=subagent_session_id,
        parent_session_id=normalized_session_id,
    )
