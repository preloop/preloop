"""Database failures must recover without leaking SQL or masking provider errors."""

from typing import Any, Generator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.models import models


def database_error() -> OperationalError:
    return OperationalError(
        "SELECT private_column FROM private_table WHERE token = :token",
        {"token": "private-value"},
        Exception("connection lost"),
    )


@pytest.fixture
def tool_session(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    engine = create_engine("sqlite://")
    with Session(engine) as session:

        def get_db() -> Generator[Session, None, None]:
            yield session

        monkeypatch.setattr(mcp, "get_db", get_db)
        yield session
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 502])
@pytest.mark.parametrize(
    "detail", ["Provider InternalError", "psycopg unavailable", "sqlalchemy timeout"]
)
async def test_provider_errors_keep_their_status_and_detail(
    tool_session: Session, status: int, detail: str
) -> None:
    error = HTTPException(status_code=status, detail=detail)

    @mcp._with_tool_db
    async def tool() -> None:
        mcp._get_tool_db().execute(text("SELECT 1"))
        raise error

    with pytest.raises(HTTPException) as caught:
        await tool()
    assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", ["cause", "context"])
async def test_nested_database_exceptions_are_sanitized(
    tool_session: Session, chain: str
) -> None:
    inner = RuntimeError("intermediate wrapper")
    error = HTTPException(status_code=502, detail="provider wrapper")
    setattr(inner, f"__{chain}__", database_error())
    setattr(error, f"__{chain}__", inner)

    @mcp._with_tool_db
    async def tool() -> None:
        mcp._get_tool_db().execute(text("SELECT 1"))
        raise error

    with pytest.raises(HTTPException) as caught:
        await tool()
    assert caught.value.detail == mcp.DATABASE_ERROR_DETAIL
    assert caught.value.status_code == 500


def test_cyclic_exception_chain_is_not_a_database_error() -> None:
    first = RuntimeError("provider error")
    second = RuntimeError("provider wrapper")
    first.__cause__ = second
    second.__cause__ = first
    assert not mcp._is_database_error(first)


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_once_and_restores_expiration(
    tool_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollback = Mock(wraps=tool_session.rollback)
    monkeypatch.setattr(tool_session, "rollback", rollback)
    monkeypatch.setattr(tool_session, "commit", Mock(side_effect=database_error()))

    @mcp._with_tool_db
    async def tool() -> str:
        mcp._get_tool_db().execute(text("SELECT 1"))
        return "done"

    with pytest.raises(HTTPException) as caught:
        await tool()
    assert caught.value.detail == mcp.DATABASE_ERROR_DETAIL
    assert caught.value.status_code == 500
    assert tool_session.expire_on_commit is True
    rollback.assert_called_once_with()
    assert mcp._tool_db.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate_fails", [False, True])
async def test_rollback_failure_invalidates_without_masking_original_error(
    tool_session: Session, monkeypatch: pytest.MonkeyPatch, invalidate_fails: bool
) -> None:
    error = HTTPException(status_code=502, detail="Provider unavailable")
    monkeypatch.setattr(tool_session, "rollback", Mock(side_effect=database_error()))
    invalidate = Mock(
        wraps=tool_session.invalidate,
        side_effect=database_error() if invalidate_fails else None,
    )
    monkeypatch.setattr(tool_session, "invalidate", invalidate)

    @mcp._with_tool_db
    async def tool() -> None:
        mcp._get_tool_db().execute(text("SELECT 1"))
        raise error

    with pytest.raises(HTTPException) as caught:
        await tool()
    assert caught.value is error
    invalidate.assert_called_once_with()
    assert mcp._tool_db.get() is None


@pytest.mark.asyncio
async def test_failed_database_rollback_does_not_claim_success(
    tool_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tool_session, "rollback", Mock(side_effect=database_error()))

    @mcp._with_tool_db
    async def tool() -> None:
        mcp._get_tool_db().execute(text("SELECT 1"))
        raise database_error()

    with pytest.raises(HTTPException) as caught:
        await tool()
    assert caught.value.status_code == 500
    assert "rollback failed" in caught.value.detail
    assert "private" not in caught.value.detail
    assert "rolled back" not in caught.value.detail


@pytest.mark.asyncio
async def test_handled_flush_failure_preserves_partial_provider_result(
    tool_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    models.Permission.__table__.create(tool_session.get_bind())
    rollback = Mock(wraps=tool_session.rollback)
    monkeypatch.setattr(tool_session, "rollback", rollback)

    @mcp._with_tool_db
    async def tool() -> str:
        db = mcp._get_tool_db()
        db.add(models.Permission(name="example", category="test"))
        try:
            db.flush()  # Required description is missing; this deactivates the session.
        except mcp.SQLAlchemyError:
            # The missing description makes flush fail and deactivates the session.
            pass
        assert not db.is_active
        return "partial: provider write completed, cache update failed"

    assert await tool() == "partial: provider write completed, cache update failed"
    rollback.assert_called_once_with()
    assert tool_session.is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["estimate_compliance", "improve_compliance"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_batch_tools_sanitize_database_errors_and_rollback_each_item(
    tool_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    wrapped: bool,
) -> None:
    rollback = Mock(wraps=tool_session.rollback)
    monkeypatch.setattr(tool_session, "rollback", rollback)
    monkeypatch.setattr(mcp, "get_http_request", lambda: SimpleNamespace(headers={}))

    async def authenticate(headers: Any) -> tuple[Session, Any]:
        return mcp._get_tool_db(), SimpleNamespace(account_id="account")

    monkeypatch.setattr(mcp, "_get_authenticated_user", authenticate)
    calls = 0

    def fail(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        # Recovery must happen before the next item, not just at tool exit.
        assert rollback.call_count == calls
        calls += 1
        try:
            raise database_error()
        except OperationalError as error:
            if wrapped:
                raise HTTPException(status_code=500, detail=str(error)) from error
            raise

    monkeypatch.setattr(mcp, "_find_issue_by_identifier", fail)
    result = await getattr(mcp, tool_name)(["EX-1", "EX-2"])
    assert result.metadata.failed_count == 2
    assert result.metadata.errors == [
        f"EX-1: {mcp.DATABASE_ERROR_DETAIL}",
        f"EX-2: {mcp.DATABASE_ERROR_DETAIL}",
    ]
    assert rollback.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_session", [False, True])
async def test_postgres_failed_statement_recovers_next_invocation(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, reuse_session: bool
) -> None:
    """Exercise PostgreSQL's aborted transaction state and the real DB dependency."""
    from preloop.models.db import session as db_module

    sessions: list[Session] = []
    rollbacks: list[Session] = []

    def factory() -> Session:
        if reuse_session and sessions:
            return sessions[0]
        session = Session(db_engine)
        event.listen(session, "after_rollback", lambda db: rollbacks.append(db))
        sessions.append(session)
        return session

    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    @mcp._with_tool_db
    async def tool(fail: bool) -> int:
        db = mcp._get_tool_db()
        if fail:
            db.execute(text("SELECT 1 / :denominator"), {"denominator": 0})
        return db.execute(text("SELECT 1")).scalar_one()

    try:
        with pytest.raises(HTTPException) as caught:
            await tool(True)
        assert caught.value.detail == mcp.DATABASE_ERROR_DETAIL
        assert rollbacks == [sessions[0]]
        assert await tool(False) == 1
        assert len(sessions) == (1 if reuse_session else 2)
        assert all(not session.in_transaction() for session in sessions)
    finally:
        for session in sessions:
            session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["estimate_compliance", "improve_compliance"])
async def test_postgres_batch_recovers_before_next_item(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    with Session(db_engine) as session:

        def get_db() -> Generator[Session, None, None]:
            yield session

        async def authenticate(headers: Any) -> tuple[Session, Any]:
            return mcp._get_tool_db(), SimpleNamespace(account_id="account")

        def find_issue(db: Session, identifier: str, account_id: str) -> None:
            if identifier == "EX-1":
                db.execute(text("SELECT 1 / :denominator"), {"denominator": 0})
            # PostgreSQL would raise InFailedSqlTransaction without the item rollback.
            assert db.execute(text("SELECT 1")).scalar_one() == 1
            raise mcp.IssueNotFoundError("Example issue is missing")

        monkeypatch.setattr(mcp, "get_db", get_db)
        monkeypatch.setattr(
            mcp, "get_http_request", lambda: SimpleNamespace(headers={})
        )
        monkeypatch.setattr(mcp, "_get_authenticated_user", authenticate)
        monkeypatch.setattr(mcp, "_find_issue_by_identifier", find_issue)
        result = await getattr(mcp, tool_name)(["EX-1", "EX-2"])
        assert result.metadata.errors == [
            f"EX-1: {mcp.DATABASE_ERROR_DETAIL}",
            "EX-2: Issue not found: Example issue is missing",
        ]
