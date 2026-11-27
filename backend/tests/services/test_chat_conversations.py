"""Chat conversations retain real API ownership, durable turns and delivery rules."""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.endpoints.chat import list_deliveries
from preloop.models import models, schemas
from preloop.models.crud import crud_chat, crud_flow_execution
from preloop.models.crud import flow_conversation as conversations
from preloop.services.chat_assistant import ChatBroker
from preloop.services.chat_worker import process_one
from preloop.services.flow_conversations import BUSY_NOTICE
from tests.services import test_chat_backend as fixtures

fixture_db = fixtures.fixture_db
real_app = fixtures.real_app
receive = fixtures.receive


@pytest.fixture(params=["slack", "mattermost", "discord", "teams"])
def provider(request: pytest.FixtureRequest, fixture_db: Any) -> str:
    """Run every lifecycle against each provider's broker address semantics."""
    name = str(request.param)
    crud_chat.connections.update(
        fixture_db.db, db_obj=fixture_db.connection, obj_in={"provider": name}
    )
    return name


@pytest.fixture
def flow(
    fixture_db: Any, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> models.Flow:
    """A hosted interactive flow; only container dispatch and checkpoints are fakes."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    result = models.Flow(
        account_id=fixture_db.account.id,
        name="Repository questions",
        chat_alias="docs",
        prompt_template="Answer {{conversation.message}}",
        agent_type=getattr(request, "param", "opencode"),
        agent_config={"interactive": {"enabled": True, "max_turns": 30}},
        is_enabled=True,
    )
    fixture_db.db.add(result)
    fixture_db.db.commit()

    async def dispatch(service: Any, **kwargs: Any) -> dict[str, Any]:
        row = conversations.get(
            service.db, kwargs["conversation_id"], fixture_db.account.id
        )
        execution = crud_flow_execution.create(
            service.db,
            schemas.FlowExecutionCreate(
                flow_id=row.flow_id,
                status="RUNNING",
                trigger_event_details={"_conversation_id": str(row.id)},
            ),
        )
        conversations.bind_execution(service.db, row, execution.id)
        service.db.commit()
        return {"id": str(execution.id)}

    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
        dispatch,
    )
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService._authorize_flow_run",
        AsyncMock(),
    )
    return result


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Record provider writes without external network traffic."""
    sender = AsyncMock(return_value="message-test")
    monkeypatch.setattr("preloop.services.chat_worker.send_private_reply", sender)
    return sender


def watch(f: Any) -> models.ChatWork:
    """Make the persistent observer due without sleeping."""
    row = next(
        row
        for row in crud_chat.work.get_multi(
            f.db, account_id=str(f.account.id), limit=1000
        )
        if row.payload.get("conversation_watch")
    )
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={
            "lease_until": None,
            "next_attempt_at": datetime.utcnow() - timedelta(seconds=1),
        },
    )
    return row


def park(f: Any) -> models.ChatWork:
    """Keep the persistent observer out of the next drain."""
    row = watch(f)
    crud_chat.work.update(
        f.db,
        db_obj=row,
        obj_in={"next_attempt_at": datetime.utcnow() + timedelta(hours=1)},
    )
    return row


async def drain(f: Any, app: Any) -> None:
    """Drain only eligible jobs; future polls remain durable and pending."""
    for _ in range(20):
        if not await process_one(f.db, app):
            return
    pytest.fail("Queue did not drain")


async def start(
    f: Any, app: Any, sender: AsyncMock, **payload: Any
) -> models.FlowConversation:
    """Start through authenticated HTTP and assert the visible cold-start notice."""
    receipt = receive(f, text="/ask docs Remember amber", **payload)
    await drain(f, app)
    assert receipt.status == "delivered"
    text = sender.call_args.args[2]
    assert "Conversation with Repository questions starting" in text
    assert "first answer can take a minute or more" in text
    assert "Messages are not queued" in text
    assert "Use end before ask" in text
    row = conversations.get(f.db, receipt.id, f.account.id)
    assert row.owner_user_id == f.user.id
    assert row.origin == f.connection.provider
    assert conversations.messages(f.db, row.id)[0].text == "Remember amber"
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["opencode", "deepseek"], indirect=True)
async def test_lifecycle_content(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start, refuse busy, answer once, status, follow up, stop/resume and end."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    receive(f, text="What is the word?")
    await drain(f, real_app)
    assert delivery.call_args.args[2] == BUSY_NOTICE
    assert row.turns == 1

    receive(f, text="/ask docs Replace this")
    await drain(f, real_app)
    assert "Use end before ask" in delivery.call_args.args[2]
    conversations.runtime_turn(
        f.db, row, turn=1, answer="The word is amber.", finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    assert delivery.call_args.args[2].startswith("The word is amber.\n\n")
    assert f"/flow-conversations/{row.id}/talk" in delivery.call_args.args[2]
    count = delivery.call_count
    watch(f)
    await drain(f, real_app)
    assert delivery.call_count == count
    receive(f, text="/status")
    await drain(f, real_app)
    assert (
        "Repository questions: waiting. Turns 1/30. Container stops in"
        in delivery.call_args.args[2]
    )
    receive(f, text="Repeat it")
    await drain(f, real_app)
    assert (
        "Message sent to Repository questions. Wait for the answer; messages are not queued."
        == delivery.call_args.args[2]
    )
    assert conversations.messages(f.db, row.id)[1].text == "Repeat it"
    conversations.runtime_turn(
        f.db, row, turn=2, answer="Amber.", finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    conversations.save(
        f.db,
        row,
        state="stopped",
        notice="The container stopped after the idle timeout. Send a message to resume this conversation.",
        idle_deadline=None,
    )
    watch(f)
    await drain(f, real_app)
    assert "Send a message to resume this conversation" in delivery.call_args.args[2]
    count = delivery.call_count
    watch(f)
    await drain(f, real_app)
    assert delivery.call_count == count
    receive(f, text="/status")
    await drain(f, real_app)
    assert (
        "Container stopped; the next message resumes it." in delivery.call_args.args[2]
    )
    monkeypatch.setattr(
        "preloop.api.endpoints.flow_conversations.resume_checkpoint",
        lambda *_: {"session_id": "synthetic-session"},
    )
    receive(f, text="Remember it after resume")
    await drain(f, real_app)
    assert (
        "Resuming this conversation. The first answer can take a minute or more."
        in delivery.call_args.args[2]
    )
    assert conversations.messages(f.db, row.id)[2].text == "Remember it after resume"
    receive(f, text="/end")
    await drain(f, real_app)
    assert (
        "Conversation ended. Your next plain message goes to the read-only account assistant."
        in delivery.call_args.args[2]
    )
    assert (
        ChatBroker(f.db, f.connection, "external-test", real_app).binding(
            receive(f, text="/status")
        )
        is None
    )
    await drain(f, real_app)
    assert "No active flow conversation" in delivery.call_args.args[2]
    monkeypatch.setattr(ChatBroker, "answer", AsyncMock(return_value="Account answer."))
    receive(f, text="List agents")
    await drain(f, real_app)
    assert delivery.call_args.args[2] == "Read-only account assistant: Account answer."


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "limit", "ended"])
async def test_terminal_notice_once(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    terminal: str,
) -> None:
    """Failure notice text, final answer/turn limit and console-ended state survive polling."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    if terminal == "limit":
        conversations.save(f.db, row, max_turns=1)
        conversations.runtime_turn(
            f.db, row, turn=1, answer="Final answer.", finished=True, error=False
        )
        expected = "The turn limit was reached. Start a new conversation."
    else:
        expected = (
            "A synthetic conversation failure. Start a new conversation."
            if terminal == "failed"
            else "You ended this conversation. Start a new conversation to continue."
        )
        conversations.save(f.db, row, state=terminal, notice=expected)
    watch(f)
    await drain(f, real_app)
    assert delivery.call_args.args[2].startswith(expected)
    if terminal == "limit":
        assert any(
            call.args[2].startswith("Final answer.") for call in delivery.call_args_list
        )
    count = delivery.call_count
    await drain(f, real_app)
    assert delivery.call_count == count
    assert (
        crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revocation", ["unlink", "disable", "account", "visibility", "relink"]
)
async def test_recheck_before_stored_answer(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    revocation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored answer cannot outlive the identity, connection, account or flow visibility."""
    from preloop.plugins.account_hooks import Decision

    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Stored answer", finished=True, error=False
    )
    watch(f)
    assert await process_one(f.db, real_app)  # queues the answer without sending
    count = delivery.call_count
    if revocation == "unlink":
        crud_chat.identities.delete(f.db, id=f.identity.id)
    elif revocation == "disable":
        crud_chat.connections.update(
            f.db, db_obj=f.connection, obj_in={"enabled": False}
        )
    elif revocation == "account":
        f.account.is_active = False
        f.db.commit()
    elif revocation == "relink":
        user = models.User(
            account_id=f.account.id,
            username=f"other-{uuid4()}",
            email="other@example.com",
            hashed_password="unused",
            is_active=True,
        )
        f.db.add(user)
        f.db.commit()
        crud_chat.identities.update(
            f.db, db_obj=f.identity, obj_in={"user_id": user.id}
        )
    else:
        monkeypatch.setattr(
            "preloop.services.chat_assistant.authorize",
            lambda *_: Decision("deny"),
        )
    await drain(f, real_app)
    assert delivery.call_count == count


@pytest.mark.asyncio
async def test_slack_thread_owner(
    fixture_db: Any, real_app: Any, flow: models.Flow, delivery: AsyncMock
) -> None:
    """Two linked users in the same channel thread never share admission rights."""
    f = fixture_db
    payload = {"private": False, "channel_id": "channel-test", "thread_id": "123.45"}
    row = await start(f, real_app, delivery, **payload)
    assert delivery.call_args.kwargs["thread_id"] == "123.45"
    other = models.User(
        account_id=f.account.id,
        username=f"other-{uuid4()}",
        email="other@example.com",
        hashed_password="unused",
        is_active=True,
    )
    f.db.add(other)
    f.db.flush()
    f.db.add(
        models.ChatIdentity(
            account_id=f.account.id,
            connection_id=f.connection.id,
            user_id=other.id,
            external_user_id="other-test",
        )
    )
    f.db.commit()
    for text in ("Continue", "/end", "/ask docs Replace"):
        crud_chat.receive(
            f.db,
            connection=f.connection,
            event_id=str(uuid4()),
            external_user_id="other-test",
            payload={**payload, "text": text},
        )
        await drain(f, real_app)
        assert (
            "Only the person who started this conversation can continue or end it."
            in delivery.call_args.args[2]
        )
        assert delivery.call_args.kwargs["thread_id"] == "123.45"
    assert row.turns == 1


@pytest.mark.asyncio
async def test_interrupted_answer_not_replayed(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """A disconnect during provider delivery is uncertain and never replayed."""
    import httpx

    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Once only", finished=True, error=False
    )
    watch(f)
    delivery.side_effect = httpx.ReadError("Synthetic disconnect")
    await drain(f, real_app)
    count = delivery.call_count
    watch(f)
    await drain(f, real_app)
    assert delivery.call_count == count
    assert any(
        item.status == "uncertain"
        for item in crud_chat.work.get_multi(f.db, account_id=str(f.account.id))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["user", "connection"])
async def test_shared_rate_limits(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    dimension: str,
) -> None:
    """Conversation turns share the durable one-shot abuse bounds."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Ready", finished=True, error=False
    )
    for _index in range(4 if dimension == "user" else 19):
        receipt = receive(f, text="synthetic reservation")
        crud_chat.work.update(
            f.db,
            db_obj=receipt,
            obj_in={
                "status": "delivered",
                "user_id": f.user.id if dimension == "user" else None,
                "run_reserved_at": datetime.utcnow(),
            },
        )
    # The observer from start() becomes due five seconds later. A slow runner
    # reaches that inside the reservation loop, and the observer then delivers
    # the "Ready" answer after the refusal. Keep it parked: only the inbound
    # turn is under test here.
    park(f)
    receive(f, text="One more turn")
    await drain(f, real_app)
    assert (
        delivery.call_args.args[2]
        == "Chat run rate limit reached. Try again in one minute."
    )
    assert row.turns == 1


@pytest.mark.asyncio
async def test_scope_reservation_survives_separate_sessions(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """A second process sees the same owner without an in-memory conversation map."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    with Session(f.engine) as other:
        binding = crud_chat.conversation_binding(
            other, f.connection.id, "dm:external-test"
        )
        assert binding.conversation_id == row.id
        assert binding.owner_user_id == f.user.id
        assert binding.external_user_id == "external-test"


@pytest.mark.asyncio
async def test_answer_respects_provider_limit(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """A long completed answer preserves its full-thread link under provider limits."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="answer " * 10000, finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    text = delivery.call_args.args[2]
    assert len(text) <= (2000 if provider == "discord" else 8000)
    assert "…" in text
    assert text.endswith(f"/console/flow-conversations/{row.id}/talk")


@pytest.mark.asyncio
async def test_terminal_poll_pages_before_closing(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """A completed long thread drains every API page before detaching its address."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    first = conversations.messages(f.db, row.id)[0]
    first.answer, first.status = "Answer 1", "completed"
    f.db.add_all(
        [
            models.FlowConversationMessage(
                conversation_id=row.id,
                request_id=uuid4(),
                turn=turn,
                text=f"Question {turn}",
                answer=f"Answer {turn}",
                status="completed",
            )
            for turn in range(2, 102)
        ]
    )
    conversations.save(f.db, row, state="ended", turns=101, max_turns=101)
    observer = watch(f)
    assert await process_one(f.db, real_app)
    f.db.refresh(observer)
    assert observer.payload["after_turn"] == 100
    assert (
        crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
        is not None
    )
    queued = [
        item
        for item in crud_chat.work.get_multi(
            f.db, account_id=str(f.account.id), limit=1000
        )
        if item.payload.get("conversation_notice")
    ]
    assert len(queued) == 100
    for item in queued:
        crud_chat.work.update(f.db, db_obj=item, obj_in={"status": "delivered"})
    watch(f)
    await drain(f, real_app)
    assert any(
        call.args[2].startswith("Answer 101\n") for call in delivery.call_args_list
    )
    assert "turn limit was reached" in delivery.call_args.args[2]
    assert (
        crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
        is None
    )


@pytest.mark.asyncio
async def test_unconfirmed_start_releases_reservation(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """An absent API object after an uncertain start cannot trap the chat address."""
    f = fixture_db
    parent = receive(f, text="/ask docs Remember amber", actor_id=str(f.user.id))
    claimed = crud_chat.claim(f.db)
    crud_chat.transition(f.db, claimed, "processing", user_id=f.user.id)
    assert (
        crud_chat.reserve_conversation(f.db, claimed, flow.id, "dm:external-test")
        is None
    )
    crud_chat.transition(f.db, claimed, "uncertain")
    observer = watch(f)
    crud_chat.work.update(
        f.db,
        db_obj=observer,
        obj_in={"created_at": datetime.utcnow() - timedelta(minutes=11)},
    )
    assert await process_one(f.db, real_app)
    f.db.refresh(observer)
    assert observer.status == "cancelled"
    assert "inspect Talk" in observer.last_error
    assert (
        crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
        is None
    )
    assert delivery.call_count == 0
    f.db.refresh(parent)
    assert parent.status == "uncertain"
    assert parent.run_flow_id is None


@pytest.mark.asyncio
async def test_owner_continues_slack_channel_thread(
    fixture_db: Any,
    real_app: Any,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """The owner's mentioned channel reply admits a turn on the original object."""
    f = fixture_db
    payload = {"private": False, "channel_id": "channel-test", "thread_id": "123.45"}
    row = await start(f, real_app, delivery, **payload)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Ready", finished=True, error=False
    )
    receive(f, text="Continue in this thread", **payload)
    await drain(f, real_app)
    assert row.turns == 2
    assert conversations.messages(f.db, row.id)[1].text == "Continue in this thread"
    assert delivery.call_args.kwargs["channel_id"] == "channel-test"
    assert delivery.call_args.kwargs["thread_id"] == "123.45"


@pytest.mark.asyncio
async def test_poll_outage_retains_durable_observer(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated read failures keep polling without replaying any effectful POST."""
    import httpx
    from fastapi import HTTPException

    f = fixture_db
    row = await start(f, real_app, delivery)
    original = ChatBroker.request
    for error in (
        httpx.ConnectError("Synthetic outage"),
        HTTPException(503),
        HTTPException(429),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(ChatBroker, "request", AsyncMock(side_effect=error))
            for _ in range(4):
                observer = watch(f)
                assert await process_one(f.db, real_app)
                f.db.refresh(observer)
                assert observer.status == "pending"
                assert observer.attempts == 0
    monkeypatch.setattr(ChatBroker, "request", original)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Recovered answer", finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    assert delivery.call_args.args[2].startswith("Recovered answer\n")


@pytest.mark.asyncio
async def test_start_refusal_rechecks_flow_before_disclosure(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An API refusal notice must not outlive visibility of the selected flow."""
    from fastapi import HTTPException
    from preloop.plugins.account_hooks import ALLOW, Decision

    revoked = False

    def authorize(*args: Any) -> Any:
        return Decision("deny") if revoked else ALLOW

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        nonlocal revoked
        revoked = True
        raise HTTPException(409, "Synthetic protected conversation notice")

    monkeypatch.setattr("preloop.services.chat_assistant.authorize", authorize)
    monkeypatch.setattr(ChatBroker, "request", refuse)
    f = fixture_db
    receipt = receive(f, text="/ask docs Start this conversation")
    assert await process_one(f.db, real_app)
    f.db.refresh(receipt)
    assert receipt.status == "failed"
    assert delivery.call_count == 0


@pytest.mark.asyncio
async def test_lifecycle_command_prefixes_remain_plain_messages(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """Words at the start of a natural follow-up cannot end or inspect a thread."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    for turn, text in enumerate(("end with an example", "status of the migration"), 1):
        conversations.runtime_turn(
            f.db, row, turn=turn, answer="Ready", finished=True, error=False
        )
        receive(f, text=text)
        await drain(f, real_app)
        assert "Message sent to Repository questions" in delivery.call_args.args[2]
        assert conversations.messages(f.db, row.id)[-1].text == text
        assert not row.end_requested
    receive(f, text="/end with extra text")
    await drain(f, real_app)
    assert "Use end or status without additional text" in delivery.call_args.args[2]
    assert not row.end_requested


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("end", "/end"),
        ("status", "/status"),
        (" end ", "/end"),
        (" link SYNTHETIC_CODE ", "/link"),
        ("end with an example", "end with an example"),
        ("status of the migration", "status of the migration"),
    ],
)
async def test_slack_lifecycle_normalization_is_exact(
    fixture_db: Any,
    real_app: Any,
    text: str,
    expected: str,
) -> None:
    """Signed Slack ingress reserves whole commands, preserving natural text."""
    import hashlib
    import hmac
    import json
    import time
    import httpx

    f = fixture_db
    event_id = str(uuid4())
    raw = json.dumps(
        {
            "team_id": "team-test",
            "event_id": event_id,
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "external-test",
                "channel": "dm-test",
                "text": text,
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
        result = await client.post(
            f"/api/v1/chat/ingress/{f.connection.id}",
            content=raw,
            headers={
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=" + signature,
            },
        )
    assert result.status_code == 200
    receipt = crud_chat.work.get_multi(
        f.db, connection_id=f.connection.id, event_id=event_id
    )[0]
    assert receipt.payload["text"] == expected
    if expected == "/link":
        assert "plain_text" not in receipt.payload
        assert (
            receipt.payload["link_digest"]
            == hashlib.sha256(b"SYNTHETIC_CODE").hexdigest()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_active", [True, False])
async def test_relinked_identity_releases_old_address(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    observer_active: bool,
) -> None:
    """Both an observer and a later input can release a former owner's binding."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    user = models.User(
        account_id=f.account.id,
        username=f"other-{uuid4()}",
        email="other@example.com",
        hashed_password="unused",
        is_active=True,
    )
    f.db.add(user)
    f.db.commit()
    crud_chat.identities.update(f.db, db_obj=f.identity, obj_in={"user_id": user.id})
    if observer_active:
        observer = watch(f)
        assert await process_one(f.db, real_app)
        f.db.refresh(observer)
        assert observer.status == "cancelled"
    else:
        receive(f, text="Continue")
        await drain(f, real_app)
        assert (
            "previous linked user's conversation is detached"
            in delivery.call_args.args[2]
        )
    assert (
        crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
        is None
    )
    receipt = receive(f, text="/ask docs A new conversation")
    await drain(f, real_app)
    assert "starting. The first answer" in delivery.call_args.args[2]
    binding = crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
    assert binding.owner_user_id == user.id and binding.conversation_id == receipt.id
    assert binding.conversation_id != row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 503, 404, 403, 401, "network"])
async def test_conversation_read_failures_are_retried_or_explained(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    failure: int | str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read outages never silently discard an input or trap an unavailable address."""
    import httpx
    from fastapi import HTTPException

    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Ready", finished=True, error=False
    )
    receipt = receive(f, text="Continue after the read")
    count = delivery.call_count
    error = (
        httpx.ConnectError("Synthetic read failure")
        if failure == "network"
        else HTTPException(failure)
    )
    with monkeypatch.context() as patch:
        patch.setattr(ChatBroker, "request", AsyncMock(side_effect=error))
        assert await process_one(f.db, real_app)
    f.db.refresh(receipt)
    if failure in {401, 403, 404}:
        assert receipt.status == "delivered"
        assert (
            "conversation is no longer available. Your message was not sent"
            in delivery.call_args.args[2]
        )
        assert (
            crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
            is None
        )
    else:
        assert receipt.status == "pending" and delivery.call_count == count
        crud_chat.work.update(
            f.db,
            db_obj=receipt,
            obj_in={"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)},
        )
        await drain(f, real_app)
        assert "Message sent to Repository questions" in delivery.call_args.args[2]
        assert (
            conversations.messages(f.db, row.id)[-1].text == "Continue after the read"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/status", "/end"])
async def test_bounded_read_outage_sends_explicit_refusal(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final safe-read retry tells the user their command was not sent."""
    from fastapi import HTTPException

    f = fixture_db
    await start(f, real_app, delivery)
    receipt = receive(f, text=command)
    crud_chat.work.update(f.db, db_obj=receipt, obj_in={"attempts": 2})
    monkeypatch.setattr(
        ChatBroker, "request", AsyncMock(side_effect=HTTPException(503))
    )
    assert await process_one(f.db, real_app)
    assert (
        "service is unavailable. Your message was not sent"
        in delivery.call_args.args[2]
    )
    f.db.refresh(receipt)
    assert receipt.status == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ended", "failed"])
@pytest.mark.parametrize("command", ["/status", "/ask docs New conversation"])
async def test_terminal_binding_cleanup_precedes_commands(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    state: str,
    command: str,
) -> None:
    """A console terminal transition releases the address before ask/status routing."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    execution = crud_flow_execution.get(f.db, id=row.execution_id)
    crud_flow_execution.update(
        f.db,
        db_obj=execution,
        obj_in={"status": "FAILED" if state == "failed" else "SUCCEEDED"},
    )
    conversations.save(
        f.db,
        row,
        state=state,
        notice="Synthetic terminal notice.",
        stop_reason="dispatch_failed" if state == "failed" else "ended",
    )
    receive(f, text=command)
    await drain(f, real_app)
    if command == "/status":
        assert (
            "Synthetic terminal notice. This chat address is released"
            in delivery.call_args.args[2]
        )
        assert (
            crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
            is None
        )
    else:
        assert "starting. The first answer" in delivery.call_args.args[2]
        assert (
            crud_chat.conversation_binding(
                f.db, f.connection.id, "dm:external-test"
            ).conversation_id
            != row.id
        )


@pytest.mark.asyncio
async def test_active_conversation_preserves_natural_command_prefixes(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
) -> None:
    """Verified ingress can preserve raw text while reserving strict explicit commands."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    for turn, text in enumerate(
        ("help me understand this", "ask me later", "note to self: explain this"), 1
    ):
        conversations.runtime_turn(
            f.db, row, turn=turn, answer="Ready", finished=True, error=False
        )
        receive(
            f,
            text=text if provider == "discord" else "/" + text,
            **({} if provider == "discord" else {"plain_text": text}),
        )
        await drain(f, real_app)
        assert "Message sent to Repository questions" in delivery.call_args.args[2]
        assert conversations.messages(f.db, row.id)[-1].text == text


@pytest.mark.asyncio
async def test_idle_poll_backoff_deduplicates_without_reinserting_notices(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle observation backs off and new turns wake it without losing the answer."""
    from unittest.mock import Mock

    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Ready", finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    conversations.save(
        f.db,
        row,
        state="stopped",
        notice="The container stopped. Send a message to resume.",
    )
    spy = Mock(wraps=crud_chat.receive)
    monkeypatch.setattr(crud_chat, "receive", spy)
    for _ in range(4):
        observer = watch(f)
        await drain(f, real_app)
    assert spy.call_count == 1
    f.db.refresh(observer)
    assert (observer.next_attempt_at - datetime.utcnow()).total_seconds() >= 150
    monkeypatch.setattr(
        "preloop.api.endpoints.flow_conversations.resume_checkpoint",
        lambda *_: {"session_id": "synthetic-session"},
    )
    receive(f, text="Resume quickly")
    await drain(f, real_app)
    f.db.refresh(observer)
    assert (observer.next_attempt_at - datetime.utcnow()).total_seconds() <= 6
    conversations.runtime_turn(
        f.db, row, turn=2, answer="Resumed answer", finished=True, error=False
    )
    watch(f)
    await drain(f, real_app)
    assert delivery.call_args.args[2].startswith("Resumed answer")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_non_json_conversation_errors_keep_the_http_status(
    fixture_db: Any,
    flow: models.Flow,
    status: int,
) -> None:
    """HTML/plain-text error responses cannot turn a retryable GET into ValueError."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import Response
    from preloop.schemas.flow_conversation import InteractiveSettings

    f = fixture_db
    row = conversations.create(
        f.db,
        conversation_id=uuid4(),
        flow=flow,
        owner=f.user,
        policy=InteractiveSettings(),
    )
    f.db.commit()
    app = FastAPI()

    @app.get("/api/v1/flow-conversations/{conversation_id}")
    def error(conversation_id: str) -> Response:
        return Response("Unavailable", status_code=status, media_type="text/plain")

    broker = ChatBroker(f.db, f.connection, "external-test", app)
    with pytest.raises(HTTPException) as caught:
        await broker.request(
            "GET", f"/api/v1/flow-conversations/{row.id}", "view_flows"
        )
    assert caught.value.status_code == status
    assert caught.value.detail == "Action denied or unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HTTPException(429),
        HTTPException(503),
        httpx.ConnectError("Synthetic outage"),
        asyncio.TimeoutError(),
    ],
    ids=["rate-limit", "unavailable", "network", "timeout"],
)
async def test_stored_answer_survives_revalidation_outage(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A read outage before sending cannot lose or replay a stored answer."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Durable answer", finished=True, error=False
    )
    watch(f)
    assert await process_one(f.db, real_app)
    count = delivery.call_count
    with monkeypatch.context() as patch:
        request = AsyncMock(side_effect=error)
        patch.setattr(ChatBroker, "request", request)
        for _ in range(3):
            assert await process_one(f.db, real_app)
            answer = next(
                item
                for item in crud_chat.work.get_multi(
                    f.db, account_id=str(f.account.id), limit=1000
                )
                if item.payload.get("conversation_notice")
            )
            assert answer.status == "reply_ready"
            crud_chat.work.update(
                f.db,
                db_obj=answer,
                obj_in={"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)},
            )
        assert request.call_count == 3
        assert all(call.args[0] == "GET" for call in request.call_args_list)
    assert delivery.call_count == count
    answer = next(
        item
        for item in crud_chat.work.get_multi(
            f.db, account_id=str(f.account.id), limit=1000
        )
        if item.payload.get("conversation_notice")
    )
    assert answer.status == "reply_ready" and answer.attempts == 0
    crud_chat.work.update(
        f.db,
        db_obj=answer,
        obj_in={"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)},
    )
    await drain(f, real_app)
    assert delivery.call_args.args[2].startswith("Durable answer")
    assert delivery.call_count == count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HTTPException(429),
        HTTPException(503),
        httpx.ConnectError("Synthetic outage"),
        asyncio.TimeoutError(),
    ],
    ids=["rate-limit", "unavailable", "network", "timeout"],
)
async def test_stored_answer_revalidation_stops_and_retains_history(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Persistent read failures stop retrying without deleting or disclosing answers."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.runtime_turn(
        f.db, row, turn=1, answer="Retained answer", finished=True, error=False
    )
    watch(f)
    assert await process_one(f.db, real_app)
    count = delivery.call_count
    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(ChatBroker, "request", request)
    delays = [30, 60, 120, 240, 300, 300, 300, 300, 300, 300, 300]
    for failure in range(12):
        before = datetime.utcnow()
        assert await process_one(f.db, real_app)
        answer = next(
            item
            for item in crud_chat.work.get_multi(
                f.db, account_id=str(f.account.id), limit=1000
            )
            if item.payload.get("conversation_notice")
        )
        assert answer.reply.startswith("Retained answer")
        assert answer.lease_until is None
        assert delivery.call_count == count
        if failure < 11:
            assert answer.status == "reply_ready"
            assert before + timedelta(seconds=delays[failure]) <= answer.next_attempt_at
            assert answer.next_attempt_at <= datetime.utcnow() + timedelta(
                seconds=delays[failure]
            )
        else:
            assert answer.status == "failed"
            assert "retry limit reached" in answer.last_error
            assert "retained for inspection" in answer.last_error
        crud_chat.work.update(
            f.db,
            db_obj=answer,
            obj_in={"next_attempt_at": datetime.utcnow() - timedelta(seconds=1)},
        )
    f.db.expire_all()
    assert crud_chat.work.get(f.db, answer.id).reply.startswith("Retained answer")
    history = list_deliveries(f.connection.id, db=f.db, current_user=f.user)
    retained = next(
        item for item in history["deliveries"] if item["id"] == str(answer.id)
    )
    assert retained["status"] == "failed"
    assert retained["last_error"] == answer.last_error
    assert not await process_one(f.db, real_app)
    assert request.call_count == 12
    assert all(call.args[0] == "GET" for call in request.call_args_list)
    assert delivery.call_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_end_refusal_releases_address_but_uncertain_post_is_not_replayed(
    fixture_db: Any,
    real_app: Any,
    provider: str,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    uncertain: bool,
) -> None:
    """Only a definitive end refusal permits safe chat-address cleanup."""
    import httpx
    from fastapi import HTTPException

    f = fixture_db
    await start(f, real_app, delivery)
    original = ChatBroker.request

    async def request(
        broker: ChatBroker,
        method: str,
        path: str,
        permission: str,
        payload: Any = None,
        **kwargs: Any,
    ) -> Any:
        if method == "POST" and path.endswith("/end"):
            if uncertain:
                raise httpx.ReadError("Synthetic uncertain end")
            raise HTTPException(404)
        return await original(broker, method, path, permission, payload, **kwargs)

    monkeypatch.setattr(ChatBroker, "request", request)
    count = delivery.call_count
    # An observer can become due while the command is being prepared. It is
    # older than the command, so a single worker step need not claim /end.
    watch(f)
    receipt = receive(f, text="/end")
    assert await process_one(f.db, real_app)
    f.db.refresh(receipt)
    assert receipt.status == "pending"
    await drain(f, real_app)
    f.db.refresh(receipt)
    if uncertain:
        assert receipt.status == "uncertain" and delivery.call_count == count
        assert (
            crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
            is not None
        )
    else:
        assert receipt.status == "delivered"
        assert (
            "conversation is no longer available. The chat address is released"
            in delivery.call_args.args[2]
        )
        assert (
            crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
            is None
        )


def test_concurrent_wake_survives_observer_deferral(fixture_db: Any) -> None:
    """A follow-up wake cannot be overwritten by an observer's longer idle delay."""
    f = fixture_db
    conversation_id = uuid4()
    observer = crud_chat.receive(
        f.db,
        connection=f.connection,
        event_id=f"flow-watch:{conversation_id}",
        external_user_id="external-test",
        payload={"conversation_watch": str(conversation_id)},
    )
    claimed = crud_chat.claim(f.db)
    assert claimed.id == observer.id
    token = claimed.lease_token
    with Session(f.engine) as other:
        crud_chat.wake_conversation_observer(other, f.connection.id, conversation_id)
    f.db.refresh(claimed)
    assert claimed.status == "processing" and claimed.lease_token == token
    crud_chat.defer_conversation_observer(f.db, claimed, 300)
    assert claimed.status == "pending" and claimed.lease_until is None
    assert 0 < (claimed.next_attempt_at - datetime.utcnow()).total_seconds() <= 5


def test_stale_cleanup_preserves_new_binding_owner(fixture_db: Any) -> None:
    """An old actor or conversation cannot detach a newly reused address."""
    f = fixture_db
    old_owner, old_conversation, new_conversation = uuid4(), uuid4(), uuid4()
    binding = crud_chat.bindings.create(
        f.db,
        obj_in={
            "account_id": f.account.id,
            "connection_id": f.connection.id,
            "scope": "dm:external-test",
            "external_user_id": "external-test",
            "owner_user_id": f.user.id,
            "channel_id": "dm-test",
            "thread_id": "",
            "conversation_id": new_conversation,
            "closed": False,
        },
    )
    crud_chat.close_bindings(
        f.db, f.connection.id, external_user_id="external-test", owner_user_id=old_owner
    )
    crud_chat.close_conversation_binding(f.db, f.connection.id, old_conversation)
    f.db.refresh(binding)
    assert not binding.closed and binding.conversation_id == new_conversation
    crud_chat.close_conversation_binding(f.db, f.connection.id, new_conversation)
    f.db.refresh(binding)
    assert binding.closed


@pytest.mark.asyncio
async def test_terminal_observer_does_not_close_reused_address(
    fixture_db: Any,
    real_app: Any,
    flow: models.Flow,
    delivery: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new reservation made during an old poll survives the old terminal cleanup."""
    f = fixture_db
    row = await start(f, real_app, delivery)
    conversations.save(f.db, row, state="ended", notice="Old conversation ended.")
    new_id = uuid4()
    original = ChatBroker.request

    async def request(
        broker: ChatBroker,
        method: str,
        path: str,
        permission: str,
        payload: Any = None,
        **kwargs: Any,
    ) -> Any:
        result = await original(broker, method, path, permission, payload, **kwargs)
        if f"/{row.id}?after_turn=" in path:
            with Session(f.engine) as other:
                binding = crud_chat.conversation_binding(
                    other, f.connection.id, "dm:external-test"
                )
                crud_chat.bindings.update(
                    other,
                    db_obj=binding,
                    obj_in={"conversation_id": new_id, "closed": False},
                )
        return result

    monkeypatch.setattr(ChatBroker, "request", request)
    watch(f)
    assert await process_one(f.db, real_app)
    binding = crud_chat.conversation_binding(f.db, f.connection.id, "dm:external-test")
    assert binding is not None and binding.conversation_id == new_id
