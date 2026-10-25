"""Durable audit outbox CRUD (EE #94) against real Postgres."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import audit_outbox as crud_audit_outbox
from preloop.models.crud import crud_account
from preloop.models.models import (
    AUDIT_OUTBOX_DELIVERED,
    AUDIT_OUTBOX_FAILED,
    AUDIT_OUTBOX_PENDING,
    AuditOutbox,
)


@pytest.fixture
def account(db_session: Session):
    return crud_account.create(
        db_session, obj_in={"organization_name": "Outbox Org", "is_active": True}
    )


def _enqueue(db, account, op="log_policy_decision", **kw):
    return crud_audit_outbox.enqueue(
        db,
        account_id=account.id,
        operation=op,
        payload={"tool_name": "t", **kw},
        priority=False,
    )


def test_enqueue_does_not_commit_and_rolls_back_with_the_business_write(
    db_session, account
):
    savepoint = db_session.begin_nested()
    row = _enqueue(db_session, account)
    assert row.status == AUDIT_OUTBOX_PENDING
    assert row.seq is not None
    savepoint.rollback()
    assert db_session.query(AuditOutbox).count() == 0


def test_enqueue_commits_with_the_business_write(db_session, account):
    savepoint = db_session.begin_nested()
    row = _enqueue(db_session, account)
    savepoint.commit()
    assert db_session.get(AuditOutbox, row.id) is not None


def test_claim_orders_priority_then_insertion(db_session, account):
    first = _enqueue(db_session, account, n=1)
    second = _enqueue(db_session, account, n=2)
    urgent = crud_audit_outbox.enqueue(
        db_session,
        account_id=account.id,
        operation="log_approval_lifecycle",
        payload={},
        priority=True,
    )
    assert crud_audit_outbox.claim_next(db_session).id == urgent.id
    crud_audit_outbox.mark_delivered(db_session, urgent)
    assert crud_audit_outbox.claim_next(db_session).id == first.id
    crud_audit_outbox.mark_delivered(db_session, first)
    assert crud_audit_outbox.claim_next(db_session).id == second.id


def test_claim_skips_rows_not_yet_due(db_session, account):
    row = _enqueue(db_session, account)
    row.next_attempt_at = row.next_attempt_at + timedelta(hours=1)
    db_session.flush()
    assert crud_audit_outbox.claim_next(db_session) is None


def test_mark_delivered_records_attempt(db_session, account):
    row = _enqueue(db_session, account)
    crud_audit_outbox.mark_delivered(db_session, row)
    assert row.status == AUDIT_OUTBOX_DELIVERED
    assert row.delivered_at is not None
    assert row.attempt_count == 1
    assert crud_audit_outbox.get_status(db_session, row.id) == AUDIT_OUTBOX_DELIVERED


def test_record_failure_backs_off_then_parks_and_requeue_replays(db_session, account):
    row = _enqueue(db_session, account)
    before = row.next_attempt_at
    crud_audit_outbox.record_failure(
        db_session, outbox_id=row.id, error="x" * 500, max_attempts=2
    )
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_PENDING
    assert row.attempt_count == 1
    assert len(row.last_error) == 200
    assert row.next_attempt_at > before

    crud_audit_outbox.record_failure(
        db_session, outbox_id=row.id, error="again", max_attempts=2
    )
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_FAILED

    assert crud_audit_outbox.requeue_failed(db_session, account_id=account.id) == 1
    db_session.refresh(row)
    assert row.status == AUDIT_OUTBOX_PENDING
    assert row.attempt_count == 0
    assert row.last_error is None


def test_oldest_pending_age_ignores_intents_in_backoff(db_session, account):
    assert crud_audit_outbox.oldest_pending_age_seconds(db_session) == 0.0
    row = _enqueue(db_session, account)
    row.created_at = row.created_at - timedelta(minutes=10)
    db_session.flush()
    assert crud_audit_outbox.oldest_pending_age_seconds(db_session) >= 590
    row.next_attempt_at = row.next_attempt_at + timedelta(hours=1)
    db_session.flush()
    assert crud_audit_outbox.oldest_pending_age_seconds(db_session) == 0.0


def test_replay_is_bounded_account_scoped_and_dry_run_does_not_reset_attempts(
    db_session: Session, account: models.Account
) -> None:
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    own = [_enqueue(db_session, account) for _ in range(3)]
    foreign = _enqueue(db_session, other_account)
    pending = _enqueue(db_session, account)
    for row in [*own, foreign]:
        row.status = AUDIT_OUTBOX_FAILED
        row.attempt_count = 20
        row.last_error = "delivery failed"
    db_session.flush()

    assert (
        crud_audit_outbox.requeue_failed(
            db_session, account_id=account.id, limit=2, dry_run=True
        )
        == 2
    )
    for row in [*own, foreign]:
        db_session.refresh(row)
        assert row.status == AUDIT_OUTBOX_FAILED
        assert row.attempt_count == 20
        assert row.last_error == "delivery failed"

    assert (
        crud_audit_outbox.requeue_failed(db_session, account_id=account.id, limit=2)
        == 2
    )
    for row in [*own, foreign, pending]:
        db_session.refresh(row)
    assert [row.status for row in own] == [
        AUDIT_OUTBOX_PENDING,
        AUDIT_OUTBOX_PENDING,
        AUDIT_OUTBOX_FAILED,
    ]
    assert all(row.attempt_count == 0 and row.last_error is None for row in own[:2])
    assert foreign.status == AUDIT_OUTBOX_FAILED
    assert pending.status == AUDIT_OUTBOX_PENDING
    assert (
        crud_audit_outbox.requeue_failed(db_session, account_id=account.id, limit=2)
        == 1
    )


def test_record_failure_leaves_the_session_alone_for_a_delivered_intent(
    db_session, account
):
    row = _enqueue(db_session, account)
    crud_audit_outbox.mark_delivered(db_session, row)
    other = _enqueue(db_session, account, n=2)
    crud_audit_outbox.record_failure(
        db_session, outbox_id=row.id, error="late", max_attempts=3
    )
    # The caller's other uncommitted work survives.
    assert db_session.get(AuditOutbox, other.id) is not None
    assert row.status == AUDIT_OUTBOX_DELIVERED


def test_two_relays_never_claim_the_same_intent(db_engine):
    """SKIP LOCKED on separate connections: the second claimer moves on."""
    from sqlalchemy.orm import Session as OrmSession

    setup = OrmSession(bind=db_engine)
    acct = crud_account.create(
        setup, obj_in={"organization_name": "Outbox Race Org", "is_active": True}
    )
    ids = [
        crud_audit_outbox.enqueue(
            setup, account_id=acct.id, operation="log_tool_call", payload={"n": n}
        ).id
        for n in range(2)
    ]
    setup.commit()
    first = OrmSession(bind=db_engine)
    second = OrmSession(bind=db_engine)
    try:
        claimed_a = crud_audit_outbox.claim_next(first)
        claimed_b = crud_audit_outbox.claim_next(second)
        assert {claimed_a.id, claimed_b.id} == set(ids)
        with OrmSession(bind=db_engine) as probe:
            assert crud_audit_outbox.claim_next(probe) is None
        crud_audit_outbox.mark_delivered(first, claimed_a)
        first.commit()
        second.rollback()
        # The released row is claimable again; the delivered one never is.
        third = OrmSession(bind=db_engine)
        again = crud_audit_outbox.claim_next(third)
        assert again.id == claimed_b.id
        third.rollback()
        third.close()
    finally:
        first.close()
        second.close()
        setup.query(AuditOutbox).filter(AuditOutbox.account_id == acct.id).delete()
        setup.delete(setup.get(type(acct), acct.id))
        setup.commit()
        setup.close()
