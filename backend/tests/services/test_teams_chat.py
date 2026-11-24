"""Protects Teams ingress, setup and durable delivery using real CRUD and HTTP.

fails_when: routing, identity checks or job transitions drift.
no_claim: local RSA and fake provider/model transports cannot prove Microsoft
single-tenant claims, real delivery, package acceptance, throttling or rendering.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest

from preloop.api.auth.jwt import create_access_token
from preloop.models import models
from preloop.models.crud import crud_chat, crud_flow_execution
from preloop.services.chat_assistant import ChatBroker
from preloop.services.chat_flows import execution_url
from preloop.services.chat_providers import (
    ChatProviderRetryError,
    TeamsInstallRequiredError,
    TEAMS_INSTALL_ERROR,
)
from preloop.services.chat_worker import process_one, enqueue_approval_notifications
from preloop.utils.encryption import encrypt_value, decrypt_value
from tests.services import test_chat_backend as base
from tests.services import test_chat_flows as flows
from tests.services.test_teams_provider import (
    APP,
    TENANT,
    USER,
    SERVICE,
    CONFIG,
    KEY,
    activity,
    signed_token,
)

fixture_db = base.fixture_db
real_app = base.real_app
chat_flow = flows.chat_flow
dispatch = flows.dispatch


@pytest.fixture
def teams_db(fixture_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Switch the shared database fixture to a single-tenant Teams connection."""
    f = fixture_db
    credentials = asdict(CONFIG)
    credentials.pop("provider")
    credentials.pop("workspace_id")
    f.connection = crud_chat.connections.update(
        f.db,
        db_obj=f.connection,
        obj_in={
            "provider": "teams",
            "workspace_id": TENANT,
            "credentials_encrypted": encrypt_value(json.dumps(credentials)),
        },
    )
    f.identity = crud_chat.identities.update(
        f.db, db_obj=f.identity, obj_in={"external_user_id": USER}
    )
    monkeypatch.setattr(
        "preloop.services.teams_auth.signing_keys", AsyncMock(return_value=[KEY])
    )
    return f


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Capture exact delivered text without live Microsoft calls."""
    sender = AsyncMock(return_value="sent-test")
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", sender)
    return sender


async def ingress(
    app: Any, f: Any, text: str, *, activity_id: str = "activity-1"
) -> httpx.Response:
    """Deliver the documented synthetic Teams fixture to the real API."""
    body = activity()
    body.update(text=text, id=activity_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/api/v1/chat/ingress/{f.connection.id}",
            json=body,
            headers={"Authorization": "Bearer " + signed_token()},
        )


def headers(f: Any) -> dict[str, str]:
    """Authenticate a synthetic local account to management APIs."""
    return {
        "Authorization": "Bearer " + create_access_token(data={"sub": str(f.user.id)})
    }


def row(f: Any) -> models.ChatWork:
    """Read the single received job through the CRUD layer."""
    return crud_chat.work.get_multi(f.db, connection_id=f.connection.id, limit=10)[0]


@pytest.mark.asyncio
async def test_link_and_health(
    teams_db: Any, real_app: Any, delivery: AsyncMock
) -> None:
    """Protects private proofs and four health states; fails on leakage; no_claim: live bot."""
    f = teams_db
    crud_chat.identities.delete(f.db, id=f.identity.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers=headers(f),
    ) as client:
        response = await client.post(
            f"/api/v1/chat/connections/{f.connection.id}/link-code"
        )
        assert response.status_code == 200
        code = response.json()["code"]
        assert (
            response.json()["instruction"]
            == "Open the Preloop app in Teams and send a personal message: link CODE"
        )
        assert (await ingress(real_app, f, "link " + code)).status_code == 200
        assert code not in json.dumps(row(f).payload)
        assert await process_one(f.db, real_app)
        assert delivery.call_args.args[2] == "Your Preloop identity is linked."
        assert delivery.call_args.kwargs["service_url"] == SERVICE
        linked = crud_chat.identity(f.db, f.connection.id, external_user_id=USER)
        assert linked.provider_state == {
            "service_url": SERVICE,
            "conversation_id": "a:synthetic-conversation",
        }
        assert crud_chat.claim_listener(f.db, f.connection.id, "synthetic-lease")
        crud_chat.listener_health(
            f.db, f.connection.id, "synthetic-lease", "worker_ready"
        )
        connections = (await client.get("/api/v1/chat/connections")).json()[
            "connections"
        ]
        state = next(c for c in connections if c["id"] == str(f.connection.id))
        assert all(
            state[key]
            for key in ("configured", "worker_ready", "callback_seen", "linked")
        )
        assert "synthetic-secret" not in json.dumps(state)


@pytest.mark.asyncio
async def test_question_and_dedupe(
    teams_db: Any, real_app: Any, delivery: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects questions and duplicate receipts; fails on repeat; no_claim: live model."""
    f = teams_db
    answer = AsyncMock(return_value="The configured budget is 100 units.")
    monkeypatch.setattr(ChatBroker, "answer", answer)
    assert (await ingress(real_app, f, "What is the budget?")).status_code == 200
    assert (await ingress(real_app, f, "What is the budget?")).status_code == 200
    assert await process_one(f.db, real_app)
    answer.assert_awaited_once_with("What is the budget?")
    assert delivery.call_args.args[2] == "The configured budget is 100 units."
    assert not await process_one(f.db, real_app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb,expected",
    [
        ("note", "Note recorded for delivery to the session."),
        ("message", "Message submitted to the selected session."),
        ("approve", "Decision recorded. Existing approval rules still apply."),
        ("deny", "Decision recorded. Existing approval rules still apply."),
    ],
)
async def test_commands(
    teams_db: Any,
    real_app: Any,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    expected: str,
) -> None:
    """Protects bare-command mapping and exact replies; fails on drift; no_claim: upstream actions."""
    f = teams_db
    first, second = uuid4(), uuid4()
    text = f"{verb} {first}"
    if verb == "message":
        text += f" {second} Sample message"
    if verb == "note":
        text += " Sample note"
    request = AsyncMock(return_value={})
    monkeypatch.setattr(ChatBroker, "request", request)
    monkeypatch.setattr(ChatBroker, "authorize_resource", lambda *args, **kwargs: None)
    assert (await ingress(real_app, f, text)).status_code == 200
    assert row(f).payload["text"] == "/" + text
    assert await process_one(f.db, real_app)
    assert delivery.call_args.args[2] == expected
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_wait_and_result(
    teams_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
) -> None:
    """Protects broker parity through ask/wait/result; fails on route loss; no_claim: hosted execution."""
    f = teams_db
    assert (
        await ingress(real_app, f, "ask docs Where is the policy?")
    ).status_code == 200
    assert await process_one(f.db, real_app)
    parent = next(
        item
        for item in crud_chat.work.get_multi(
            f.db, connection_id=f.connection.id, limit=10
        )
        if item.run_execution_id
    )
    url = execution_url(str(parent.run_execution_id))
    assert delivery.call_args.args[2] == f"Flow started. {url}"
    assert (
        dispatch[0]["trigger_event_data"]["payload"]["chat"]["text"]
        == "Where is the policy?"
    )
    execution = crud_flow_execution.get(f.db, id=parent.run_execution_id)
    crud_flow_execution.update(
        f.db, db_obj=execution, obj_in={"status": "WAITING_FOR_HUMAN"}
    )
    flows.due(f)
    await process_one(f.db, real_app)
    await process_one(f.db, real_app)
    assert delivery.call_args.args[2] == f"Flow is waiting for approval. {url}"
    assert delivery.call_args.kwargs["conversation_id"] == "a:synthetic-conversation"
    crud_flow_execution.update(
        f.db,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "result": {"text": "The policy is in handbook.md."},
            "result_schema_validation": {"valid": True},
        },
    )
    flows.due(f)
    await process_one(f.db, real_app)
    assert delivery.call_args.args[2] == f"The policy is in handbook.md.\n\n{url}"
    assert delivery.call_args.kwargs["service_url"] == SERVICE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected",
    [("retry", "pending"), ("forbidden", "failed"), ("timeout", "uncertain")],
)
async def test_worker_outcomes(
    teams_db: Any, real_app: Any, delivery: AsyncMock, kind: str, expected: str
) -> None:
    """Protects retry/403/uncertain state; fails on unsafe replay; no_claim: live status."""
    f = teams_db
    work = crud_chat.receive(
        f.db,
        connection=f.connection,
        event_id="outcome",
        external_user_id=USER,
        payload={"actor_id": str(f.user.id)},
    )
    crud_chat.work.update(
        f.db,
        db_obj=work,
        obj_in={"user_id": f.user.id, "reply": "Ready reply", "status": "reply_ready"},
    )
    delivery.side_effect = {
        "retry": ChatProviderRetryError(42),
        "forbidden": TeamsInstallRequiredError(),
        "timeout": httpx.ReadTimeout("sensitive provider text"),
    }[kind]
    before = datetime.utcnow()
    await process_one(f.db, real_app)
    f.db.refresh(work)
    assert work.status == expected
    if kind == "retry":
        assert work.next_attempt_at >= before + timedelta(seconds=42)
        crud_chat.work.update(
            f.db,
            db_obj=work,
            obj_in={"attempts": 2, "lease_until": None, "next_attempt_at": before},
        )
        await process_one(f.db, real_app)
        f.db.refresh(work)
        assert work.status == "failed" and work.attempts == 3
    if kind == "forbidden":
        assert work.last_error == TEAMS_INSTALL_ERROR
    assert "sensitive" not in work.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unlink", "disable"])
async def test_revoked_delivery(
    teams_db: Any, real_app: Any, delivery: AsyncMock, action: str
) -> None:
    """Protects revocation before disclosure; fails on delivery; no_claim: live Teams."""
    f = teams_db
    work = crud_chat.receive(
        f.db,
        connection=f.connection,
        event_id="revoked",
        external_user_id=USER,
        payload={"actor_id": str(f.user.id)},
    )
    crud_chat.work.update(
        f.db,
        db_obj=work,
        obj_in={
            "user_id": f.user.id,
            "reply": "Private reply",
            "status": "reply_ready",
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers=headers(f),
    ) as client:
        path = f"/api/v1/chat/connections/{f.connection.id}"
        response = (
            await client.delete(path + "/identity")
            if action == "unlink"
            else await client.patch(path, json={"enabled": False})
        )
        assert response.status_code in {200, 204}
    await process_one(f.db, real_app)
    f.db.refresh(work)
    assert work.status == "cancelled"
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_package_and_encryption(
    teams_db: Any, real_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects draft ownership and encrypted credentials; fails on disclosure; no_claim: package install."""
    from preloop.config import settings

    f = teams_db
    monkeypatch.setattr(settings, "preloop_url", "https://preloop.example.com")
    monkeypatch.setattr(
        "preloop.services.teams_auth.bot_token",
        AsyncMock(return_value="synthetic-access"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers=headers(f),
    ) as client:
        response = await client.post(
            "/api/v1/chat/setup",
            json={
                "provider": "teams",
                "teams_app_id": APP,
                "teams_app_tenant_id": TENANT,
                "bot_token": "synthetic-secret",
                "teams_privacy_url": "https://example.com/privacy",
            },
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        assert "synthetic-secret" not in response.text
        assert draft["workspaces"] == [{"id": TENANT, "name": TENANT}]
        package = await client.get(f"/api/v1/chat/setup/{draft['id']}/teams-package")
        assert (
            package.status_code == 200
            and package.headers["cache-control"] == "no-store"
        )
        response_schema = real_app.openapi()["paths"][
            "/api/v1/chat/setup/{draft_id}/teams-package"
        ]["get"]["responses"]["200"]
        assert response_schema["content"] == {
            "application/zip": {"schema": {"type": "string", "format": "binary"}}
        }
        with ZipFile(io.BytesIO(package.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["developer"]["privacyUrl"] == "https://example.com/privacy"
            assert manifest["id"] == draft["id"]
        wrong = await client.post(
            f"/api/v1/chat/setup/{draft['id']}/finish",
            json={"workspace_id": "another-tenant"},
        )
        assert wrong.status_code == 422
        finished = await client.post(
            f"/api/v1/chat/setup/{draft['id']}/finish", json={"workspace_id": TENANT}
        )
        assert finished.status_code == 201, finished.text
        saved = crud_chat.connections.get(f.db, draft["id"])
        assert "synthetic-secret" not in saved.credentials_encrypted
        values = json.loads(decrypt_value(saved.credentials_encrypted))
        assert (
            values["bot_token"] == "synthetic-secret"
            and values["teams_app_tenant_id"] == TENANT
        )
        assert "teams_privacy_url" not in values
        assert (
            await client.get(f"/api/v1/chat/setup/{draft['id']}/teams-package")
        ).status_code == 404


@pytest.mark.asyncio
async def test_proactive_approval(
    teams_db: Any, real_app: Any, delivery: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects proactive text and saved routing; fails on drift; no_claim: installation."""
    from sqlalchemy.orm import sessionmaker

    f = teams_db
    crud_chat.remember_delivery_state(
        f.db,
        f.connection.id,
        USER,
        {"service_url": SERVICE, "conversation_id": "a:synthetic-conversation"},
    )
    workflow = models.ApprovalWorkflow(
        account_id=f.account.id,
        name="Synthetic approval",
        approval_type="manual",
        approver_user_ids=[str(f.user.id)],
    )
    f.db.add(workflow)
    f.db.flush()
    tool = models.ToolConfiguration(
        account_id=f.account.id,
        tool_name="sample_tool",
        approval_workflow_id=workflow.id,
    )
    f.db.add(tool)
    f.db.flush()
    request = models.ApprovalRequest(
        account_id=f.account.id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="sample_tool",
        tool_args={},
        status="pending",
    )
    f.db.add(request)
    f.db.commit()
    monkeypatch.setattr(
        "preloop.services.chat_worker.get_session_factory",
        lambda: sessionmaker(f.engine),
    )
    assert enqueue_approval_notifications(f.account.id, request.id, [f.user.id]) == 1
    assert await process_one(f.db, real_app)
    assert (
        delivery.call_args.args[2]
        == f"Preloop approval {request.id} needs your review. Use approve {request.id} or deny {request.id}. Review full details in the dashboard before deciding."
    )
    assert delivery.call_args.kwargs["service_url"] == SERVICE
    assert delivery.call_args.kwargs["conversation_id"] == "a:synthetic-conversation"


@pytest.mark.asyncio
async def test_worker_authentication_health(
    teams_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects fixed health failures; fails on false readiness; no_claim: live auth."""
    from preloop.services.chat_worker import probe_teams_credentials
    from preloop.services.chat_providers import ChatProviderError

    f = teams_db
    assert crud_chat.claim_listener(f.db, f.connection.id, "synthetic-lease")
    token = AsyncMock(side_effect=ChatProviderError("sensitive-provider-body"))
    monkeypatch.setattr("preloop.services.teams_auth.bot_token", token)
    assert not await probe_teams_credentials(f.db, f.connection, "synthetic-lease")
    state = crud_chat.listeners.get_multi(f.db, connection_id=f.connection.id, limit=1)[
        0
    ]
    assert state.status == "failed"
    assert state.last_error == "Teams authentication failed; check credentials"
    token.side_effect = None
    token.return_value = "synthetic-access"
    assert await probe_teams_credentials(f.db, f.connection, "synthetic-lease")


@pytest.mark.asyncio
async def test_package_actor_permission_and_expiry(
    teams_db: Any, real_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protects package authorization; fails on cross-actor access; no_claim: admin upload."""
    from preloop.config import settings
    from preloop.api.endpoints import chat_setup

    f = teams_db
    monkeypatch.setattr(settings, "preloop_url", "https://preloop.example.com")
    draft = crud_chat.drafts.create(
        f.db,
        obj_in={
            "account_id": f.account.id,
            "user_id": f.user.id,
            "provider": "teams",
            "state_encrypted": encrypt_value(json.dumps({"bot_user_id": APP})),
            "expires_at": datetime.utcnow() + timedelta(minutes=10),
        },
    )
    path = f"/api/v1/chat/setup/{draft.id}/teams-package"
    other = models.User(
        account_id=f.account.id,
        username="other-" + uuid4().hex,
        email="other@example.com",
        hashed_password="unused",
        is_active=True,
    )
    f.db.add(other)
    f.db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        other_headers = {
            "Authorization": "Bearer " + create_access_token({"sub": str(other.id)})
        }
        assert (await client.get(path, headers=other_headers)).status_code == 404
        monkeypatch.setattr(chat_setup, "has_permission", lambda *args: False)
        assert (await client.get(path, headers=headers(f))).status_code == 403
        monkeypatch.setattr(chat_setup, "has_permission", lambda *args: True)
        crud_chat.drafts.update(
            f.db,
            db_obj=draft,
            obj_in={"expires_at": datetime.utcnow() - timedelta(seconds=1)},
        )
        assert (await client.get(path, headers=headers(f))).status_code == 404


@pytest.mark.asyncio
async def test_help(
    teams_db: Any, real_app: Any, chat_flow: models.Flow, delivery: AsyncMock
) -> None:
    """Protects help parity; fails on verb drift; no_claim: real Teams UI."""
    assert (await ingress(real_app, teams_db, "help")).status_code == 200
    assert await process_one(teams_db.db, real_app)
    assert delivery.call_args.args[2] == "Chat flows: docs. Use ask ALIAS TEXT."


@pytest.mark.asyncio
async def test_ingress_constant_failure(teams_db: Any, real_app: Any) -> None:
    """Protects fixed ingress errors; fails on queueing or leakage; no_claim: live JWT."""
    f = teams_db
    body = activity()
    body["channelData"]["tenant"]["id"] = "other-tenant"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/chat/ingress/{f.connection.id}",
            json=body,
            headers={"Authorization": "Bearer " + signed_token()},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid provider request"}
    assert crud_chat.work.get_multi(f.db, connection_id=f.connection.id) == []
