"""Management permissions, draft binding, OAuth replay and body-size boundaries."""

from __future__ import annotations
import hashlib
import ipaddress
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from preloop.api.endpoints import chat_setup
from preloop.models import models
from preloop.models.crud import crud_chat
from preloop.services.chat_providers import ChatProviderError
from preloop.utils.encryption import decrypt_value, encrypt_value


@pytest.mark.asyncio
async def test_discovery_denied_before_provider_lookup_without_management_permission(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(chat_setup.router)
    app.dependency_overrides[chat_setup.get_db_session] = lambda: object()
    app.dependency_overrides[chat_setup.get_current_active_user] = lambda: (
        SimpleNamespace(id=uuid4(), account_id=uuid4())
    )
    monkeypatch.setattr(chat_setup, "require_active_account", lambda *args: None)
    monkeypatch.setattr(chat_setup, "ensure_permission_in_oss", lambda *args: None)
    monkeypatch.setattr(chat_setup, "has_permission", lambda *args: False)

    async def forbidden(*args):
        raise AssertionError("No provider request may run before authorization")

    monkeypatch.setattr(chat_setup, "discover", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/setup", json={"provider": "slack", "bot_token": "secret"}
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_setup_bounds_stream_before_json_parsing() -> None:
    app = FastAPI()
    app.include_router(chat_setup.router)

    async def chunks():
        for _ in range(5):
            yield b"x" * 8192

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/setup",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_draft_actor_account_expiry_and_oauth_replay() -> None:
    url = os.getenv("CHAT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires isolated migrated CHAT_TEST_DATABASE_URL")
    engine = create_engine(url)
    with Session(engine) as db:
        account = models.Account(organization_name="Synthetic draft")
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username="draft-" + uuid4().hex,
            email="member@example.com",
            hashed_password="unused",
            is_active=True,
        )
        db.add(user)
        db.commit()
        account_id, user_id = account.id, user.id
        digest = hashlib.sha256(uuid4().bytes).hexdigest()
        try:
            draft = crud_chat.drafts.create(
                db,
                obj_in={
                    "account_id": account_id,
                    "user_id": user_id,
                    "provider": "slack",
                    "state_encrypted": "encrypted",
                    "expires_at": datetime.utcnow() + timedelta(minutes=10),
                    "oauth_digest": digest,
                },
            )
            assert crud_chat.setup_draft(db, draft.id, account_id, user_id)
            assert crud_chat.setup_draft(db, draft.id, uuid4(), user_id) is None
            assert crud_chat.setup_draft(db, draft.id, account_id, uuid4()) is None
            assert crud_chat.oauth_draft(db, digest).id == draft.id
            assert crud_chat.oauth_draft(db, digest) is None
            crud_chat.drafts.update(
                db,
                db_obj=draft,
                obj_in={"expires_at": datetime.utcnow() - timedelta(seconds=1)},
            )
            assert crud_chat.setup_draft(db, draft.id, account_id, user_id) is None
        finally:
            db.query(models.Account).filter_by(id=account_id).delete()
            db.commit()
    engine.dispose()


@pytest.fixture
def setup_rig(monkeypatch):
    url = os.getenv("CHAT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires isolated CHAT_TEST_DATABASE_URL")
    engine = create_engine(url)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        account = models.Account(organization_name="Synthetic setup review")
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username="setup-" + uuid4().hex,
            email="setup@example.com",
            hashed_password="unused",
            is_active=True,
        )
        db.add(user)
        db.commit()
        account_id, user_id = account.id, user.id
    user = SimpleNamespace(id=user_id, account_id=account_id)
    app = FastAPI()
    app.include_router(chat_setup.router, prefix="/api/v1")

    def sessions():
        with factory() as db:
            yield db

    app.dependency_overrides[chat_setup.get_db_session] = sessions
    app.dependency_overrides[chat_setup.manager] = lambda: user
    yield SimpleNamespace(app=app, factory=factory, user=user)
    with factory() as db:
        db.query(models.Account).filter_by(id=account_id).delete()
        db.commit()
    engine.dispose()


@pytest.mark.asyncio
async def test_slack_oauth_requires_initiating_browser_and_consumes_state_once(
    setup_rig, monkeypatch
):
    monkeypatch.setattr(chat_setup, "oauth_configured", lambda: True)
    for key in ("CLIENT_ID", "CLIENT_SECRET", "SIGNING_SECRET", "REDIRECT_URI"):
        monkeypatch.setenv(
            "CHAT_SLACK_" + key,
            "https://preloop.example.com/api/v1/chat/setup/slack/callback"
            if key == "REDIRECT_URI"
            else "synthetic",
        )
    exchange = AsyncMock(
        return_value={"access_token": "synthetic-bot-token", "team": {"id": "team"}}
    )
    monkeypatch.setattr(chat_setup, "_exchange_slack", exchange)
    monkeypatch.setattr(
        chat_setup,
        "discover",
        AsyncMock(
            return_value={
                "name": "Slack",
                "bot_user_id": "bot",
                "workspaces": [{"id": "team", "name": "Team"}],
            }
        ),
    )
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_rig.app),
            base_url="https://preloop.example.com",
        ) as first,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_rig.app),
            base_url="https://preloop.example.com",
        ) as other,
    ):
        started = await first.post("/api/v1/chat/setup/slack/oauth")
        cookie = started.headers["set-cookie"]
        assert all(
            flag in cookie.lower()
            for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=900")
        )
        state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][
            0
        ]
        path = "/api/v1/chat/setup/slack/callback"
        query = {"state": state, "code": "synthetic-code"}
        assert (await other.get(path, params=query)).status_code == 403
        other.cookies.set("__Host-preloop_slack_oauth", "wrong-browser-proof")
        assert (await other.get(path, params=query)).status_code == 403
        exchange.assert_not_awaited()
        saved_cookie = first.cookies.get("__Host-preloop_slack_oauth")
        accepted = await first.get(path, params=query)
        assert accepted.status_code == 200, accepted.text
        assert first.cookies.get("__Host-preloop_slack_oauth") is None
        first.cookies.set("__Host-preloop_slack_oauth", saved_cookie)
        assert (await first.get(path, params=query)).status_code == 400
        exchange.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_team_hint_is_422(setup_rig, monkeypatch):
    async def discovery(config, *, team_hint=""):
        from preloop.services.chat_setup import mattermost_team_name

        mattermost_team_name(team_hint, config.base_url)
        raise AssertionError("Malformed URL must be rejected")

    monkeypatch.setattr(chat_setup, "discover", discovery)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup_rig.app),
        base_url="https://preloop.example.com",
    ) as client:
        response = await client.post(
            "/api/v1/chat/setup",
            json={
                "provider": "mattermost",
                "bot_token": "synthetic",
                "base_url": "https://chat.example.com",
                "team_hint": "https://[broken",
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_discord_persists_before_install_and_retries_after_failure(
    setup_rig, monkeypatch
):
    info = {
        "name": "Discord",
        "bot_user_id": "bot",
        "application_id": "app",
        "verification_secret": "public-key",
        "workspaces": [{"id": "guild", "name": "Guild"}],
    }
    monkeypatch.setattr(
        chat_setup,
        "discover",
        AsyncMock(side_effect=lambda *args, **kwargs: dict(info)),
    )
    with setup_rig.factory() as db:
        draft = crud_chat.drafts.create(
            db,
            obj_in={
                "account_id": setup_rig.user.account_id,
                "user_id": setup_rig.user.id,
                "provider": "discord",
                "state_encrypted": encrypt_value(
                    '{"bot_token":"synthetic","bot_user_id":"bot"}'
                ),
                "expires_at": datetime.utcnow() + timedelta(minutes=10),
            },
        )
        draft_id = draft.id
    install = AsyncMock(side_effect=ChatProviderError("Synthetic command failure"))
    monkeypatch.setattr(chat_setup, "_install_discord", install)
    complete = crud_chat.complete_setup
    monkeypatch.setattr(
        crud_chat,
        "complete_setup",
        lambda *args: (_ for _ in ()).throw(ValueError("Setup expired")),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=setup_rig.app),
        base_url="https://preloop.example.com",
    ) as client:
        path = f"/api/v1/chat/setup/{draft_id}/finish"
        response = await client.post(path, json={"workspace_id": "guild"})
        assert response.status_code == 409
        install.assert_not_awaited()
        monkeypatch.setattr(crud_chat, "complete_setup", complete)
        response = await client.post(path, json={"workspace_id": "guild"})
        assert response.status_code == 422
        with setup_rig.factory() as db:
            assert crud_chat.connections.get(db, draft_id)
        install.side_effect = None
        assert (
            await client.post(path, json={"workspace_id": "guild"})
        ).status_code == 201
        assert install.await_count == 2


@pytest.mark.asyncio
async def test_empty_mattermost_draft_refresh_preserves_credentials_and_finishes(
    setup_rig, monkeypatch
) -> None:
    """A valid new bot can join a team after discovery without a new token."""
    from preloop.services.chat_setup import discover

    teams = []
    unavailable = False
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer synthetic-bot-token"
        if unavailable:
            return httpx.Response(503)
        if request.url.path == "/api/v4/users/me":
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-bot",
                    "is_bot": True,
                    "username": "team-assistant",
                    "first_name": "Team assistant",
                },
            )
        assert request.url.path == "/api/v4/users/synthetic-bot/teams"
        return httpx.Response(200, json=teams)

    monkeypatch.setattr(
        "preloop.services.ai_model_provider._resolve_endpoint_host",
        lambda _: [ipaddress.ip_address("93.184.216.34")],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:

        async def discovery(config, **kwargs):
            return await discover(config, client=upstream, **kwargs)

        monkeypatch.setattr(chat_setup, "discover", discovery)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_rig.app),
            base_url="https://preloop.example.com",
        ) as client:
            response = await client.post(
                "/api/v1/chat/setup",
                json={
                    "provider": "mattermost",
                    "bot_token": "synthetic-bot-token",
                    "base_url": "https://chat.example.com",
                },
            )
            assert response.status_code == 201, response.text
            draft = response.json()
            assert draft["workspaces"] == []
            assert draft["bot_username"] == "team-assistant"
            assert "synthetic-bot-token" not in response.text
            draft_id = UUID(draft["id"])
            with setup_rig.factory() as db:
                stored = crud_chat.drafts.get(db, draft_id)
                assert "synthetic-bot-token" not in stored.state_encrypted
                credentials = json.loads(decrypt_value(stored.state_encrypted))
            path = f"/api/v1/chat/setup/{draft_id}"
            blocked = await client.post(
                path + "/finish", json={"workspace_id": "team-one"}
            )
            assert blocked.status_code == 422
            unavailable = True
            assert (await client.post(path + "/refresh", json={})).status_code == 422
            unavailable = False
            teams.append({"id": "team-one", "display_name": "Engineering"})
            refreshed = await client.post(path + "/refresh", json={})
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["id"] == draft["id"]
            assert refreshed.json()["expires_at"] == draft["expires_at"]
            assert refreshed.json()["workspaces"] == [
                {"id": "team-one", "name": "Engineering"}
            ]
            assert "synthetic-bot-token" not in refreshed.text
            with setup_rig.factory() as db:
                state = json.loads(
                    decrypt_value(crud_chat.drafts.get(db, draft_id).state_encrypted)
                )
                assert state["bot_token"] == credentials["bot_token"]
                assert state["bot_user_id"] == credentials["bot_user_id"]
                assert state["base_url"] == credentials["base_url"]
            finished = await client.post(
                path + "/finish", json={"workspace_id": "team-one"}
            )
            assert finished.status_code == 201, finished.text
            assert finished.json()["id"] == draft["id"]
            with setup_rig.factory() as db:
                connection = crud_chat.connections.get(db, draft_id)
                assert connection.workspace_id == "team-one"
                saved = json.loads(decrypt_value(connection.credentials_encrypted))
                assert saved["bot_token"] == "synthetic-bot-token"
    assert all(method == "GET" for method, _ in calls)
