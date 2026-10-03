"""Chat persistence and real authorized API roundtrips on an isolated database.

Set CHAT_TEST_DATABASE_URL to an Alembic-migrated synthetic PostgreSQL database.
Provider/model network I/O is mocked; authorization, CRUD, queue and HTTP routes
are real. These fixtures never send external messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import create_access_token
from preloop.models import models
from preloop.models.crud import crud_chat
from preloop.models.crud.chat import ChatLeaseLostError
from preloop.models.db.session import get_db_session
from preloop.services.chat_assistant import ChatBroker
from preloop.services.chat_worker import process_one
from preloop.utils.encryption import encrypt_value


@pytest.fixture
def fixture_db():
    url = os.getenv("CHAT_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "CHAT_TEST_DATABASE_URL requires an isolated migrated PostgreSQL database"
        )
    engine = create_engine(url)
    with Session(engine) as db:
        account = models.Account(organization_name="Synthetic chat tests")
        db.add(account)
        db.flush()
        user = models.User(
            account_id=account.id,
            username=f"chat-{uuid4()}",
            email="member@example.com",
            hashed_password="unused",
            is_active=True,
        )
        db.add(user)
        db.flush()
        role = models.Role(name="owner", is_system_role=True, account_id=account.id)
        db.add(role)
        db.flush()
        db.add(models.UserRole(user_id=user.id, role_id=role.id))
        connection = models.ChatConnection(
            account_id=account.id,
            provider="slack",
            workspace_id="team-test",
            name="Synthetic chat",
            credentials_encrypted=encrypt_value(
                json.dumps(
                    {
                        "verification_secret": "fixture-secret",
                        "bot_token": "fixture-token",
                        "bot_user_id": "bot-test",
                        "base_url": "",
                    }
                )
            ),
        )
        db.add(connection)
        db.flush()
        identity = models.ChatIdentity(
            account_id=account.id,
            connection_id=connection.id,
            user_id=user.id,
            external_user_id="external-test",
        )
        db.add(identity)
        db.commit()
        aid = account.id
        yield SimpleNamespace(
            db=db,
            engine=engine,
            account=account,
            user=user,
            connection=connection,
            identity=identity,
            role=role,
        )
        db.rollback()
        account = db.get(models.Account, aid)
        if account:
            db.delete(account)
            db.commit()
    engine.dispose()


@pytest.fixture
def real_app(fixture_db, monkeypatch):
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "chat")
    from preloop.api.app import create_app

    app = create_app()

    def session_override():
        with Session(fixture_db.engine) as db:
            yield db

    app.dependency_overrides[get_db_session] = session_override
    # flows uses the legacy get_db alias, which may be a separate function.
    from preloop.api.endpoints.flows import get_db

    app.dependency_overrides[get_db] = session_override
    return app


def receive(f, event_id=None, text="List agents", **payload):
    return crud_chat.receive(
        f.db,
        connection=f.connection,
        event_id=event_id or str(uuid4()),
        external_user_id="external-test",
        payload={"text": text, "private": True, **payload},
    )


def proof(f, code="a-high-entropy-synthetic-proof", **values):
    return crud_chat.codes.create(
        f.db,
        obj_in={
            "account_id": f.account.id,
            "connection_id": f.connection.id,
            "user_id": f.user.id,
            "digest": hashlib.sha256(code.encode()).hexdigest(),
            "expires_at": datetime.utcnow() + timedelta(minutes=10),
            **values,
        },
    )


@pytest.mark.parametrize("violation", ["not_null", "foreign_key"])
def test_receipt_preserves_unrelated_integrity_errors(fixture_db, violation):
    f = fixture_db
    connection = f.connection
    external_user_id = "external-test"
    if violation == "foreign_key":
        connection = SimpleNamespace(id=uuid4(), account_id=f.account.id)
    else:
        external_user_id = None

    with pytest.raises(IntegrityError) as caught:
        crud_chat.receive(
            f.db,
            connection=connection,
            event_id=str(uuid4()),
            external_user_id=external_user_id,
            payload={"text": "List agents"},
        )
    assert caught.value.orig.sqlstate == (
        "23503" if violation == "foreign_key" else "23502"
    )
    # The rollback also leaves the session usable for a subsequent valid receipt.
    assert receive(f).status == "pending"


def test_duplicate_receipt_and_claim_recovery(fixture_db):
    f = fixture_db
    first = receive(f, "same-event")
    second = receive(f, "same-event", text="changed duplicate")
    assert first.id == second.id and second.payload["text"] == "List agents"
    claimed = crud_chat.claim(f.db)
    with Session(f.engine) as other:
        assert crud_chat.claim(other) is None
    original_token = claimed._chat_lease_token
    crud_chat.work.update(
        f.db,
        db_obj=claimed,
        obj_in={"lease_until": datetime.utcnow() - timedelta(seconds=1)},
    )
    with Session(f.engine) as other:
        reclaimed = crud_chat.claim(other)
        assert reclaimed.id == first.id and reclaimed.lease_token != original_token
    with pytest.raises(ChatLeaseLostError):
        crud_chat.transition(f.db, claimed, "delivered")


def test_reply_ready_cannot_be_stolen_during_live_lease(fixture_db):
    f = fixture_db
    row = receive(f)
    claimed = crud_chat.claim(f.db)
    crud_chat.transition(f.db, claimed, "reply_ready", reply="private result")
    with Session(f.engine) as other:
        assert crud_chat.claim(other) is None
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={"lease_until": datetime.utcnow() - timedelta(seconds=1)},
    )
    with Session(f.engine) as other:
        recovered = crud_chat.claim(other)
        assert recovered.reply == "private result"


def test_crashed_write_is_uncertain_never_replayed(fixture_db):
    f = fixture_db
    row = receive(f)
    claimed = crud_chat.claim(f.db)
    crud_chat.transition(f.db, claimed, "acting")
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={"lease_until": datetime.utcnow() - timedelta(seconds=1)},
    )
    assert crud_chat.claim(f.db) is None
    f.db.refresh(row)
    assert row.status == "uncertain"


def test_single_use_proof_and_relink_denial(fixture_db):
    f = fixture_db
    proof(f)
    assert (
        crud_chat.consume_code(
            f.db, f.connection, "external-test", "a-high-entropy-synthetic-proof"
        ).user_id
        == f.user.id
    )
    with pytest.raises(ValueError, match="already used"):
        crud_chat.consume_code(
            f.db, f.connection, "external-test", "a-high-entropy-synthetic-proof"
        )
    f.db.rollback()
    proof(f, "new-proof")
    with pytest.raises(ValueError, match="already linked"):
        crud_chat.consume_code(
            f.db, f.connection, "different-external-user", "new-proof"
        )


def test_proof_bound_to_tenant_and_expiry(fixture_db):
    f = fixture_db
    proof(f, expires_at=datetime.utcnow() - timedelta(seconds=1))
    with pytest.raises(ValueError, match="expired"):
        crud_chat.consume_code(
            f.db, f.connection, "external-test", "a-high-entropy-synthetic-proof"
        )
    f.db.rollback()
    other = models.ChatConnection(
        account_id=f.account.id,
        provider="slack",
        workspace_id="other",
        name="Other",
        credentials_encrypted="unused",
    )
    f.db.add(other)
    f.db.commit()
    proof(f, "another-proof")
    with pytest.raises(ValueError, match="expired"):
        crud_chat.consume_code(f.db, other, "external-test", "another-proof")


@pytest.mark.asyncio
async def test_constructed_chat_worker_app_calls_real_scoped_api(fixture_db, real_app):
    f = fixture_db
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    models_result = await broker.read("models")
    assert models_result == {"items": [], "limit": 20}
    agents_result = await broker.read("agents")
    assert agents_result == {"items": [], "limit": 20}
    assert await broker.read("flows") == {"items": [], "limit": 20}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/operator-notes",
            headers={
                "Authorization": "Bearer "
                + create_access_token({"sub": str(f.user.id)})
            },
            json={"runtime_session_id": str(uuid4()), "text": "Synthetic note"},
        )
    assert response.status_code == 404  # real target guard, route is registered


@pytest.mark.asyncio
async def test_broker_rechecks_role_and_revoked_actor(fixture_db, real_app):
    f = fixture_db
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    await broker.read("models")
    f.db.query(models.UserRole).filter_by(user_id=f.user.id).delete()
    f.db.commit()
    with pytest.raises(HTTPException) as exc:
        await broker.read("models")
    assert exc.value.status_code == 403
    f.user.is_active = False
    f.db.commit()
    assert crud_chat.principal(f.db, f.connection, "external-test") is None


@pytest.mark.asyncio
async def test_broker_blocks_unregistered_routes(fixture_db, real_app):
    f = fixture_db
    with pytest.raises(HTTPException):
        await ChatBroker(f.db, f.connection, "external-test", real_app).request(
            "POST", "/api/v1/account/delete", "view_ai_models", {}
        )


@pytest.mark.asyncio
async def test_tenant_connection_denial_and_member_link_status(fixture_db, real_app):
    f = fixture_db
    user_token = create_access_token({"sub": str(f.user.id)})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + user_token},
    ) as client:
        listing = await client.get("/api/v1/chat/connections")
        assert listing.status_code == 200
        assert listing.json()["connections"][0]["linked"] is True
        assert "credentials" not in json.dumps(listing.json())
        denied = await client.post(f"/api/v1/chat/connections/{uuid4()}/link-code")
        assert denied.status_code == 404
        f.db.query(models.UserRole).filter_by(user_id=f.user.id).delete()
        f.db.commit()
        listing = await client.get("/api/v1/chat/connections")
        assert listing.status_code == 200 and listing.json()["can_manage"] is False
        link = await client.post(
            f"/api/v1/chat/connections/{f.connection.id}/link-code"
        )
        assert link.status_code == 200
        denied = await client.patch(
            f"/api/v1/chat/connections/{f.connection.id}", json={"enabled": False}
        )
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_signed_ingress_dedupe_and_proof_is_not_persisted(fixture_db, real_app):
    f = fixture_db
    raw = json.dumps(
        {
            "team_id": "team-test",
            "event_id": "signed-link",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "external-test",
                "channel": "dm-test",
                "text": "/link TOP_SECRET_SYNTHETIC_PROOF",
            },
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"fixture-secret", b"v0:" + timestamp.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    headers = {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": "v0=" + signature,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                f"/api/v1/chat/ingress/{f.connection.id}", content=raw, headers=headers
            )
            assert response.status_code == 200
        forged = await client.post(
            f"/api/v1/chat/ingress/{f.connection.id}",
            content=raw + b" ",
            headers=headers,
        )
        assert forged.status_code == 401
    rows = f.db.query(models.ChatWork).filter_by(connection_id=f.connection.id).all()
    assert len(rows) == 1 and "TOP_SECRET" not in json.dumps(rows[0].payload)
    assert (
        rows[0].payload["link_digest"]
        == hashlib.sha256(b"TOP_SECRET_SYNTHETIC_PROOF").hexdigest()
    )


@pytest.mark.asyncio
async def test_deleted_actor_cannot_receive_persisted_reply(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    row = receive(f, actor_id=str(f.user.id))
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={
            "user_id": f.user.id,
            "reply": "Sensitive private result",
            "status": "reply_ready",
        },
    )
    f.db.delete(f.user)
    f.db.commit()
    send = AsyncMock()
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", send)
    assert await process_one(f.db, real_app)
    f.db.refresh(row)
    assert row.user_id is None and row.status == "cancelled"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_identity_blocks_private_delivery(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    row = receive(f, actor_id=str(f.user.id))
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={
            "user_id": f.user.id,
            "reply": "Sensitive private result",
            "status": "reply_ready",
        },
    )
    f.db.delete(f.identity)
    f.db.commit()
    send = AsyncMock()
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", send)
    assert await process_one(f.db, real_app)
    f.db.refresh(row)
    assert row.status == "cancelled"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_queue_private_delivery_and_timeout_visibility(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    row = receive(f, actor_id=str(f.user.id))
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={"user_id": f.user.id, "reply": "Safe result", "status": "reply_ready"},
    )
    send = AsyncMock(return_value="provider-message")
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", send)
    assert await process_one(f.db, real_app)
    f.db.refresh(row)
    assert row.status == "delivered" and row.provider_message_id == "provider-message"
    assert send.call_args.args[1] == "external-test"
    assert not await process_one(f.db, real_app)
    second = receive(f, actor_id=str(f.user.id))
    crud_chat.work.update(
        f.db,
        db_obj=second,
        obj_in={"user_id": f.user.id, "reply": "Safe result", "status": "reply_ready"},
    )
    send.side_effect = httpx.ReadTimeout("synthetic timeout")
    assert await process_one(f.db, real_app)
    f.db.refresh(second)
    assert second.status == "uncertain"
    assert not await process_one(f.db, real_app)


@pytest.mark.asyncio
async def test_scoped_read_proof_revalidates_resource_denial(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    await broker.read("models")
    await broker.validate_reads()
    f.db.query(models.UserRole).filter_by(user_id=f.user.id).delete()
    f.db.commit()
    with pytest.raises(HTTPException):
        await broker.validate_reads()


@pytest.mark.asyncio
async def test_spend_projection_is_stable_across_actual_reads(fixture_db, real_app):
    f = fixture_db
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    data = await broker.read("spend")
    assert data["estimated_cost"] == 0 and "period_end" in data
    await broker.validate_reads()


@pytest.mark.asyncio
async def test_deterministic_commands_preserve_actor_and_correlation(fixture_db):
    f = fixture_db
    broker = ChatBroker(f.db, f.connection, "external-test", None)
    broker.request = AsyncMock(return_value={})
    session_id, agent_id, request_id, correlation = map(
        str, [uuid4(), uuid4(), uuid4(), uuid4()]
    )
    await broker.command(f"/note {session_id} Synthetic note", correlation)
    assert broker.request.call_args.args == (
        "POST",
        "/api/v1/operator-notes",
        "control_managed_agent",
        {
            "runtime_session_id": session_id,
            "text": "Synthetic note",
            "source": "chat",
            "correlation_id": correlation,
        },
    )
    await broker.command(
        f"/message {agent_id} {session_id} Synthetic message", correlation
    )
    assert broker.request.call_args.args[3]["metadata"]["correlation_id"] == correlation
    await broker.command(f"/approve {request_id}", correlation)
    assert (
        broker.request.call_args.args[1]
        == f"/api/v1/approval-requests/{request_id}/decide"
    )
    assert broker.request.call_args.args[3]["approved"] is True
    await broker.command(f"/deny {request_id}", correlation)
    assert broker.request.call_args.args[3]["approved"] is False


def test_gateway_exact_default_uuid_retains_authorization():
    from preloop.services.openai_gateway import OpenAIGatewayService
    from preloop.services.model_gateway_auth import ModelGatewayAuthContext
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    first = SimpleNamespace(
        id=str(uuid4()),
        name="Earlier alias",
        provider_name="openai",
        model_identifier="example-model",
        api_endpoint=None,
        is_default=False,
        model_parameters=None,
        meta_data={"gateway": {"enabled": True, "model_alias": "openai/example-model"}},
    )
    default = SimpleNamespace(
        **{
            **vars(first),
            "id": str(uuid4()),
            "name": "Account default",
            "is_default": True,
        }
    )
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(id="user", account_id="account")
        ),
    )
    service._get_account_models = lambda: [first, default]
    service._authorized_model_ids = lambda rows: {first.id, default.id}
    assert (
        service._resolve_requested_model_row(default.id, provider="openai").id
        == default.id
    )
    service._authorized_model_ids = lambda rows: {first.id}
    with pytest.raises(ModelGatewayAPIError):
        service._resolve_requested_model_row(default.id, provider="openai")


@pytest.mark.asyncio
async def test_actual_default_gateway_roundtrip_and_tool_projection(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    model = models.AIModel(
        account_id=f.account.id,
        name="Synthetic default",
        provider_name="openai",
        model_identifier="example-model",
        is_default=True,
        meta_data={"gateway": {"enabled": True, "model_alias": "openai/example-model"}},
    )
    f.db.add(model)
    f.db.commit()
    from preloop.services.openai_gateway import OpenAIGatewayService

    calls = []

    def completion(service, payload):
        assert str(service.auth_context.user.id) == str(f.user.id)
        assert payload["model"] == str(model.id)
        assert "fixture-token" not in json.dumps(payload)
        assert all(
            item["function"]["name"]
            in {"agents", "sessions", "flows", "models", "spend"}
            for item in payload["tools"]
        )
        calls.append(json.loads(json.dumps(payload)))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-models",
                                    "type": "function",
                                    "function": {"name": "models", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "One visible synthetic model.",
                    }
                }
            ]
        }

    monkeypatch.setattr(OpenAIGatewayService, "create_chat_completion", completion)
    answer = await ChatBroker(f.db, f.connection, "external-test", real_app).answer(
        "Which models are visible?"
    )
    assert answer == "One visible synthetic model."
    assert len(calls) == 2
    result = json.loads(calls[1]["messages"][-1]["content"])
    assert result["items"][0]["id"] == str(model.id)
    assert "api_key" not in result["items"][0] and "meta_data" not in result["items"][0]


@pytest.mark.asyncio
async def test_session_resource_denial_and_spend_fail_closed(fixture_db, real_app):
    f = fixture_db
    session = models.RuntimeSession(
        account_id=f.account.id,
        session_source_type="synthetic",
        session_source_id=str(uuid4()),
        started_at=datetime.utcnow(),
    )
    f.db.add(session)
    f.db.commit()
    from preloop.plugins.account_hooks import register_authorizer, Decision

    def deny(ctx, action, resource):
        if resource is not None and getattr(resource, "id", None) == session.id:
            return Decision("deny")
        return Decision("allow")

    register_authorizer(deny)
    try:
        broker = ChatBroker(f.db, f.connection, "external-test", real_app)
        assert (await broker.read("sessions"))["items"] == []
        with pytest.raises(HTTPException) as exc:
            await broker.command(f"/note {session.id} Synthetic note", str(uuid4()))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException):
            await broker.read("spend")
    finally:
        register_authorizer(None)


@pytest.mark.asyncio
async def test_approval_notifications_dedupe_and_current_eligibility(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    workflow = models.ApprovalWorkflow(
        account_id=f.account.id,
        name="Synthetic approval",
        approval_type="manual",
        approver_user_ids=[str(f.user.id)],
        approvals_required=2,
    )
    f.db.add(workflow)
    f.db.flush()
    tool = models.ToolConfiguration(
        account_id=f.account.id,
        tool_name="synthetic_tool",
        approval_workflow_id=workflow.id,
    )
    f.db.add(tool)
    f.db.flush()
    request = models.ApprovalRequest(
        account_id=f.account.id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="synthetic_tool",
        tool_args={},
        status="pending",
    )
    f.db.add(request)
    f.db.commit()
    from sqlalchemy.orm import sessionmaker
    from preloop.services.chat_worker import enqueue_approval_notifications

    monkeypatch.setattr(
        "preloop.services.chat_worker.get_session_factory",
        lambda: sessionmaker(f.engine),
    )
    assert enqueue_approval_notifications(f.account.id, request.id, [f.user.id]) == 1
    assert enqueue_approval_notifications(f.account.id, request.id, [f.user.id]) == 1
    rows = f.db.query(models.ChatWork).filter_by(connection_id=f.connection.id).all()
    assert len(rows) == 1
    assert crud_chat.eligible_approver(f.db, f.user, request.id)
    workflow.approver_user_ids = [str(uuid4())]
    f.db.commit()
    assert not crud_chat.eligible_approver(f.db, f.user, request.id)
    with pytest.raises(HTTPException) as exc:
        await ChatBroker(f.db, f.connection, "external-test", real_app).command(
            f"/approve {request.id}", str(uuid4())
        )
    assert exc.value.status_code == 403
    send = AsyncMock()
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", send)
    await process_one(f.db, real_app)
    f.db.refresh(rows[0])
    assert rows[0].status == "cancelled"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_approval_vote_preserves_quorum_and_replay(fixture_db, real_app):
    f = fixture_db
    workflow = models.ApprovalWorkflow(
        account_id=f.account.id,
        name="Synthetic quorum",
        approval_type="manual",
        approver_user_ids=[str(f.user.id)],
        approvals_required=2,
    )
    f.db.add(workflow)
    f.db.flush()
    tool = models.ToolConfiguration(
        account_id=f.account.id,
        tool_name="synthetic_vote",
        approval_workflow_id=workflow.id,
    )
    f.db.add(tool)
    f.db.flush()
    request = models.ApprovalRequest(
        account_id=f.account.id,
        tool_configuration_id=tool.id,
        approval_workflow_id=workflow.id,
        tool_name="synthetic_vote",
        tool_args={},
        status="pending",
    )
    f.db.add(request)
    f.db.commit()
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    for _ in range(2):
        await broker.command(f"/approve {request.id}", str(uuid4()))
    f.db.expire_all()
    f.db.refresh(request)
    assert request.status == "pending"
    assert len(request.responses) == 1 and request.responses[0]["user_id"] == str(
        f.user.id
    )


@pytest.mark.asyncio
async def test_plain_slack_link_verb_normalizes_without_exposing_code(
    fixture_db, real_app
):
    f = fixture_db
    raw = json.dumps(
        {
            "team_id": "team-test",
            "event_id": "plain-slack-link",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "external-test",
                "channel": "dm-test",
                "text": "link SYNTHETIC_CODE",
            },
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"fixture-secret", b"v0:" + timestamp.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/chat/ingress/{f.connection.id}",
            content=raw,
            headers={
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=" + signature,
            },
        )
    assert response.status_code == 200
    row = f.db.query(models.ChatWork).filter_by(connection_id=f.connection.id).one()
    assert (
        row.payload["text"] == "/link"
        and row.payload["link_digest"] == hashlib.sha256(b"SYNTHETIC_CODE").hexdigest()
    )


def test_simultaneous_database_claims_have_one_owner(fixture_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    f = fixture_db
    row = receive(f)
    barrier = Barrier(2)

    def claim():
        with Session(f.engine) as db:
            barrier.wait(timeout=5)
            work = crud_chat.claim(db)
            return work.id if work else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim), pool.submit(claim)]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(row.id) == 1 and results.count(None) == 1


@pytest.mark.asyncio
async def test_actual_note_carries_human_chat_provenance(fixture_db, real_app):
    f = fixture_db
    session = models.RuntimeSession(
        account_id=f.account.id,
        session_source_type="synthetic",
        session_source_id=str(uuid4()),
        started_at=datetime.utcnow(),
    )
    f.db.add(session)
    f.db.commit()
    correlation = str(uuid4())
    await ChatBroker(f.db, f.connection, "external-test", real_app).command(
        f"/note {session.id} Synthetic operator note", correlation
    )
    note = (
        f.db.query(models.AgentControlCommand)
        .filter_by(runtime_session_id=session.id)
        .one()
    )
    assert note.created_by_user_id == f.user.id and note.source == "chat"
    audit = (
        f.db.query(models.AuditLog)
        .filter_by(user_id=f.user.id, resource_type="operator_note")
        .one()
    )
    assert (
        audit.details["source"] == "chat"
        and audit.details["correlation_id"] == correlation
    )


@pytest.mark.asyncio
async def test_queued_request_is_not_reattributed_after_identity_relink(
    fixture_db, real_app, monkeypatch
):
    f = fixture_db
    row = receive(f, actor_id=str(f.user.id))
    other = models.User(
        account_id=f.account.id,
        username=f"chat-other-{uuid4()}",
        email="other@example.com",
        hashed_password="unused",
        is_active=True,
    )
    f.db.add(other)
    f.db.flush()
    f.identity.user_id = other.id
    f.db.commit()
    send = AsyncMock()
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", send)
    await process_one(f.db, real_app)
    f.db.refresh(row)
    assert row.status == "cancelled"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_and_session_projection_keeps_names_without_private_fields(
    fixture_db, real_app
):
    f = fixture_db
    session = models.RuntimeSession(
        account_id=f.account.id,
        session_source_type="synthetic",
        session_source_id=str(uuid4()),
        runtime_principal_name="Synthetic employee",
        title="Synthetic task",
        started_at=datetime.utcnow(),
    )
    f.db.add(session)
    f.db.flush()
    agent = models.ManagedAgent(
        account_id=f.account.id,
        display_name="Synthetic employee",
        agent_kind="synthetic",
        session_source_type="synthetic",
        session_source_id=str(uuid4()),
        runtime_session_id=session.id,
        enrolled_via="synthetic",
        last_seen_at=datetime.utcnow(),
        enrollment_hostname="private.example.com",
        lifecycle_updated_at=datetime.utcnow(),
    )
    f.db.add(agent)
    f.db.commit()
    broker = ChatBroker(f.db, f.connection, "external-test", real_app)
    agents = (await broker.read("agents"))["items"]
    assert agents[0]["display_name"] == "Synthetic employee"
    assert agents[0]["runtime_session_id"] == str(session.id)
    assert "enrollment_hostname" not in agents[0] and "owner_email" not in agents[0]
    sessions = (await broker.read("sessions"))["items"]
    assert (
        sessions[0]["title"] == "Synthetic task"
        and sessions[0]["runtime_principal_name"] == "Synthetic employee"
    )
    assert "activity_status" in sessions[0] and "is_active_now" in sessions[0]
    assert "session_reference" not in sessions[0] and "summary" not in sessions[0]


@pytest.mark.asyncio
async def test_real_foreign_tenant_connection_is_hidden(fixture_db, real_app):
    f = fixture_db
    account = models.Account(organization_name="Foreign synthetic tenant")
    f.db.add(account)
    f.db.flush()
    foreign = models.ChatConnection(
        account_id=account.id,
        provider="slack",
        workspace_id="foreign-team",
        name="Foreign connection",
        credentials_encrypted="unused",
    )
    f.db.add(foreign)
    f.db.commit()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=real_app),
            base_url="http://test",
            headers={
                "Authorization": "Bearer "
                + create_access_token({"sub": str(f.user.id)})
            },
        ) as client:
            result = await client.get("/api/v1/chat/connections")
            assert str(foreign.id) not in {
                item["id"] for item in result.json()["connections"]
            }
            result = await client.post(
                f"/api/v1/chat/connections/{foreign.id}/link-code"
            )
            assert result.status_code == 404
    finally:
        f.db.delete(account)
        f.db.commit()
