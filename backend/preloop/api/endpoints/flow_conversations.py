"""Client-neutral hosted conversation and execution-scoped runtime endpoints."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import jwt
from anyio import from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.crud import flow_conversation as crud
from preloop.models.db.session import get_db_session
from preloop.plugins.account_hooks import (
    AuthorizationContext,
    VISIBLE_FLOW,
    filter_viewable,
)
from preloop.schemas.flow_conversation import (
    ConversationStart,
    ConversationSend,
    ConversationResponse,
    ConversationMessage,
    RuntimeTurnUpdate,
)
from preloop.services.flow_conversations import (
    admission_notice,
    budget_notice,
    interactive_settings,
    reconcile,
    resume_checkpoint,
)
from preloop.services.flow_trigger_service import FlowDispatchError, FlowTriggerService
from preloop.services.kill_switch import FLOW_DENIAL_MESSAGE, FlowHaltActiveError
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from preloop.utils.audit import log_config_change

router = APIRouter()


def visible_flow(db: Session, user: models.User, flow_id: UUID) -> models.Flow:
    """Enforce account and resource visibility for reads and writes."""
    flow = crud_flow.get(db, id=flow_id, account_id=user.account_id)
    if not flow or not filter_viewable(db, user, VISIBLE_FLOW, [flow]):
        raise HTTPException(404, "Flow not found")
    return flow


def conversation(
    db: Session, user: models.User, conversation_id: UUID, *, write: bool = False
) -> models.FlowConversation:
    """Authorize the thread before exposing any text or runtime state."""
    row = crud.get(db, conversation_id, user.account_id, lock=write)
    if row is None:
        raise HTTPException(404, "Conversation not found")
    visible_flow(db, user, row.flow_id)
    if write:
        ensure_permission_in_oss(db, user, "execute_flows")
        if row.owner_user_id != user.id:
            raise HTTPException(
                403,
                "Only the person who started this conversation can send messages or end it.",
            )
    reconcile(db, row)
    if write:
        row = crud.get(db, conversation_id, user.account_id, lock=True)
    return row


def response(
    db: Session,
    row: models.FlowConversation,
    *,
    after: int = 0,
    user: models.User | None = None,
) -> ConversationResponse:
    """Return an ordered page; after_turn lets clients load long threads."""
    result = ConversationResponse.model_validate(row)
    result.can_write = user is not None and row.owner_user_id == user.id
    flow = crud_flow.get(db, id=row.flow_id, account_id=row.account_id)
    result.flow_name = flow.name if flow else "Flow conversation"
    result.messages = [
        ConversationMessage.model_validate(item)
        for item in crud.messages(db, row.id, after=after)
    ]
    return result


def audit(
    db: Session, user: models.User, row: models.FlowConversation, action: str
) -> None:
    """Record the human initiator without copying conversation text into audit."""
    log_config_change(
        db,
        user=user,
        config_type="flow_conversation",
        action=action,
        new_value={
            "id": str(row.id),
            "flow_id": str(row.flow_id),
            "execution_id": str(row.execution_id),
            "turn": row.turns,
            "owner_user_id": str(row.owner_user_id),
            "origin": row.origin,
        },
    )


async def dispatch(
    db: Session,
    row: models.FlowConversation,
    user: models.User,
    native: dict[str, Any] | None = None,
) -> None:
    """Dispatch through ordinary flow authorization, concurrency and gateway policy."""
    details: dict[str, Any] = {}
    previous = row.execution_id if native else None
    if previous:
        details["_resume"] = {
            "execution_id": str(previous),
            "cli_session": native,
            "resume_root": str(
                await run_db_off_loop(lambda: crud.executions(db, row.id)[0])
            ),
        }
    try:
        await FlowTriggerService(db).trigger_flow(
            flow_id=row.flow_id,
            trigger_event_data=details,
            source_execution_id=previous,
            triggered_by=user.username,
            authorization_context=AuthorizationContext(
                account_id=user.account_id, db=db, user=user
            ),
            conversation_id=row.id,
        )
    except FlowHaltActiveError as exc:
        notice = (
            FLOW_DENIAL_MESSAGE + ". Start a new conversation after the halt is lifted."
        )
        await run_db_off_loop(lambda: crud.fail_dispatch(db, row, notice))
        raise HTTPException(403, notice) from exc
    except FlowDispatchError as exc:
        notice = (
            f"Execution {exc.execution_id} was created but dispatch failed. "
            "This conversation cannot continue. Inspect the execution "
            "before starting a new conversation."
        )
        await run_db_off_loop(lambda: crud.fail_dispatch(db, row, notice))
        raise HTTPException(503, notice) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.post("/flows/{flow_id}/conversations", response_model=ConversationResponse)
@require_permission("execute_flows")
def start_conversation(
    flow_id: UUID,
    body: ConversationStart,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> ConversationResponse:
    """Start a hosted conversation; the first request ID is its stable identity."""
    ensure_permission_in_oss(db, current_user, "execute_flows")
    flow = visible_flow(db, current_user, flow_id)
    try:
        policy = interactive_settings(flow.agent_type, flow.agent_config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not policy.enabled:
        raise HTTPException(409, "Enable interactive conversations on this flow first.")
    if not settings.flow_artifact_direct_upload:
        raise HTTPException(
            409,
            "Interactive conversations require direct workspace and native-session checkpoint storage.",
        )
    existing = crud.get(db, body.request_id, current_user.account_id)
    if existing:
        if existing.flow_id != flow_id or existing.owner_user_id != current_user.id:
            raise HTTPException(
                409, "This request ID already belongs to another conversation."
            )
        first = crud.find_request(db, existing.id, body.request_id)
        if first and first.text != body.message:
            raise HTTPException(
                409, "This request ID was already used for a different first message."
            )
        return response(db, existing, user=current_user)
    try:
        row = crud.create(
            db,
            conversation_id=body.request_id,
            flow=flow,
            owner=current_user,
            policy=policy,
            origin=body.origin,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    first = crud.find_request(db, row.id, body.request_id)
    if first:
        if first.text != body.message:
            raise HTTPException(
                409, "This request ID was already used for a different first message."
            )
        return response(db, row, user=current_user)
    crud.admit(db, row, text=body.message, request_id=body.request_id, starting=True)
    # Keep orchestration on the application loop so local execution tasks survive.
    from_thread.run(dispatch, db, row, current_user)
    audit(db, current_user, row, "started")
    return response(db, row, user=current_user)


@router.get("/flow-conversations", response_model=list[ConversationResponse])
def list_conversations(
    flow_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> list[ConversationResponse]:
    """List visible conversation identities for console, mobile and chat clients."""
    rows = crud.list_conversations(
        db, current_user.account_id, flow_id=flow_id, offset=offset
    )
    result = []
    for row in rows:
        try:
            visible_flow(db, current_user, row.flow_id)
        except HTTPException:
            continue
        reconcile(db, row)
        item = ConversationResponse.model_validate(row)
        item.can_write = row.owner_user_id == current_user.id
        item.flow_name = visible_flow(db, current_user, row.flow_id).name
        result.append(item)
    return result


@router.get(
    "/flow-conversations/{conversation_id}", response_model=ConversationResponse
)
def get_conversation(
    conversation_id: UUID,
    after_turn: int = Query(0, ge=0),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> ConversationResponse:
    """Poll durable state and streamed answers; reloads and tabs share this thread."""
    return response(
        db,
        conversation(db, current_user, conversation_id),
        after=after_turn,
        user=current_user,
    )


@router.post(
    "/flow-conversations/{conversation_id}/messages",
    response_model=ConversationResponse,
)
@require_permission("execute_flows")
def send_message(
    conversation_id: UUID,
    body: ConversationSend,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> ConversationResponse:
    """Admit one input or explicitly refuse it; stopped threads resume natively."""
    row = conversation(db, current_user, conversation_id, write=True)
    duplicate = crud.find_request(db, row.id, body.request_id)
    if duplicate:
        if duplicate.text != body.message:
            raise HTTPException(
                409, "This request ID was already used for a different message."
            )
        return response(db, row, user=current_user)
    notice = admission_notice(row)
    if notice:
        raise HTTPException(409, notice)
    flow = visible_flow(db, current_user, row.flow_id)
    try:
        from_thread.run(
            FlowTriggerService(db)._authorize_flow_run,
            flow,
            AuthorizationContext(
                account_id=current_user.account_id, db=db, user=current_user
            ),
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    limit = budget_notice(db, row, flow)
    if limit:
        crud.save(
            db,
            row,
            state="failed",
            notice=limit,
            end_requested=True,
            stop_reason="limit",
        )
        raise HTTPException(409, limit)
    native = None
    if row.state == "stopped":
        try:
            native = resume_checkpoint(db, row)
        except ValueError as exc:
            crud.save(db, row, state="failed", notice=str(exc))
            raise HTTPException(410, str(exc)) from exc
    crud.admit(
        db,
        row,
        text=body.message,
        request_id=body.request_id,
        starting=native is not None,
    )
    if native:
        from_thread.run(dispatch, db, row, current_user, native)
    else:
        db.commit()
    audit(db, current_user, row, "message_sent")
    return response(db, row, user=current_user)


@router.post(
    "/flow-conversations/{conversation_id}/end", response_model=ConversationResponse
)
@require_permission("execute_flows")
def end_conversation(
    conversation_id: UUID,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> ConversationResponse:
    """End a conversation; the runtime saves its checkpoint before shutdown."""
    row = conversation(db, current_user, conversation_id, write=True)
    crud.save(
        db,
        row,
        end_requested=True,
        state="ended" if row.state in {"stopped", "failed"} else row.state,
        notice="You ended this conversation.",
        stop_reason="ended",
    )
    audit(db, current_user, row, "ended")
    return response(db, row, user=current_user)


def runtime_conversation(
    conversation_id: UUID,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db_session),
) -> models.FlowConversation:
    """Accept only a capability bound to the current execution and conversation."""
    try:
        claims = jwt.decode(
            authorization.removeprefix("Bearer "),
            settings.security.secret_key,
            algorithms=["HS256"],
            audience="flow-conversation",
            options={
                "require": ["exp", "account_id", "conversation_id", "execution_id"]
            },
        )
        if claims["conversation_id"] != str(conversation_id):
            raise ValueError("conversation mismatch")
        row = crud.get(db, conversation_id, UUID(claims["account_id"]), lock=True)
        if row is None or str(row.execution_id) != claims["execution_id"]:
            raise ValueError("runtime mismatch")
        execution = crud_flow_execution.get(
            db, id=row.execution_id, account_id=row.account_id
        )
        if not execution or execution.status in {
            "SUCCEEDED",
            "FAILED",
            "STOPPED",
            "CANCELLED",
            "ABORTED",
        }:
            raise HTTPException(
                409,
                "This execution has stopped. Its runtime can no longer update the conversation.",
            )
        return row
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(
            401, "This runtime capability is invalid or expired."
        ) from exc


@router.get("/flow-conversations/{conversation_id}/runtime")
def runtime_poll(
    row: models.FlowConversation = Depends(runtime_conversation),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Deliver the admitted turn or request checkpointing and shutdown."""
    expired = row.idle_deadline is not None and row.idle_deadline.replace(
        tzinfo=UTC
    ) <= datetime.now(UTC)
    stop = row.end_requested or expired or row.state in {"failed", "ended"}
    if expired:
        crud.save(
            db,
            row,
            notice="The idle timeout passed. Saving the checkpoint before stopping.",
            stop_reason="idle_timeout",
        )
    turn = crud.messages(db, row.id, after=max(0, row.turns - 1), limit=1)
    return {
        "stop": stop,
        "turn": row.turns,
        "message": turn[0].text
        if turn and turn[0].status == "pending" and not stop
        else None,
    }


@router.post("/flow-conversations/{conversation_id}/runtime")
def runtime_update(
    body: RuntimeTurnUpdate,
    row: models.FlowConversation = Depends(runtime_conversation),
    db: Session = Depends(get_db_session),
) -> dict[str, bool]:
    """Persist an answer snapshot before the next input can be accepted."""
    try:
        crud.runtime_turn(db, row, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"stop": row.end_requested}
