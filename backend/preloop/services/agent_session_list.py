"""The ``list_sessions`` builtin tool: find the runs you started (#1045).

``send_note`` needs a runtime session id, and a conductor that spawned a
worker from a shell had no tool that would tell it one. This is that tool,
kept as narrow as the problem: by default it lists the live children of the
calling session, which is exactly the set the default note scope reaches.

Scope and refusals follow ``search_sessions``
(:mod:`preloop.services.agent_session_search`). The caller's own runs, and
anything that descends from them, need nothing. Any other parent, or the
whole account, needs the same operator grant ``search_sessions`` reads for
scope ``account``, and is refused by name without it rather than quietly
narrowed. The answer is compact and capped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import false
from sqlalchemy.orm import Session

from preloop.models.crud.runtime_session import CRUDRuntimeSession
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services import agent_session_lineage
from preloop.services.agent_session_search import (
    REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED,
    REFUSAL_INVALID_REQUEST,
    REFUSAL_NO_AGENT_IDENTITY,
    account_scope_granted,
)
from preloop.tools.builtin_defs import (
    LIST_SESSIONS_DEFAULT_LIMIT,
    LIST_SESSIONS_MAX_LIMIT,
)

#: Tool name, shared with the catalogue entry and the MCP registration.
LIST_SESSIONS_TOOL_NAME = "list_sessions"

#: ``parent_session_id`` value meaning "every session in the account".
PARENT_ANY = "any"

#: The calling session could not be identified, so "my children" has no
#: meaning. Distinct from no agent identity: the caller is known, its session
#: is not.
REFUSAL_NO_CALLER_SESSION = "no_caller_session"

SCOPE_OWN = "own"
SCOPE_ACCOUNT = "account"

#: Longest title returned per session.
MAX_TITLE_CHARS = 120


def _refusal(reason: str, detail: str, *, scope: str) -> Dict[str, Any]:
    """Same shape as a ``search_sessions`` refusal."""
    return {
        "refused": True,
        "reason": reason,
        "detail": detail,
        "scope": scope,
        "results": [],
    }


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("started_since needs a timezone offset")
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _row(session: RuntimeSession, now: datetime) -> Dict[str, Any]:
    last = _aware(session.last_activity_at or session.started_at)
    active = (
        session.ended_at is None
        and last is not None
        and now - last <= CRUDRuntimeSession.ACTIVE_WINDOW
    )
    title = session.title or session.runtime_principal_name
    started = _aware(session.started_at)
    return {
        "id": str(session.id),
        "started_at": started.isoformat() if started else None,
        "agent_kind": session.session_source_type,
        "cwd": session.cwd,
        "parent_session_id": (
            str(session.parent_session_id) if session.parent_session_id else None
        ),
        "title": title[:MAX_TITLE_CHARS] if title else None,
        "is_active_now": active,
        "ended": session.ended_at is not None,
    }


def list_for_agent(
    db: Session,
    *,
    account_id: Any,
    managed_agent_id: Any,
    caller_session_ids: Sequence[Any],
    subject_context: Dict[str, Optional[str]],
    parent_session_id: Optional[str] = None,
    started_since: Optional[str] = None,
    external_session_id: Optional[str] = None,
    agent_kind: Optional[str] = None,
    cwd: Optional[str] = None,
    active_only: Optional[bool] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """List runtime sessions for the calling agent, scoped like a search.

    Args:
        db: Database session.
        account_id: The caller's account; every row is bounded by it.
        managed_agent_id: The calling agent. None is refused.
        caller_session_ids: The sessions the call was made from, resolved by
            the platform from the credential, never from an argument.
        subject_context: ``api_key_id`` and ``managed_agent_id``, the chain
            the account grant is read along.
        parent_session_id: Whose children to list; default the caller's own
            session, ``"any"`` for the whole account.

    Returns:
        ``{"scope", "results", "total", "truncated"}``, or a refusal record.
    """
    own = [s for s in caller_session_ids if s]
    requested_parent = (parent_session_id or "").strip()
    if managed_agent_id is None and not own:
        return _refusal(
            REFUSAL_NO_AGENT_IDENTITY,
            "list_sessions answers for an agent identity, and this call has none.",
            scope=SCOPE_OWN,
        )
    try:
        since = _parse_since(started_since)
    except ValueError as exc:
        return _refusal(
            REFUSAL_INVALID_REQUEST,
            f"started_since is not an ISO 8601 instant with an offset: {exc}.",
            scope=SCOPE_OWN,
        )
    bounded = max(
        1, min(int(limit or LIST_SESSIONS_DEFAULT_LIMIT), LIST_SESSIONS_MAX_LIMIT)
    )

    parents: Optional[List[Any]]
    scope = SCOPE_OWN
    if not requested_parent:
        if not own:
            return _refusal(
                REFUSAL_NO_CALLER_SESSION,
                "This call does not identify the session it was made from, so "
                "it has no children to list. Pass parent_session_id with your "
                "runtime session id, or use the id printed at spawn.",
                scope=SCOPE_OWN,
            )
        parents = list(own)
    elif requested_parent.lower() == PARENT_ANY:
        parents = None
        scope = SCOPE_ACCOUNT
    else:
        parents = [requested_parent]
        is_own = requested_parent in {str(s) for s in own}
        if not is_own and not agent_session_lineage.session_descends_from(
            db,
            account_id=account_id,
            target_session_id=requested_parent,
            ancestor_session_ids=own,
        ):
            scope = SCOPE_ACCOUNT

    if scope == SCOPE_ACCOUNT:
        from preloop.services.session_search_audit import account_meta_data

        if not account_scope_granted(
            account_meta_data(db, account_id=account_id),
            subject_context=subject_context,
        ):
            return _refusal(
                REFUSAL_ACCOUNT_SCOPE_NOT_GRANTED,
                "Listing sessions you did not start needs the account grant an "
                "operator makes (session_search.account_scope). Omit "
                "parent_session_id to list your own children.",
                scope=SCOPE_ACCOUNT,
            )

    query = db.query(RuntimeSession).filter(RuntimeSession.account_id == account_id)
    if parents is not None:
        parent_ids = [
            pid
            for pid in (agent_session_lineage.as_uuid(p) for p in parents)
            if pid is not None
        ]
        if not parent_ids:
            return {"scope": scope, "results": [], "total": 0, "truncated": False}
        query = query.filter(RuntimeSession.parent_session_id.in_(parent_ids))
    if active_only is None or active_only:
        query = query.filter(RuntimeSession.ended_at.is_(None))
    if since is not None:
        query = query.filter(RuntimeSession.started_at >= since)
    if agent_kind:
        query = query.filter(RuntimeSession.session_source_type == agent_kind.strip())
    if cwd:
        escaped = cwd.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(RuntimeSession.cwd.like(f"{escaped}%", escape="\\"))
    if external_session_id:
        matches = agent_session_lineage.resolve_external_session(
            db, account_id=account_id, external_session_id=external_session_id
        )
        query = query.filter(
            RuntimeSession.id.in_([m.id for m in matches]) if matches else false()
        )

    total = query.count()
    rows = query.order_by(RuntimeSession.started_at.desc()).limit(bounded).all()
    now = datetime.now(UTC)
    return {
        "scope": scope,
        "results": [_row(row, now) for row in rows],
        "total": total,
        "truncated": total > len(rows),
    }


__all__ = ["LIST_SESSIONS_TOOL_NAME", "list_for_agent"]
