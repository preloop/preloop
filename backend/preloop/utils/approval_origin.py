"""Display the recorded originating session and turn model without guessing."""

from typing import Any, Optional


#: Server-set marker naming whether a hook-recorded approval is for a native
#: harness tool (``"native"``) or an MCP tool the harness observed (``"mcp"``).
#: Callers cannot set it: the permission-check endpoint and the MCP approval
#: path strip it from caller input before anything is stored.
TOOL_ORIGIN_MARKER = "_preloop_tool_origin"


HOOK_ONLY_MARKERS = ("_preloop_origin", "_preloop_source", TOOL_ORIGIN_MARKER)


def strip_hook_markers(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop the keys only the permission hook may store on an approval.

    An MCP tool call goes through ``require_approval``; its arguments come
    from the caller, so these markers are removed before anything is stored.
    Otherwise a caller could make an MCP approval look like a native one.

    Args:
        arguments: Caller-supplied tool arguments.

    Returns:
        A copy without the hook-only markers.
    """
    return {k: v for k, v in arguments.items() if k not in HOOK_ONLY_MARKERS}


def is_native_tool_approval(tool_args: Any) -> bool:
    """Whether an approval was raised by a harness hook for a native tool.

    Only the server-set :data:`TOOL_ORIGIN_MARKER` decides. ``_preloop_source``
    and ``_preloop_origin`` are not used: a hook stamps them on MCP-origin
    checks too, and an approval of an MCP tool must still be replayed.

    Args:
        tool_args: The approval request's stored ``tool_args``.

    Returns:
        True when the approval belongs to a native harness tool call.
    """
    if not isinstance(tool_args, dict):
        return False
    return tool_args.get(TOOL_ORIGIN_MARKER) == "native"


def native_tool_approval_note(tool_name: str) -> str:
    """Explain what an approved native tool request means for the agent.

    Args:
        tool_name: The native tool the approval names.

    Returns:
        One paragraph for the ``get_approval_status`` response.
    """
    return (
        f"'{tool_name}' is a native tool of the agent harness, not a Preloop "
        "MCP tool, so Preloop recorded the decision but cannot run the call. "
        "The harness does not resume a native call after its permission hook "
        "has returned: issue the call again in the session. It is checked "
        "against the rules again when it is issued."
    )


def approval_origin_text(
    tool_args: dict[str, Any], runtime_session_id: Optional[str] = None
) -> Optional[str]:
    """Format known origin context; shared credential sessions never replace it."""
    raw = tool_args.get("_preloop_origin")
    origin = raw if isinstance(raw, dict) else {}
    native = bool(tool_args.get("_preloop_source")) or bool(origin)
    if not native and not runtime_session_id:
        return None
    session = origin.get("session_id") if native else runtime_session_id
    model = origin.get("model")
    session = session.strip() if isinstance(session, str) else ""
    model = model.strip() if isinstance(model, str) else ""
    if len(session) > 16:
        session = f"{session[:8]}…{session[-8:]}"
    return f"Session: {session or 'Unknown'} · Model: {model or 'Unknown'}"
