"""Integration tests for brokered desktop views (#870).

The real app is served by uvicorn inside the test's event loop. A fake RFB 3.8
server stands in for x11vnc, a fake endpoint plays ``preloop desktop serve``,
and a minimal Python RFB client plays the viewer, so bytes cross the real
relay, the server-side view-only filter and the bus.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import uvicorn
import websockets

from preloop.api.endpoints import desktop
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import Decision
from preloop.services import desktop_broker as broker
from tests.permission_helpers import grant_permission

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Fakes


class FakeVncServer:
    """Minimal RFB 3.8 responder serving a fixed 4x4 framebuffer."""

    WIDTH = 4
    HEIGHT = 4

    def __init__(self) -> None:
        self.received_types: list[int] = []
        self.server: Optional[asyncio.base_events.Server] = None
        self.port = 0
        self.connections = 0
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for writer in self.writers:
            writer.close()
        if self.server:
            self.server.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            writer.write(b"RFB 003.008\n")
            await reader.readexactly(12)
            writer.write(b"\x01\x01")  # one security type: None
            await reader.readexactly(1)
            writer.write(struct.pack(">I", 0))
            await reader.readexactly(1)  # ClientInit
            pixel_format = struct.pack(
                ">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
            )
            name = b"fake"
            writer.write(
                struct.pack(">HH", self.WIDTH, self.HEIGHT)
                + pixel_format
                + struct.pack(">I", len(name))
                + name
            )
            await writer.drain()
            while True:
                kind = (await reader.readexactly(1))[0]
                self.received_types.append(kind)
                if kind == 0:
                    await reader.readexactly(19)
                elif kind == 2:
                    _, count = struct.unpack(">BH", await reader.readexactly(3))
                    await reader.readexactly(4 * count)
                elif kind == 3:
                    await reader.readexactly(9)
                    pixels = b"\xff\x00\x00\x00" * (self.WIDTH * self.HEIGHT)
                    writer.write(
                        struct.pack(">BxH", 0, 1)
                        + struct.pack(">HHHHi", 0, 0, self.WIDTH, self.HEIGHT, 0)
                        + pixels
                    )
                    await writer.drain()
                elif kind == 4:
                    await reader.readexactly(7)
                elif kind == 5:
                    await reader.readexactly(5)
                elif kind == 6:
                    await reader.readexactly(3)
                    (length,) = struct.unpack(">i", await reader.readexactly(4))
                    await reader.readexactly(abs(length))
                else:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


class FakeEndpoint:
    """Plays ``preloop desktop serve``: control socket plus per-view relay."""

    def __init__(self, base: str, api_key: str, vnc_port: int, **register: Any):
        self.base = base
        self.api_key = api_key
        self.vnc_port = vnc_port
        self.register = {
            "type": "desktop.register",
            "endpoint_id": register.pop("endpoint_id", f"ep-{uuid.uuid4().hex}"),
            "hostname": "host.example.com",
            "display": ":99",
            "vnc_addr": f"127.0.0.1:{vnc_port}",
            **register,
        }
        self.control: Any = None
        self.registered: dict = {}
        self.messages: list[dict] = []
        self.view_closes: dict[str, int] = {}
        self.tasks: list[asyncio.Task] = []
        self.answer_open = True
        self.open_error: Optional[str] = None

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def connect(self) -> dict:
        self.control = await websockets.connect(
            f"{self.base}/api/v1/desktop/endpoints/ws", additional_headers=self.headers
        )
        await self.control.send(json.dumps(self.register))
        self.registered = json.loads(await self.control.recv())
        self.tasks.append(asyncio.create_task(self._listen()))
        return self.registered

    async def _listen(self) -> None:
        try:
            async for raw in self.control:
                message = json.loads(raw)
                self.messages.append(message)
                if message["type"] == "desktop.open" and self.answer_open:
                    if self.open_error:
                        await self.control.send(
                            json.dumps(
                                {
                                    "type": "desktop.open_result",
                                    "view_id": message["view_id"],
                                    "ok": False,
                                    "error": self.open_error,
                                }
                            )
                        )
                        continue
                    self.tasks.append(asyncio.create_task(self._serve_view(message)))
        except websockets.ConnectionClosed:
            return

    async def _serve_view(self, message: dict) -> None:
        view_id = message["view_id"]
        url = (
            f"{self.base}/api/v1/desktop/endpoints/"
            f"{self.registered['endpoint_id']}/views/{view_id}"
        )
        ws = await websockets.connect(
            url,
            additional_headers={
                **self.headers,
                "X-Preloop-View-Token": message["view_token"],
            },
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", self.vnc_port)
        # Local handshake with the VNC server.
        await reader.readexactly(12)
        writer.write(b"RFB 003.008\n")
        count = (await reader.readexactly(1))[0]
        await reader.readexactly(count)
        writer.write(b"\x01")
        await reader.readexactly(4)
        # Present RFB 3.8 with security None to the relay.
        await ws.send(b"RFB 003.008\n")
        client_bytes = bytearray()

        async def need(n: int) -> bytes:
            while len(client_bytes) < n:
                client_bytes.extend(await ws.recv())
            out = bytes(client_bytes[:n])
            del client_bytes[:n]
            return out

        try:
            await need(12)
            await ws.send(b"\x01\x01")
            await need(1)
            await ws.send(struct.pack(">I", 0))
            if client_bytes:
                writer.write(bytes(client_bytes))
                client_bytes.clear()

            async def up() -> None:
                async for data in ws:
                    writer.write(data)
                    await writer.drain()

            async def down() -> None:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        return
                    await ws.send(data)

            await asyncio.wait(
                {asyncio.create_task(up()), asyncio.create_task(down())},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except websockets.ConnectionClosed:
            pass
        finally:
            self.view_closes[view_id] = ws.close_code or 0
            writer.close()
            await ws.close()

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.control is not None:
            await self.control.close()


class RfbViewer:
    """Minimal RFB client over the viewer WebSocket."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.buf = bytearray()

    async def need(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf.extend(await asyncio.wait_for(self.ws.recv(), 10))
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def handshake(self) -> tuple[int, int]:
        assert await self.need(12) == b"RFB 003.008\n"
        await self.ws.send(b"RFB 003.008\n")
        count = (await self.need(1))[0]
        assert list(await self.need(count)) == [1]
        await self.ws.send(b"\x01")
        assert await self.need(4) == b"\x00\x00\x00\x00"
        await self.ws.send(b"\x01")
        width, height = struct.unpack(">HH", await self.need(4))
        await self.need(16)
        (name_len,) = struct.unpack(">I", await self.need(4))
        await self.need(name_len)
        return width, height

    async def request_frame(self) -> bytes:
        await self.ws.send(struct.pack(">BBHHHH", 3, 0, 0, 0, 4, 4))
        header = await self.need(4)
        assert header[0] == 0
        rect = await self.need(12)
        _, _, w, h, _ = struct.unpack(">HHHHi", rect)
        return await self.need(w * h * 4)

    async def send_input(self) -> None:
        await self.ws.send(
            struct.pack(">BBxxI", 4, 1, 0x61)
            + struct.pack(">BBHH", 5, 1, 1, 1)
            + struct.pack(">Bxxxi", 6, 3)
            + b"abc"
        )


# ---------------------------------------------------------------------------
# Fixtures


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_user(db_session, *, account_id=None, username: str) -> models.User:
    if account_id is None:
        account_id = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Org {username}", "is_active": True},
        ).id
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "email": f"{username}@example.com",
            "username": username,
            "full_name": username,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )


class Harness:
    def __init__(self, app, db_session, test_user, monkeypatch) -> None:
        self.app = app
        self.db = db_session
        self.user = test_user
        self.monkeypatch = monkeypatch
        self.port = _free_port()
        self.base_http = f"http://127.0.0.1:{self.port}"
        self.base_ws = f"ws://127.0.0.1:{self.port}"
        self.viewer_tokens: dict[str, models.User] = {}
        self.current_user = test_user

    def viewer_token(self, user: models.User) -> str:
        token = f"jwt-{uuid.uuid4().hex}"
        self.viewer_tokens[token] = user
        return token

    def endpoint_key(self, user: Optional[models.User] = None, **context: Any) -> str:
        user = user or self.user
        value = f"pk-{uuid.uuid4().hex}"
        key = crud_api_key.create_with_owner(
            self.db,
            obj_in={"name": f"desktop endpoint {value[-6:]}"},
            owner_username=user.username,
            key_value=value,
        )
        if context:
            key.context_data = context
            self.db.commit()
        return value

    def as_user(self, user: models.User) -> None:
        self.current_user = user

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_http, timeout=10)

    def audit(self, action: str) -> list[models.AuditLog]:
        self.db.expire_all()
        return (
            self.db.query(models.AuditLog)
            .filter(
                models.AuditLog.action == action,
                models.AuditLog.account_id == self.user.account_id,
            )
            .order_by(models.AuditLog.timestamp)
            .all()
        )

    def view(self, view_id: str) -> models.DesktopView:
        self.db.expire_all()
        return self.db.get(models.DesktopView, uuid.UUID(view_id))

    def viewer(self, view: dict, user: Optional[models.User] = None):
        token = self.viewer_token(user or self.user)
        return websockets.connect(
            f"{self.base_ws}{view['ws_url']}?token={token}", subprotocols=["binary"]
        )


@pytest.fixture
def harness(app, db_session, test_user, monkeypatch, request):
    from preloop.api.auth import get_current_active_user

    monkeypatch.setattr(settings, "desktop_views_enabled", True)
    if getattr(request, "param", "local") == "nats":
        from tests.services.test_desktop_broker import FakeNats

        broker.set_desktop_bus(broker.NatsDesktopBus(FakeNats()))
    else:
        broker.set_desktop_bus(broker.LocalDesktopBus())

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import NoInspectionAvailable

    def assert_detached_safe(value):
        # Production _run_db closes its session, so ORM rows must not leave
        # it; the shared test session would otherwise hide that bug.
        if isinstance(value, (tuple, list)):
            for item in value:
                assert_detached_safe(item)
            return
        try:
            sa_inspect(value)
        except NoInspectionAvailable:
            return
        if isinstance(value, models.User):
            return  # detach_user() copies; the viewer lookup is stubbed here
        raise AssertionError(f"ORM object {type(value).__name__} escaped _run_db")

    async def run_db(operation):
        result = operation(db_session)
        assert_detached_safe(result)
        return result

    monkeypatch.setattr(desktop, "_run_db", run_db)
    h = Harness(app, db_session, test_user, monkeypatch)

    def console_user(user: models.User) -> models.User:
        # Endpoint key authentication tags the shared session's ORM instance.
        # Console requests use their own principal, as separate requests do.
        return models.User(
            id=user.id,
            account_id=user.account_id,
            username=user.username,
            email=user.email,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
        )

    async def resolve_viewer(token: str) -> models.User | None:
        user = h.viewer_tokens.get(token)
        return console_user(user) if user is not None else None

    monkeypatch.setattr(desktop, "_resolve_viewer", resolve_viewer)

    def current_user() -> models.User:
        return console_user(h.current_user)

    app.dependency_overrides[get_current_active_user] = current_user
    account_hooks.reset_account_hooks()
    yield h
    account_hooks.reset_account_hooks()
    broker.set_desktop_bus(None)


@asynccontextmanager
async def serving(h: Harness) -> AsyncIterator[FakeVncServer]:
    config = uvicorn.Config(
        h.app,
        host="127.0.0.1",
        port=h.port,
        lifespan="off",
        log_level="warning",
        ws="websockets",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    vnc = FakeVncServer()
    await vnc.start()
    try:
        yield vnc
    finally:
        await vnc.stop()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        for pending in list(desktop._background):
            pending.cancel()


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def open_view(h: Harness, endpoint: FakeEndpoint, **body: Any) -> dict:
    async with h.http() as client:
        response = await client.post(
            f"/api/v1/desktop/endpoints/{endpoint.registered['endpoint_id']}/views",
            json=body,
        )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Tests


@pytest.mark.parametrize("harness", ["local", "nats"], indirect=True)
def test_view_only_relay_blocks_input_then_control_grant_and_release(harness):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            registered = await endpoint.connect()
            assert registered["type"] == "desktop.registered"
            view = await open_view(h, endpoint)
            assert view["state"] == "pending" and view["mode"] == "view_only"
            assert set(view) >= {"view_id", "ws_url", "expires_at", "mode"}
            async with h.viewer(view) as ws:
                assert ws.subprotocol == "binary"
                viewer = RfbViewer(ws)
                assert await viewer.handshake() == (4, 4)
                assert h.view(view["view_id"]).state == "live"
                assert await viewer.request_frame() == b"\xff\x00\x00\x00" * 16
                await viewer.send_input()
                await viewer.request_frame()
                assert 4 not in vnc.received_types
                assert 5 not in vnc.received_types
                assert 6 not in vnc.received_types
                assert vnc.received_types.count(3) == 2

                async with h.http() as client:
                    granted = await client.post(
                        f"/api/v1/desktop/views/{view['view_id']}/control",
                        json={"reason": "fix the stuck dialog"},
                    )
                assert granted.status_code == 200, granted.text
                assert granted.json()["mode"] == "control"
                await asyncio.sleep(0.1)
                await viewer.send_input()
                await viewer.request_frame()
                assert {4, 5, 6} <= set(vnc.received_types)

                async with h.http() as client:
                    released = await client.post(
                        f"/api/v1/desktop/views/{view['view_id']}/control/release"
                    )
                assert released.status_code == 204
                await asyncio.sleep(0.1)
                before = list(vnc.received_types)
                await viewer.send_input()
                await viewer.request_frame()
                assert vnc.received_types[len(before) :] == [3]

                async with h.http() as client:
                    deleted = await client.delete(
                        f"/api/v1/desktop/views/{view['view_id']}"
                    )
                assert deleted.status_code == 204
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_USER_CLOSED
            await wait_for(lambda: view["view_id"] in endpoint.view_closes)
            await endpoint.close()

    asyncio.run(scenario())
    assert [a.action for a in h.audit("desktop.control.requested")] == [
        "desktop.control.requested"
    ]
    assert len(h.audit("desktop.control.granted")) == 1
    assert len(h.audit("desktop.control.released")) == 1
    started = h.audit("desktop.view.started")
    ended = h.audit("desktop.view.ended")
    assert len(started) == 1 and len(ended) == 1
    assert ended[0].details["closed_reason"] == "user_closed"
    assert ended[0].details["duration_seconds"] >= 0
    assert ended[0].user_id == h.user.id
    view = h.view(started[0].resource_id)
    assert view.view_token_hash is None and view.state == "closed"


def test_agent_drop_closes_viewer_with_4410(harness):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)
            async with h.viewer(view) as ws:
                viewer = RfbViewer(ws)
                await viewer.handshake()
                await vnc.stop()  # the host side goes away
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    while True:
                        await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_AGENT_DISCONNECTED
            await endpoint.close()
            return view

    view = asyncio.run(scenario())
    assert h.view(view["view_id"]).closed_reason == "agent_disconnected"
    assert (
        h.audit("desktop.view.ended")[0].details["closed_reason"]
        == "agent_disconnected"
    )


def test_view_expires(harness):
    h = harness
    h.monkeypatch.setattr(settings, "desktop_view_max_seconds", 1)

    async def scenario() -> dict:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)
            async with h.viewer(view) as ws:
                await RfbViewer(ws).handshake()
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    while True:
                        await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_EXPIRED
            await endpoint.close()
            return view

    view = asyncio.run(scenario())
    row = h.view(view["view_id"])
    assert row.closed_reason == "expired"
    ended = h.audit("desktop.view.ended")[0].details
    assert ended["closed_reason"] == "expired"
    assert 0.5 <= ended["duration_seconds"] <= 3


def test_idle_viewer_closes(harness):
    h = harness
    h.monkeypatch.setattr(broker, "VIEWER_IDLE_SECONDS", 0.5)

    async def scenario() -> dict:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)
            async with h.viewer(view) as ws:
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    while True:
                        await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_IDLE
            await endpoint.close()
            return view

    view = asyncio.run(scenario())
    assert h.view(view["view_id"]).closed_reason == "idle"


def test_second_viewer_is_busy_and_names_holder(harness):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)
            async with h.http() as client:
                second = await client.post(
                    f"/api/v1/desktop/endpoints/{endpoint.registered['endpoint_id']}/views",
                    json={},
                )
            assert second.status_code == 409
            detail = second.json()["detail"]
            assert detail["code"] == "desktop_view_busy"
            assert detail["holder"]["username"] == h.user.username
            assert detail["holder"]["view_id"] == view["view_id"]
            async with h.viewer(view) as first:
                await RfbViewer(first).handshake()
                async with h.viewer(view) as again:
                    with pytest.raises(websockets.ConnectionClosed) as closed:
                        await asyncio.wait_for(again.recv(), 5)
                    assert closed.value.rcvd.code == broker.CLOSE_VIEWER_BUSY
            await endpoint.close()

    asyncio.run(scenario())


def test_stolen_url_and_foreign_users_are_rejected(harness, db_session):
    h = harness
    colleague = _make_user(
        db_session, account_id=h.user.account_id, username="colleague"
    )
    outsider = _make_user(db_session, username="outsider")
    for user in (colleague, outsider):
        grant_permission(db_session, user, "control_managed_agent")

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)
            for user in (colleague, outsider):
                async with h.viewer(view, user) as ws:
                    with pytest.raises(websockets.ConnectionClosed) as closed:
                        await asyncio.wait_for(ws.recv(), 5)
                    assert closed.value.rcvd.code == broker.CLOSE_FORBIDDEN
            h.as_user(outsider)
            async with h.http() as client:
                eid = endpoint.registered["endpoint_id"]
                assert (
                    await client.post(f"/api/v1/desktop/endpoints/{eid}/views", json={})
                ).status_code == 404
                assert (
                    await client.get(f"/api/v1/desktop/views/{view['view_id']}")
                ).status_code == 404
            h.as_user(colleague)
            async with h.http() as client:
                assert (
                    await client.get(f"/api/v1/desktop/views/{view['view_id']}")
                ).status_code == 403
                assert (
                    await client.delete(f"/api/v1/desktop/views/{view['view_id']}")
                ).status_code == 403
            h.as_user(h.user)
            assert h.view(view["view_id"]).viewer_attached_at is None
            await endpoint.close()

    asyncio.run(scenario())


def test_missing_permission_is_403_and_4403(harness):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            view = await open_view(h, endpoint)

            def deny(ctx, action, resource):
                if action == "control_managed_agent":
                    return Decision("deny", reason="no control")
                return Decision("allow")

            account_hooks.register_authorizer(deny)
            async with h.http() as client:
                eid = endpoint.registered["endpoint_id"]
                created = await client.post(
                    f"/api/v1/desktop/endpoints/{eid}/views", json={}
                )
            assert created.status_code == 403
            async with h.viewer(view) as ws:
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_FORBIDDEN
            await endpoint.close()

    asyncio.run(scenario())


def test_bad_view_token_is_rejected_and_unreachable_view_closes(harness):
    h = harness
    h.monkeypatch.setattr(broker, "VIEW_TOKEN_TTL_SECONDS", 0.5)

    async def scenario() -> dict:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            endpoint.answer_open = False
            await endpoint.connect()
            view = await open_view(h, endpoint)
            url = (
                f"{h.base_ws}/api/v1/desktop/endpoints/"
                f"{endpoint.registered['endpoint_id']}/views/{view['view_id']}"
            )
            async with websockets.connect(
                url,
                additional_headers={
                    **endpoint.headers,
                    "X-Preloop-View-Token": "forged",
                },
            ) as ws:
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_UNAUTHORIZED
            await wait_for(lambda: h.view(view["view_id"]).state == "closed")
            opened = [m for m in endpoint.messages if m["type"] == "desktop.open"]
            token = opened[0]["view_token"]
            assert "view_token" not in view
            async with websockets.connect(
                url,
                additional_headers={**endpoint.headers, "X-Preloop-View-Token": token},
            ) as ws:
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), 5)
                assert closed.value.rcvd.code == broker.CLOSE_UNAUTHORIZED
            await endpoint.close()
            return view

    view = asyncio.run(scenario())
    assert h.view(view["view_id"]).closed_reason == "desktop_agent_unreachable"


def test_display_unavailable_from_endpoint_closes_view(harness):
    h = harness

    async def scenario() -> dict:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            endpoint.open_error = "display_unavailable"
            await endpoint.connect()
            view = await open_view(h, endpoint)
            await wait_for(lambda: h.view(view["view_id"]).state == "closed")
            await endpoint.close()
            return view

    view = asyncio.run(scenario())
    assert h.view(view["view_id"]).closed_reason == "display_unavailable"


def test_offline_or_unknown_endpoint_is_not_available(harness, db_session):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            await endpoint.close()
            eid = endpoint.registered["endpoint_id"]
            await wait_for(
                lambda: (
                    (
                        db_session.expire_all(),
                        db_session.query(models.DesktopEndpoint)
                        .filter_by(endpoint_id=eid)
                        .one()
                        .state,
                    )[1]
                    == "offline"
                )
            )
            async with h.http() as client:
                offline = await client.post(
                    f"/api/v1/desktop/endpoints/{eid}/views", json={}
                )
                missing = await client.post(
                    "/api/v1/desktop/endpoints/nope-nope/views", json={}
                )
                agent = await client.post(
                    f"/api/v1/agents/{uuid.uuid4()}/desktop-views", json={}
                )
            assert offline.status_code == 409
            assert offline.json()["detail"]["code"] == "desktop_not_available"
            assert missing.status_code == 404
            assert agent.status_code == 404

    asyncio.run(scenario())


def _make_agent(db_session, user) -> models.ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = models.ManagedAgent(
        id=uuid.uuid4(),
        account_id=user.account_id,
        runtime_session_id=None,
        agent_kind="custom",
        session_source_type="custom",
        session_source_id=f"browser-use-{uuid.uuid4().hex[:6]}",
        display_name="Browser agent",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
        tags={},
    )
    db_session.add(agent)
    db_session.commit()
    return agent


def test_agent_scoped_view_and_session_linkage(harness, db_session):
    """The demo case: an endpoint registered with an agent's key shows up
    for that agent and for a session run with a key bound to that session."""
    h = harness
    agent = _make_agent(db_session, h.user)
    now = datetime.now(UTC).replace(tzinfo=None)
    session = models.RuntimeSession(
        account_id=h.user.account_id,
        session_source_type="custom",
        session_source_id=f"s-{uuid.uuid4().hex[:8]}",
        started_at=now,
    )
    db_session.add(session)
    db_session.commit()
    agent_key = h.endpoint_key(managed_agent_id=str(agent.id))
    session_key = h.endpoint_key(runtime_session_id=str(session.id))

    async def scenario() -> None:
        async with serving(h) as vnc:
            by_agent = FakeEndpoint(h.base_ws, agent_key, vnc.port)
            by_session = FakeEndpoint(h.base_ws, session_key, vnc.port)
            await by_agent.connect()
            await by_session.connect()
            async with h.http() as client:
                listed = await client.get(
                    "/api/v1/desktop/endpoints",
                    params={"managed_agent_id": str(agent.id)},
                )
                linked = await client.get(
                    "/api/v1/desktop/endpoints",
                    params={"runtime_session_id": str(session.id), "state": "online"},
                )
                created = await client.post(
                    f"/api/v1/agents/{agent.id}/desktop-views", json={}
                )
            assert listed.status_code == 200, listed.text
            assert linked.status_code == 200, linked.text
            rows = listed.json()["endpoints"]
            assert [r["endpoint_id"] for r in rows] == [
                by_agent.registered["endpoint_id"]
            ]
            assert rows[0]["managed_agent_id"] == str(agent.id)
            assert rows[0]["state"] == "online"
            assert [r["endpoint_id"] for r in linked.json()["endpoints"]] == [
                by_session.registered["endpoint_id"]
            ]
            assert created.status_code == 201, created.text
            assert created.json()["managed_agent_id"] == str(agent.id)
            async with h.http() as client:
                listed = await client.get(
                    "/api/v1/desktop/endpoints",
                    params={"managed_agent_id": str(agent.id)},
                )
                fetched = await client.get(
                    f"/api/v1/agents/{agent.id}/desktop-views/{created.json()['view_id']}"
                )
            assert listed.status_code == 200, listed.text
            assert listed.json()["endpoints"][0]["active_view"]["user_id"] == str(
                h.user.id
            )
            assert fetched.status_code == 200
            await by_agent.close()
            await by_session.close()

    asyncio.run(scenario())


def test_agent_bound_key_cannot_claim_another_agent(harness, db_session):
    h = harness
    agent = _make_agent(db_session, h.user)
    other = _make_agent(db_session, h.user)
    key = h.endpoint_key(managed_agent_id=str(agent.id))

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(
                h.base_ws, key, vnc.port, managed_agent_id=str(other.id)
            )
            endpoint.control = await websockets.connect(
                f"{h.base_ws}/api/v1/desktop/endpoints/ws",
                additional_headers=endpoint.headers,
            )
            await endpoint.control.send(json.dumps(endpoint.register))
            error = json.loads(await endpoint.control.recv())
            assert error["type"] == "error" and error["code"] == "forbidden"
            with pytest.raises(websockets.ConnectionClosed):
                await endpoint.control.recv()
            assert endpoint.control.close_code == broker.CLOSE_FORBIDDEN

    asyncio.run(scenario())


def test_endpoint_auth_and_eviction(harness):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            async with websockets.connect(
                f"{h.base_ws}/api/v1/desktop/endpoints/ws",
                additional_headers={"Authorization": "Bearer not-a-key"},
            ) as ws:
                error = json.loads(await ws.recv())
                assert error["code"] == "unauthorized"
                with pytest.raises(websockets.ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == broker.CLOSE_UNAUTHORIZED
            key = h.endpoint_key()
            first = FakeEndpoint(h.base_ws, key, vnc.port, endpoint_id="ep-stable-0001")
            await first.connect()
            second = FakeEndpoint(
                h.base_ws, key, vnc.port, endpoint_id="ep-stable-0001"
            )
            await second.connect()
            await wait_for(lambda: first.control.close_code is not None)
            assert first.control.close_code == broker.CLOSE_EVICTED
            await second.control.send(json.dumps({"type": "desktop.heartbeat"}))
            await wait_for(lambda: {"type": "desktop.heartbeat_ack"} in second.messages)
            await second.close()

    asyncio.run(scenario())


def test_feature_flag_off_hides_everything(harness):
    h = harness
    h.monkeypatch.setattr(settings, "desktop_views_enabled", False)

    async def scenario() -> None:
        async with serving(h):
            async with h.http() as client:
                listed = await client.get("/api/v1/desktop/endpoints")
                created = await client.post(
                    "/api/v1/desktop/endpoints/x/views", json={}
                )
            assert listed.status_code == 404
            assert listed.json()["detail"]["code"] == "desktop_views_disabled"
            assert created.status_code == 404
            async with websockets.connect(
                f"{h.base_ws}/api/v1/ws/desktop/{uuid.uuid4()}"
            ) as ws:
                with pytest.raises(websockets.ConnectionClosed):
                    await ws.recv()
                assert ws.close_code == broker.CLOSE_NOT_FOUND

    asyncio.run(scenario())


def test_rate_limit(harness, db_session):
    h = harness
    endpoint_row = models.DesktopEndpoint(
        account_id=h.user.account_id, endpoint_id="ep-rate-limit", state="offline"
    )
    db_session.add(endpoint_row)
    db_session.commit()
    now = datetime.now(UTC).replace(tzinfo=None)
    for _ in range(broker.VIEW_CREATIONS_PER_HOUR):
        db_session.add(
            models.DesktopView(
                account_id=h.user.account_id,
                endpoint_pk=endpoint_row.id,
                user_id=h.user.id,
                state="closed",
                expires_at=now,
            )
        )
    db_session.commit()

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            async with h.http() as client:
                eid = endpoint.registered["endpoint_id"]
                response = await client.post(
                    f"/api/v1/desktop/endpoints/{eid}/views", json={}
                )
            assert response.status_code == 429
            await endpoint.close()

    asyncio.run(scenario())


def test_sweeper_closes_abandoned_views(harness, db_session):
    h = harness
    endpoint_row = models.DesktopEndpoint(
        account_id=h.user.account_id,
        endpoint_id="ep-sweep",
        state="online",
        last_seen_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(endpoint_row)
    db_session.commit()
    now = datetime.now(UTC).replace(tzinfo=None)
    pending = models.DesktopView(
        account_id=h.user.account_id,
        endpoint_pk=endpoint_row.id,
        user_id=h.user.id,
        state="pending",
        expires_at=now + timedelta(minutes=10),
        created_at=now - timedelta(minutes=2),
    )
    live = models.DesktopView(
        account_id=h.user.account_id,
        endpoint_pk=endpoint_row.id,
        user_id=h.user.id,
        state="live",
        live_at=now - timedelta(minutes=20),
        expires_at=now - timedelta(minutes=5),
    )
    db_session.add_all([pending, live])
    db_session.commit()

    assert asyncio.run(desktop.sweep_once()) == 2
    assert h.view(str(pending.id)).closed_reason == "desktop_agent_unreachable"
    assert h.view(str(live.id)).closed_reason == "server_restart"
    db_session.refresh(endpoint_row)
    assert endpoint_row.state == "offline"
    ended = h.audit("desktop.view.ended")
    assert [e.details["closed_reason"] for e in ended] == ["server_restart"]


def test_machine_credentials_cannot_open_views() -> None:
    """Runner execution keys, agent keys and session keys are not operators."""
    from types import SimpleNamespace

    for context in (
        {"flow_execution_id": str(uuid.uuid4())},
        {"flow_execution_id": str(uuid.uuid4()), "runtime_session_id": None},
        {"managed_agent_id": str(uuid.uuid4())},
        {"runtime_session_id": str(uuid.uuid4())},
    ):
        user = SimpleNamespace(_auth_api_key=SimpleNamespace(context_data=context))
        assert desktop._is_machine_credential(user), context
        with pytest.raises(Exception) as denied:
            desktop._authorize_operator(None, user)  # type: ignore[arg-type]
        assert getattr(denied.value, "status_code", None) == 403
    personal = SimpleNamespace(_auth_api_key=SimpleNamespace(context_data={}))
    assert not desktop._is_machine_credential(personal)
    assert not desktop._is_machine_credential(SimpleNamespace())


def test_runtime_session_must_be_linked_to_the_endpoint(harness, db_session):
    h = harness
    now = datetime.now(UTC).replace(tzinfo=None)
    unrelated = models.RuntimeSession(
        account_id=h.user.account_id,
        session_source_type="custom",
        session_source_id=f"s-{uuid.uuid4().hex[:8]}",
        started_at=now,
    )
    db_session.add(unrelated)
    db_session.commit()

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
            await endpoint.connect()
            async with h.http() as client:
                eid = endpoint.registered["endpoint_id"]
                response = await client.post(
                    f"/api/v1/desktop/endpoints/{eid}/views",
                    json={"runtime_session_id": str(unrelated.id)},
                )
            assert response.status_code == 422, response.text
            assert response.json()["detail"]["code"] == "runtime_session_not_linked"
            await endpoint.close()

    asyncio.run(scenario())


def test_list_endpoints_is_not_n_plus_one(harness, db_session):
    """Active views and holders are fetched in one query for all endpoints."""
    from sqlalchemy import event

    h = harness
    now = datetime.now(UTC).replace(tzinfo=None)
    for i in range(6):
        row = models.DesktopEndpoint(
            account_id=h.user.account_id,
            endpoint_id=f"ep-batch-{i:04d}",
            state="online",
            last_seen_at=now,
        )
        db_session.add(row)
        db_session.flush()
        db_session.add(
            models.DesktopView(
                account_id=h.user.account_id,
                endpoint_pk=row.id,
                user_id=h.user.id,
                state="live",
                expires_at=now + timedelta(minutes=5),
            )
        )
    db_session.commit()
    statements: list[str] = []

    def count(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
        if "desktop_view" in statement or '"user"' in statement:
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        rows = asyncio.run(
            desktop.list_desktop_endpoints(
                managed_agent_id=None,
                runner_id=None,
                runtime_session_id=None,
                state=None,
                current_user=h.user,
            )
        )["endpoints"]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len([r for r in rows if r["active_view"]]) == 6
    assert len(statements) <= 2, statements


# ---------------------------------------------------------------------------
# Explicit session -> desktop link via PRELOOP_DESKTOP_ENDPOINT_ID (#1568)


def _plain_session(db_session, account_id) -> models.RuntimeSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="custom",
        session_source_id=f"s-{uuid.uuid4().hex[:8]}",
        started_at=now,
    )
    db_session.add(session)
    db_session.commit()
    return session


def _runtime_key(db_session, user, *, runtime_session_id=None) -> str:
    context = None
    if runtime_session_id is not None:
        context = {"runtime_session_id": str(runtime_session_id)}
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Adapter key",
        account_id=user.account_id,
        user_id=user.id,
        context_data=context,
    )
    return token


def test_session_named_endpoint_shows_live_desktop(harness, db_session):
    """A session run with a different credential than the tunnel is linked
    once the adapter names the endpoint (PRELOOP_DESKTOP_ENDPOINT_ID)."""
    h = harness
    agent = _make_agent(db_session, h.user)
    session = _plain_session(db_session, h.user.account_id)
    tunnel_key = h.endpoint_key(managed_agent_id=str(agent.id))
    adapter_key = _runtime_key(db_session, h.user, runtime_session_id=session.id)

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint = FakeEndpoint(h.base_ws, tunnel_key, vnc.port)
            await endpoint.connect()
            eid = endpoint.registered["endpoint_id"]
            params = {"runtime_session_id": str(session.id)}
            async with h.http() as client:
                before = await client.get("/api/v1/desktop/endpoints", params=params)
                linked = await client.put(
                    f"/api/v1/runtime-sessions/{session.id}/desktop-endpoint",
                    json={"desktop_endpoint_id": eid},
                    headers={"Authorization": f"Bearer {adapter_key}"},
                )
                after = await client.get("/api/v1/desktop/endpoints", params=params)
                created = await client.post(
                    f"/api/v1/desktop/endpoints/{eid}/views",
                    json={"runtime_session_id": str(session.id)},
                )
            assert before.json()["endpoints"] == []
            assert linked.status_code == 200, linked.text
            assert linked.json() == {
                "runtime_session_id": str(session.id),
                "desktop_endpoint_id": eid,
                "linked": True,
            }
            assert [r["endpoint_id"] for r in after.json()["endpoints"]] == [eid]
            assert created.status_code == 201, created.text
            await endpoint.close()

    asyncio.run(scenario())


def test_session_naming_another_accounts_endpoint_is_ignored(harness, db_session):
    h = harness
    outsider = _make_user(db_session, username="desk-outsider")
    session = _plain_session(db_session, h.user.account_id)
    adapter_key = _runtime_key(db_session, h.user)

    async def scenario() -> None:
        async with serving(h) as vnc:
            foreign = FakeEndpoint(h.base_ws, h.endpoint_key(outsider), vnc.port)
            await foreign.connect()
            eid = foreign.registered["endpoint_id"]
            foreign_pk = str(
                db_session.query(models.DesktopEndpoint)
                .filter(models.DesktopEndpoint.endpoint_id == eid)
                .one()
                .id
            )
            async with h.http() as client:
                results = [
                    await client.put(
                        f"/api/v1/runtime-sessions/{session.id}/desktop-endpoint",
                        json={"desktop_endpoint_id": ident},
                        headers={"Authorization": f"Bearer {adapter_key}"},
                    )
                    for ident in (eid, foreign_pk)
                ]
                listed = await client.get(
                    "/api/v1/desktop/endpoints",
                    params={"runtime_session_id": str(session.id)},
                )
            for result in results:
                assert result.status_code == 200, result.text
                assert result.json()["linked"] is False
            assert listed.json()["endpoints"] == []
            await foreign.close()

    asyncio.run(scenario())


def test_desktop_endpoint_link_rejects_other_sessions_and_accounts(
    client, db_session, test_user
):
    session = _plain_session(db_session, test_user.account_id)
    other = _plain_session(db_session, test_user.account_id)
    outsider = _make_user(db_session, username="desk-outsider-2")
    pinned = _runtime_key(db_session, test_user, runtime_session_id=other.id)
    foreign = _runtime_key(db_session, outsider)
    url = f"/api/v1/runtime-sessions/{session.id}/desktop-endpoint"
    body = {"desktop_endpoint_id": "dep_abc"}

    assert client.put(url, json=body).status_code == 401
    wrong_session = client.put(
        url, json=body, headers={"Authorization": f"Bearer {pinned}"}
    )
    assert wrong_session.status_code == 403
    wrong_account = client.put(
        url, json=body, headers={"Authorization": f"Bearer {foreign}"}
    )
    assert wrong_account.status_code == 404
    db_session.refresh(session)
    assert session.desktop_endpoint_id is None


def test_clearing_the_link_and_sessions_without_it(client, db_session, test_user):
    session = _plain_session(db_session, test_user.account_id)
    key = _runtime_key(db_session, test_user, runtime_session_id=session.id)
    url = f"/api/v1/runtime-sessions/{session.id}/desktop-endpoint"
    headers = {"Authorization": f"Bearer {key}"}

    assert crud_desktop_filters(db_session, test_user, session) == {
        "managed_agent_ids": [],
        "api_key_ids": [crud_api_key.get_by_key(db_session, key=key).id],
    }
    set_ = client.put(url, json={"desktop_endpoint_id": "dep_abc"}, headers=headers)
    assert set_.status_code == 200, set_.text
    assert crud_desktop_filters(db_session, test_user, session)["endpoint_idents"] == [
        "dep_abc"
    ]
    cleared = client.put(url, json={"desktop_endpoint_id": None}, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["desktop_endpoint_id"] is None
    assert "endpoint_idents" not in crud_desktop_filters(db_session, test_user, session)


def crud_desktop_filters(db_session, user, session):
    from preloop.models.crud import crud_desktop

    db_session.expire_all()
    return crud_desktop.session_link_filters(
        db_session, account_id=user.account_id, runtime_session_id=session.id
    )


def test_runtime_session_token_records_the_desktop_endpoint(
    client, db_session, test_user
):
    """Hooks and plugins name the endpoint when they mint the session token."""
    from preloop.api.auth import get_current_active_user

    client.app.dependency_overrides[get_current_active_user] = lambda: test_user
    try:
        response = client.post(
            "/api/v1/auth/runtime-sessions/token",
            json={
                "session_source_type": "custom",
                "session_source_id": f"hook-{uuid.uuid4().hex[:8]}",
                "desktop_endpoint_id": "dep_from_hook",
            },
        )
    finally:
        client.app.dependency_overrides.pop(get_current_active_user, None)
    assert response.status_code == 201, response.text
    session = db_session.get(
        models.RuntimeSession, uuid.UUID(response.json()["runtime_session_id"])
    )
    assert session.desktop_endpoint_id == "dep_from_hook"


# ---------------------------------------------------------------------------
# Take control through the approvals service (#1567)


def _approvals(h: Harness):
    """Approval service on the shared test session, as the route would use."""
    from preloop.models.db.session import SyncApprovalSession
    from preloop.services.approval_service import ApprovalService

    return ApprovalService(SyncApprovalSession(h.db), "http://localhost")


@pytest.fixture
def two_admins(harness, db_session):
    h = harness
    colleague = _make_user(
        db_session, account_id=h.user.account_id, username="desk-colleague"
    )
    for user in (h.user, colleague):
        grant_permission(db_session, user, "control_managed_agent")
        grant_permission(db_session, user, "decide_approvals")

    @asynccontextmanager
    async def approval_db():
        from preloop.models.db.session import SyncApprovalSession

        yield SyncApprovalSession(db_session)

    h.monkeypatch.setattr(desktop, "_approval_db", approval_db)
    return colleague


async def _live_viewer(h: Harness, vnc: FakeVncServer):
    endpoint = FakeEndpoint(h.base_ws, h.endpoint_key(), vnc.port)
    await endpoint.connect()
    view = await open_view(h, endpoint)
    return endpoint, view


async def _input_reaches_display(viewer: RfbViewer, vnc: FakeVncServer) -> bool:
    before = len(vnc.received_types)
    await viewer.send_input()
    await viewer.request_frame()
    return bool({4, 5, 6} & set(vnc.received_types[before:]))


def test_multi_admin_control_waits_for_another_admin(harness, two_admins):
    h = harness
    colleague = two_admins

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            view_id = view["view_id"]
            async with h.viewer(view) as ws:
                viewer = RfbViewer(ws)
                await viewer.handshake()
                async with h.http() as client:
                    asked = await client.post(
                        f"/api/v1/desktop/views/{view_id}/control",
                        json={"reason": "fix the stuck dialog"},
                    )
                    again = await client.post(
                        f"/api/v1/desktop/views/{view_id}/control",
                        json={"reason": "fix the stuck dialog"},
                    )
                    waiting = await client.get(f"/api/v1/desktop/views/{view_id}")
                assert asked.status_code == 202, asked.text
                body = asked.json()
                assert body["mode"] == "view_only"
                assert body["control_request"]["state"] == "pending"
                approval_id = body["control_request"]["approval_id"]
                assert again.status_code == 202
                assert again.json()["control_request"]["approval_id"] == approval_id
                assert waiting.json()["control_request"] == {
                    "approval_id": approval_id,
                    "state": "pending",
                }
                assert not await _input_reaches_display(viewer, vnc)

                service = _approvals(h)
                from fastapi import HTTPException

                with pytest.raises(HTTPException) as own:
                    await service.approve_request(
                        uuid.UUID(approval_id), "ok", user_id=h.user.id
                    )
                assert own.value.status_code == 403
                with pytest.raises(HTTPException) as anonymous:
                    await service.approve_request(uuid.UUID(approval_id), "ok")
                assert anonymous.value.status_code == 403
                assert h.view(view_id).mode == "view_only"
                assert not await _input_reaches_display(viewer, vnc)

                await service.approve_request(
                    uuid.UUID(approval_id), "go ahead", user_id=colleague.id
                )
                assert h.view(view_id).mode == "control"
                await asyncio.sleep(0.1)
                assert await _input_reaches_display(viewer, vnc)
                async with h.http() as client:
                    granted = await client.get(f"/api/v1/desktop/views/{view_id}")
                assert granted.json()["mode"] == "control"
                assert granted.json()["control_request"] is None
                expires = datetime.fromisoformat(
                    granted.json()["control_expires_at"].rstrip("Z")
                )
                now = datetime.now(UTC).replace(tzinfo=None)
                assert expires <= now + timedelta(seconds=601)

                async with h.http() as client:
                    released = await client.post(
                        f"/api/v1/desktop/views/{view_id}/control/release"
                    )
                assert released.status_code == 204
                await asyncio.sleep(0.1)
                assert not await _input_reaches_display(viewer, vnc)
            await endpoint.close()

    asyncio.run(scenario())
    granted = h.audit("desktop.control.granted")
    assert len(granted) == 1
    assert granted[0].details["approval"] == "approved"
    assert granted[0].details["approver_id"] == str(colleague.id)
    assert len(h.audit("desktop.control.requested")) == 1
    assert h.audit("desktop.control.denied") == []


@pytest.mark.parametrize("outcome", ["declined", "expired"])
def test_denied_or_expired_control_request_never_forwards_input(
    harness, two_admins, outcome
):
    h = harness
    colleague = two_admins

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            view_id = view["view_id"]
            async with h.viewer(view) as ws:
                viewer = RfbViewer(ws)
                await viewer.handshake()
                async with h.http() as client:
                    asked = await client.post(
                        f"/api/v1/desktop/views/{view_id}/control",
                        json={"reason": "fix the stuck dialog"},
                    )
                approval_id = uuid.UUID(asked.json()["control_request"]["approval_id"])
                service = _approvals(h)
                if outcome == "declined":
                    await service.decline_request(
                        approval_id, "not now", user_id=colleague.id
                    )
                else:
                    row = h.db.get(models.ApprovalRequest, approval_id)
                    row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                        seconds=5
                    )
                    h.db.commit()
                async with h.http() as client:
                    after = await client.get(f"/api/v1/desktop/views/{view_id}")
                assert after.json()["mode"] == "view_only"
                assert after.json()["control_request"] is None
                assert not await _input_reaches_display(viewer, vnc)
                # A late approval of a settled request changes nothing.
                await service.approve_request(approval_id, "late", user_id=colleague.id)
                assert h.view(view_id).mode == "view_only"
            await endpoint.close()

    asyncio.run(scenario())
    denied = h.audit("desktop.control.denied")
    assert len(denied) == 1
    assert denied[0].details["approval_status"] == outcome
    assert denied[0].details["approver_id"] == (
        str(colleague.id) if outcome == "declined" else None
    )
    assert h.audit("desktop.control.granted") == []


def test_single_admin_still_self_confirms(harness, db_session):
    """With nobody else to decide, the OSS v1 self-confirm stays."""
    h = harness
    grant_permission(db_session, h.user, "decide_approvals")
    grant_permission(db_session, h.user, "control_managed_agent")
    # A member without decide_approvals is not a second admin.
    _make_user(db_session, account_id=h.user.account_id, username="desk-member")
    from preloop.utils import permissions

    real = permissions.user_holds_permission

    def holds(db, user, name):
        if getattr(user, "username", "") == "desk-member":
            return False
        return real(db, user, name)

    h.monkeypatch.setattr(desktop, "user_holds_permission", holds)
    h.monkeypatch.setattr(permissions, "members_have_full_access", lambda: False)

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                granted = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
            assert granted.status_code == 200, granted.text
            assert granted.json()["mode"] == "control"
            await endpoint.close()

    asyncio.run(scenario())
    granted = h.audit("desktop.control.granted")
    assert granted[0].details["approval"] == "self_confirmed"


def test_single_admin_cannot_self_confirm_an_expired_view(
    harness: Any, db_session: Any
) -> None:
    """The self-confirm path must reject expiry before the sweeper runs."""
    h = harness
    grant_permission(db_session, h.user, "decide_approvals")
    grant_permission(db_session, h.user, "control_managed_agent")

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.viewer(view) as sock:
                await RfbViewer(sock).handshake()
                row = h.view(view["view_id"])
                assert row.state == "live"
                row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                    seconds=1
                )
                h.db.commit()
                async with h.http() as client:
                    response = await client.post(
                        f"/api/v1/desktop/views/{view['view_id']}/control",
                        json={"reason": "fix the stuck dialog"},
                    )
                assert response.status_code == 409, response.text
                assert response.json()["detail"]["code"] == "desktop_view_closed"
                assert h.view(view["view_id"]).mode == "view_only"
            await endpoint.close()

    asyncio.run(scenario())
    assert h.audit("desktop.control.granted") == []


def test_closing_the_view_settles_its_control_request(harness, two_admins):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
                await client.delete(f"/api/v1/desktop/views/{view['view_id']}")
            approval_id = uuid.UUID(asked.json()["control_request"]["approval_id"])
            h.db.expire_all()
            assert h.db.get(models.ApprovalRequest, approval_id).status == "expired"
            await endpoint.close()

    asyncio.run(scenario())
    denied = h.audit("desktop.control.denied")
    assert len(denied) == 1 and denied[0].details["approval_status"] == "expired"


def test_bulk_inbox_expiry_leaves_control_requests_to_the_service(harness, two_admins):
    from preloop.models.crud import crud_approval_request

    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
            approval_id = uuid.UUID(asked.json()["control_request"]["approval_id"])
            row = h.db.get(models.ApprovalRequest, approval_id)
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=5
            )
            h.db.commit()
            crud_approval_request.expire_stale_pending(
                h.db, account_id=h.user.account_id
            )
            h.db.expire_all()
            assert h.db.get(models.ApprovalRequest, approval_id).status == "pending"
            async with h.http() as client:
                await client.get(f"/api/v1/desktop/views/{view['view_id']}")
            h.db.expire_all()
            assert h.db.get(models.ApprovalRequest, approval_id).status == "expired"
            await endpoint.close()

    asyncio.run(scenario())
    assert len(h.audit("desktop.control.denied")) == 1


def test_approval_after_the_view_ran_out_is_a_denial(harness, two_admins):
    h = harness
    colleague = two_admins

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
            row = h.view(view["view_id"])
            row.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=1
            )
            h.db.commit()
            await _approvals(h).approve_request(
                uuid.UUID(asked.json()["control_request"]["approval_id"]),
                "ok",
                user_id=colleague.id,
            )
            assert h.view(view["view_id"]).mode == "view_only"
            await endpoint.close()

    asyncio.run(scenario())
    assert h.audit("desktop.control.granted") == []
    assert len(h.audit("desktop.control.denied")) == 1


def test_second_admin_found_beyond_the_first_page(harness, db_session, monkeypatch):
    from preloop.utils import permissions

    h = harness
    members = [
        _make_user(db_session, account_id=h.user.account_id, username=f"desk-m{i}")
        for i in range(3)
    ]
    approver = max(members, key=lambda m: m.id)
    monkeypatch.setattr(permissions, "members_have_full_access", lambda: False)
    monkeypatch.setattr(
        desktop, "user_holds_permission", lambda db, u, n: u.id == approver.id
    )
    real = desktop.crud_desktop.active_member_ids
    monkeypatch.setattr(
        desktop.crud_desktop,
        "active_member_ids",
        lambda db, **kw: real(db, **{**kw, "limit": 1}),
    )
    assert desktop._has_other_approver(db_session, h.user)
    monkeypatch.setattr(desktop, "user_holds_permission", lambda db, u, n: False)
    assert not desktop._has_other_approver(db_session, h.user)


def test_control_request_notifies_the_other_admins_not_the_requester(
    harness, two_admins, monkeypatch
):
    from preloop.services.approval_service import ApprovalService

    h = harness
    colleague = two_admins
    notified = []

    async def capture(self, approval, workflow, *, recipient_user_ids):
        notified.append(recipient_user_ids)
        return {}

    monkeypatch.setattr(ApprovalService, "send_notifications", capture)

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
            assert asked.status_code == 202
            await endpoint.close()

    asyncio.run(scenario())
    assert notified == [[colleague.id]]


@pytest.mark.parametrize("channel", ["webhook", "slack", "mattermost"])
def test_control_request_queues_workflow_webhook(
    harness: Harness, two_admins: models.User, monkeypatch: Any, channel: str
) -> None:
    """Recipient selection must not replace the persistent workflow row."""
    from preloop.services import chat_worker
    from preloop.services.approval_service import ApprovalService

    h = harness
    workflow = models.ApprovalWorkflow(
        account_id=h.user.account_id,
        name="Desktop approval notifications",
        approval_type="standard",
        is_default=True,
        approver_user_ids=[h.user.id],
        channel_configs={channel: {"url": "https://chat.example.com/approvals"}},
    )
    h.db.add(workflow)
    h.db.commit()
    # Only external per-user delivery is stubbed. Keep the webhook shim,
    # delivery outbox and notification audit on the real database session.
    monkeypatch.setattr(
        ApprovalService,
        "_partition_approvers_for_stagger",
        AsyncMock(
            return_value={
                "push_capable": [two_admins.id],
                "email_only": [two_admins.id],
                "both_immediate": [],
                "both_stagger": [],
                "all_email": [two_admins.id],
            }
        ),
    )
    push = AsyncMock(return_value={"success": True})
    email = AsyncMock(return_value={"success": True})
    chat = Mock(return_value=1)
    monkeypatch.setattr(ApprovalService, "_send_push_notification", push)
    monkeypatch.setattr(ApprovalService, "_send_email_notification", email)
    monkeypatch.setattr(chat_worker, "enqueue_approval_notifications", chat)

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the stuck dialog"},
                )
            assert asked.status_code == 202, asked.text
            approval_id = uuid.UUID(asked.json()["control_request"]["approval_id"])
            delivery = (
                h.db.query(models.WebhookDelivery)
                .filter(models.WebhookDelivery.subject_id == approval_id)
                .one()
            )
            assert delivery.status == "pending"
            shim = h.db.get(models.WebhookEndpoint, delivery.endpoint_id)
            assert shim.approval_workflow_id == workflow.id
            assert shim.url == "https://chat.example.com/approvals"
            h.db.refresh(workflow)
            assert workflow.approver_user_ids == [h.user.id]
            assert workflow.approval_config["webhook_secret"]
            assert h.db.is_active
            push.assert_awaited_once_with(
                push.await_args.args[0], workflow, user_ids=[two_admins.id]
            )
            email.assert_awaited_once_with(
                email.await_args.args[0], workflow, user_ids=[two_admins.id]
            )
            assert chat.call_args.args[2] == [two_admins.id]
            await endpoint.close()

    asyncio.run(scenario())


def test_existing_approval_found_under_the_lock_is_reused(harness, two_admins):
    """The branch the view lock exists for: the pre-lock check missed a
    pending approval that another request created."""
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            url = f"/api/v1/desktop/views/{view['view_id']}/control"
            async with h.http() as client:
                first = await client.post(url, json={"reason": "fix the dialog"})
                h.monkeypatch.setattr(desktop, "_pending_control", lambda db, v: None)
                second = await client.post(url, json={"reason": "fix the dialog"})
            assert second.status_code == 202
            assert (
                second.json()["control_request"]["approval_id"]
                == first.json()["control_request"]["approval_id"]
            )
            assert (
                h.db.query(models.ApprovalRequest)
                .filter(models.ApprovalRequest.tool_name == "desktop_control")
                .count()
                == 1
            )
            await endpoint.close()

    asyncio.run(scenario())


def test_view_closed_before_the_lock_gets_no_approval(harness, two_admins):
    h = harness

    async def scenario() -> None:
        async with serving(h) as vnc:
            endpoint, view = await _live_viewer(h, vnc)
            real = desktop.crud_desktop.lock_view_async

            async def close_first(db, *, view_id):
                row = h.view(view["view_id"])
                row.state = "closed"
                h.db.commit()
                return await real(db, view_id=view_id)

            h.monkeypatch.setattr(desktop.crud_desktop, "lock_view_async", close_first)
            async with h.http() as client:
                asked = await client.post(
                    f"/api/v1/desktop/views/{view['view_id']}/control",
                    json={"reason": "fix the dialog"},
                )
            assert asked.status_code == 409
            assert asked.json()["detail"]["code"] == "desktop_view_closed"
            assert (
                h.db.query(models.ApprovalRequest)
                .filter(models.ApprovalRequest.tool_name == "desktop_control")
                .count()
                == 0
            )
            await endpoint.close()

    asyncio.run(scenario())
