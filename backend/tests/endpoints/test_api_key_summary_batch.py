"""Real-query regression tests for list API-key activity summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth import router as auth_router
from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_api_usage,
    crud_runtime_session_activity,
)
from preloop.schemas.auth import AuthUserResponse

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class FrozenDateTime(datetime):
    """Keep every summary's inclusive recent boundary identical."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def _current_user(user: models.User) -> AuthUserResponse:
    return AuthUserResponse(
        id=user.id,
        account_id=user.account_id,
        username=user.username,
        email=user.email,
        email_verified=False,
        team_ids=[],
    )


def _keys(db: Session, user: models.User, count: int) -> list[models.ApiKey]:
    keys = [
        models.ApiKey(
            id=uuid4(),
            account_id=user.account_id,
            user_id=user.id,
            name=f"Synthetic key {index}",
            scopes=["read"],
            is_active=index % 2 == 0,
            created_at=(NOW - timedelta(seconds=index)).replace(tzinfo=None),
            context_data={
                "managed_agent_id": str(uuid4()),
                "runtime_principal": {"type": "custom", "id": f"key-{index}"},
            },
        )
        for index in range(count)
    ]
    db.add_all(keys)
    db.flush()
    return keys


def _facts(db: Session, key: models.ApiKey) -> None:
    session = models.RuntimeSession(
        id=uuid4(),
        account_id=key.account_id,
        session_source_type="custom",
        session_source_id=str(key.id),
        session_reference="synthetic",
        started_at=NOW,
    )
    db.add(session)
    db.flush()
    cutoff = NOW - auth_router.API_KEY_RECENT_WINDOW
    for timestamp in [cutoff - timedelta(microseconds=1), cutoff, NOW, NOW]:
        db.add(
            models.ApiUsage(
                id=uuid4(),
                account_id=key.account_id,
                api_key_id=key.id,
                endpoint="/synthetic",
                method="POST",
                status_code=500,
                duration=0.01,
                action_type="model_gateway",
                timestamp=timestamp.replace(tzinfo=None),
                is_retry=True,
                meta_data={"purpose": "replay_validation"},
            )
        )
        db.add(
            models.RuntimeSessionActivity(
                id=uuid4(),
                account_id=key.account_id,
                api_key_id=key.id,
                runtime_session_id=session.id,
                activity_type="tool_call",
                timestamp=timestamp,
                status=None,
            )
        )
    # Unrelated activity/action types never become model or tool calls.
    db.add(
        models.ApiUsage(
            account_id=key.account_id,
            api_key_id=key.id,
            endpoint="/synthetic",
            method="GET",
            status_code=200,
            duration=0.01,
            action_type="unrelated",
            timestamp=(NOW + timedelta(days=1)).replace(tzinfo=None),
        )
    )
    db.add(
        models.RuntimeSessionActivity(
            account_id=key.account_id,
            api_key_id=key.id,
            runtime_session_id=session.id,
            activity_type="model_gateway_call",
            timestamp=NOW + timedelta(days=1),
        )
    )
    db.flush()


@pytest.mark.parametrize("key_count", [1, 12])
def test_list_key_summaries_use_three_queries(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    key_count: int,
) -> None:
    """The list and two grouped aggregates remain constant as key count grows."""
    monkeypatch.setattr(auth_router, "datetime", FrozenDateTime)
    keys = _keys(db_session, test_user, key_count)
    for key in keys:
        _facts(db_session, key)
    current = _current_user(test_user)
    expected = [auth_router._build_api_key_summary(db_session, key) for key in keys]
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        actual = auth_router.list_api_keys(current, db_session)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert [row.model_dump() for row in actual] == [
        row.model_dump() for row in expected
    ]
    assert len(statements) == 3
    assert all(row.recent_model_calls == 3 for row in actual)
    assert all(row.recent_tool_calls == 3 for row in actual)


def test_empty_key_list_avoids_activity_queries(
    db_session: Session,
    test_user: models.User,
) -> None:
    """No keys need neither activity table nor per-key work."""
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        assert auth_router.list_api_keys(_current_user(test_user), db_session) == []
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(statements) == 1


def test_key_summary_last_use_without_calls_and_nullable_context(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle/revoked keys, naive timestamps and malformed context retain parity."""
    monkeypatch.setattr(auth_router, "datetime", FrozenDateTime)
    keys = _keys(db_session, test_user, 3)
    keys[0].last_used_at = NOW.replace(tzinfo=None)
    keys[0].context_data = None
    keys[1].context_data = {"runtime_principal": "invalid"}
    keys[2].context_data = []
    db_session.flush()
    expected = [auth_router._build_api_key_summary(db_session, key) for key in keys]
    actual = auth_router.list_api_keys(_current_user(test_user), db_session)
    assert [row.model_dump() for row in actual] == [
        row.model_dump() for row in expected
    ]
    assert [row.activity_status for row in actual] == ["active_now", "revoked", "idle"]
    assert all(row.recent_model_calls == row.recent_tool_calls == 0 for row in actual)


def test_foreign_users_keys_are_not_listed(
    db_session: Session,
    test_user: models.User,
) -> None:
    """The existing user-ownership selector excludes unrelated credentials."""
    keys = _keys(db_session, test_user, 1)
    foreign_account = models.Account(organization_name="Foreign synthetic")
    db_session.add(foreign_account)
    db_session.flush()
    foreign = models.User(
        account_id=foreign_account.id,
        username=f"foreign-{uuid4()}",
        email=f"foreign-{uuid4()}@example.com",
        hashed_password="not-an-authentication-credential",
    )
    db_session.add(foreign)
    db_session.flush()
    _keys(db_session, foreign, 1)
    actual = auth_router.list_api_keys(_current_user(test_user), db_session)
    assert [row.id for row in actual] == [keys[0].id]
    assert crud_api_key.get_by_user(db_session, username=test_user.username) == keys


def test_batched_fact_queries_verify_key_and_fact_accounts(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Foreign IDs/facts cannot influence an owned key; legacy NULL is owned."""
    own = _keys(db_session, test_user, 1)[0]
    foreign_account = models.Account(organization_name="Other facts tenant")
    db_session.add(foreign_account)
    db_session.flush()
    foreign_user = models.User(
        account_id=foreign_account.id,
        username=f"facts-{uuid4()}",
        email=f"facts-{uuid4()}@example.com",
        hashed_password="synthetic",
    )
    db_session.add(foreign_user)
    db_session.flush()
    foreign_key = _keys(db_session, foreign_user, 1)[0]
    _facts(db_session, own)
    _facts(db_session, foreign_key)
    later = NOW + timedelta(days=1)
    for account_id in [None, foreign_account.id]:
        db_session.add(
            models.ApiUsage(
                account_id=account_id,
                api_key_id=own.id,
                endpoint="/synthetic",
                method="POST",
                status_code=200,
                duration=0.01,
                action_type="model_gateway",
                timestamp=later.replace(tzinfo=None),
            )
        )
    runtime = models.RuntimeSession(
        account_id=foreign_account.id,
        session_source_type="custom",
        session_source_id="foreign-corrupt-fact",
        session_reference="synthetic",
        started_at=NOW,
    )
    db_session.add(runtime)
    db_session.flush()
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=foreign_account.id,
            api_key_id=own.id,
            runtime_session_id=runtime.id,
            activity_type="tool_call",
            timestamp=later,
        )
    )
    db_session.flush()
    kwargs = {
        "account_id": test_user.account_id,
        "api_key_ids": [own.id, foreign_key.id, uuid4()],
        "recent_start": NOW - auth_router.API_KEY_RECENT_WINDOW,
    }
    model = crud_api_usage.get_model_call_stats_for_api_keys(db_session, **kwargs)
    tool = crud_runtime_session_activity.get_tool_call_stats_for_api_keys(
        db_session, **kwargs
    )
    assert model == {own.id: (later.replace(tzinfo=None), 4)}
    assert tool == {own.id: (NOW, 3)}
    string_kwargs = {
        **kwargs,
        "account_id": str(test_user.account_id),
        "api_key_ids": [str(own.id), str(foreign_key.id)],
    }
    assert (
        crud_api_usage.get_model_call_stats_for_api_keys(db_session, **string_kwargs)
        == model
    )
    assert (
        crud_runtime_session_activity.get_tool_call_stats_for_api_keys(
            db_session, **string_kwargs
        )
        == tool
    )


def test_empty_batched_fact_queries_do_not_read_database(
    db_session: Session,
    test_user: models.User,
) -> None:
    """Empty selectors do no work rather than accidentally aggregating a tenant."""
    statements: list[str] = []

    def capture(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        kwargs = {
            "account_id": test_user.account_id,
            "api_key_ids": [],
            "recent_start": NOW,
        }
        assert (
            crud_api_usage.get_model_call_stats_for_api_keys(db_session, **kwargs) == {}
        )
        assert (
            crud_runtime_session_activity.get_tool_call_stats_for_api_keys(
                db_session, **kwargs
            )
            == {}
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements == []
