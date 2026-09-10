"""Operator note endpoints: send, list, cancel, and the harness-side pull.

Sending a note is a governed action, not a chat message. It requires
``control_managed_agent``, the same permission that already lets the caller
stop the agent, and it is written to the audit log before the API answers. The
API never reaches outside the caller's account: every target is resolved with
an account-scoped query, so an id from another account is a 404 and can never
become a delivery.

The pull route (``POST /agents/notes/pending``) is the other half, for agents
whose model calls bypass the gateway. It authenticates with the managed-agent
runtime bearer token and is called by a harness hook, a Claude Code channel
server or an inbox bridge. The model never calls it and never spends a token
deciding to.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.auth.jwt import authenticate_runtime_bearer_token
from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import (
    crud_agent_control_command,
    crud_api_key,
    crud_audit_log,
    crud_managed_agent,
    crud_runtime_session,
)
from preloop.models.db.session import get_db_session, get_session_factory
from preloop.models.models.api_usage import ApiUsage
from preloop.schemas.operator_notes import (
    OperatorNoteAuthor,
    OperatorNoteCreate,
    OperatorNoteList,
    OperatorNotePendingRequest,
    OperatorNotePendingResponse,
    OperatorNoteResponse,
)
from preloop.services import operator_notes
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)

router = APIRouter()

#: Sending a note is the same class of act as stopping the agent, so it takes
#: the same permission. The decorator enforces it on EE; ``CONTROL_PERMISSION``
#: with :func:`ensure_permission_in_oss` enforces the equivalent on OSS builds,
#: where the decorator is a no-op and a viewer would otherwise be able to steer
#: someone else's run.
CONTROL_PERMISSION = "control_managed_agent"

#: One author, one agent, one hour. Bursts are how a note channel turns into
#: a firehose nobody reads, and every push design that shipped before ours
#: needed this.
NOTE_RATE_LIMIT_PER_HOUR = 20


def _to_response(note: Any) -> OperatorNoteResponse:
    """Serialise one stored note, including where and how it landed."""
    return OperatorNoteResponse(
        note_id=note.command_id,
        state=operator_notes.note_state(note),
        text=note.body or "",
        managed_agent_id=note.managed_agent_id,
        runtime_session_id=note.runtime_session_id,
        author=OperatorNoteAuthor(
            user_id=note.created_by_user_id,
            display=note.author_display,
            auth_method=note.author_auth_method,
        ),
        created_at=note.created_at,
        expires_at=note.expires_at,
        delivered_at=note.delivered_at,
        delivery_channel=note.delivery_channel,
        delivered_turn_index=note.delivered_turn_index,
        acknowledged_turn_id=note.acknowledged_turn_id,
        cancelled_at=note.cancelled_at,
    )


def _session_for_execution(
    db: Session, *, account_id: str, execution_id: UUID
) -> Optional[Any]:
    """Resolve the runtime session a flow execution is running on.

    An execution has no session column: the link is the usage it produced, so
    the newest governed call for that execution names the session. Scoped to
    the account on both sides.
    """
    row = (
        db.query(ApiUsage.runtime_session_id)
        .filter(
            ApiUsage.account_id == account_id,
            ApiUsage.flow_execution_id == execution_id,
            ApiUsage.runtime_session_id.isnot(None),
        )
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )
    if row is None or row[0] is None:
        return None
    return crud_runtime_session.get_account_session(
        db, account_id=account_id, runtime_session_id=row[0]
    )


def _resolve_target(
    db: Session, *, account_id: str, payload: OperatorNoteCreate
) -> Tuple[Optional[UUID], Optional[UUID]]:
    """Resolve the note's target to (managed agent, runtime session).

    Every lookup is account-scoped, so a foreign id resolves to nothing and
    the caller gets a 404 instead of a cross-account delivery.
    """
    if payload.runtime_session_id is not None:
        session = crud_runtime_session.get_account_session(
            db,
            account_id=account_id,
            runtime_session_id=payload.runtime_session_id,
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Runtime session not found",
            )
        agent = getattr(session, "managed_agent", None)
        return (agent.id if agent is not None else None, session.id)

    if payload.execution_id is not None:
        session = _session_for_execution(
            db, account_id=account_id, execution_id=payload.execution_id
        )
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "This execution has no runtime session yet. A note can "
                    "only be delivered once the run has made a governed call."
                ),
            )
        agent = getattr(session, "managed_agent", None)
        return (agent.id if agent is not None else None, session.id)

    agent = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=str(payload.agent_id)
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed agent not found"
        )
    # A note with no live session waits for the next one the agent opens,
    # which is what "tell it before it starts" means.
    session_id = None
    if agent.runtime_session_id is not None:
        session = crud_runtime_session.get_account_session(
            db, account_id=account_id, runtime_session_id=agent.runtime_session_id
        )
        if session is not None and session.ended_at is None:
            session_id = session.id
    return (agent.id, session_id)


def _author_auth_method(db: Session, request: Request) -> str:
    """Name the credential the author used, derived server side.

    Never taken from a header: this string is stamped into the label the model
    reads, so the sender must not be able to choose it.
    """
    header = request.headers.get("authorization") or ""
    token = header.split(" ", 1)[1].strip() if " " in header else ""
    if not token:
        return "session"
    try:
        if crud_api_key.get_by_key(db, key=token) is not None:
            return "api_key"
    except Exception:  # pragma: no cover - identity is best effort, never fatal
        logger.debug("Could not classify note author credential", exc_info=True)
    return "jwt"


def _create_note(
    db: Session,
    *,
    request: Request,
    current_user: models.User,
    payload: OperatorNoteCreate,
) -> Any:
    """Persist one note, audited before the author is told it worked."""
    ensure_permission_in_oss(db, current_user, CONTROL_PERMISSION)
    account_id = str(current_user.account_id)
    managed_agent_id, runtime_session_id = _resolve_target(
        db, account_id=account_id, payload=payload
    )

    now = datetime.now(timezone.utc)
    if managed_agent_id is not None:
        recent = crud_agent_control_command.count_recent_notes_by_author(
            db,
            account_id=account_id,
            managed_agent_id=managed_agent_id,
            created_by_user_id=current_user.id,
            since=(now - timedelta(hours=1)).replace(tzinfo=None),
        )
        if recent >= NOTE_RATE_LIMIT_PER_HOUR:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit reached: {NOTE_RATE_LIMIT_PER_HOUR} notes per "
                    "hour per agent. Steering an agent this often usually "
                    "means restarting it with a better prompt."
                ),
            )

    note_id = operator_notes.new_note_id()
    author_display = (
        current_user.full_name or current_user.username or current_user.email
    )
    auth_method = _author_auth_method(db, request)
    ttl = payload.expires_in_seconds or operator_notes.DEFAULT_NOTE_TTL_SECONDS
    expires_at = now + timedelta(seconds=ttl)
    envelope = operator_notes.build_note_envelope(
        note_id=note_id,
        body=payload.text,
        runtime_session_id=str(runtime_session_id) if runtime_session_id else None,
        managed_agent_id=str(managed_agent_id) if managed_agent_id else None,
        author_user_id=str(current_user.id),
        author_display=author_display,
        author_auth_method=auth_method,
        created_at=now,
        expires_at=expires_at,
    )
    note = crud_agent_control_command.create_note(
        db,
        account_id=account_id,
        managed_agent_id=managed_agent_id,
        runtime_session_id=runtime_session_id,
        note_id=note_id,
        body=payload.text,
        envelope=envelope,
        author_display=author_display,
        author_auth_method=auth_method,
        created_by_user_id=current_user.id,
        expires_at=expires_at,
        source="api",
        commit=False,
    )
    # Written before the note can be delivered, and before the author is told
    # it worked: a note the model saw is never missing from the record.
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=current_user.id,
        action=operator_notes.AUDIT_NOTE_SENT,
        resource_type="operator_note",
        resource_id=note_id,
        status="success",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        details={
            "note_id": note_id,
            "managed_agent_id": str(managed_agent_id) if managed_agent_id else None,
            "runtime_session_id": (
                str(runtime_session_id) if runtime_session_id else None
            ),
            "author_display": author_display,
            "author_auth_method": auth_method,
            "expires_at": expires_at.isoformat(),
            "body_chars": len(payload.text),
        },
        commit=False,
    )
    from preloop.services.event_webhooks.emitters import emit_agent_note_sent

    emit_agent_note_sent(db, note)
    db.commit()
    db.refresh(note)
    return note


@router.post(
    "/operator-notes",
    response_model=OperatorNoteResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Operator Notes"],
)
@require_permission(CONTROL_PERMISSION)
# Declared ``def`` rather than ``async def`` on purpose, here and on the two
# routes below: they hold a synchronous ``Session``, and FastAPI runs sync
# handlers on the threadpool, so a pool checkout here can never block the event
# loop the liveness probe shares (``tests/api/test_event_loop_pool_wait.py``).
def create_operator_note(
    request: Request,
    payload: OperatorNoteCreate,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> OperatorNoteResponse:
    """Send a note to a running agent, delivered at its next turn boundary."""
    note = _create_note(db, request=request, current_user=current_user, payload=payload)
    return _to_response(note)


@router.get(
    "/operator-notes",
    response_model=OperatorNoteList,
    tags=["Operator Notes"],
)
@require_permission(CONTROL_PERMISSION)
def list_operator_notes(
    agent_id: Optional[UUID] = Query(None),
    runtime_session_id: Optional[UUID] = Query(None),
    execution_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> OperatorNoteList:
    """List notes for one agent, session or execution, newest first."""
    account_id = str(current_user.account_id)

    def _load() -> list[Any]:
        session_id = runtime_session_id
        if execution_id is not None:
            session = _session_for_execution(
                db, account_id=account_id, execution_id=execution_id
            )
            if session is None:
                return []
            session_id = session.id
        if agent_id is None and session_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Name an agent_id, runtime_session_id or execution_id",
            )
        return crud_agent_control_command.list_notes(
            db,
            account_id=account_id,
            managed_agent_id=agent_id,
            runtime_session_id=session_id,
            limit=limit,
        )

    return OperatorNoteList(notes=[_to_response(note) for note in _load()])


@router.post(
    "/operator-notes/{note_id}/cancel",
    response_model=OperatorNoteResponse,
    tags=["Operator Notes"],
)
@require_permission(CONTROL_PERMISSION)
def cancel_operator_note(
    note_id: str,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> OperatorNoteResponse:
    """Withdraw a note that has not been delivered yet.

    Cancelling is a state, never a delete. A delivered note cannot be unsent,
    so this returns it unchanged and the caller can see why.
    """

    def _cancel() -> Any:
        ensure_permission_in_oss(db, current_user, CONTROL_PERMISSION)
        note = crud_agent_control_command.cancel_note(
            db,
            account_id=str(current_user.account_id),
            note_id=note_id,
            cancelled_at=datetime.now(timezone.utc),
            cancelled_by_user_id=current_user.id,
        )
        if note is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Note not found"
            )
        return note

    return _to_response(_cancel())


def _claim_for_hook(
    token: str, payload: OperatorNotePendingRequest
) -> OperatorNotePendingResponse:
    """Authenticate a runtime token and claim its session's pending notes."""
    with get_session_factory()() as db:
        context = authenticate_runtime_bearer_token(
            db, token, enforce_current_binding=False
        )
        notes = operator_notes.claim_pending_notes(
            db,
            account_id=str(context.user.account_id),
            managed_agent_id=str(context.managed_agent.id),
            runtime_session_id=(
                str(context.runtime_session.id)
                if context.runtime_session is not None
                else None
            ),
            channel=payload.channel,
        )
        if not notes:
            return OperatorNotePendingResponse(notes=[], text=None, channel_event=None)
        return OperatorNotePendingResponse(
            notes=operator_notes.notes_payload(notes),
            text=operator_notes.render_notes_block(notes),
            channel_event=operator_notes.render_channel_event(notes),
        )


@router.post(
    "/agents/notes/pending",
    response_model=OperatorNotePendingResponse,
    tags=["Operator Notes"],
)
async def pending_operator_notes(
    payload: OperatorNotePendingRequest,
    authorization: Optional[str] = Header(None),
) -> OperatorNotePendingResponse:
    """Hand a harness the notes its session has been given.

    Claiming is the delivery: what this returns is marked delivered on the
    named channel, audited and evented before it leaves. Call it from the hook
    process, not from the model.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    return await run_db_off_loop(lambda: _claim_for_hook(token, payload))
