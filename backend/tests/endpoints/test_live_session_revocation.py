"""Local HTTP/WebSocket revocation across independent replica registries."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.api.auth import auth_router
from preloop.api.auth.jwt import (
    create_access_token,
    create_refresh_token,
    get_current_active_user,
)
from preloop.api.endpoints import users, websockets
from preloop.api.middleware.websocket_auth import WebSocketAuthMiddleware
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_cli_session, crud_user
from preloop.models.db.session import get_db_session
from preloop.plugins import account_hooks
from preloop.schemas.auth import TokenData
from preloop.services import auth_revocation, websocket_manager
from preloop.services.websocket_manager import WebSocketManager


def make_user(
    db: Session, account_id: UUID, *, person_id: UUID | None = None
) -> models.User:
    """Create a synthetic member using the real identity defaults."""
    suffix = uuid4().hex
    return crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "username": f"member-{suffix}",
            "email": f"{suffix}@example.com",
            "email_verified": True,
            "is_active": True,
            "person_id": person_id,
        },
    )


def access(user: models.User) -> str:
    return create_access_token(
        data={"sub": str(user.id)}, auth_generation=user.auth_generation
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def api(
    db_override: Session | None = None,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(websockets.router, prefix="/api/v1")
    app.add_middleware(WebSocketAuthMiddleware)
    if db_override is not None:
        app.dependency_overrides[get_db_session] = lambda: db_override

    @app.get("/probe")
    def probe(user: models.User = Depends(get_current_active_user)) -> dict[str, str]:
        return {"id": str(user.id)}

    return app


def test_admin_revoke_is_account_scoped_audited_and_preserves_siblings(
    db_session: Session, test_user: models.User
) -> None:
    admin = test_user
    member = make_user(db_session, admin.account_id)
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    sibling = make_user(db_session, other_account.id, person_id=member.person_id)
    member_id, sibling_id, admin_id = member.id, sibling.id, admin.id
    session = crud_cli_session.create(
        db_session, user_id=member_id, refresh_jti=uuid4().hex
    )
    session_id = session.id
    sibling_session = crud_cli_session.create(
        db_session, user_id=sibling_id, refresh_jti=uuid4().hex
    )
    sibling_session_id = sibling_session.id
    old, other, admin_token = access(member), access(sibling), access(admin)
    refresh = create_refresh_token(
        sub=str(member_id), scopes=[], auth_generation=member.auth_generation
    )
    _, key = crud_api_key.issue(
        db_session,
        obj_in={
            "account_id": member.account_id,
            "user_id": member_id,
            "name": "Independent key",
        },
    )
    # Even an installed global fan-out must not broaden the admin operation.
    account_hooks.register_revoke_fanout(lambda db, user_id: [sibling_id])
    try:
        with TestClient(api(db_session)) as client:
            response = client.post(
                f"/api/v1/users/{member_id}/sessions/revoke-all",
                headers=bearer(admin_token),
            )
            assert response.status_code == 200, response.text
            assert client.get("/probe", headers=bearer(old)).status_code == 401
            assert (
                client.post(
                    "/api/v1/auth/refresh", json={"refresh_token": refresh}
                ).status_code
                == 401
            )
            assert client.get("/probe", headers=bearer(other)).status_code == 200
            assert client.get("/probe", headers=bearer(key)).status_code == 200
        db_session.expire_all()
        assert db_session.get(models.CliSession, session_id).revoked_at is not None
        assert db_session.get(models.CliSession, sibling_session_id).revoked_at is None
        assert db_session.get(models.User, sibling_id).auth_generation == 0
        audit = db_session.scalar(
            select(models.AuditLog).where(
                models.AuditLog.action == "auth.sessions.revoked",
                models.AuditLog.resource_id == str(member_id),
            )
        )
        assert audit is not None
        assert audit.account_id == admin.account_id
        assert audit.user_id == admin_id
        assert audit.details["reason_code"] == "admin_member_sign_out"
    finally:
        account_hooks.register_revoke_fanout(None)


def test_admin_cross_account_is_404_and_denied_permission_is_403(
    db_session: Session, test_user: models.User
) -> None:
    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Foreign account"}
    )
    foreign = make_user(db_session, foreign_account.id)
    token = access(test_user)
    with TestClient(api(db_session)) as client:
        response = client.post(
            f"/api/v1/users/{foreign.id}/sessions/revoke-all", headers=bearer(token)
        )
        assert response.status_code == 404
        account_hooks.register_authorizer(
            lambda context, action, resource: SimpleNamespace(
                allowed=False, reason="Requires manage_users"
            )
        )
        try:
            response = client.post(
                f"/api/v1/users/{test_user.id}/sessions/revoke-all",
                headers=bearer(token),
            )
            assert response.status_code == 403
        finally:
            account_hooks.register_authorizer(None)
    assert test_user.auth_generation == 0
    assert foreign.auth_generation == 0


def test_admin_audit_failure_rolls_back_generation_and_cli_revoke(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit failure cannot leave an unaudited committed admin revoke."""
    from preloop.services import auth_audit

    auth_revocation.install_revocation_notifications()
    member = make_user(db_session, test_user.account_id)
    session = crud_cli_session.create(
        db_session, user_id=member.id, refresh_jti=uuid4().hex
    )
    member_id, session_id = member.id, session.id
    generation = member.auth_generation
    token = access(test_user)
    published = Mock()
    monkeypatch.setattr(auth_revocation, "publish_realtime", published)
    monkeypatch.setattr(
        auth_audit,
        "record_auth_event",
        Mock(side_effect=RuntimeError("Audit unavailable")),
    )
    with TestClient(api(db_session), raise_server_exceptions=False) as client:
        response = client.post(
            f"/api/v1/users/{member_id}/sessions/revoke-all", headers=bearer(token)
        )
    assert response.status_code == 500
    db_session.expire_all()
    assert db_session.get(models.User, member_id).auth_generation == generation
    assert db_session.get(models.CliSession, session_id).revoked_at is None
    published.assert_not_called()


class LocalNats:
    """Exercise production subscriptions with Core NATS fan-out semantics."""

    is_connected = True

    def __init__(self) -> None:
        self.subscriptions: list[
            tuple[str, str, Callable[[SimpleNamespace], Coroutine[Any, Any, None]]]
        ] = []
        self.deliveries: list[str] = []

    async def subscribe(
        self,
        subject: str,
        *,
        queue: str = "",
        cb: Callable[[SimpleNamespace], Coroutine[Any, Any, None]],
    ) -> SimpleNamespace:
        subscription = (subject, queue, cb)
        self.subscriptions.append(subscription)

        async def unsubscribe() -> None:
            self.subscriptions.remove(subscription)

        return SimpleNamespace(unsubscribe=unsubscribe)

    async def publish(self, subject: str, data: bytes) -> None:
        for subscribed, queue, callback in list(self.subscriptions):
            if subscribed == subject:
                assert queue == "", "Revocations must reach every replica"
                await callback(SimpleNamespace(data=data))
                self.deliveries.append(subject)


@pytest.fixture
def committed_member(db_engine: Engine) -> Iterator[models.User]:
    """A real committed identity, visible to HTTP and timer worker sessions."""
    with Session(db_engine, expire_on_commit=False) as db:
        account = crud_account.create(db, obj_in={"organization_name": "Socket test"})
        member = make_user(db, account.id)
        person_id = member.person_id
        yield member
        db.rollback()
        db.delete(db.get(models.Account, account.id))
        db.commit()
        db.delete(db.get(models.Person, person_id))
        db.commit()


@pytest.mark.parametrize("event_delivery", [True, False])
@pytest.mark.parametrize(
    "message_auth,credential",
    [(True, "access"), (False, "access"), (False, "refresh"), (False, "purpose")],
)
def test_unified_revoke_closes_second_replica_or_timer(
    monkeypatch: pytest.MonkeyPatch,
    committed_member: models.User,
    event_delivery: bool,
    message_auth: bool,
    credential: str,
) -> None:
    """Call the real self revoke endpoint while /ws/unified stays open."""
    auth_revocation.install_revocation_notifications()
    first, second = WebSocketManager(), WebSocketManager()
    bus = LocalNats()
    monkeypatch.setattr(websockets, "manager", second)
    monkeypatch.setattr(
        websocket_manager,
        "get_task_publisher",
        AsyncMock(return_value=SimpleNamespace(nc=bus)),
    )
    monkeypatch.setattr(
        auth_revocation, "REVOCATION_RECHECK_SECONDS", 60 if event_delivery else 0.05
    )
    # Session telemetry is independent of authentication and is not under test.
    session = SimpleNamespace(
        id=str(uuid4()),
        connection_id=str(uuid4()),
        is_authenticated=not message_auth,
        display_name="Test member",
        metadata={},
    )
    monkeypatch.setattr(
        websockets.session_manager, "create_session", AsyncMock(return_value=session)
    )
    monkeypatch.setattr(websockets.session_manager, "end_session", AsyncMock())
    monkeypatch.setattr(websockets.session_manager, "upgrade_session", AsyncMock())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = [
            asyncio.create_task(websocket_manager.nats_consumer(manager))
            for manager in (first, second)
        ]
        while len([s for s in bus.subscriptions if s[0] == "auth.revoked"]) < 2:
            await asyncio.sleep(0)
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    access_token = access(committed_member)
    token = access_token
    if credential == "refresh":
        token = create_refresh_token(
            sub=str(committed_member.id),
            scopes=[],
            auth_generation=committed_member.auth_generation,
        )
    elif credential == "purpose":
        token = create_access_token(
            data={"sub": str(committed_member.id), "purpose": "mcp_oauth_authorize"},
            auth_generation=committed_member.auth_generation,
        )
    with TestClient(api(lifespan=lifespan)) as client:

        def publish(subject: str, data: bytes) -> None:
            if event_delivery:
                client.portal.call(bus.publish, subject, data)

        monkeypatch.setattr(auth_revocation, "publish_realtime", publish)
        headers = {} if message_auth else bearer(token)
        with client.websocket_connect("/api/v1/ws/unified", headers=headers) as socket:
            assert socket.receive_json()["type"] == "handshake"
            if message_auth:
                socket.send_json({"type": "authenticate", "token": token})
                assert socket.receive_json()["type"] == "authenticated"
            assert first.revocable_connections == {}
            assert len(second.revocable_connections) == 1
            response = client.post(
                "/api/v1/auth/sessions/revoke-all", headers=bearer(access_token)
            )
            assert response.status_code == 200, response.text
            frame = socket.receive()
            assert frame["type"] == "websocket.close"
            assert frame["code"] == 4401
            assert len(bus.deliveries) == (2 if event_delivery else 0)
    assert second.revocable_connections == {}


@pytest.fixture
def committed_admin_family(db_engine: Engine) -> Iterator[tuple[models.User, ...]]:
    """An admin, target member and linked membership visible across sessions."""
    with Session(db_engine, expire_on_commit=False) as db:
        account = crud_account.create(db, obj_in={"organization_name": "Admin test"})
        other = crud_account.create(db, obj_in={"organization_name": "Sibling test"})
        admin = make_user(db, account.id)
        member = make_user(db, account.id)
        sibling = make_user(db, other.id, person_id=member.person_id)
        person_ids = [admin.person_id, member.person_id]
        try:
            yield admin, member, sibling
        finally:
            db.rollback()
            for account_id in (account.id, other.id):
                db.delete(db.get(models.Account, account_id))
            db.commit()
            for person_id in person_ids:
                db.delete(db.get(models.Person, person_id))
            db.commit()


def test_admin_event_closes_target_live_socket_and_preserves_sibling(
    monkeypatch: pytest.MonkeyPatch,
    committed_admin_family: tuple[models.User, ...],
) -> None:
    """The real admin transaction publishes to a second replica's live sockets."""
    admin, member, sibling = committed_admin_family
    auth_revocation.install_revocation_notifications()
    first, second = WebSocketManager(), WebSocketManager()
    bus = LocalNats()
    monkeypatch.setattr(websockets, "manager", second)
    monkeypatch.setattr(
        websocket_manager,
        "get_task_publisher",
        AsyncMock(return_value=SimpleNamespace(nc=bus)),
    )
    monkeypatch.setattr(auth_revocation, "REVOCATION_RECHECK_SECONDS", 60)
    monkeypatch.setattr(
        websockets.session_manager,
        "create_session",
        AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(
                id=str(uuid4()),
                connection_id=str(uuid4()),
                is_authenticated=True,
                display_name="Test member",
                metadata={},
            )
        ),
    )
    monkeypatch.setattr(websockets.session_manager, "end_session", AsyncMock())
    checked: set[str] = set()
    original_check = auth_revocation.session_is_revoked

    async def check(claims: TokenData) -> bool:
        result = await original_check(claims)
        checked.add(claims.sub)
        return result

    monkeypatch.setattr(auth_revocation, "session_is_revoked", check)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = [
            asyncio.create_task(websocket_manager.nats_consumer(manager))
            for manager in (first, second)
        ]
        try:
            while len([s for s in bus.subscriptions if s[0] == "auth.revoked"]) < 2:
                await asyncio.sleep(0)
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with TestClient(api(lifespan=lifespan)) as client:

        def publish(subject: str, data: bytes) -> None:
            client.portal.call(bus.publish, subject, data)

        monkeypatch.setattr(auth_revocation, "publish_realtime", publish)
        with (
            client.websocket_connect(
                "/api/v1/ws/unified", headers=bearer(access(member))
            ) as target,
            client.websocket_connect(
                "/api/v1/ws/unified", headers=bearer(access(sibling))
            ) as other,
        ):
            assert target.receive_json()["type"] == "handshake"
            assert other.receive_json()["type"] == "handshake"

            async def initial_checks() -> None:
                while checked != {str(member.id), str(sibling.id)}:
                    await asyncio.sleep(0.01)

            client.portal.call(asyncio.wait_for, initial_checks(), 2)
            response = client.post(
                f"/api/v1/users/{member.id}/sessions/revoke-all",
                headers=bearer(access(admin)),
            )
            assert response.status_code == 200
            frame = target.receive()
            assert frame["type"] == "websocket.close" and frame["code"] == 4401
            assert bus.deliveries == ["auth.revoked", "auth.revoked"]
            assert first.revocable_connections == {}
            assert len(second.revocable_connections) == 1
            other.send_json({"type": "ping"})
            assert other.receive_json()["type"] == "pong"
    assert second.revocable_connections == {}
