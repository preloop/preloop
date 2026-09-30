"""Display the recorded originating session and turn model without guessing."""

from typing import Any, Optional


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
