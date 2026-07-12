"""Tests for tracker credential encryption backfill."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from preloop.models.crud.tracker import crud_tracker
from preloop.models.models.tracker import Tracker
from preloop.services.tracker_credential_backfill import (
    run_tracker_credential_encryption_backfill,
)


def _insert_plaintext_tracker(
    db_session, account_id, *, name: str = "Legacy"
) -> Tracker:
    now = datetime.now(UTC).replace(tzinfo=None)
    tracker = Tracker(
        id=uuid4(),
        account_id=account_id,
        name=name,
        tracker_type="github",
        api_key="plaintext-api-key",
        jira_webhook_secret=None,
        credentials_secret_id=None,
        webhook_secret_id=None,
        is_active=True,
        is_deleted=False,
        is_valid=False,
        connection_details={},
        meta_data={},
        created=now,
        last_updated=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(tracker)
    db_session.commit()
    db_session.refresh(tracker)
    return tracker


def test_migrate_plaintext_credentials_encrypts_and_clears(
    db_session, create_account
) -> None:
    account = create_account()
    tracker = _insert_plaintext_tracker(db_session, account.id)

    result = crud_tracker.migrate_plaintext_credentials(db_session, tracker=tracker)

    assert result["migrated_api_key"] is True
    assert result["migrated_webhook_secret"] is False
    db_session.refresh(tracker)
    assert tracker.api_key is None
    assert tracker.credentials_secret_id is not None
    assert tracker.resolved_api_key == "plaintext-api-key"


def test_run_tracker_credential_encryption_backfill_migrates_candidates(
    db_session, create_account, monkeypatch
) -> None:
    account = create_account()
    tracker = _insert_plaintext_tracker(db_session, account.id, name="Boot Migrate")

    class _Factory:
        def __call__(self):
            return db_session

    monkeypatch.setattr(
        "preloop.services.tracker_credential_backfill.get_session_factory",
        lambda: _Factory(),
    )
    # Avoid closing the shared test session (list + migrate + final close).
    monkeypatch.setattr(db_session, "close", lambda: None)

    counts = run_tracker_credential_encryption_backfill()

    assert counts["scanned"] >= 1
    assert counts["migrated_api_keys"] >= 1
    db_session.refresh(tracker)
    assert tracker.api_key is None
    assert tracker.credentials_secret_id is not None


def test_backfill_skips_already_encrypted_trackers(db_session, create_account) -> None:
    account = create_account()
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "name": "Already Encrypted",
            "tracker_type": "github",
            "api_key": "secret-value",
            "account_id": account.id,
            "is_active": True,
            "connection_details": {},
            "meta_data": {},
        },
    )
    assert tracker.credentials_secret_id is not None

    candidates = crud_tracker.list_plaintext_credential_candidates(db_session)
    assert all(row.id != tracker.id for row in candidates)


def test_backfill_continues_after_partial_failure(
    db_session, create_account, monkeypatch
) -> None:
    """One tracker failure must not abort remaining candidates."""
    account = create_account()
    bad = _insert_plaintext_tracker(db_session, account.id, name="Bad Tracker")
    good = _insert_plaintext_tracker(db_session, account.id, name="Good Tracker")

    class _Factory:
        def __call__(self):
            return db_session

    monkeypatch.setattr(
        "preloop.services.tracker_credential_backfill.get_session_factory",
        lambda: _Factory(),
    )
    monkeypatch.setattr(db_session, "close", lambda: None)

    original = crud_tracker.migrate_plaintext_credentials

    def _flaky_migrate(db, *, tracker, commit=True):
        if tracker.id == bad.id:
            raise RuntimeError("simulated encryption failure")
        return original(db, tracker=tracker, commit=commit)

    monkeypatch.setattr(
        "preloop.services.tracker_credential_backfill.crud_tracker.migrate_plaintext_credentials",
        _flaky_migrate,
    )

    counts = run_tracker_credential_encryption_backfill()

    assert counts["scanned"] >= 2
    assert counts["migrated_api_keys"] >= 1
    db_session.refresh(good)
    assert good.api_key is None
    assert good.credentials_secret_id is not None
    db_session.refresh(bad)
    assert bad.api_key == "plaintext-api-key"
