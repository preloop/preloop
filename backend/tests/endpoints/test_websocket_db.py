"""Tests for the WebSocket short-lived database session helpers.

``preloop.api.endpoints.websocket_db`` is not an HTTP router -- it re-exports
two helpers used by WebSocket handlers:

* ``detach_user`` -- eagerly loads a user's scalar attributes and expunges it
  from its session so it can be safely used after the session closes.
* ``run_db_async`` -- runs a sync DB callable off the event loop in a
  short-lived session.

These tests exercise the helpers directly (there is no route to call). The
``run_db_async`` tests use the real test Postgres via a short-lived session,
performing only read-only ``SELECT`` statements so they remain hermetic.
"""

import asyncio

from sqlalchemy import text

from preloop.api.endpoints import websocket_db
from preloop.models.crud import crud_account, crud_user


def _make_user(db, email="ws@example.com"):
    account = crud_account.create(
        db, obj_in={"organization_name": "WS Org", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "WS User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_expected_helpers():
    """The module should publicly re-export the two helper callables."""
    assert set(websocket_db.__all__) == {"detach_user", "run_db_async"}
    assert callable(websocket_db.detach_user)
    assert asyncio.iscoroutinefunction(websocket_db.run_db_async)


# ---------------------------------------------------------------------------
# detach_user
# ---------------------------------------------------------------------------


def test_detach_user_none_returns_none(db_session):
    """Passing None should be a no-op returning None."""
    assert websocket_db.detach_user(db_session, None) is None


def test_detach_user_expunges_user_from_session(db_session):
    """A detached user must no longer be tracked by the session."""
    user = _make_user(db_session, "detach@example.com")
    assert user in db_session

    returned = websocket_db.detach_user(db_session, user)

    assert returned is user
    assert user not in db_session


def test_detach_user_scalars_remain_accessible_after_detach(db_session):
    """Scalar attributes should be loaded before detaching so they survive."""
    user = _make_user(db_session, "scalars@example.com")
    user_id = user.id
    account_id = user.account_id

    detached = websocket_db.detach_user(db_session, user)

    # Accessing these after expunge would raise DetachedInstanceError if the
    # helper had not eagerly loaded them.
    assert detached.id == user_id
    assert detached.account_id == account_id
    assert detached.username == "scalars"
    assert detached.email == "scalars@example.com"
    assert detached.is_active is True


# ---------------------------------------------------------------------------
# run_db_async
# ---------------------------------------------------------------------------


def test_run_db_async_returns_operation_result():
    """The helper should run the callable and return its value."""
    result = asyncio.run(
        websocket_db.run_db_async(lambda db: db.execute(text("SELECT 1")).scalar())
    )
    assert result == 1


def test_run_db_async_provides_a_usable_session():
    """The callable should receive a live Session it can query through."""

    def _op(db):
        # A trivial scalar query proves the session is connected/usable.
        return db.execute(text("SELECT 42")).scalar()

    assert asyncio.run(websocket_db.run_db_async(_op)) == 42


def test_run_db_async_propagates_exceptions():
    """Errors raised inside the operation should surface to the awaiter."""

    def _boom(db):
        raise ValueError("kaboom")

    try:
        asyncio.run(websocket_db.run_db_async(_boom))
    except ValueError as exc:
        assert str(exc) == "kaboom"
    else:  # pragma: no cover - failure path
        raise AssertionError("expected ValueError to propagate")


def test_run_db_async_runs_off_the_event_loop():
    """Successive calls should each complete with their own short-lived session."""

    async def _drive():
        first = await websocket_db.run_db_async(
            lambda db: db.execute(text("SELECT 1")).scalar()
        )
        second = await websocket_db.run_db_async(
            lambda db: db.execute(text("SELECT 2")).scalar()
        )
        return first, second

    assert asyncio.run(_drive()) == (1, 2)
