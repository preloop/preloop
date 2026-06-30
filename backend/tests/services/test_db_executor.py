"""Tests for shared thread-pool DB session helpers."""

import asyncio

import pytest
from sqlalchemy import inspect as sa_inspect, text

from preloop.services import db_executor
from preloop.services.db_executor import (
    detach_user,
    run_db_async,
    run_db_sync,
)


class TestDetachUser:
    """Tests for detach_user."""

    def test_none_returns_none(self, db_session):
        assert detach_user(db_session, None) is None

    def test_detaches_user_from_session(self, db_session, test_user):
        # Sanity: user is currently tracked by the session
        assert test_user in db_session

        result = detach_user(db_session, test_user)

        assert result is test_user
        assert test_user not in db_session
        assert sa_inspect(test_user).detached is True

    def test_scalar_attributes_loaded_before_detach(self, db_session, test_user):
        # After detach the user is expunged; scalar attributes must still be
        # accessible without a session-bound lazy load raising.
        detached = detach_user(db_session, test_user)
        # These were eagerly accessed inside detach_user; reading them now must
        # not trigger a DB round-trip on a detached instance.
        assert detached.email == "test@example.com"
        assert detached.username == "testuser"
        assert detached.is_active is True


class TestRunDbSync:
    """Tests for run_db_sync."""

    def test_runs_operation_and_returns_value(self):
        result = run_db_sync(lambda db: db.execute(text("SELECT 1")).scalar())
        assert result == 1

    def test_operation_receives_session(self):
        captured = {}

        def op(db):
            captured["has_execute"] = hasattr(db, "execute")
            return "ok"

        assert run_db_sync(op) == "ok"
        assert captured["has_execute"] is True

    def test_exception_propagates(self):
        def op(db):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            run_db_sync(op)

    def test_session_closed_even_on_error(self, monkeypatch):
        closed = {"called": False}

        real_close = db_executor._safe_close_db_session

        def spy_close(db):
            closed["called"] = True
            return real_close(db)

        monkeypatch.setattr(db_executor, "_safe_close_db_session", spy_close)

        with pytest.raises(ValueError):
            run_db_sync(lambda db: (_ for _ in ()).throw(ValueError("x")))

        assert closed["called"] is True


class TestRunDbAsync:
    """Tests for run_db_async."""

    @pytest.mark.asyncio
    async def test_runs_operation_off_loop(self):
        result = await run_db_async(lambda db: db.execute(text("SELECT 2")).scalar())
        assert result == 2

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        with pytest.raises(ValueError, match="async-boom"):
            await run_db_async(
                lambda db: (_ for _ in ()).throw(ValueError("async-boom"))
            )

    @pytest.mark.asyncio
    async def test_runs_in_executor_thread(self):
        main_thread = (
            asyncio.get_event_loop()._thread_id
            if hasattr(asyncio.get_event_loop(), "_thread_id")
            else None
        )
        import threading

        main_ident = threading.get_ident()

        def op(db):
            return threading.get_ident()

        worker_ident = await run_db_async(op)
        # Work should run in a separate pool thread, not the event loop thread.
        assert worker_ident != main_ident
        _ = main_thread
