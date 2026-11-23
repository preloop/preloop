"""Chat runs use real CRUD, leases and authenticated HTTP; upstreams are fakes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from preloop.models import models, schemas
from preloop.models.crud import crud_chat, crud_flow_execution
from preloop.services.chat_flows import (
    bounded_reply,
    execution_url,
    final_assistant_text,
)
from preloop.services.chat_worker import process_one
from tests.services import test_chat_backend as chat_fixtures

fixture_db = chat_fixtures.fixture_db
real_app = chat_fixtures.real_app
receive = chat_fixtures.receive


@pytest.fixture
def chat_flow(fixture_db: Any) -> models.Flow:
    """An opted-in synthetic flow with no upstream model credentials."""
    flow = models.Flow(
        account_id=fixture_db.account.id,
        name="Document answers",
        chat_alias="docs",
        prompt_template="{{trigger_event.payload.chat.text}}",
        agent_type="codex",
        agent_config={},
        is_enabled=True,
    )
    fixture_db.db.add(flow)
    fixture_db.db.commit()
    return flow


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Record exact messages without calling a provider."""
    sender = AsyncMock(return_value="sent-test")
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", sender)
    return sender


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace only execution dispatch; API identity and database writes are real."""
    calls = []

    async def trigger(service: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({**kwargs, "actor_id": kwargs["authorization_context"].user.id})
        execution = crud_flow_execution.create(
            service.db,
            obj_in=schemas.FlowExecutionCreate(
                flow_id=kwargs["flow_id"],
                status="RUNNING",
                trigger_event_details=kwargs["trigger_event_data"],
            ),
        )
        service.db.commit()
        return {"id": str(execution.id), "status": execution.status}

    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow", trigger
    )
    return calls


def due(f: Any) -> models.ChatWork:
    """Make this account's durable watcher eligible immediately."""
    rows = crud_chat.work.get_multi(f.db, account_id=str(f.account.id), limit=100)
    row = next(row for row in rows if row.payload.get("flow_watch"))
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={
            "next_attempt_at": datetime.utcnow() - timedelta(seconds=1),
            "lease_until": None,
        },
    )
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,text",
    [
        ("SUCCEEDED", "The policy is in handbook.md."),
        ("FAILED", "Flow failed."),
        ("STOPPED", "Flow stopped."),
        ("TIMEOUT", "Flow timeout."),
        ("TIMED_OUT", "Flow timed_out."),
        ("ABORTED", "Flow aborted."),
        ("CANCELED", "Flow canceled."),
        ("CANCELLED", "Flow cancelled."),
    ],
)
async def test_run_payload_and_exact_completion(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: list[dict[str, Any]],
    status: str,
    text: str,
) -> None:
    """The actor, question and same Slack thread survive asynchronous completion."""
    f = fixture_db
    parent = receive(
        f,
        text="/ask docs Where is the policy?",
        channel_id="channel-test",
        thread_id="123.45",
    )
    assert await process_one(f.db, real_app)
    f.db.refresh(parent)
    assert len(dispatch) == 1
    call = dispatch[0]
    assert call["test_mode"] is False
    assert call["actor_id"] == f.user.id
    event = call["trigger_event_data"]
    assert event["payload"] == {
        "chat": {
            "text": "Where is the policy?",
            "sender_display_name": f.user.username,
            "connection_name": "Synthetic chat",
        }
    }
    assert event["_chat_origin"]["user_id"] == str(f.user.id)
    url = execution_url(str(parent.run_execution_id))
    assert delivery.call_args.args[2] == f"Flow started. {url}"
    assert delivery.call_args.kwargs["channel_id"] == "channel-test"
    execution = crud_flow_execution.get(f.db, id=parent.run_execution_id)
    crud_flow_execution.update(
        f.db,
        db_obj=execution,
        obj_in={
            "status": status,
            "result": {"text": text},
            "result_schema_validation": {"valid": True},
        },
    )
    due(f)
    assert await process_one(f.db, real_app)
    assert delivery.call_args.args[2] == f"{text}\n\n{url}"
    assert delivery.call_args.kwargs["thread_id"] == "123.45"
    receive(f, text="/ask docs Another question")
    assert await process_one(f.db, real_app)
    assert len(dispatch) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["WAITING_FOR_HUMAN", "RUNNING"])
async def test_approval_once_and_completion(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting produces a single notice and keeps the completion observer alive."""
    f = fixture_db
    if status == "RUNNING":
        monkeypatch.setattr(
            "preloop.services.chat_flows.crud_approval_request.get_multi_by_execution",
            lambda *args, **kwargs: [object()],
        )
    parent = receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    f.db.refresh(parent)
    execution = crud_flow_execution.get(f.db, id=parent.run_execution_id)
    crud_flow_execution.update(f.db, db_obj=execution, obj_in={"status": status})
    due(f)
    await process_one(f.db, real_app)
    await process_one(f.db, real_app)
    assert (
        delivery.call_args.args[2]
        == f"Flow is waiting for approval. {execution_url(str(execution.id))}"
    )
    assert delivery.call_count == 2
    due(f)
    await process_one(f.db, real_app)
    assert delivery.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", ["unlink", "disable"])
async def test_no_completion_after_revocation(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
    revoke: str,
) -> None:
    """A stored answer cannot outlive the linked identity or connection."""
    f = fixture_db
    parent = receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    if revoke == "unlink":
        f.db.delete(f.identity)
        f.db.commit()
    else:
        crud_chat.connections.update(
            f.db, db_obj=f.connection, obj_in={"enabled": False}
        )
    due(f)
    await process_one(f.db, real_app)
    assert delivery.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [None, "other"])
async def test_unavailable_alias(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    alias: str | None,
) -> None:
    """Unconfigured and unknown aliases do not start a run."""
    chat_flow.chat_alias = alias
    fixture_db.db.commit()
    receive(fixture_db, text="/ask docs Question")
    await process_one(fixture_db.db, real_app)
    assert (
        delivery.call_args.args[2]
        == "Unknown or unavailable chat flow. Use help to list flows you may run."
    )


@pytest.mark.asyncio
async def test_inflight_bound(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
) -> None:
    """A second question cannot start overlapping work for the same flow."""
    receive(fixture_db, text="/ask docs First")
    await process_one(fixture_db.db, real_app)
    receive(fixture_db, text="/ask docs Second")
    await process_one(fixture_db.db, real_app)
    assert (
        delivery.call_args.args[2]
        == "You already have a chat run in progress for this flow."
    )
    assert len(dispatch) == 1


def test_long_reply_and_explicit_final_events() -> None:
    """Long replies retain their URL; tool output cannot become an answer."""
    url = "https://example.com/result"
    reply = bounded_reply("a" * 3000, url, "discord")
    assert reply == "a" * (2000 - len(url) - 3) + "…\n\n" + url

    def log(event: dict[str, Any]) -> dict[str, Any]:
        return {"type": "agent_log_line", "payload": {"message": json.dumps(event)}}

    logs = [
        log(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "text": "untrusted"},
            }
        ),
        log({"type": "turn.completed"}),
    ]
    assert final_assistant_text(logs) is None
    logs += [
        log(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Final answer"},
            }
        ),
        log({"type": "turn.completed"}),
    ]
    assert final_assistant_text(logs) == "Final answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,count", [("user", 5), ("connection", 20)])
async def test_rate_limits(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    scope: str,
    count: int,
) -> None:
    """Both rate dimensions count durable reservations, including finished runs."""
    f = fixture_db
    for _ in range(count):
        row = receive(f)
        crud_chat.work.update(
            f.db,
            db_obj=row,
            obj_in={
                "status": "delivered",
                "user_id": f.user.id if scope == "user" else None,
                "run_reserved_at": datetime.utcnow(),
            },
        )
    receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    assert (
        delivery.call_args.args[2]
        == "Chat run rate limit reached. Try again in one minute."
    )


@pytest.mark.asyncio
async def test_permission_denied(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied sender receives a clear response without executing anything."""
    monkeypatch.setattr(
        "preloop.services.chat_flows.has_permission", lambda *args: False
    )
    receive(fixture_db, text="/ask docs Question")
    await process_one(fixture_db.db, real_app)
    assert (
        delivery.call_args.args[2]
        == "You do not have permission to run and read chat flows."
    )


@pytest.mark.asyncio
async def test_unlinked_sender(
    fixture_db: Any, real_app: Any, chat_flow: models.Flow, delivery: AsyncMock
) -> None:
    """An unlinked sender receives only the existing fixed linking help."""
    f = fixture_db
    f.db.delete(f.identity)
    f.db.commit()
    receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    assert (
        delivery.call_args.args[2]
        == "Link your identity from Preloop Settings → Chat connections, then use the provider-specific linking command shown there."
    )


@pytest.mark.asyncio
async def test_default_help_and_audit(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alias discovery, default selection and audit retain the linked actor."""
    from preloop.services.audit import get_audit_service

    monkeypatch.setattr(get_audit_service(), "enabled", True)
    f = fixture_db
    receive(f, text="/help")
    await process_one(f.db, real_app)
    assert delivery.call_args.args[2] == "Chat flows: docs. Use ask ALIAS TEXT."
    crud_chat.connections.update(
        f.db, db_obj=f.connection, obj_in={"default_flow_id": chat_flow.id}
    )
    receive(f, text="/ask Where is the handbook?")
    await process_one(f.db, real_app)
    assert (
        dispatch[0]["trigger_event_data"]["payload"]["chat"]["text"]
        == "Where is the handbook?"
    )
    audit = (
        f.db.query(models.AuditLog)
        .filter_by(account_id=f.account.id, resource_id="flow_execution")
        .one()
    )
    assert audit.user_id == f.user.id
    assert audit.details["action"] == "chat_started"
    assert audit.details["new_value"]["origin"]["connection_id"] == str(f.connection.id)


@pytest.mark.asyncio
async def test_recover_committed_run_after_worker_crash(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
) -> None:
    """A watcher finds a committed run without ever replaying the trigger POST."""
    f = fixture_db
    parent = receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    f.db.refresh(parent)
    execution_id = parent.run_execution_id
    crud_chat.work.update(
        f.db, db_obj=parent, obj_in={"status": "uncertain", "run_execution_id": None}
    )
    execution = crud_flow_execution.get(f.db, id=execution_id)
    crud_flow_execution.update(f.db, db_obj=execution, obj_in={"status": "FAILED"})
    due(f)
    await process_one(f.db, real_app)
    assert len(dispatch) == 1
    assert (
        delivery.call_args.args[2]
        == f"Flow failed.\n\n{execution_url(str(execution_id))}"
    )


@pytest.mark.asyncio
async def test_long_answer_delivered_with_link(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
) -> None:
    """Truncation happens before the durable outbox calls the provider."""
    f = fixture_db
    parent = receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    f.db.refresh(parent)
    execution = crud_flow_execution.get(f.db, id=parent.run_execution_id)
    crud_flow_execution.update(
        f.db,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "result": {"text": "a" * 9000},
            "result_schema_validation": {"valid": True},
        },
    )
    due(f)
    await process_one(f.db, real_app)
    url = execution_url(str(execution.id))
    assert delivery.call_args.args[2] == "a" * (8000 - len(url) - 3) + "…\n\n" + url


@pytest.mark.asyncio
async def test_final_assistant_event_is_delivered(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
) -> None:
    """An unstructured run returns the final assistant event, not log output."""
    f = fixture_db
    parent = receive(f, text="/ask docs Question")
    await process_one(f.db, real_app)
    f.db.refresh(parent)
    execution = crud_flow_execution.get(f.db, id=parent.run_execution_id)
    crud_flow_execution.update(f.db, db_obj=execution, obj_in={"status": "SUCCEEDED"})
    for event in (
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "See handbook.md, Review process.",
            },
        },
        {"type": "turn.completed"},
    ):
        f.db.add(
            models.FlowExecutionLog(
                execution_id=execution.id,
                log_type="agent_log_line",
                message=json.dumps(event),
            )
        )
    f.db.commit()
    due(f)
    await process_one(f.db, real_app)
    assert (
        delivery.call_args.args[2]
        == f"See handbook.md, Review process.\n\n{execution_url(str(execution.id))}"
    )


def test_alias_validation_and_uniqueness(
    fixture_db: Any, chat_flow: models.Flow
) -> None:
    """API shape rejects invalid aliases; concurrent writers have a DB constraint."""
    from pydantic import ValidationError
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(ValidationError):
        schemas.FlowUpdate(chat_alias="Upper Case")
    duplicate = models.Flow(
        account_id=fixture_db.account.id,
        name="Other documents",
        chat_alias="docs",
        prompt_template="Question",
        agent_type="codex",
        agent_config={},
    )
    fixture_db.db.add(duplicate)
    with pytest.raises(IntegrityError):
        fixture_db.db.commit()
    fixture_db.db.rollback()


@pytest.mark.asyncio
async def test_connection_default_validation(
    fixture_db: Any, real_app: Any, chat_flow: models.Flow
) -> None:
    """Connection defaults only select enabled opted-in flows in the account."""
    import httpx
    from preloop.api.auth.jwt import create_access_token

    f = fixture_db
    headers = {
        "Authorization": "Bearer " + create_access_token({"sub": str(f.user.id)})
    }
    path = f"/api/v1/chat/connections/{f.connection.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers=headers,
    ) as client:
        response = await client.patch(path, json={"default_flow_id": str(chat_flow.id)})
        assert response.status_code == 200
        assert response.json()["default_flow_id"] == str(chat_flow.id)
        assert response.json()["enabled"] is True
        response = await client.patch(path, json={"default_flow_id": None})
        assert response.json()["default_flow_id"] is None
        chat_flow.git_clone_config = {"publication_mode": "read_only"}
        f.db.commit()
        response = await client.patch(path, json={"default_flow_id": str(chat_flow.id)})
        assert response.status_code == 422
        assert (
            response.json()["detail"]
            == "Default flow must be enabled and opted in to chat"
        )
        chat_flow.git_clone_config = None
        chat_flow.chat_alias = None
        f.db.commit()
        response = await client.patch(path, json={"default_flow_id": str(chat_flow.id)})
        assert response.status_code == 422
        assert (
            response.json()["detail"]
            == "Default flow must be enabled and opted in to chat"
        )


def test_chat_provenance_subject() -> None:
    """Chat uses the console's shared provenance hint, retaining attribution."""
    from preloop.sync.event_normalizer import extract_trigger_subject

    assert extract_trigger_subject(
        {
            "source": "chat",
            "triggered_by": "Jane Doe",
            "_chat_origin": {"connection_name": "Team chat"},
        }
    ) == {
        "text": "Chat run by Jane Doe",
        "event": "Chat question",
        "ci": "Team chat",
    }


@pytest.mark.asyncio
async def test_trigger_fixture_commits_without_audit(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake trigger must model the service's commit independently of audit."""
    from preloop.services.audit import get_audit_service

    monkeypatch.setattr(get_audit_service(), "enabled", False)
    parent = receive(fixture_db, text="/ask docs Question")
    await process_one(fixture_db.db, real_app)
    fixture_db.db.refresh(parent)
    assert parent.status == "delivered"
    assert (
        delivery.call_args.args[2]
        == f"Flow started. {execution_url(str(parent.run_execution_id))}"
    )


@pytest.mark.asyncio
async def test_ask_does_not_scan_catalog(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    delivery: AsyncMock,
    dispatch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias lookup never loads the full opted-in flow catalog."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("ask scanned the catalog")

    monkeypatch.setattr(crud_chat, "chat_flows", forbidden)
    receive(fixture_db, text="/ask docs Question")
    assert await process_one(fixture_db.db, real_app)
    assert len(dispatch) == 1
    assert delivery.call_args.args[2].startswith("Flow started. ")


def test_help_catalog_uses_resolved_principal(
    fixture_db: Any,
    chat_flow: models.Flow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Help policies use loaded resources without re-resolving the principal."""
    from types import SimpleNamespace
    from preloop.services.chat_flows import available_flows

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("help re-resolved the principal")

    monkeypatch.setattr(crud_chat, "principal", forbidden)
    broker = SimpleNamespace(db=fixture_db.db, connection=fixture_db.connection)
    assert available_flows(broker, fixture_db.user) == [chat_flow]


@pytest.mark.asyncio
async def test_manual_chat_origin_is_template_data(
    fixture_db: Any,
    real_app: Any,
    chat_flow: models.Flow,
    dispatch: Any,
) -> None:
    """Ordinary template data stays compatible; internal provenance is reserved."""
    import httpx
    from preloop.api.auth.jwt import create_access_token

    headers = {
        "Authorization": "Bearer "
        + create_access_token({"sub": str(fixture_db.user.id)})
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app),
        base_url="http://test",
        headers=headers,
    ) as client:
        path = f"/api/v1/flows/{chat_flow.id}/trigger"
        response = await client.post(
            path, json={"chat_origin": "custom template input"}
        )
        assert response.status_code == 200, response.text
        assert dispatch[0]["test_mode"] is True
        assert (
            dispatch[0]["trigger_event_data"]["chat_origin"] == "custom template input"
        )
        response = await client.post(
            path, json={"_chat_origin": {"connection_id": "forged"}}
        )
        assert response.status_code == 400
        assert "reserved key(s) _chat_origin" in response.json()["detail"]
        assert len(dispatch) == 1
