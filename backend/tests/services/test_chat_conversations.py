"""Chat conversations retain real API ownership, durable turns and delivery rules."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

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


@pytest.fixture(params=["slack", "mattermost", "discord"])
def provider(request: pytest.FixtureRequest, fixture_db: Any) -> str:
    """Run every lifecycle against each provider's broker address semantics."""
    name = str(request.param)
    crud_chat.connections.update(
        fixture_db.db, db_obj=fixture_db.connection, obj_in={"provider": name}
    )
    return name


@pytest.fixture
def flow(fixture_db: Any, monkeypatch: pytest.MonkeyPatch) -> models.Flow:
    """A hosted interactive flow; only container dispatch and checkpoints are fakes."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    result = models.Flow(
        account_id=fixture_db.account.id,
        name="Repository questions",
        chat_alias="docs",
        prompt_template="Answer {{conversation.message}}",
        agent_type="opencode",
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
