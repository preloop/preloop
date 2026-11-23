"""Conversation transactions serialize admission across clients and workers."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert

from preloop.models import models
from preloop.schemas.flow_conversation import InteractiveSettings


def get(
    db: Session, conversation_id: UUID, account_id: UUID, *, lock: bool = False
) -> models.FlowConversation | None:
    """Read within an account, optionally taking the admission lock."""
    query = db.query(models.FlowConversation).filter_by(
        id=conversation_id, account_id=account_id
    )
    if lock:
        query = query.populate_existing().with_for_update()
    return query.first()


def list_conversations(
    db: Session,
    account_id: UUID,
    *,
    flow_id: UUID | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[models.FlowConversation]:
    """Return a bounded page of account conversations."""
    query = db.query(models.FlowConversation).filter_by(account_id=account_id)
    if flow_id:
        query = query.filter_by(flow_id=flow_id)
    return (
        query.order_by(models.FlowConversation.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def messages(
    db: Session, conversation_id: UUID, *, after: int = 0, limit: int = 100
) -> list[models.FlowConversationMessage]:
    """Read turns in order; callers authorize the conversation first."""
    return (
        db.query(models.FlowConversationMessage)
        .filter(
            models.FlowConversationMessage.conversation_id == conversation_id,
            models.FlowConversationMessage.turn > after,
        )
        .order_by(models.FlowConversationMessage.turn)
        .limit(limit)
        .all()
    )


def find_request(
    db: Session, conversation_id: UUID, request_id: UUID
) -> models.FlowConversationMessage | None:
    """Find a previously admitted request while holding the conversation lock."""
    return (
        db.query(models.FlowConversationMessage)
        .filter_by(conversation_id=conversation_id, request_id=request_id)
        .first()
    )


def create(
    db: Session,
    *,
    conversation_id: UUID,
    flow: models.Flow,
    owner: models.User,
    policy: InteractiveSettings,
) -> models.FlowConversation:
    """Create a conversation in the caller's transaction."""
    values = dict(
        id=conversation_id,
        account_id=flow.account_id,
        flow_id=flow.id,
        owner_user_id=owner.id,
        max_turns=policy.max_turns,
        idle_timeout_seconds=policy.idle_timeout_seconds,
        turns=0,
        state="starting",
        notice="Starting your conversation.",
        end_requested=False,
    )
    db.execute(
        insert(models.FlowConversation)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    row = get(db, conversation_id, flow.account_id, lock=True)
    if row is None or row.flow_id != flow.id or row.owner_user_id != owner.id:
        raise ValueError("This request ID already belongs to another conversation.")
    return row


def admit(
    db: Session,
    row: models.FlowConversation,
    *,
    text: str,
    request_id: UUID,
    starting: bool,
) -> models.FlowConversationMessage:
    """Reserve exactly one turn; callers hold the conversation row lock."""
    row.turns += 1
    row.state = "starting" if starting else "working"
    row.notice = (
        "Starting your conversation."
        if starting
        else "The agent is working. Wait for its answer before sending another message."
    )
    row.idle_deadline = None
    message = models.FlowConversationMessage(
        conversation_id=row.id,
        request_id=request_id,
        turn=row.turns,
        text=text,
        answer="",
        status="pending",
        execution_id=None if starting else row.execution_id,
    )
    db.add(message)
    db.flush()
    return message


def bind_execution(
    db: Session, row: models.FlowConversation, execution_id: UUID
) -> None:
    """Bind the reserved turn before dispatch can reach a worker."""
    row.execution_id = execution_id
    db.query(models.FlowConversationMessage).filter_by(
        conversation_id=row.id, turn=row.turns
    ).update({"execution_id": execution_id})
    db.flush()


def save(db: Session, row: models.FlowConversation, **changes: object) -> None:
    """Persist a transition under the caller's admission lock."""
    for key, value in changes.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()


def runtime_turn(
    db: Session,
    row: models.FlowConversation,
    *,
    turn: int,
    answer: str,
    finished: bool,
    error: bool,
    interrupted: bool = False,
) -> None:
    """Persist a retryable answer snapshot and release the input gate."""
    message = (
        db.query(models.FlowConversationMessage)
        .filter_by(conversation_id=row.id, turn=turn, execution_id=row.execution_id)
        .first()
    )
    if message is None or turn != row.turns:
        raise ValueError("This turn no longer belongs to the active runtime.")
    if message.status in {"completed", "failed", "interrupted"}:
        return
    if interrupted and not row.end_requested:
        raise ValueError("Only an ended conversation can interrupt its turn.")
    message.answer = answer
    message.status = "failed" if error else "completed" if finished else "working"
    if interrupted:
        message.status = "interrupted"
    row.state = "working"
    row.notice = (
        "The agent is working. Wait for its answer before sending another message."
    )
    if error:
        row.state = "failed"
        row.notice = "The agent could not finish this turn. Start a new conversation."
        row.stop_reason = "failed"
    elif finished:
        if row.turns >= row.max_turns or row.end_requested:
            row.end_requested = True
            row.stop_reason = "turn_limit" if row.turns >= row.max_turns else "ended"
            row.notice = (
                "The turn limit was reached. Saving the conversation."
                if row.stop_reason == "turn_limit"
                else "Ending the conversation."
            )
        else:
            row.state = "waiting"
            row.notice = (
                "Waiting for you. The container will stop after the idle timeout."
            )
            row.idle_deadline = datetime.now(UTC) + timedelta(
                seconds=row.idle_timeout_seconds
            )
    db.commit()


def executions(db: Session, conversation_id: UUID) -> list[UUID]:
    """Return every execution whose gateway usage belongs to this conversation."""
    return [
        item[0]
        for item in db.query(models.FlowConversationMessage.execution_id)
        .filter(
            models.FlowConversationMessage.conversation_id == conversation_id,
            models.FlowConversationMessage.execution_id.is_not(None),
        )
        .group_by(models.FlowConversationMessage.execution_id)
        .order_by(func.min(models.FlowConversationMessage.turn))
        .all()
    ]
