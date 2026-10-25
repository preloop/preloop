"""Editable note defaults and human-specific approval recipient bindings."""

from uuid import uuid4
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_user
from preloop.models.crud.agent_note_policy import (
    caller_approval_workflow,
    seed_note_policy,
    seed_all_note_policies,
)


def test_seeding_preserves_existing_rules_and_edits(db_session, test_user):
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="send_note",
        tool_source="builtin",
        is_enabled=True,
        custom_config={"existing": True},
    )
    db_session.add(config)
    db_session.flush()
    rule = models.ToolAccessRule(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        action="deny",
        priority=1,
        description="Existing restriction",
    )
    db_session.add(rule)
    db_session.flush()
    assert seed_note_policy(db_session, account_id=test_user.account_id)
    rules = (
        db_session.query(models.ToolAccessRule)
        .filter_by(tool_configuration_id=config.id)
        .all()
    )
    assert len(rules) == 4
    assert config.is_enabled
    assert config.custom_config["existing"]
    assert rule.priority == 1
    seeded = next(item for item in rules if item.action == "allow")
    seeded.is_enabled = False
    db_session.flush()
    assert not seed_note_policy(db_session, account_id=test_user.account_id)
    assert not seeded.is_enabled
    assert (
        db_session.query(models.ToolAccessRule)
        .filter_by(tool_configuration_id=config.id)
        .count()
        == 4
    )


def test_caller_workflows_keep_recipients_separate_and_follow_template(
    db_session, test_user
):
    account_id = test_user.account_id
    seed_note_policy(db_session, account_id=account_id)
    template = (
        db_session.query(models.ApprovalWorkflow)
        .filter_by(account_id=account_id, name="Note sender approval")
        .one()
    )
    other = crud_user.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "email": "other@example.com",
            "username": f"other-{uuid4()}",
            "hashed_password": "unused",
            "is_active": True,
        },
    )
    template.require_reason = True
    db_session.flush()
    first_id = caller_approval_workflow(
        db_session, account_id=account_id, workflow_id=template.id, user_id=test_user.id
    )
    other_id = caller_approval_workflow(
        db_session, account_id=account_id, workflow_id=template.id, user_id=other.id
    )
    assert first_id != other_id
    first = db_session.get(models.ApprovalWorkflow, first_id)
    second = db_session.get(models.ApprovalWorkflow, other_id)
    assert first.approver_user_ids == [test_user.id]
    assert second.approver_user_ids == [other.id]
    assert first.require_reason is True
    assert second.require_reason is True
    assert not template.approver_user_ids
    template.async_approval_enabled = False
    template.timeout_seconds = 120
    template.require_reason = False
    db_session.flush()
    assert (
        caller_approval_workflow(
            db_session,
            account_id=account_id,
            workflow_id=template.id,
            user_id=test_user.id,
        )
        == first_id
    )
    assert first.async_approval_enabled is False
    assert first.timeout_seconds == 120
    assert first.require_reason is False
    assert (
        caller_approval_workflow(
            db_session, account_id=account_id, workflow_id=template.id, user_id=None
        )
        is None
    )


def test_seeded_account_does_not_lock_or_write(
    db_session: Session, test_user: models.User
) -> None:
    seed_note_policy(db_session, account_id=test_user.account_id)
    db_session.commit()
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement.upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert not seed_note_policy(db_session, account_id=test_user.account_id)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements
    assert not any("FOR UPDATE" in sql for sql in statements)
    assert not any(sql.startswith(("INSERT", "UPDATE")) for sql in statements)


def test_backfill_preserves_progress_when_one_account_fails(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from preloop.models.crud import agent_note_policy, crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Another account", "is_active": True}
    )
    failed_id, other_id = test_user.account_id, other.id
    db_session.commit()
    seeded: list[object] = []

    def seed(db: Session, *, account_id: Any) -> bool:
        if account_id == failed_id:
            raise SQLAlchemyError("synthetic account failure")
        seeded.append(account_id)
        return seed_note_policy(db, account_id=account_id)

    monkeypatch.setattr(agent_note_policy, "seed_note_policy", seed)
    with Session(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    ) as seed_db:
        seed_all_note_policies(seed_db)
    assert other_id in seeded
    assert str(failed_id) in caplog.text
    assert not seed_note_policy(db_session, account_id=other_id)
