"""Account halt locking must serialize admission without blocking audit FKs."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account_halt, crud_audit_log


@pytest.fixture
def committed_account(db_engine: Engine) -> Iterator[UUID]:
    """Use a committed account visible to independent PostgreSQL sessions."""
    account_id = uuid4()
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="halt-lock-fixture"))
        db.commit()
    try:
        yield account_id
    finally:
        with Session(db_engine) as db:
            db.query(models.Account).filter(models.Account.id == account_id).delete()
            db.commit()


def test_halt_lock_allows_audit_foreign_key_insert(
    db_engine: Engine, committed_account: UUID
) -> None:
    """An independent audit insert must finish while admission owns the mutex."""
    with Session(db_engine) as admission, Session(db_engine) as audit:
        crud_account_halt.lock_account(admission, account_id=committed_account)
        audit.execute(text("SET LOCAL lock_timeout = '250ms'"))
        event = crud_audit_log.log_action(
            audit,
            account_id=committed_account,
            action="approval_created",
            resource_type="approval_request",
            status="success",
        )
        assert event.id is not None
        assert admission.in_transaction()
        admission.rollback()


def test_halt_lock_still_serializes_competing_admission(
    db_engine: Engine, committed_account: UUID
) -> None:
    """A second halt/admission owner waits until the first transaction releases."""
    with Session(db_engine) as first, Session(db_engine) as second:
        crud_account_halt.lock_account(first, account_id=committed_account)
        second.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            crud_account_halt.lock_account(second, account_id=committed_account)
        second.rollback()
        first.rollback()
        second.execute(text("SET LOCAL lock_timeout = '250ms'"))
        crud_account_halt.lock_account(second, account_id=committed_account)


def test_halt_lock_rejects_missing_account(db_session: Session) -> None:
    """An unknown account does not silently bypass serialized admission."""
    with pytest.raises(ValueError, match="Account not found"):
        crud_account_halt.lock_account(db_session, account_id=uuid4())
