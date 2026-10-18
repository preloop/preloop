"""Verifier state and finding history stay tenant-local and reject late writes."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models import crud, models


def test_verification_is_scoped_monotonic_and_audited(
    db_session: Session, test_user: models.User
) -> None:
    now = datetime.now(UTC)
    args = dict(
        account_id=test_user.account_id,
        source_ref=uuid4(),
        subject_key="a" * 64,
        policy_revision="b" * 64,
        evidence_hash="c" * 64,
        verified_at=now,
        expires_at=now + timedelta(hours=1),
    )
    store = crud.crud_observation_verification
    row = store.record(db_session, **args)
    assert store.list_for_account(db_session, account_id=uuid4()) == []
    assert not store.invalidate(
        db_session, account_id=uuid4(), id=row.id, now=now, reason="policy_changed"
    )
    assert store.invalidate(
        db_session,
        account_id=test_user.account_id,
        id=row.id,
        now=now,
        reason="policy_changed",
    )
    assert not store.invalidate(
        db_session,
        account_id=test_user.account_id,
        id=row.id,
        now=now,
        reason="policy_changed",
    )
    assert row.invalidation_reason == "policy_changed"
    store.record(db_session, **{**args, "verified_at": now - timedelta(hours=1)})
    assert row.invalidated_at == now
    renewed = store.record(
        db_session, **{**args, "verified_at": now + timedelta(minutes=1)}
    )
    assert renewed.id == row.id and renewed.invalidated_at is None
    audits = crud.crud_audit_log.get_multi(
        db_session,
        account_id=str(test_user.account_id),
        resource_type="observation_verification",
    )
    assert len(audits) == 3


def test_finding_history_retains_evidence_and_dedupes_identical_runs(
    db_session: Session, test_user: models.User
) -> None:
    now = datetime.now(UTC)
    args = dict(
        account_id=test_user.account_id,
        rule="example",
        subject_key="one",
        limitation="Synthetic",
        evidence={"reason": "stale", "source_ref": "example"},
    )
    store = crud.crud_fleet_finding
    first, _ = store.upsert(db_session, **args, state="open", now=now)
    store.upsert(db_session, **args, state="open", now=now + timedelta(minutes=1))
    store.upsert(
        db_session,
        **{**args, "evidence": {"reason": "fresh", "source_ref": "example"}},
        state="resolved",
        now=now + timedelta(minutes=2),
    )
    events = crud.crud_audit_log.get_multi(
        db_session,
        account_id=str(test_user.account_id),
        resource_type="fleet_finding",
        resource_id=str(first.id),
    )
    assert len(events) == 2
    assert {event.details["evidence"]["reason"] for event in events} == {
        "stale",
        "fresh",
    }
    assert all(event.details["evaluated_at"] for event in events)
