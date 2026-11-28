"""Display the recorded originating session and turn model without guessing."""

from typing import Any, Optional


def is_native_tool_approval(tool_args: Any) -> bool:
    """Whether an approval was raised by a harness hook for a native tool.

    The permission-check endpoint stamps ``_preloop_source`` and/or
    ``_preloop_origin`` on the tool input of every native call it records;
    MCP tool approvals never carry either key.

    Args:
        tool_args: The approval request's stored ``tool_args``.

    Returns:
        True when the approval belongs to a native harness tool call.
    """
    if not isinstance(tool_args, dict):
        return False
    return bool(tool_args.get("_preloop_source")) or isinstance(
        tool_args.get("_preloop_origin"), dict
    )


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
