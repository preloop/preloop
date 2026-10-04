"""The ``send_note`` builtin tool: one agent leaves a note for another.

Notes are a human to agent channel today. An agent that finishes work, or
notices something a sibling needs to know, has had no governed way to say so,
so the hand off happened through people or through files nobody sweeps.

Nothing here is a new channel. The note is written into the same store, with
the same A2A shaped envelope, and delivered by the same rail that already
carries an operator's note into the target's next turn
(:mod:`preloop.services.operator_notes`). The only new fact is who the author
is: a managed agent identity rather than a person, recorded as such on the row
(``created_by_managed_agent_id``) and in the label the model reads
(``auth_method="agent"``). The delivery block uses agent framing when every
note in it is agent-authored, and mixed framing when a human and an agent
share a delivery, so the prompt does not grant the sibling the operator's
stop-authority. Per-note ``from`` and ``auth`` attributes still name the
author either way.

Four properties this module owes the caller, all of them enforced here and
tested directly:

* **Account boundary.** The target is resolved with the same account-scoped
  resolver the REST route uses, against the account on the *caller's* identity.
  An id from another account resolves to nothing and comes back as "not found",
  which is also the only thing the caller learns about it.
* **Scope inside the account.** Resolving a target is not reaching it. An
  agent may note the runs it started and nothing else, unless a tool access
  rule grants more; :mod:`preloop.services.agent_note_scope` decides that and
  this module refuses before the rate limit is even read.
* **Structured refusals.** Naming zero targets or two is an answer, not an
  exception: the model gets a refusal that names the problem and can correct
  it on the next turn, and no row is written either way.
* **One record per call.** The audit row naming the agent as actor is written
  in the same transaction as the note, before the caller is told it worked.

Returned as a dict which the tool serialises to JSON: ``{"ok": true, ...}``
with the note, or ``{"ok": false, "error": {"code", "message"}}``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_agent_control_command,
    crud_audit_log,
    crud_managed_agent,
)
from preloop.services import agent_note_scope, operator_notes

logger = logging.getLogger(__name__)

#: ``source`` stamped on rows this tool writes, so the record separates a note
#: a person sent through the API from one an agent sent through the tool.
NOTE_SOURCE_AGENT_TOOL = "agent_tool"

#: Refusal codes. Stable strings: a model that reads one of these on its
#: previous turn should be able to correct the call without parsing prose.
ERROR_NO_AUTHOR = "no_agent_identity"
ERROR_TARGET_COUNT = "invalid_target"
ERROR_TARGET_NOT_FOUND = "target_not_found"
ERROR_EMPTY_BODY = "empty_body"
ERROR_BODY_TOO_LONG = "body_too_long"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_TARGET_AMBIGUOUS = "target_ambiguous"
ERROR_NO_CHILDREN = "no_live_children"

#: Values ``children`` accepts: the newest live run this caller started, or
#: every live run it started.
CHILDREN_LATEST = "latest"
CHILDREN_ALL = "all"
CHILDREN_VALUES = (CHILDREN_LATEST, CHILDREN_ALL)

#: Most notes one ``children="all"`` call writes. Each one still passes the
#: scope check and the per-target rate limit on its own.
MAX_CHILDREN_FANOUT = 20


def _refusal(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """One refusal, shaped so the model can act on it without parsing prose.

    ``extra`` lands inside the error object: a scope refusal names the scope
    the call would have needed, which is the one thing that tells the model
    whether to rephrase the call or stop asking.
    """
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if value is not None})
    return {"ok": False, "error": error}


def _agent_display(agent: Any) -> str:
    """The label an agent author is stamped with, marked as an agent.

    The suffix is not decoration: the same string is rendered into the
    ``from=`` attribute of the delivered element, next to notes written by
    people, and a reader of that block must not have to guess which is which.
    """
    name = (
        (getattr(agent, "display_name", None) or "").strip()
        or (getattr(agent, "session_reference", None) or "").strip()
        or str(getattr(agent, "id", "unknown"))
    )
    return f"{name} (agent)"[:255]


def send_note_from_agent(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Optional[Any],
    text: str,
    agent_id: Optional[Any] = None,
    runtime_session_id: Optional[Any] = None,
    execution_id: Optional[Any] = None,
    author_execution_id: Optional[Any] = None,
    subject_context: Optional[Dict[str, Any]] = None,
    external_session_id: Optional[str] = None,
    children: Optional[str] = None,
    author_session_ids: Optional[List[Any]] = None,
    author_principal: Optional[Tuple[str, str]] = None,
) -> Dict[str, Any]:
    """Write one note authored by a managed agent, or refuse and write none.

    Args:
        db: Session the note, the audit row and the outbox entry share.
        account_id: Account of the calling agent. The only scope in which a
            target is resolved.
        author_agent_id: The calling managed agent. Without one there is no
            author, and an unattributed note must not be deliverable.
        text: The note body, as the agent wrote it.
        agent_id: Target managed agent, current or next session.
        runtime_session_id: Target runtime session, and only that session.
        execution_id: Target flow execution, resolved to its live session.
        author_execution_id: The execution the call was made from, as the
            platform recorded it on the caller's identity. It is what the
            note scope is keyed on, and it is never read from an argument:
            an agent that could name its own lineage could name any.
        subject_context: The same caller attributes the preceding ``send_note``
            policy evaluation used, so a grant consults API-key-scoped
            rules under the same subject chain. Target identity is not
            copied here.
        external_session_id: Target named by the harness's own session id
            (the Claude Code ``session_id``), resolved inside the account.
        children: ``"latest"`` or ``"all"``: the live runs the calling
            session started, newest first.
        author_session_ids: The runtime sessions the call was made from, as
            the platform resolved them. Never read from an argument.
        author_principal: The caller's ``(type, id)`` runtime principal, so
            an external id the caller's own credential created resolves to
            that session even when another principal reused the id.

    Returns:
        ``{"ok": True, "note": {...}}`` on success, or a structured refusal.
        Never raises for caller error: a refusal is an answer the model can
        act on, an exception is a turn it cannot.
    """
    if author_agent_id is None:
        return _refusal(
            ERROR_NO_AUTHOR,
            "send_note is only callable by a managed agent identity: this "
            "call carries no agent, so the note would have no author.",
        )

    targets = {
        "agent_id": agent_id,
        "runtime_session_id": runtime_session_id,
        "execution_id": execution_id,
    }
    aliases = {
        "external_session_id": external_session_id,
        "children": children,
    }
    named = [
        key for key, value in {**targets, **aliases}.items() if value not in (None, "")
    ]
    if len(named) != 1:
        named_text = ", ".join(named) if named else "none"
        return _refusal(
            ERROR_TARGET_COUNT,
            "Name exactly one target: runtime_session_id, external_session_id, "
            "children, agent_id or execution_id. This call named "
            f"{len(named)} ({named_text}).",
        )

    # The body is checked before an alias fans out, so ``children="all"``
    # with an empty note gets one actionable refusal, not one per child.
    body_refusal = _body_refusal(text)
    if body_refusal is not None:
        return body_refusal

    if named[0] in aliases:
        return _send_to_alias(
            db,
            account_id=account_id,
            author_agent_id=author_agent_id,
            text=text,
            author_execution_id=author_execution_id,
            subject_context=subject_context,
            external_session_id=external_session_id,
            children=children,
            author_session_ids=author_session_ids,
            author_principal=author_principal,
        )

    # Parsed before any query: every target column is a UUID, and a malformed
    # id is the caller's mistake to correct, not a database error to surface.
    parsed: Dict[str, Optional[UUID]] = {}
    for key, value in targets.items():
        if value in (None, ""):
            parsed[key] = None
            continue
        try:
            parsed[key] = value if isinstance(value, UUID) else UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            return _refusal(ERROR_TARGET_COUNT, f"{key} is not a valid id: {value!r}.")
    agent_id = parsed["agent_id"]
    runtime_session_id = parsed["runtime_session_id"]
    execution_id = parsed["execution_id"]

    body = (text or "").strip()

    try:
        author_uuid = (
            author_agent_id
            if isinstance(author_agent_id, UUID)
            else UUID(str(author_agent_id))
        )
    except (ValueError, AttributeError, TypeError):
        return _refusal(
            ERROR_NO_AUTHOR, "The calling identity is not a managed agent id."
        )
    author = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=str(author_uuid)
    )
    if author is None:
        return _refusal(
            ERROR_NO_AUTHOR,
            "The calling agent identity does not belong to this account.",
        )

    try:
        target_agent_id, target_session_id = operator_notes.resolve_note_target(
            db,
            account_id=account_id,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
            execution_id=execution_id,
        )
    except operator_notes.NoteTargetError as exc:
        # A target in another account and a target that does not exist are the
        # same answer on purpose: the account is the resolution scope, so the
        # refusal must not tell the caller that an id exists elsewhere.
        return _refusal(ERROR_TARGET_NOT_FOUND, exc.detail)

    # Resolving a target is not reaching it. Which agents this one may steer
    # is a policy question, and it is answered before anything is counted or
    # written, so a refused note leaves no note row and spends no budget.
    scope = agent_note_scope.evaluate_note_scope(
        db,
        account_id=account_id,
        author_agent_id=author.id,
        author_execution_id=author_execution_id,
        target_agent_id=target_agent_id,
        target_session_id=target_session_id,
        named_execution_id=execution_id,
        text=body,
        subject_context=subject_context,
        author_session_ids=author_session_ids,
    )
    if not scope.allowed:
        agent_note_scope.audit_refusal(
            db,
            account_id=account_id,
            author_agent_id=author.id,
            decision=scope,
        )
        return _refusal(
            scope.reason_code or agent_note_scope.REASON_OUT_OF_SCOPE,
            scope.message or "This target is out of the note scope.",
            required_scope=scope.required_scope,
            target_relation=scope.relation,
        )

    now = datetime.now(timezone.utc)
    recent = crud_agent_control_command.count_recent_notes_by_author(
        db,
        account_id=account_id,
        managed_agent_id=target_agent_id,
        runtime_session_id=(target_session_id if target_agent_id is None else None),
        created_by_managed_agent_id=author.id,
        since=(now - timedelta(hours=1)).replace(tzinfo=None),
    )
    if recent >= operator_notes.NOTE_RATE_LIMIT_PER_HOUR:
        return _refusal(
            ERROR_RATE_LIMITED,
            f"Rate limit reached: {operator_notes.NOTE_RATE_LIMIT_PER_HOUR} "
            "notes per hour per agent or session, per author. Repeating "
            "yourself this often usually means the hand off belongs in the "
            "work itself, not in a note.",
        )

    note_id = operator_notes.new_note_id()
    author_display = _agent_display(author)
    auth_method = operator_notes.classify_author_auth_method(db, is_managed_agent=True)
    # The tool schema does not expose expiry. Agent notes use the same 24 hour
    # default REST uses when a human omits expires_in_seconds.
    expires_at = now + timedelta(seconds=operator_notes.DEFAULT_NOTE_TTL_SECONDS)
    # The same envelope builder, so delivery and rendering see exactly the
    # shape they already handle. ``author_user_id`` is null because there is
    # no user: the agent behind it is on the row and in the audit record.
    envelope = operator_notes.build_note_envelope(
        note_id=note_id,
        body=body,
        runtime_session_id=str(target_session_id) if target_session_id else None,
        managed_agent_id=str(target_agent_id) if target_agent_id else None,
        author_user_id=None,
        author_display=author_display,
        author_auth_method=auth_method,
        created_at=now,
        expires_at=expires_at,
    )
    note = crud_agent_control_command.create_note(
        db,
        account_id=account_id,
        managed_agent_id=target_agent_id,
        runtime_session_id=target_session_id,
        note_id=note_id,
        body=body,
        envelope=envelope,
        author_display=author_display,
        author_auth_method=auth_method,
        created_by_user_id=None,
        created_by_managed_agent_id=author.id,
        expires_at=expires_at,
        source=NOTE_SOURCE_AGENT_TOOL,
        commit=False,
    )
    # Written before the note can be delivered, and before the caller is told
    # it worked. ``user_id`` is null and the actor is the agent, named in the
    # details: the audit table has no agent actor column, and inventing a user
    # for an agent's act would be the wrong record.
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=None,
        action=operator_notes.AUDIT_NOTE_SENT,
        resource_type="operator_note",
        resource_id=note_id,
        status="success",
        details={
            "note_id": note_id,
            "actor_type": "managed_agent",
            "actor_managed_agent_id": str(author.id),
            "managed_agent_id": str(target_agent_id) if target_agent_id else None,
            "runtime_session_id": (
                str(target_session_id) if target_session_id else None
            ),
            "author_display": author_display,
            "author_auth_method": auth_method,
            "expires_at": expires_at.isoformat(),
            "body_chars": len(body),
            "source": NOTE_SOURCE_AGENT_TOOL,
            # Which scope carried this note, and the rule that widened it when
            # one did: an account-scoped note is the interesting row in a
            # review, and it should not take a second lookup to find it.
            "note_scope": scope.scope,
            "target_relation": scope.relation,
            "scope_rule_description": scope.rule_description,
            "author_execution_id": (
                str(author_execution_id) if author_execution_id else None
            ),
        },
        commit=False,
    )
    from preloop.services.event_webhooks.emitters import emit_agent_note_sent

    emit_agent_note_sent(db, note)
    db.commit()
    db.refresh(note)
    logger.info(
        "Agent %s left a note (%s) for agent=%s session=%s",
        author.id,
        note_id,
        target_agent_id,
        target_session_id,
    )
    return {"ok": True, "note": note_result(note)}


def _body_refusal(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The refusal for an empty or over-long note body, or None."""
    body = (text or "").strip()
    if not body:
        return _refusal(ERROR_EMPTY_BODY, "The note body is empty.")
    if len(body) > operator_notes.MAX_NOTE_BODY_CHARS:
        return _refusal(
            ERROR_BODY_TOO_LONG,
            f"The note body is {len(body)} characters; the limit is "
            f"{operator_notes.MAX_NOTE_BODY_CHARS}.",
        )
    return None


def _send_to_alias(
    db: Session,
    *,
    account_id: str,
    author_agent_id: Any,
    text: str,
    author_execution_id: Any,
    subject_context: Optional[Dict[str, Any]],
    external_session_id: Optional[str],
    children: Optional[str],
    author_session_ids: Optional[List[Any]],
    author_principal: Optional[Tuple[str, str]],
) -> Dict[str, Any]:
    """Resolve ``external_session_id`` or ``children`` and send as usual.

    Both are names for one or more runtime sessions. Each resolved session
    goes through :func:`send_note_from_agent` as a ``runtime_session_id``
    target, so scope, rate limit, audit and delivery are not reimplemented.
    """
    from preloop.services import agent_session_lineage

    def _send(session_id: Any) -> Dict[str, Any]:
        return send_note_from_agent(
            db,
            account_id=account_id,
            author_agent_id=author_agent_id,
            text=text,
            runtime_session_id=session_id,
            author_execution_id=author_execution_id,
            subject_context=subject_context,
            author_session_ids=author_session_ids,
        )

    if external_session_id not in (None, ""):
        principal_type, principal_id = author_principal or (None, None)
        matches = agent_session_lineage.resolve_external_session(
            db,
            account_id=account_id,
            external_session_id=str(external_session_id),
            principal_type=principal_type,
            principal_id=principal_id,
        )
        if not matches:
            return _refusal(
                ERROR_TARGET_NOT_FOUND,
                "No runtime session in this account carries external session "
                f"id {external_session_id!r}. It appears once the run's first "
                "hook or model call reaches Preloop.",
            )
        if len(matches) > 1:
            return _refusal(
                ERROR_TARGET_AMBIGUOUS,
                f"External session id {external_session_id!r} matches "
                f"{len(matches)} runtime sessions; name one by "
                "runtime_session_id (list_sessions shows them).",
                candidates=[str(match.id) for match in matches],
            )
        return _send(matches[0].id)

    if children not in CHILDREN_VALUES:
        return _refusal(
            ERROR_TARGET_COUNT,
            f"children must be 'latest' or 'all', not {children!r}.",
        )
    if not author_session_ids:
        return _refusal(
            agent_note_scope.REASON_NO_LINEAGE,
            "children needs the calling session, and this call carries none: "
            "name the run by runtime_session_id instead.",
        )
    live = agent_session_lineage.live_children(
        db,
        account_id=account_id,
        parent_session_ids=author_session_ids,
        limit=MAX_CHILDREN_FANOUT,
    )
    if not live:
        return _refusal(
            ERROR_NO_CHILDREN,
            "This session has no live runs it started. A run started from a "
            "shell is recorded as a child when its SessionStart hook sees "
            "PRELOOP_PARENT_SESSION_ID.",
        )
    if children == CHILDREN_LATEST:
        return _send(live[0].id)
    notes: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for child in live:
        result = _send(child.id)
        if result.get("ok"):
            notes.append(result["note"])
        else:
            refused.append({"runtime_session_id": str(child.id), **result["error"]})
    if not notes:
        failure = _refusal(
            refused[0].get("code", ERROR_TARGET_NOT_FOUND),
            f"None of the {len(live)} live runs this session started took the "
            "note; see refused for each one.",
        )
        failure["refused"] = refused
        return failure
    return {"ok": True, "notes": notes, "refused": refused}


def note_result(note: Any) -> Dict[str, Any]:
    """What the calling agent is told about the note it just wrote."""

    def _str(value: Any) -> Optional[str]:
        return str(value) if isinstance(value, UUID) or value else None

    return {
        "note_id": note.command_id,
        "state": operator_notes.note_state(note),
        "text": note.body or "",
        "managed_agent_id": _str(note.managed_agent_id),
        "runtime_session_id": _str(note.runtime_session_id),
        "author": {
            "agent_id": _str(note.created_by_managed_agent_id),
            "display": note.author_display,
            "auth_method": note.author_auth_method,
        },
        "expires_at": (
            note.expires_at.isoformat() if note.expires_at is not None else None
        ),
    }
