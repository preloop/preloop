"""Native permission authentication must release DB resources before human waits."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session, sessionmaker

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_managed_agent,
    crud_user,
)


@pytest.fixture
def permission_credential(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict]:
    """Commit an isolated identity so worker-owned connections can authenticate it."""
    factory = sessionmaker(bind=db_engine)
    unique = uuid4().hex
    with factory() as db:
        account = crud_account.create(db, obj_in={"organization_name": unique})
        account_id = account.id
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account_id,
                "email": f"{unique}@example.com",
                "username": unique,
                "hashed_password": "unused-test-password",
                "is_active": True,
                "email_verified": True,
            },
        )
        now = datetime.now(timezone.utc)
        agent = crud_managed_agent.create(
            db,
            obj_in={
                "account_id": account_id,
                "owner_user_id": user.id,
                "agent_kind": "opencode",
                "session_source_type": "opencode",
                "session_source_id": unique,
                "display_name": "Permission test agent",
                "lifecycle_updated_at": now,
                "last_seen_at": now,
            },
        )
        key, token = crud_api_key.create_runtime_key(
            db,
            name=unique,
            account_id=account_id,
            user_id=user.id,
            context_data={"managed_agent_id": str(agent.id)},
        )
        identity = {
            "account_id": account_id,
            "user_id": user.id,
            "agent_id": agent.id,
            "key_id": key.id,
            "token": token,
        }
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: factory)
    monkeypatch.setattr(endpoint.settings, "preloop_url", "https://example.invalid")
    try:
        yield identity
    finally:
        with factory() as db:
            db.execute(delete(models.Account).where(models.Account.id == account_id))
            db.commit()


@pytest.mark.asyncio
async def test_permission_wait_releases_connection_and_keeps_scalar_identity(
    db_engine: Engine, permission_credential: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = db_engine.pool.checkedout()
    waiting, release = asyncio.Event(), asyncio.Event()
    captured = {}

    async def decide(**kwargs: Any) -> tuple[str, str, str, bool]:
        captured.update(kwargs)
        waiting.set()
        await release.wait()
        return "allow", "Approved", "request", False

    monkeypatch.setattr(endpoint, "request_agent_permission", decide)
    task = asyncio.create_task(
        endpoint.agent_permission_check(
            endpoint.AgentPermissionCheckRequest(tool_name="Bash"),
            authorization="Bearer " + permission_credential["token"],
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=3)
        assert not task.done()
        assert db_engine.pool.checkedout() == baseline
        assert captured["account_id"] == str(permission_credential["account_id"])
        assert captured["user_id"] == permission_credential["user_id"]
        assert captured["managed_agent_id"] == permission_credential["agent_id"]
        assert captured["runtime_session_id"] is None
        assert captured["managed_agent_name"] == "Permission test agent"
        with Session(db_engine) as db:
            key = crud_api_key.get(db, id=permission_credential["key_id"])
            assert key.last_used_at is not None
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_blocked_credential_query_keeps_event_loop_responsive(
    permission_credential: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered, release = asyncio.Event(), threading.Event()
    real_get = crud_api_key.get_by_key
    worker_threads = []

    def blocked_get(db: Session, *, key: str) -> models.ApiKey | None:
        worker_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        # Bounded even if the regression accidentally executes on the event loop.
        release.wait(timeout=2)
        return real_get(db, key=key)

    monkeypatch.setattr(crud_api_key, "get_by_key", blocked_get)
    monkeypatch.setattr(
        endpoint,
        "request_agent_permission",
        AsyncMock(return_value=("deny", "No", None, False)),
    )
    task = asyncio.create_task(
        endpoint.agent_permission_check(
            endpoint.AgentPermissionCheckRequest(tool_name="Bash"),
            authorization="Bearer " + permission_credential["token"],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.parametrize(
    "invalid",
    [
        "revoked",
        "expired",
        "inactive_user",
        "suspended_agent",
        "foreign_agent",
        "unbound",
        "missing_runtime",
    ],
)
def test_identity_auth_rejects_invalid_bindings_and_returns_connection(
    invalid: str, db_engine: Engine, permission_credential: dict
) -> None:
    with Session(db_engine) as db:
        key = crud_api_key.get(db, id=permission_credential["key_id"])
        if invalid == "revoked":
            key.is_active = False
        elif invalid == "expired":
            key.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elif invalid == "inactive_user":
            crud_user.get(db, id=permission_credential["user_id"]).is_active = False
        elif invalid == "suspended_agent":
            crud_managed_agent.get(
                db, id=permission_credential["agent_id"]
            ).lifecycle_state = "suspended"
        elif invalid == "foreign_agent":
            # An existing agent in another account cannot satisfy this key's binding.
            other = crud_account.create(db, obj_in={"organization_name": uuid4().hex})
            key.account_id = other.id
            other_id = other.id
        elif invalid == "unbound":
            key.context_data = {}
        else:
            key.context_data = {**key.context_data, "runtime_session_id": str(uuid4())}
        db.commit()
    baseline = db_engine.pool.checkedout()
    try:
        with pytest.raises(HTTPException) as error:
            endpoint._resolve_permission_identity(permission_credential["token"])
        assert error.value.status_code == 401
        assert error.value.headers == {"WWW-Authenticate": "Bearer"}
        assert db_engine.pool.checkedout() == baseline
    finally:
        if invalid == "foreign_agent":
            with Session(db_engine) as db:
                db.execute(delete(models.Account).where(models.Account.id == other_id))
                db.commit()


def test_identity_snapshot_is_immutable(permission_credential: dict) -> None:
    identity = endpoint._resolve_permission_identity(permission_credential["token"])
    with pytest.raises(FrozenInstanceError):
        identity.account_id = "different-account"
