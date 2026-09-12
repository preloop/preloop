"""Real PostgreSQL / real WebSocket acceptance for worker-owned control phases.

Run against a migrated disposable database using DATABASE_URL. No external
broker, notification, agent, or paid model is contacted.
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import socket
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session
from unittest.mock import AsyncMock

from preloop.api.auth.jwt import create_refresh_token
from preloop.api.auth.router import router as auth_router
from preloop.api.endpoints import agent_control as control, health
from preloop.models import models
from preloop.models.crud import crud_agent_control_command, crud_managed_agent
from preloop.models.crud import agent_control_connection as ownership
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_control import AgentControlInboundEnvelope


@dataclass(frozen=True)
class Principal:
    token: str
    connection: ownership.AgentControlConnectionContext


@pytest.fixture
def principal(db_engine: Engine) -> Iterator[list[Principal]]:
    assert db_engine.dialect.name == "postgresql", "Acceptance requires PostgreSQL"
    principals = []
    account_id = uuid4()
    with Session(db_engine) as db:
        account = models.Account(id=account_id, organization_name="Control acceptance")
        user = models.User(
            id=uuid4(),
            account_id=account_id,
            email=f"{uuid4()}@example.com",
            username=str(uuid4()),
            hashed_password="synthetic",
            is_active=True,
        )
        db.add_all([account, user])
        db.flush()
        for _ in range(4):
            source = str(uuid4())
            runtime = models.RuntimeSession(
                id=uuid4(),
                account_id=account_id,
                session_source_type="openclaw",
                session_source_id=source,
                started_at=datetime.now(UTC),
                last_activity_at=datetime.now(UTC),
            )
            db.add(runtime)
            db.flush()
            agent = models.ManagedAgent(
                id=uuid4(),
                account_id=account_id,
                runtime_session_id=runtime.id,
                agent_kind="openclaw",
                session_source_type="openclaw",
                session_source_id=source,
                display_name="Synthetic control agent",
                lifecycle_state="active",
                lifecycle_updated_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
            db.add(agent)
            db.flush()
            token = f"synthetic-{uuid4()}"
            key = models.ApiKey(
                id=uuid4(),
                account_id=account_id,
                user_id=user.id,
                name=str(uuid4()),
                key=token,
                is_active=True,
                scopes=[],
                context_data={
                    "managed_agent_id": str(agent.id),
                    "runtime_session_id": str(runtime.id),
                },
            )
            db.add(key)
            principals.append(
                Principal(
                    token,
                    ownership.AgentControlConnectionContext(
                        account_id=str(account_id),
                        user_id=str(user.id),
                        api_key_id=str(key.id),
                        managed_agent_id=str(agent.id),
                        runtime_session_id=str(runtime.id),
                        agent_kind="openclaw",
                        session_source_type="openclaw",
                        session_source_id=source,
                        managed_agent_session_source_type="openclaw",
                        managed_agent_session_source_id=source,
                        connection_id=str(uuid4()),
                    ),
                )
            )
        db.commit()
    try:
        yield principals
    finally:
        with Session(db_engine) as db:
            db.query(models.Account).filter_by(id=account_id).delete()
            db.commit()


@dataclass
class Server:
    url: str
    engine: Engine


@pytest.fixture
def control_server(db_engine: Engine, monkeypatch: Any) -> Iterator[Server]:
    engine = create_engine(db_engine.url, pool_size=2, max_overflow=0, pool_timeout=2)
    monkeypatch.setattr(
        control, "agent_control_manager", control.AgentControlConnectionManager()
    )
    monkeypatch.setattr(control, "get_nats_client", AsyncMock(return_value=None))
    monkeypatch.setattr(control, "emit_account_event", lambda *args, **kwargs: None)
    app = FastAPI()
    app.include_router(control.router)
    app.include_router(health.router)
    app.include_router(auth_router)

    def dependency() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db_session] = dependency

    @app.get("/auth")
    async def auth(token: str) -> dict[str, str]:
        with Session(engine) as dependency:
            identity = await control._ControlDatabase(dependency).run(
                lambda db: control._load_control_identity(db, token)
            )
        assert all(
            value is None or isinstance(value, str)
            for value in asdict(identity).values()
        )
        return {"account_id": identity.account_id}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield Server(f"127.0.0.1:{listener.getsockname()[1]}", engine)
    finally:
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()
        engine.dispose()


async def connect(server: Server, principal: Principal) -> Any:
    ws = await websockets.connect(
        f"ws://{server.url}/agents/control/ws",
        additional_headers={"Authorization": f"Bearer {principal.token}"},
    )
    assert json.loads(await asyncio.wait_for(ws.recv(), 3))["name"] == "connected"
    return ws


async def heartbeat(ws: Any) -> None:
    message_id = str(uuid4())
    await ws.send(
        json.dumps({"type": "heartbeat", "message_id": message_id, "payload": {}})
    )
    result = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert result["type"] == "ack" and result["message_id"] == message_id


async def pool_idle(engine: Engine) -> None:
    deadline = asyncio.get_running_loop().time() + 3
    while engine.pool.checkedout() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert engine.pool.checkedout() == 0


def persist_command(db_engine: Engine, principal: Principal) -> str:
    command_id = str(uuid4())
    with Session(db_engine) as db:
        crud_agent_control_command.create_command(
            db,
            account_id=principal.connection.account_id,
            managed_agent_id=principal.connection.managed_agent_id,
            runtime_session_id=principal.connection.runtime_session_id,
            command_id=command_id,
            envelope={
                "type": "command",
                "name": "send_message",
                "message_id": command_id,
                "payload": {"text": "Synthetic instruction"},
            },
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    return command_id


@pytest.mark.asyncio
async def test_pool_two_serves_four_idle_authenticated_sockets_and_auth(
    control_server: Server, principal: list[Principal]
) -> None:
    async with AsyncExitStack() as stack:
        sockets = [
            await stack.enter_async_context(await connect(control_server, p))
            for p in principal
        ]
        await pool_idle(control_server.engine)
        assert control_server.engine.pool.size() == 2
        async with httpx.AsyncClient(base_url=f"http://{control_server.url}") as client:
            assert (await client.get("/ping")).status_code == 200
            assert (
                await client.get("/auth", params={"token": principal[0].token})
            ).status_code == 200
            assert (
                await client.post(
                    "/refresh",
                    json={
                        "refresh_token": create_refresh_token(
                            sub=principal[0].connection.user_id, scopes=[]
                        )
                    },
                )
            ).status_code == 200
        for ws in sockets:
            await heartbeat(ws)
            await pool_idle(control_server.engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_kind", ["heartbeat", "ack"])
async def test_independent_row_lock_is_bounded_and_other_socket_http_progress(
    control_server: Server,
    principal: list[Principal],
    db_engine: Engine,
    message_kind: str,
) -> None:
    ws = await connect(control_server, principal[0])
    other = await connect(control_server, principal[1])
    command_id = persist_command(db_engine, principal[0])
    requested_lock = threading.Event()

    def before_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            message_kind == "heartbeat"
            and "FROM managed_agent" in statement
            and "FOR UPDATE" in statement
        ):
            requested_lock.set()
        if message_kind == "ack" and statement.lower().startswith(
            "update agent_control_command"
        ):
            requested_lock.set()

    event.listen(control_server.engine, "before_cursor_execute", before_execute)
    try:
        with Session(db_engine) as blocker:
            if message_kind == "heartbeat":
                blocker.query(models.ManagedAgent).filter_by(
                    id=principal[0].connection.managed_agent_id
                ).with_for_update().one()
            else:
                blocker.query(models.AgentControlCommand).filter_by(
                    command_id=command_id
                ).with_for_update().one()
            start = time.monotonic()
            await ws.send(
                json.dumps(
                    {"type": "heartbeat"}
                    if message_kind == "heartbeat"
                    else {
                        "type": "status",
                        "name": "command_ack",
                        "payload": {"command_id": command_id},
                    }
                )
            )
            assert await asyncio.to_thread(requested_lock.wait, 2)
            async with httpx.AsyncClient(
                base_url=f"http://{control_server.url}"
            ) as client:
                assert (
                    await asyncio.wait_for(client.get("/ping"), 0.8)
                ).status_code == 200
                assert (
                    await asyncio.wait_for(
                        client.get("/auth", params={"token": principal[1].token}), 0.8
                    )
                ).status_code == 200
                assert (
                    await asyncio.wait_for(
                        client.post(
                            "/refresh",
                            json={
                                "refresh_token": create_refresh_token(
                                    sub=principal[1].connection.user_id, scopes=[]
                                )
                            },
                        ),
                        0.8,
                    )
                ).status_code == 200
            await asyncio.wait_for(heartbeat(other), 0.8)
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 4)
            assert closed.value.rcvd.code == 1013
            assert time.monotonic() - start < 3
        await pool_idle(control_server.engine)
        with control_server.engine.connect() as conn:
            assert conn.execute(text("SHOW lock_timeout")).scalar() == "0"
        replacement = await connect(control_server, principal[0])
        # Pending command is replayed after the handshake.
        assert json.loads(await replacement.recv())["message_id"] == command_id
        await heartbeat(replacement)
        await replacement.close()
    finally:
        event.remove(control_server.engine, "before_cursor_execute", before_execute)
        await ws.close()
        await other.close()


@pytest.mark.asyncio
async def test_distinct_managers_replacement_fences_stale_ack_and_disconnect(
    control_server: Server,
    principal: list[Principal],
    db_engine: Engine,
    monkeypatch: Any,
) -> None:
    old = await connect(control_server, principal[0])
    monkeypatch.setattr(
        control, "agent_control_manager", control.AgentControlConnectionManager()
    )
    new = await connect(control_server, principal[0])
    await heartbeat(new)
    command_id = persist_command(db_engine, principal[0])
    await old.send(
        json.dumps(
            {
                "type": "status",
                "name": "command_ack",
                "payload": {"command_id": command_id},
            }
        )
    )
    with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
        await asyncio.wait_for(old.recv(), 3)
    assert closed.value.rcvd.code == control.EVICTION_CLOSE_CODE
    await heartbeat(new)
    with Session(db_engine) as db:
        agent = db.get(models.ManagedAgent, principal[0].connection.managed_agent_id)
        assert (
            agent.runtime_session_id is not None
            and agent.control_last_heartbeat_at is not None
        )
        assert (
            crud_agent_control_command.get_by_command_id(
                db, account_id=principal[0].connection.account_id, command_id=command_id
            ).status
            == "pending"
        )
    for _ in range(2):
        await new.send(
            json.dumps(
                {
                    "type": "status",
                    "name": "command_ack",
                    "payload": {"command_id": command_id},
                }
            )
        )
    await heartbeat(new)
    with Session(db_engine) as db:
        assert (
            crud_agent_control_command.get_by_command_id(
                db, account_id=principal[0].connection.account_id, command_id=command_id
            ).status
            == "acked"
        )
    await new.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["revoke", "suspended", "decommissioned", "inactive_user", "inactive_account"],
)
async def test_live_socket_revalidates_credential_and_lifecycle(
    control_server: Server, principal: list[Principal], db_engine: Engine, change: str
) -> None:
    ws = await connect(control_server, principal[0])
    command_id = persist_command(db_engine, principal[0])
    identity = principal[0].connection
    with Session(db_engine) as db:
        if change == "revoke":
            db.get(models.ApiKey, identity.api_key_id).is_active = False
        elif change == "inactive_user":
            db.get(models.User, identity.user_id).is_active = False
        elif change == "inactive_account":
            db.get(models.Account, identity.account_id).is_active = False
        else:
            crud_managed_agent.update_operator_state(
                db,
                account_id=identity.account_id,
                agent_id=identity.managed_agent_id,
                lifecycle_state=change,
            )
        db.commit()
    await ws.send(
        json.dumps(
            {
                "type": "status",
                "name": "command_ack",
                "payload": {"command_id": command_id},
            }
        )
    )
    with pytest.raises(websockets.exceptions.ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 3)
    with Session(db_engine) as db:
        assert (
            crud_agent_control_command.get_by_command_id(
                db, account_id=identity.account_id, command_id=command_id
            ).status
            == "pending"
        )
    await ws.close()


@pytest.mark.asyncio
async def test_cancelled_persistence_drains_and_replacement_survives_retirement(
    db_engine: Engine, principal: list[Principal]
) -> None:
    engine = create_engine(db_engine.url, pool_size=2, max_overflow=0)
    old = principal[0].connection
    replacement = replace(old, connection_id=str(uuid4()))
    entered = threading.Event()
    released = threading.Event()
    sessions = []

    def phase(db: Session) -> None:
        sessions.append(db)
        assert control._claim_control_connection(db, old, datetime.now(UTC))
        assert ownership.authorize(db, old) is not None
        entered.set()
        assert released.wait(3)
        control._touch_presence(db, old, observed_at=datetime.now(UTC))

    try:
        with Session(engine) as dependency:
            database = control._ControlDatabase(dependency)
            task = asyncio.create_task(database.run(phase))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            next_phase = asyncio.create_task(
                database.run(
                    lambda db: control._claim_control_connection(
                        db, replacement, datetime.now(UTC)
                    )
                )
            )
            await asyncio.sleep(0.05)
            assert not task.done() and not next_phase.done()
            released.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await next_phase
            assert not await database.run(
                lambda db: control._retire_control_presence(db, old, datetime.now(UTC))
            )
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_broker_callback_rechecks_replacement_and_revocation(
    db_engine: Engine, principal: list[Principal], monkeypatch: Any
) -> None:
    identity = principal[0].connection
    command_id = persist_command(db_engine, principal[0])
    callbacks = []

    class Broker:
        is_connected = True

        async def subscribe(self, subject: str, cb: Any) -> object:
            callbacks.append(cb)
            return object()

    monkeypatch.setattr(control, "get_nats_client", AsyncMock(return_value=Broker()))
    websocket = AsyncMock()
    with Session(db_engine) as dependency:
        database = control._ControlDatabase(dependency)
        assert await database.run(
            lambda db: control._claim_control_connection(
                db, identity, datetime.now(UTC)
            )
        )
        await control._subscribe_to_commands(
            managed_agent_id=identity.managed_agent_id,
            websocket=websocket,
            database=database,
            account_id=identity.account_id,
            connection=identity,
        )
        new = replace(identity, connection_id=str(uuid4()))
        assert await database.run(
            lambda db: control._claim_control_connection(db, new, datetime.now(UTC))
        )
        from types import SimpleNamespace

        notification = SimpleNamespace(
            data=json.dumps({"type": "command", "message_id": command_id}).encode()
        )
        await callbacks[0](notification)
        websocket.send_json.assert_not_awaited()
        with Session(db_engine) as db:
            assert (
                crud_agent_control_command.get_by_command_id(
                    db, account_id=identity.account_id, command_id=command_id
                ).status
                == "pending"
            )
        await control._subscribe_to_commands(
            managed_agent_id=new.managed_agent_id,
            websocket=websocket,
            database=database,
            account_id=new.account_id,
            connection=new,
        )
        with Session(db_engine) as db:
            db.get(models.ApiKey, identity.api_key_id).is_active = False
            db.commit()
        await callbacks[1](notification)
        websocket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_replacement_during_send_cannot_mark_delivery(
    db_engine: Engine, principal: list[Principal]
) -> None:
    identity = principal[0].connection
    replacement = replace(identity, connection_id=str(uuid4()))
    command_id = persist_command(db_engine, principal[0])
    engine = create_engine(db_engine.url, pool_size=2, max_overflow=0)
    try:
        with Session(engine) as dependency:
            database = control._ControlDatabase(dependency)
            assert await database.run(
                lambda db: control._claim_control_connection(
                    db, identity, datetime.now(UTC)
                )
            )

            async def send(payload: dict[str, Any]) -> None:
                assert engine.pool.checkedout() == 0
                assert payload["payload"]["text"] == "Synthetic instruction"
                assert await database.run(
                    lambda db: control._claim_control_connection(
                        db, replacement, datetime.now(UTC)
                    )
                )

            assert await control._send_control_command(
                database,
                identity,
                AsyncMock(send_json=send),
                {
                    "type": "command",
                    "message_id": command_id,
                    "payload": {"text": "Untrusted broker content"},
                },
            )
        with Session(db_engine) as db:
            assert (
                crud_agent_control_command.get_by_command_id(
                    db, account_id=identity.account_id, command_id=command_id
                ).status
                == "pending"
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_broker_failure_does_not_break_presence_or_pending_replay(
    control_server: Server,
    principal: list[Principal],
    db_engine: Engine,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        control,
        "get_nats_client",
        AsyncMock(side_effect=OSError("Synthetic broker unavailable")),
    )
    command_id = persist_command(db_engine, principal[0])
    ws = await connect(control_server, principal[0])
    assert json.loads(await asyncio.wait_for(ws.recv(), 3))["message_id"] == command_id
    await heartbeat(ws)
    await ws.close()
    await pool_idle(control_server.engine)


@pytest.mark.asyncio
async def test_disconnect_during_write_preserves_replacement(
    control_server: Server,
    principal: list[Principal],
    db_engine: Engine,
    monkeypatch: Any,
) -> None:
    old = await connect(control_server, principal[0])
    await heartbeat(old)
    requested = threading.Event()

    def before_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FROM managed_agent" in statement and "FOR UPDATE" in statement:
            requested.set()

    event.listen(control_server.engine, "before_cursor_execute", before_execute)
    try:
        with Session(db_engine) as blocker:
            blocker.query(models.ManagedAgent).filter_by(
                id=principal[0].connection.managed_agent_id
            ).with_for_update().one()
            await old.send(json.dumps({"type": "heartbeat"}))
            assert await asyncio.to_thread(requested.wait, 2)
            closing = asyncio.create_task(old.close())
            monkeypatch.setattr(
                control,
                "agent_control_manager",
                control.AgentControlConnectionManager(),
            )
            replacing = asyncio.create_task(connect(control_server, principal[0]))
            await asyncio.sleep(0.05)
        new = await asyncio.wait_for(replacing, 3)
        await asyncio.wait_for(closing, 3)
        await heartbeat(new)
        await pool_idle(control_server.engine)
        with Session(db_engine) as db:
            agent = db.get(
                models.ManagedAgent, principal[0].connection.managed_agent_id
            )
            assert agent.control_connection_id is not None
            assert agent.control_last_heartbeat_at is not None
            assert (
                str(agent.runtime_session_id)
                == principal[0].connection.runtime_session_id
            )
        await new.close()
    finally:
        event.remove(control_server.engine, "before_cursor_execute", before_execute)
        await old.close()


@pytest.mark.asyncio
async def test_runtime_identity_churn_keeps_durable_credential_valid(
    control_server: Server, principal: list[Principal], db_engine: Engine
) -> None:
    ws = await connect(control_server, principal[0])
    identity = principal[0].connection
    with Session(db_engine) as db:
        db.get(
            models.RuntimeSession, identity.runtime_session_id
        ).ended_at = datetime.now(UTC)
        db.commit()
    await heartbeat(ws)
    await ws.close()
    replacement = await connect(control_server, principal[0])
    await heartbeat(replacement)
    with Session(db_engine) as db:
        assert db.get(models.ApiKey, identity.api_key_id).is_active
        assert (
            db.get(models.RuntimeSession, identity.runtime_session_id).ended_at is None
        )
    await replacement.close()


@pytest.mark.asyncio
async def test_duplicate_out_of_order_results_and_peer_commands_are_isolated(
    db_engine: Engine, principal: list[Principal]
) -> None:
    identity = principal[0].connection
    command_id = persist_command(db_engine, principal[0])
    peer_command_id = persist_command(db_engine, principal[1])
    with Session(db_engine) as dependency:
        database = control._ControlDatabase(dependency)
        assert await database.run(
            lambda db: control._claim_control_connection(
                db, identity, datetime.now(UTC)
            )
        )
        for name, command, reply in [
            ("command_result", command_id, "First result"),
            ("command_error", command_id, "Late error"),
            ("command_ack", command_id, ""),
            ("command_result", peer_command_id, "Peer injection"),
        ]:
            inbound = AgentControlInboundEnvelope(
                type="status",
                name=name,
                payload={"command_id": command, "reply_text": reply},
            )
            assert await database.run(
                lambda db, inbound=inbound: control._process_control_message(
                    db, identity, inbound, datetime.now(UTC)
                )
            )
    with Session(db_engine) as db:
        activities = (
            db.query(models.RuntimeSessionActivity)
            .filter(
                models.RuntimeSessionActivity.account_id == identity.account_id,
                models.RuntimeSessionActivity.metadata_["source"].astext
                == "agent_control_result",
            )
            .all()
        )
        assert len(activities) == 1
        assert activities[0].metadata_["command_id"] == command_id
        assert (
            crud_agent_control_command.get_by_command_id(
                db, account_id=identity.account_id, command_id=peer_command_id
            ).status
            == "pending"
        )


@pytest.mark.parametrize("late_mark", ["delivery", "failure", "ack"])
def test_stale_worker_cannot_regress_terminal_command(
    db_engine: Engine, principal: list[Principal], late_mark: str
) -> None:
    command_id = persist_command(db_engine, principal[0])
    identity = principal[0].connection
    with Session(db_engine) as stale:
        cached = crud_agent_control_command.get_by_command_id(
            stale, account_id=identity.account_id, command_id=command_id
        )
        assert cached.status == "pending"
        with Session(db_engine) as current:
            crud_agent_control_command.mark_acked(
                current,
                account_id=identity.account_id,
                managed_agent_id=identity.managed_agent_id,
                command_id=command_id,
                acked_at=datetime.now(UTC),
            )
        if late_mark == "failure":
            result = crud_agent_control_command.mark_failed(
                stale,
                account_id=identity.account_id,
                command_id=command_id,
                error="Delayed broker failure",
            )
        elif late_mark == "delivery":
            result = crud_agent_control_command.mark_delivered(
                stale,
                account_id=identity.account_id,
                command_id=command_id,
                delivered_at=datetime.now(UTC),
            )
        else:
            result = crud_agent_control_command.mark_acked(
                stale,
                account_id=identity.account_id,
                command_id=command_id,
                acked_at=datetime.now(UTC),
            )
        assert result.status == "acked"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["question", "prompt", "action"])
async def test_two_producers_release_pool_before_guarded_senders(
    db_engine: Engine, principal: list[Principal], monkeypatch: Any, producer: str
) -> None:
    """Two pending sends must not consume the entire pool needed by their senders."""
    from types import SimpleNamespace

    from preloop.schemas.agent_control import (
        AgentControlSendMessageRequest,
        AgentControlSessionActionRequest,
    )
    from preloop.services import ask_user_inband

    engine = create_engine(db_engine.url, pool_size=2, max_overflow=0, pool_timeout=1)
    manager = control.AgentControlConnectionManager()
    sockets = []
    dependencies = []
    for p in principal[:2]:
        with Session(db_engine) as db:
            assert control._claim_control_connection(
                db, p.connection, datetime.now(UTC)
            )
        dependency = Session(engine)
        dependencies.append(dependency)
        database = control._ControlDatabase(dependency)
        socket = AsyncMock()
        sockets.append(socket)

        async def sender(
            payload: dict[str, Any],
            database: control._ControlDatabase = database,
            p: Principal = p,
            socket: Any = socket,
        ) -> bool:
            return await control._send_control_command(
                database, p.connection, socket, payload
            )

        await manager.connect(
            managed_agent_id=p.connection.managed_agent_id,
            websocket=socket,
            sender=sender,
        )
    real_send = manager.send_to_agent
    arrivals = 0
    barrier = asyncio.Event()

    async def synchronized_send(**kwargs: Any) -> bool:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            assert engine.pool.checkedout() == 0
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 3)
        return await real_send(**kwargs)

    monkeypatch.setattr(manager, "send_to_agent", synchronized_send)
    monkeypatch.setattr(control, "agent_control_manager", manager)
    monkeypatch.setattr(control, "_agent_has_control_config", lambda *a, **kw: True)
    monkeypatch.setattr(control, "emit_account_event", lambda *a, **kw: None)
    monkeypatch.setattr(control, "_publish_command", AsyncMock(return_value=None))
    monkeypatch.setattr(ask_user_inband, "_agent_control_manager", lambda: manager)
    monkeypatch.setattr(
        ask_user_inband, "get_db_session", lambda: iter([Session(engine)])
    )
    monkeypatch.setattr(
        ask_user_inband, "_publish_command_to_peers", AsyncMock(return_value=None)
    )

    async def produce(p: Principal) -> bool:
        identity = p.connection
        if producer == "question":
            return await ask_user_inband._deliver(
                account_id=identity.account_id,
                approval_request_id=uuid4(),
                tool_name="ask_user",
                arguments={"question": "Synthetic question"},
                console_url="https://example.com/approval",
                mobile_link="preloop://approve/synthetic",
                runtime_session_id=identity.runtime_session_id,
                managed_agent_id=identity.managed_agent_id,
                user_id=identity.user_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        with Session(engine) as db:
            user = SimpleNamespace(id=identity.user_id, account_id=identity.account_id)
            if producer == "prompt":
                response = await control._route_managed_agent_prompt(
                    agent_id=identity.managed_agent_id,
                    request=AgentControlSendMessageRequest(message="Synthetic prompt"),
                    current_user=user,
                    db=db,
                )
            else:
                response = await control._route_session_action(
                    agent_id=identity.managed_agent_id,
                    request=AgentControlSessionActionRequest(),
                    current_user=user,
                    db=db,
                    name="request_takeover",
                )
            return response.local_delivery

    try:
        assert await asyncio.gather(*(produce(p) for p in principal[:2])) == [
            True,
            True,
        ]
        assert [socket.send_json.await_count for socket in sockets] == [1, 1]
        assert engine.pool.checkedout() == 0
    finally:
        for dependency in dependencies:
            dependency.close()
        engine.dispose()


def test_producer_release_does_not_commit_or_discard_pending_changes(
    db_engine: Engine, principal: list[Principal]
) -> None:
    identity = principal[0].connection
    with Session(db_engine) as db:
        agent = db.get(models.ManagedAgent, identity.managed_agent_id)
        agent.display_name = "Unrelated pending edit"
        with pytest.raises(RuntimeError, match="pending writes"):
            ownership.release_read_transaction(db)
        assert agent in db.dirty
        db.rollback()
    with Session(db_engine) as db:
        assert db.get(models.ManagedAgent, identity.managed_agent_id).display_name != (
            "Unrelated pending edit"
        )


@pytest.mark.asyncio
async def test_result_idempotency_markers_cannot_be_overridden_by_client_metadata(
    db_engine: Engine, principal: list[Principal]
) -> None:
    identity = principal[0].connection
    command_id = persist_command(db_engine, principal[0])
    with Session(db_engine) as dependency:
        database = control._ControlDatabase(dependency)
        assert await database.run(
            lambda db: control._claim_control_connection(
                db, identity, datetime.now(UTC)
            )
        )
        for _ in range(3):
            inbound = AgentControlInboundEnvelope(
                type="status",
                name="command_result",
                payload={
                    "command_id": f" {command_id} ",
                    "reply_text": "Synthetic result",
                    "source": "runtime-client",
                    "role": "user",
                    "direction": "operator_to_agent",
                    "client_note": "preserved",
                },
            )
            assert await database.run(
                lambda db, inbound=inbound: control._process_control_message(
                    db, identity, inbound, datetime.now(UTC)
                )
            )
    with Session(db_engine) as db:
        activities = (
            db.query(models.RuntimeSessionActivity)
            .filter(models.RuntimeSessionActivity.account_id == identity.account_id)
            .all()
        )
        assert len(activities) == 1
        metadata = activities[0].metadata_
        assert metadata["command_id"] == command_id
        assert metadata["source"] == "agent_control_result"
        assert metadata["direction"] == "agent_to_operator"
        assert metadata["role"] == "assistant"
        assert metadata["client_note"] == "preserved"
