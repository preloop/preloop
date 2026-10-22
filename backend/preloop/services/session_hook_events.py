"""Prompt, turn-end and session-end timestamps from harness hooks (#1889).

The SessionStart/UserPromptSubmit, Stop and SessionEnd hooks already run on
every onboarded harness. This module persists *when* they fired as
``runtime_session_activity`` rows (``user_prompt``, ``turn_end``,
``session_end``) and announces them on the ``runtime_sessions`` realtime
topic. It never stores what was typed: the rows carry a kind, a time, the
source and the credential's user, nothing else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity
from preloop.models.models import RuntimeSession, RuntimeSessionActivity
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

PROMPT_HOOK_EVENTS = frozenset({"UserPromptSubmit", "beforeSubmitPrompt"})
TURN_END_HOOK_EVENTS = frozenset({"Stop", "SubagentStop", "stop"})
SESSION_END_HOOK_EVENTS = frozenset({"SessionEnd", "sessionEnd"})
TOOL_END_HOOK_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
NOTIFICATION_HOOK_EVENTS = frozenset({"Notification"})
#: Notification types that mean the harness is waiting for the operator.
WAITING_NOTIFICATION_TYPES = frozenset({"idle_prompt"})
#: Longest tool output preview kept on a ``tool_end`` row, after scrubbing.
TOOL_OUTPUT_PREVIEW_CHARS = 400

EVENT_PROMPT = "runtime_session_prompt"
EVENT_TURN_ENDED = "runtime_session_turn_ended"
EVENT_SESSION_ENDED = "runtime_session_hook_ended"
EVENT_TOOL_ENDED = "runtime_session_tool_ended"
EVENT_WAITING_FOR_INPUT = "runtime_session_waiting_for_input"


def is_prompt_event(hook_event_name: Optional[str]) -> bool:
    """Whether a session-start call came from a prompt submission."""
    return (hook_event_name or "").strip() in PROMPT_HOOK_EVENTS


def _metadata(
    *,
    source: Optional[str],
    hook_event_name: str,
    actor_user_id: Any,
    managed_agent_id: Any = None,
    subagent: Optional[bool] = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "source": (source or "").strip()[:64] or None,
        "hook_event_name": hook_event_name,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "managed_agent_id": str(managed_agent_id) if managed_agent_id else None,
    }
    if subagent is not None:
        data["subagent"] = subagent
    return data


def _emit(
    activity: RuntimeSessionActivity,
    session: Optional[RuntimeSession],
    event_type: str,
    actor_user_id: Any,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Announce one hook timestamp; realtime never fails the hook."""
    try:
        emit_account_event(
            build_account_event(
                account_id=str(activity.account_id),
                topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                event_type=event_type,
                payload={
                    "runtime_session_id": str(activity.runtime_session_id),
                    "runtime_principal_id": (
                        session.runtime_principal_id if session else None
                    ),
                    "actor_user_id": str(actor_user_id) if actor_user_id else None,
                    "timestamp": activity.timestamp.isoformat(),
                    "turn_state": session.turn_state if session else None,
                    **(extra or {}),
                },
                runtime_session_id=str(activity.runtime_session_id),
            )
        )
    except Exception:
        logger.warning("Failed to emit %s", event_type, exc_info=True)


def record_prompt(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    api_key_id: Any,
    actor_user_id: Any,
    source: Optional[str],
    hook_event_name: str,
    managed_agent_id: Any = None,
) -> Optional[RuntimeSessionActivity]:
    """Write one ``user_prompt`` row (no text) and announce it.

    Args:
        db: Database session.
        account_id: Owning account.
        runtime_session_id: Session the prompt went to.
        api_key_id: Hook credential.
        actor_user_id: The credential's user, the person who prompted.
        source: Agent adapter, e.g. ``claude_code``.
        hook_event_name: ``UserPromptSubmit`` or ``beforeSubmitPrompt``.
        managed_agent_id: The credential's managed agent, when it has one.

    Returns:
        The row, or None when it duplicated one within the dedupe window.
    """
    activity = crud_runtime_session_activity.log_hook_turn_event(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        activity_type="user_prompt",
        api_key_id=api_key_id,
        metadata=_metadata(
            source=source,
            hook_event_name=hook_event_name,
            actor_user_id=actor_user_id,
            managed_agent_id=managed_agent_id,
        ),
    )
    if activity is not None:
        _emit(
            activity,
            db.get(RuntimeSession, activity.runtime_session_id),
            EVENT_PROMPT,
            actor_user_id,
        )
    return activity


def _tool_end_details(details: dict[str, Any]) -> dict[str, Any]:
    """Keep the bounded tool-end fields; scrub and clip the output preview."""
    from preloop.utils.secret_scrubbing import scrub_secrets

    out: dict[str, Any] = {}
    for key, limit in (("tool_use_id", 200), ("tool_name", 100)):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:limit]
    exit_code = details.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        out["exit_code"] = exit_code
    duration = details.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        out["duration_ms"] = max(0, int(duration))
    is_error = details.get("is_error")
    if isinstance(is_error, bool):
        out["is_error"] = is_error
    preview = details.get("output_preview")
    if isinstance(preview, str) and preview:
        out["output_preview"] = (scrub_secrets(preview) or "")[
            :TOOL_OUTPUT_PREVIEW_CHARS
        ]
    return out


def record_session_event(
    db: Session,
    *,
    session: RuntimeSession,
    api_key_id: Any,
    actor_user_id: Any,
    source: Optional[str],
    hook_event_name: str,
    managed_agent_id: Any = None,
    details: Optional[dict[str, Any]] = None,
) -> Optional[RuntimeSessionActivity]:
    """Write a ``turn_end`` or ``session_end`` row for a resolved session.

    ``SessionEnd`` stamps ``ended_at`` once and records ``session_end`` only
    on the call that ended the session; it never re-opens one.

    Args:
        db: Database session.
        session: The runtime session the hook belongs to.
        api_key_id: Hook credential.
        actor_user_id: The credential's user.
        source: Agent adapter.
        hook_event_name: One of the Stop, SessionEnd, PostToolUse,
            PostToolUseFailure or Notification hook names.
        managed_agent_id: The credential's managed agent, when it has one.
        details: Optional hook fields: tool end details, the notification
            type, or ``interrupted`` for a Stop.

    Returns:
        The row, or None when it was a duplicate or carried nothing to record
        (a Notification that is not a wait for input).

    Raises:
        ValueError: For a hook name that is neither a turn end nor a session end.
    """
    now = datetime.now(timezone.utc)
    details = details or {}
    base = _metadata(
        source=source,
        hook_event_name=hook_event_name,
        actor_user_id=actor_user_id,
        managed_agent_id=managed_agent_id,
    )
    if hook_event_name in TOOL_END_HOOK_EVENTS:
        tool = _tool_end_details(details)
        if hook_event_name == "PostToolUseFailure":
            tool["is_error"] = True
        activity = crud_runtime_session_activity.log_hook_turn_event(
            db,
            account_id=session.account_id,
            runtime_session_id=session.id,
            activity_type="tool_end",
            api_key_id=api_key_id,
            timestamp=now,
            metadata={**base, **tool},
            # Tool calls end in quick succession; each one is its own row.
            dedupe_seconds=0,
        )
        if activity is not None:
            public = {k: v for k, v in tool.items() if k != "output_preview"}
            _emit(activity, session, EVENT_TOOL_ENDED, actor_user_id, public)
        return activity
    if hook_event_name in NOTIFICATION_HOOK_EVENTS:
        kind = details.get("notification_type")
        if kind not in WAITING_NOTIFICATION_TYPES:
            return None
        activity = crud_runtime_session_activity.log_hook_turn_event(
            db,
            account_id=session.account_id,
            runtime_session_id=session.id,
            activity_type="waiting_for_input",
            api_key_id=api_key_id,
            timestamp=now,
            metadata={**base, "notification_type": kind},
        )
        if activity is not None:
            _emit(
                activity,
                session,
                EVENT_WAITING_FOR_INPUT,
                actor_user_id,
                {"notification_type": kind},
            )
        return activity
    if hook_event_name in TURN_END_HOOK_EVENTS:
        interrupted = details.get("interrupted") is True
        activity = crud_runtime_session_activity.log_hook_turn_event(
            db,
            account_id=session.account_id,
            runtime_session_id=session.id,
            activity_type="turn_end",
            api_key_id=api_key_id,
            timestamp=now,
            metadata=_metadata(
                source=source,
                hook_event_name=hook_event_name,
                actor_user_id=actor_user_id,
                managed_agent_id=managed_agent_id,
                subagent=hook_event_name == "SubagentStop",
            )
            | ({"interrupted": True} if interrupted else {}),
        )
        if activity is not None:
            _emit(
                activity,
                session,
                EVENT_TURN_ENDED,
                actor_user_id,
                {"interrupted": True} if interrupted else None,
            )
        return activity
    if hook_event_name not in SESSION_END_HOOK_EVENTS:
        raise ValueError(f"Unsupported hook event: {hook_event_name}")
    activity, _ = end_session_by_hook(
        db,
        session=session,
        api_key_id=api_key_id,
        actor_user_id=actor_user_id,
        source=source,
        hook_event_name=hook_event_name,
        managed_agent_id=managed_agent_id,
    )
    return activity


def end_session_by_hook(
    db: Session,
    *,
    session: RuntimeSession,
    api_key_id: Any,
    actor_user_id: Any,
    source: Optional[str],
    hook_event_name: str = "SessionEnd",
    managed_agent_id: Any = None,
    reason: str = "session_end",
) -> tuple[Optional[RuntimeSessionActivity], bool]:
    """End a session from a SessionEnd hook, exactly once across routes.

    Both ``/agents/session-event`` and the notes ``/agents/session-end``
    route call this; the CLI posts them together. The end is one conditional
    UPDATE, and only the call that ended the session writes ``session_end``,
    enqueues ``session.ended`` and emits ``runtime_session_hook_ended``.

    Returns:
        The ``session_end`` row (None when another call ended the session)
        and whether this call ended it.
    """
    now = datetime.now(timezone.utc)
    _, ended = crud_runtime_session.mark_ended_by_hook(
        db,
        account_id=session.account_id,
        runtime_session_id=session.id,
        ended_at=now,
        commit=False,
    )
    if not ended:
        db.commit()
        return None, False
    activity = crud_runtime_session_activity.log_hook_turn_event(
        db,
        account_id=session.account_id,
        runtime_session_id=session.id,
        activity_type="session_end",
        api_key_id=api_key_id,
        timestamp=now,
        metadata=_metadata(
            source=source,
            hook_event_name=hook_event_name,
            actor_user_id=actor_user_id,
            managed_agent_id=managed_agent_id,
        ),
        commit=False,
    )
    from preloop.services.event_webhooks.emitters import emit_session_ended

    db.refresh(session)
    emit_session_ended(db, session, reason=reason)
    db.commit()
    if activity is not None:
        db.refresh(activity)
        _emit(activity, session, EVENT_SESSION_ENDED, actor_user_id)
    return activity, True
