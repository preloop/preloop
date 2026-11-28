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


def hook_recorded_tool_origin(tool_args: Any) -> Optional[str]:
    """Origin of an approval the harness permission hook recorded, if any.

    A hook-recorded approval is a decision for the harness, not a call
    Preloop can run (#2595): the hook has returned by the time a late
    decision lands, and its stored arguments carry hook bookkeeping. The
    server-set :data:`TOOL_ORIGIN_MARKER` names the origin. Rows recorded
    before the marker existed are recognised by the hook's own
    ``_preloop_source`` / ``_preloop_origin`` stamps and reported as
    ``"native"``; the MCP approval path strips all three keys from caller
    input, so its rows never match.

    Args:
        tool_args: The approval request's stored ``tool_args``.

    Returns:
        ``"native"`` or ``"mcp"`` for a hook-recorded approval, else ``None``.
    """
    if not isinstance(tool_args, dict):
        return None
    marker = tool_args.get(TOOL_ORIGIN_MARKER)
    if marker in ("native", "mcp"):
        return str(marker)
    if tool_args.get("_preloop_source") or isinstance(
        tool_args.get("_preloop_origin"), dict
    ):
        return "native"
    return None


def hook_recorded_approval_note(tool_name: str, origin: str) -> str:
    """Explain what an approved hook-recorded request means for the agent.

    Args:
        tool_name: The tool the approval names.
        origin: ``"native"`` or ``"mcp"`` (see :func:`hook_recorded_tool_origin`).

    Returns:
        One paragraph for the ``get_approval_status`` response.
    """
    kind = (
        "a native tool of the agent harness"
        if origin == "native"
        else "an MCP tool call the agent harness reported"
    )
    return (
        f"'{tool_name}' is {kind}. Its permission hook recorded this "
        "request, so Preloop recorded the decision but does not run the "
        "call. The harness does not resume a call after its permission hook "
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
