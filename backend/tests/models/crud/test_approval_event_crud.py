"""CRUD tests for approval timeline events, including viewed-event dedupe."""

from datetime import datetime, UTC

from sqlalchemy import text

from preloop.models.crud import crud_approval_event, crud_approval_workflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate


def _ensure_viewed_indexes(db_session) -> None:
    """Create the unique indexes if this database has not been migrated yet."""
    db_session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_event_viewed_actor "
            "ON approval_event (approval_request_id, event_type, actor_id) "
            "WHERE event_type = 'viewed' AND actor_id IS NOT NULL"
        )
    )
    db_session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_event_viewed_anonymous "
            "ON approval_event (approval_request_id, event_type) "
            "WHERE event_type = 'viewed' AND actor_id IS NULL"
        )
    )
    db_session.flush()


def _make_request(db_session, account_id) -> ApprovalRequest:
    workflow = crud_approval_workflow.create(
        db_session,
        obj_in=ApprovalWorkflowCreate(name="Viewed uniq", approval_type="manual"),
        account_id=str(account_id),
    )
    db_session.flush()
    tool_config = ToolConfiguration(
        tool_name="test_tool",
        tool_source="builtin",
        account_id=account_id,
        approval_workflow_id=workflow.id,
    )
    db_session.add(tool_config)
    db_session.flush()
    request = ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=tool_config.id,
        approval_workflow_id=workflow.id,
        tool_name="test_tool",
        tool_args={},
        status="pending",
        requested_at=datetime.now(UTC),
        approval_token="viewed-uniq-token",
    )
    db_session.add(request)
    db_session.flush()
    return request


def test_viewed_record_is_idempotent_for_the_same_actor(
    db_session, create_account, create_user
) -> None:
    """A unique partial index plus IntegrityError handling collapses races."""
    _ensure_viewed_indexes(db_session)
    account = create_account()
    user = create_user(account=account)
    request = _make_request(db_session, account.id)

    first = crud_approval_event.record(
        db_session,
        approval_request_id=request.id,
        account_id=account.id,
        event_type="viewed",
        detail="Approval request opened",
        actor_id=user.id,
        commit=False,
    )
    second = crud_approval_event.record(
        db_session,
        approval_request_id=request.id,
        account_id=account.id,
        event_type="viewed",
        detail="Approval request opened again",
        actor_id=user.id,
        commit=False,
    )

    events = crud_approval_event.get_by_request(
        db_session, approval_request_id=request.id
    )
    viewed = [event for event in events if event.event_type == "viewed"]
    assert len(viewed) == 1
    assert first.id == second.id == viewed[0].id


def test_anonymous_viewed_record_is_idempotent(db_session, create_account) -> None:
    """Token-path views (actor_id NULL) also collapse under concurrency."""
    _ensure_viewed_indexes(db_session)
    account = create_account()
    request = _make_request(db_session, account.id)

    crud_approval_event.record(
        db_session,
        approval_request_id=request.id,
        account_id=account.id,
        event_type="viewed",
        detail="Approval link opened (token link)",
        actor_id=None,
        commit=False,
    )
    crud_approval_event.record(
        db_session,
        approval_request_id=request.id,
        account_id=account.id,
        event_type="viewed",
        detail="Approval link opened (token link)",
        actor_id=None,
        commit=False,
    )

    events = crud_approval_event.get_by_request(
        db_session, approval_request_id=request.id
    )
    viewed = [event for event in events if event.event_type == "viewed"]
    assert len(viewed) == 1
