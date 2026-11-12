"""Optional local NATS integration with two independent API worker processes.

Set NATS_SERVER_BINARY to a local nats-server executable (or put it on PATH).
The test launches its own loopback-only broker and workers; it never connects
to a deployed API or an upstream model provider.
"""

import asyncio
import json
import logging
import multiprocessing
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import nats
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from preloop.models import models
from preloop.schemas.auth import TokenData
from preloop.models.crud import crud_account, crud_api_key
from tests.endpoints.test_live_session_revocation import access, api, bearer, make_user


def free_port() -> int:
    """Reserve an ephemeral loopback port long enough to select it."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def serve_replica(port: int, nats_port: int, *, deliver: bool, interval: float) -> None:
    """Run the production auth routes, manager and consumer in a fresh process."""
    from preloop.api.endpoints import websockets
    from preloop.services import auth_revocation, websocket_manager
    from preloop.sync.services.realtime_publisher import realtime_publisher

    logging.disable(logging.CRITICAL)
    auth_revocation.install_revocation_notifications()
    auth_revocation.REVOCATION_RECHECK_SECONDS = interval
    if not deliver:
        auth_revocation.publish_realtime = lambda subject, payload: False
    manager = websocket_manager.WebSocketManager()
    websockets.manager = manager
    counts = {"checks": 0, "events": 0}
    original_check = auth_revocation.session_is_revoked
    original_event = manager.recheck_user_connections

    async def check(claims: TokenData) -> bool:
        result = await original_check(claims)
        counts["checks"] += 1
        return result

    async def event(user_ids: Collection[str]) -> None:
        counts["events"] += 1
        await original_event(user_ids)

    auth_revocation.session_is_revoked = check
    manager.recheck_user_connections = event

    # Session activity telemetry is independent of credential enforcement.
    async def create_session(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            id=str(uuid4()),
            connection_id=str(uuid4()),
            is_authenticated=kwargs["user"] is not None,
            display_name="Local integration member",
            metadata={},
        )

    async def end_session(session_id: str) -> None:
        return None

    websockets.session_manager.create_session = create_session
    websockets.session_manager.end_session = end_session

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nc = await nats.connect(f"nats://127.0.0.1:{nats_port}")

        async def publisher() -> SimpleNamespace:
            return SimpleNamespace(nc=nc)

        websocket_manager.get_task_publisher = publisher
        realtime_publisher.start(lambda: nc)
        consumer = asyncio.create_task(websocket_manager.nats_consumer(manager))
        # The worker's ready route must not race its subscription setup.
        while len(nc._subs) < 6:
            if consumer.done():
                consumer.result()
            await asyncio.sleep(0.01)
        await nc.flush()
        try:
            yield
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            await realtime_publisher.stop()
            await nc.close()

    app = api(lifespan=lifespan)

    @app.get("/ready")
    def ready() -> dict[str, int]:
        return {
            **counts,
            "pid": os.getpid(),
            "sockets": len(manager.revocable_connections),
        }

    uvicorn.run(
        app, host="127.0.0.1", port=port, log_level="critical", access_log=False
    )


@pytest.mark.parametrize("deliver", [True, False])
def test_revocation_crosses_processes_with_local_nats(
    db_engine: Engine, deliver: bool
) -> None:
    """The revoke request and live socket run in distinct OS processes."""
    binary = os.getenv("NATS_SERVER_BINARY") or shutil.which("nats-server")
    if not binary:
        pytest.skip("A local nats-server executable is required")
    ports = [free_port() for _ in range(3)]
    nats_port, source_port, receiver_port = ports
    with Session(db_engine, expire_on_commit=False) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": "Local replica test"}
        )
        member = make_user(db, account.id)
        account_id, person_id = account.id, member.person_id
        token = access(member)
        _, api_key = crud_api_key.issue(
            db,
            obj_in={
                "account_id": account_id,
                "user_id": member.id,
                "name": "Independent viewer key",
            },
        )
    broker = subprocess.Popen(
        [binary, "--addr", "127.0.0.1", "--port", str(nats_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(
            target=serve_replica,
            args=(port, nats_port),
            kwargs={"deliver": deliver, "interval": 60.0 if deliver else 0.05},
            daemon=True,
        )
        for port in (source_port, receiver_port)
    ]
    try:
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + 30
        for port in (source_port, receiver_port):
            while True:
                try:
                    response = httpx.get(f"http://127.0.0.1:{port}/ready", timeout=0.5)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "Local API worker did not start"
                time.sleep(0.05)

        async def exercise() -> None:
            async with httpx.AsyncClient(timeout=5) as client:
                source = f"http://127.0.0.1:{source_port}"
                receiver = f"http://127.0.0.1:{receiver_port}"
                async with (
                    connect(
                        f"ws://127.0.0.1:{receiver_port}/api/v1/ws/unified",
                        additional_headers=bearer(token),
                    ) as stream,
                    connect(
                        f"ws://127.0.0.1:{receiver_port}/api/v1/ws/unified"
                    ) as key_stream,
                ):
                    assert json.loads(await stream.recv())["type"] == "handshake"
                    assert json.loads(await key_stream.recv())["type"] == "handshake"
                    await key_stream.send(
                        json.dumps({"type": "authenticate", "token": api_key})
                    )
                    assert (
                        json.loads(await key_stream.recv())["type"] == "authenticated"
                    )
                    # Finish the immediate registration check before revoking:
                    # the event case must close before the 60-second timer.
                    while True:
                        before = (await client.get(f"{receiver}/ready")).json()
                        if before["checks"] >= 1:
                            break
                        await asyncio.sleep(0.01)
                    first = (await client.get(f"{source}/ready")).json()
                    assert first["pid"] != before["pid"]
                    assert first["sockets"] == 0 and before["sockets"] == 1
                    response = await client.post(
                        f"{source}/api/v1/auth/sessions/revoke-all",
                        headers=bearer(token),
                    )
                    assert response.status_code == 200
                    with pytest.raises(ConnectionClosed) as closed:
                        await asyncio.wait_for(stream.recv(), timeout=2)
                    assert closed.value.rcvd.code == 4401
                    after = (await client.get(f"{receiver}/ready")).json()
                    assert after["events"] == (1 if deliver else 0)
                    assert after["checks"] > before["checks"]
                    assert after["sockets"] == 0
                    await key_stream.send(json.dumps({"type": "ping"}))
                    assert (
                        json.loads(
                            await asyncio.wait_for(key_stream.recv(), timeout=2)
                        )["type"]
                        == "pong"
                    )

        asyncio.run(asyncio.wait_for(exercise(), timeout=10))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=5)
        broker.terminate()
        broker.wait(timeout=5)
        with Session(db_engine) as db:
            db.delete(db.get(models.Account, account_id))
            db.commit()
            db.delete(db.get(models.Person, person_id))
            db.commit()
