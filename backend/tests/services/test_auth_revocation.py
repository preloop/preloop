"""Committed revocation notifications and credential-specific live teardown."""

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import create_access_token
from preloop.models import models
from preloop.models.crud import crud_person, crud_user
from preloop.models.crud.auth_revocation import stage_revocation
from preloop.models.crud.membership_lifecycle import offboard_record
from preloop.models.crud.sso_enforcement import revoke_non_sso
from preloop.services import auth_revocation
from preloop.services.websocket_manager import WebSocketManager


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Collect bytes emitted by the real commit hook, without a NATS server."""
    auth_revocation.install_revocation_notifications()
    messages: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        auth_revocation,
        "publish_realtime",
        lambda subject, data: messages.append((subject, json.loads(data))),
    )
    return messages


@pytest.mark.parametrize(
    "mutation",
    ["self", "password", "email", "verify", "status", "member", "sso", "offboard"],
)
def test_generation_writers_publish_only_after_commit(
    db_session: Session,
    test_user: models.User,
    published: list[tuple[str, dict[str, Any]]],
    mutation: str,
) -> None:
    """All generation writers, including plugin-facing CRUD, use the same seam."""
    published.clear()
    user_id = test_user.id
    person = crud_person.resolve_for_membership(db_session, membership_id=user_id)
    if mutation == "self":
        crud_person.revoke_sessions(db_session, person_id=person.id, commit=False)
    elif mutation == "password":
        crud_person.set_password(
            db_session,
            person_id=person.id,
            hashed_password="synthetic-hash",
            commit=False,
        )
    elif mutation == "email":
        crud_person.update_profile(
            db_session,
            membership_id=user_id,
            changes={"email": f"{uuid4()}@example.com"},
            commit=False,
        )
    elif mutation == "verify":
        crud_person.verify_email(
            db_session,
            person_id=person.id,
            expected_email=person.email,
            expected_generation=person.auth_generation,
            commit=False,
        )
    elif mutation == "status":
        crud_person.set_status(
            db_session, person_id=person.id, status="suspended", commit=False
        )
    elif mutation == "member":
        crud_user.bump_auth_generation(db_session, user_id, commit=False)
    elif mutation == "sso":
        revoke_non_sso(
            db_session,
            account_id=test_user.account_id,
            domains={person.email.rpartition("@")[2]},
        )
    else:
        offboard_record(
            db_session,
            account_id=test_user.account_id,
            user_id=user_id,
            reason="Test removal",
        )
    assert published == []
    db_session.commit()
    assert len(published) == 1
    assert published[0][0] == "auth.revoked"
    assert published[0][1]["user_ids"] == [str(user_id)]
    assert published[0][1]["generation"] > 0


def test_rollback_and_savepoint_delivery(
    db_engine: Engine, published: list[tuple[str, dict[str, Any]]]
) -> None:
    """An inner commit must never publish an outer rollback's revocation."""
    target = uuid4()
    with Session(db_engine) as db:
        with db.begin():
            with db.begin_nested():
                stage_revocation(db, user_id=target, generation=1)
            assert published == []
            db.rollback()
        assert published == []
        with db.begin():
            stage_revocation(db, user_id=target, generation=2)
            try:
                with db.begin_nested():
                    stage_revocation(db, user_id=target, generation=3)
                    raise ValueError("roll back this savepoint")
            except ValueError:
                pass
            with db.begin_nested():
                stage_revocation(db, user_id=target, generation=4)
            assert published == []
        assert [data["generation"] for _, data in published] == [2, 4]


def token(user_id: UUID, generation: int = 0) -> str:
    """Mint a local test JWT with no upstream calls."""
    return create_access_token(data={"sub": str(user_id)}, auth_generation=generation)


@pytest.mark.asyncio
async def test_close_only_target_jwts_and_preserve_machine_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API-key, runner-token and agent-control sockets survive user sign-out."""
    from preloop.api.endpoints.agent_control import AgentControlConnectionManager
    from preloop.api.endpoints import runners

    monkeypatch.setattr(
        auth_revocation, "session_is_revoked", AsyncMock(return_value=False)
    )
    manager = WebSocketManager()
    user, other = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    target = [SimpleNamespace(close=AsyncMock()) for _ in range(2)]
    bystander = SimpleNamespace(close=AsyncMock())
    api_key = SimpleNamespace(close=AsyncMock())
    runner = SimpleNamespace(close=AsyncMock())
    agent = SimpleNamespace(close=AsyncMock())
    machine_user = SimpleNamespace(id=user.id, _auth_api_key=object())
    watches = [manager.watch_user_connection(ws, user, token(user.id)) for ws in target]
    watches.append(manager.watch_user_connection(bystander, other, token(other.id)))
    assert (
        manager.watch_user_connection(api_key, machine_user, "synthetic-api-key")
        is None
    )
    assert manager.watch_user_connection(runner, user, "synthetic-runner-token") is None
    control = AgentControlConnectionManager()
    agent_id = await control.connect(managed_agent_id=str(uuid4()), websocket=agent)
    runner_id = str(uuid4())
    monkeypatch.setitem(runners._live, runner_id, runner)
    try:
        await manager.close_user_connections([str(user.id)])
        for ws in target:
            ws.close.assert_awaited_once_with(code=4401, reason="Session revoked")
        for ws in (bystander, api_key, runner, agent):
            ws.close.assert_not_awaited()
        assert runners._live[runner_id] is runner
        assert control._connections[agent_id] is agent
        # A missing or empty target set never means "all users".
        await manager.close_user_connections([])
        bystander.close.assert_not_awaited()
    finally:
        for watch in watches:
            await manager.unwatch_user_connection(watch)
        await control.disconnect(agent_id)


@pytest.mark.asyncio
async def test_delayed_event_rechecks_generation_instead_of_closing_fresh_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WebSocketManager()
    user = SimpleNamespace(id=uuid4())
    old, fresh = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(
        auth_revocation,
        "session_is_revoked",
        AsyncMock(side_effect=lambda claims: claims.gen < 2),
    )
    old_id = manager.watch_user_connection(old, user, token(user.id, 1))
    fresh_id = manager.watch_user_connection(fresh, user, token(user.id, 2))
    try:
        await manager.recheck_user_connections([str(user.id)])
        old.close.assert_awaited_once()
        fresh.close.assert_not_awaited()
    finally:
        await manager.unwatch_user_connection(old_id)
        await manager.unwatch_user_connection(fresh_id)


@pytest.mark.asyncio
async def test_token_expiring_during_handler_setup_still_gets_a_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the verified admission claims even after their encoding expires."""
    from fastapi import HTTPException
    from preloop.api.auth import jwt

    manager = WebSocketManager()
    user_id = uuid4()
    encoded = token(user_id)
    admitted = jwt.decode_token(encoded)
    user = SimpleNamespace(id=user_id, _auth_token_data=admitted)
    socket = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(jwt, "decode_token", MagicMock(side_effect=HTTPException(401)))
    monkeypatch.setattr(
        auth_revocation, "session_is_revoked", AsyncMock(return_value=False)
    )
    watch = manager.watch_user_connection(socket, user, encoded)
    try:
        assert watch is not None
        await manager.close_user_connections([str(user_id)])
        socket.close.assert_awaited_once_with(code=4401, reason="Session revoked")
    finally:
        await manager.unwatch_user_connection(watch)


@pytest.mark.asyncio
async def test_timer_checks_without_client_traffic_or_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WebSocketManager()
    user = SimpleNamespace(id=uuid4())
    checked = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(auth_revocation, "session_is_revoked", checked)
    monkeypatch.setattr(auth_revocation, "REVOCATION_RECHECK_SECONDS", 0.01)
    closed = asyncio.Event()
    socket = SimpleNamespace(close=AsyncMock(side_effect=lambda **kwargs: closed.set()))
    watch = manager.watch_user_connection(socket, user, token(user.id))
    try:
        await asyncio.wait_for(closed.wait(), timeout=1)
        socket.close.assert_awaited_once_with(code=4401, reason="Session revoked")
        assert checked.await_count == 2
        assert manager.revocable_connections == {}
    finally:
        await manager.unwatch_user_connection(watch)


@pytest.mark.asyncio
async def test_recheck_uses_existing_sso_policy_and_short_lived_session(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the policy-rejected non-SSO session is revoked by enforcement."""
    from fastapi import HTTPException
    from preloop.api.auth import jwt

    async def run(operation: Callable[[Session], bool]) -> bool:
        return operation(db_session)

    def policy(
        db: Session, user: models.User, *, claims: dict[str, Any], purpose: str
    ) -> None:
        if claims.get("amr") != ["sso", "oidc"]:
            raise HTTPException(401, "Session revoked")

    monkeypatch.setattr(auth_revocation, "run_db_async", run)
    monkeypatch.setattr(jwt, "authorize_authentication", policy)
    claims = jwt.decode_token(token(test_user.id, test_user.auth_generation))
    assert await auth_revocation.session_is_revoked(claims)
    claims.claims["amr"] = ["sso", "oidc"]
    assert not await auth_revocation.session_is_revoked(claims)
