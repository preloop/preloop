"""Pool checkout waits must not stall probes or invalidate credentials."""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from preloop.api.auth import jwt
from preloop.api.auth.router import router as auth_router
from preloop.api.endpoints import agent_control, health
from preloop.models.db.session import get_db_session


@pytest.mark.asyncio
async def test_refresh_pool_wait_keeps_ping_responsive(monkeypatch: Any) -> None:
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(health.router)
    app.dependency_overrides[get_db_session] = object
    started = threading.Event()
    release = threading.Event()

    def wait_for_pool(*args: Any, **kwargs: Any) -> None:
        started.set()
        release.wait(2)
        raise PoolTimeout("test pool exhausted")

    @app.exception_handler(PoolTimeout)
    async def unavailable(request: Any, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Database capacity unavailable"}, 503)

    monkeypatch.setattr(jwt.crud_user, "get", wait_for_pool)
    token = jwt.create_refresh_token(sub=str(uuid4()), scopes=[])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        refresh = asyncio.create_task(
            client.post("/refresh", json={"refresh_token": token})
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            ping = await client.get("/ping")
            assert ping.status_code == 200
            assert not refresh.done(), "refresh checkout blocked the event loop"
        finally:
            release.set()
            result = await refresh
        assert result.status_code == 503


def test_jwt_pool_failure_is_not_invalid_credentials(monkeypatch: Any) -> None:
    def exhausted(*args: Any, **kwargs: Any) -> None:
        raise PoolTimeout("test pool exhausted")

    monkeypatch.setattr(jwt.crud_user, "get", exhausted)
    token = jwt.create_access_token({"sub": str(uuid4()), "scopes": []})
    with pytest.raises(PoolTimeout):
        jwt.get_current_user(token=token, db=MagicMock())


@pytest.mark.asyncio
async def test_control_auth_pool_wait_keeps_loop_responsive(monkeypatch: Any) -> None:
    started = threading.Event()
    release = threading.Event()

    def wait_for_pool(*args: Any, **kwargs: Any) -> None:
        started.set()
        release.wait(2)
        raise PoolTimeout("test pool exhausted")

    monkeypatch.setattr(
        agent_control, "authenticate_runtime_bearer_token", wait_for_pool
    )
    ws = MagicMock()
    ws.headers = {"authorization": "Bearer synthetic-control-token"}
    ws.close = AsyncMock()
    with Session(bind=create_engine("sqlite://")) as db:
        connection = asyncio.create_task(
            agent_control.managed_agent_control_websocket(ws, db)
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.sleep(0)
            assert not connection.done(), "control auth checkout blocked the event loop"
        finally:
            release.set()
            with pytest.raises(PoolTimeout):
                result = await connection
                pytest.fail(f"control auth returned {result!r}")


@pytest.mark.asyncio
async def test_redelivery_releases_connection_before_socket_send(
    monkeypatch: Any,
) -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    connection = agent_control.AgentControlConnectionContext(
        account_id="account",
        managed_agent_id="agent",
        runtime_session_id="session",
        session_source_type="test",
        session_source_id="source",
        managed_agent_session_source_type="test",
        managed_agent_session_source_id="source",
    )
    record = MagicMock(
        command_id="command",
        envelope={"type": "command", "message_id": "command"},
        kind="command",
        status="pending",
        expires_at=None,
    )

    def load(db: Session, **kwargs: Any) -> list[Any]:
        db.connection()
        assert engine.pool.checkedout() == 1
        return [record]

    monkeypatch.setattr(
        agent_control.crud_agent_control_command, "expire_stale", MagicMock()
    )
    monkeypatch.setattr(
        agent_control.crud_agent_control_command, "get_undelivered_for_agent", load
    )
    monkeypatch.setattr(
        agent_control.control_connection, "authorize", MagicMock(return_value=True)
    )
    monkeypatch.setattr(
        agent_control.crud_agent_control_command,
        "get_by_command_id",
        lambda db, **kwargs: load(db)[0],
    )
    marked = MagicMock()
    monkeypatch.setattr(
        agent_control.crud_agent_control_command, "mark_delivered", marked
    )

    async def send(envelope: dict[str, Any]) -> None:
        assert envelope == {"type": "command", "message_id": "command"}
        assert engine.pool.checkedout() == 0

    with Session(engine) as dependency:
        database = agent_control._ControlDatabase(dependency)
        await agent_control._redeliver_pending_commands(
            database, connection, MagicMock(send_json=send)
        )
    marked.assert_called_once()
    assert engine.pool.checkedout() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_control_phase_drains_worker_before_next_phase() -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    entered = threading.Event()
    release = threading.Event()
    sessions: list[Session] = []

    def wait(db: Session) -> None:
        sessions.append(db)
        db.connection()
        entered.set()
        assert release.wait(2)

    with Session(engine) as dependency:
        database = agent_control._ControlDatabase(dependency)
        first = asyncio.create_task(database.run(wait))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        second = asyncio.create_task(database.run(lambda db: sessions.append(db)))
        await asyncio.sleep(0.02)
        assert len(sessions) == 1
        assert engine.pool.checkedout() == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            result = await first
            pytest.fail(f"cancelled phase returned {result!r}")
        assert await second is None
    assert sessions[0] is not sessions[1]
    assert engine.pool.checkedout() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_stalled_eviction_close_cannot_pin_global_registration(
    monkeypatch: Any,
) -> None:
    """A dead socket must not prevent unrelated agents from registering."""
    from preloop.api.endpoints import agent_control as control

    monkeypatch.setattr(control, "EVICTION_CLOSE_TIMEOUT_SECONDS", 0.02)
    manager = control.AgentControlConnectionManager()
    close_started = asyncio.Event()

    async def stalled_close(**kwargs: Any) -> None:
        close_started.set()
        await asyncio.Event().wait()

    old_socket = AsyncMock()
    old_socket.close.side_effect = stalled_close
    await manager.connect(managed_agent_id="synthetic-agent", websocket=old_socket)

    async def register(agent_id: str) -> str:
        async with manager.registration_lock:
            return await manager.connect(
                managed_agent_id=agent_id, websocket=AsyncMock()
            )

    replacement = asyncio.create_task(register("synthetic-agent"))
    await asyncio.wait_for(close_started.wait(), 1)
    unrelated = asyncio.create_task(register("unrelated-agent"))
    try:
        await asyncio.wait_for(asyncio.gather(replacement, unrelated), 0.5)
        assert manager.snapshot("unrelated-agent")["online"]
        assert manager.snapshot("synthetic-agent")["online"]
    finally:
        for task in (replacement, unrelated):
            task.cancel()
        await asyncio.gather(replacement, unrelated, return_exceptions=True)
