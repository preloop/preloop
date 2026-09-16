"""The backfill watermark row: one per account, and it only moves backwards."""

import uuid
from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_session_search_backfill_state as crud_state,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_get_or_create_keeps_one_row_per_account(db_session, test_user):
    """A second call returns the same row rather than a second one."""
    first = crud_state.get_or_create(db_session, account_id=test_user.account_id)
    second = crud_state.get_or_create(db_session, account_id=test_user.account_id)

    assert first.id == second.id
    assert first.cursor_started_at is None
    assert first.completed_at is None


def test_the_watermark_only_moves_backwards(db_session, test_user):
    """A pass that walked nothing new must not rewind the cursor."""
    older = NOW - timedelta(days=10)
    session_id = uuid.uuid4()
    crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=older,
        cursor_session_id=session_id,
        sessions_scanned=2,
        rows_written=5,
        now=NOW,
    )

    state = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=NOW,
        cursor_session_id=uuid.uuid4(),
        sessions_scanned=1,
        rows_written=1,
        now=NOW,
    )

    assert state.cursor_started_at.replace(tzinfo=timezone.utc) == older
    assert state.cursor_session_id == session_id
    # Counters still accumulate: the work happened, the cursor just stayed.
    assert state.sessions_scanned == 3
    assert state.rows_written == 6


def test_completion_is_stamped_once(db_session, test_user):
    """A second completed pass does not restamp the account."""
    first = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        completed=True,
        now=NOW,
    )
    completed_at = first.completed_at

    second = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        completed=True,
        now=NOW + timedelta(hours=1),
    )

    assert second.completed_at == completed_at


def test_record_error_keeps_the_watermark(db_session, test_user):
    """A failure is recorded without losing the progress already made."""
    older = NOW - timedelta(days=3)
    crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=older,
        cursor_session_id=uuid.uuid4(),
        now=NOW,
    )

    state = crud_state.record_error(
        db_session,
        account_id=test_user.account_id,
        error="RuntimeError: session scan blew up",
        now=NOW,
    )

    assert state.cursor_started_at.replace(tzinfo=timezone.utc) == older
    assert "session scan blew up" in state.last_error


def test_pending_accounts_exclude_completed_and_inactive_ones(db_session, test_user):
    """The pass reads one row per account, and a finished one drops out."""
    done = crud_account.create(
        db_session,
        obj_in={"organization_name": "Done Organization", "is_active": True},
    )
    inactive = crud_account.create(
        db_session,
        obj_in={"organization_name": "Closed Organization", "is_active": False},
    )
    crud_state.record_pass(db_session, account_id=done.id, completed=True, now=NOW)

    pending = crud_state.pending_account_ids(
        db_session, account_ids=[test_user.account_id, done.id, inactive.id]
    )

    assert test_user.account_id in pending
    assert done.id not in pending
    assert inactive.id not in pending


def test_accounts_never_walked_come_first(db_session, test_user):
    """No account is starved by one that was just given a pass."""
    fresh = crud_account.create(
        db_session,
        obj_in={"organization_name": "Fresh Organization", "is_active": True},
    )
    crud_state.record_pass(
        db_session, account_id=test_user.account_id, rows_written=1, now=NOW
    )

    pending = crud_state.pending_account_ids(
        db_session, account_ids=[test_user.account_id, fresh.id]
    )

    assert pending.index(fresh.id) < pending.index(test_user.account_id)


def test_an_empty_account_id_filter_matches_nothing(db_session, test_user):
    """An empty list is 'these accounts, none of them', not 'every account'."""
    pending = crud_state.pending_account_ids(db_session, account_ids=[])

    assert pending == []
    assert test_user.account_id in crud_state.pending_account_ids(db_session)


def test_get_or_create_recovers_from_an_insert_race(db_session, test_user, monkeypatch):
    """A unique-index loser re-reads the winner instead of raising."""
    winner = crud_state.get_or_create(db_session, account_id=test_user.account_id)
    db_session.flush()
    calls = {"n": 0}
    original = crud_state.get_for_account

    def first_miss(db, *, account_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original(db, account_id=account_id)

    monkeypatch.setattr(crud_state, "get_for_account", first_miss)
    recovered = crud_state.get_or_create(db_session, account_id=test_user.account_id)

    assert recovered.id == winner.id


def test_lock_for_account_returns_the_created_row(db_session, test_user):
    """The walk lock is the backfill row, created if it did not exist."""
    locked = crud_state.lock_for_account(db_session, account_id=test_user.account_id)

    assert locked is not None
    assert locked.account_id == test_user.account_id


def test_lock_for_account_commits_a_new_row_before_locking(
    db_session, test_user, monkeypatch
):
    """A concurrent replica can see the empty row and skip immediately."""
    commits = {"n": 0}
    original = db_session.commit

    def counting_commit() -> None:
        commits["n"] += 1
        original()

    monkeypatch.setattr(db_session, "commit", counting_commit)
    locked = crud_state.lock_for_account(db_session, account_id=test_user.account_id)

    assert locked is not None
    assert locked.account_id == test_user.account_id
    assert commits["n"] == 1


def test_lock_for_account_does_not_commit_an_existing_row(
    db_session, test_user, monkeypatch
):
    """An already created row is skip-locked without a first-touch commit."""
    crud_state.get_or_create(db_session, account_id=test_user.account_id)
    db_session.flush()
    commits = {"n": 0}

    def counting_commit() -> None:
        commits["n"] += 1

    monkeypatch.setattr(db_session, "commit", counting_commit)
    locked = crud_state.lock_for_account(db_session, account_id=test_user.account_id)

    assert locked is not None
    assert commits["n"] == 0
