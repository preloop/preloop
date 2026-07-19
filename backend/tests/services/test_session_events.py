"""OSS-side tests for the first-session hook extension point.

Without an EE plugin registering a hook, session recording must behave
exactly as before: the notification is an inert no-op. These tests run
against the OSS tree alone — no plugin linked.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from preloop.models.crud import crud_runtime_session
from preloop.services import session_events


@pytest.fixture(autouse=True)
def _clean_hook_registry():
    """Isolate the module-global hook registry across tests."""
    previous = session_events.get_first_session_hook()
    session_events.register_first_session_hook(None)
    yield
    session_events.register_first_session_hook(previous)


def _record_session(db, account_id, source_id: str):
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="agent_control",
        session_source_id=source_id,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )


class TestHookRegistry:
    def test_no_hook_registered_by_default(self):
        assert session_events.get_first_session_hook() is None

    def test_register_and_clear(self):
        hook = MagicMock()
        session_events.register_first_session_hook(hook)
        assert session_events.get_first_session_hook() is hook
        session_events.register_first_session_hook(None)
        assert session_events.get_first_session_hook() is None

    def test_notify_is_noop_without_hook(self):
        db = MagicMock()
        # Must not raise, must not touch the db.
        session_events.notify_first_session_recorded(
            db,
            account_id="irrelevant",
            occurred_at=datetime.now(timezone.utc),
        )
        db.assert_not_called()


class TestFirstSessionSignal:
    def test_session_recording_works_without_hook(self, db_session, test_user):
        """OSS default: recording an account's first session is unchanged."""
        session = _record_session(db_session, test_user.account_id, "sess-oss-1")
        assert session.id is not None
        assert session_events.get_first_session_hook() is None

    def test_hook_fires_once_for_first_session_only(self, db_session, test_user):
        calls = []

        def hook(db, *, account_id, occurred_at):
            calls.append((account_id, occurred_at))

        session_events.register_first_session_hook(hook)

        _record_session(db_session, test_user.account_id, "sess-hook-1")
        assert len(calls) == 1
        assert calls[0][0] == test_user.account_id
        assert calls[0][1] is not None

        # Second, distinct session for the same account: no new signal.
        _record_session(db_session, test_user.account_id, "sess-hook-2")
        assert len(calls) == 1

        # Re-upserting an existing session: no new signal either.
        _record_session(db_session, test_user.account_id, "sess-hook-1")
        assert len(calls) == 1

    def test_hook_failure_never_breaks_session_recording(self, db_session, test_user):
        def exploding_hook(db, *, account_id, occurred_at):
            raise RuntimeError("analytics outage")

        session_events.register_first_session_hook(exploding_hook)

        session = _record_session(db_session, test_user.account_id, "sess-boom-1")
        assert session.id is not None
