"""Conversation content, admission, attribution and aggregate cost invariants."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints import flow_conversations as api
from preloop.api.auth import get_current_active_user
from preloop.models.db.session import get_db_session
from preloop.models import models
from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.crud import flow_conversation as crud
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.schemas.flow_conversation import InteractiveSettings
from preloop.services import flow_conversations as service
from preloop.services.audit.service import AuditService
from preloop.services.flow_execution_limits import (
    ExecutionLimits,
    evaluate_execution_limits,
    get_execution_usage,
)


@pytest.fixture
def thread(db_session: Session, test_user: models.User) -> models.FlowConversation:
    """Create a real conversation with an execution and its first admitted turn."""
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Repository questions",
            prompt_template="Answer {{conversation.message}}",
            agent_type="opencode",
            agent_config={"interactive": {"enabled": True}},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
        ),
        account_id=test_user.account_id,
    )
    row = crud.create(
        db_session,
        conversation_id=uuid4(),
        flow=flow,
        owner=test_user,
        policy=InteractiveSettings(enabled=True),
    )
    crud.admit(
        db_session,
        row,
        text="Remember the code word amber.",
        request_id=uuid4(),
        starting=True,
    )
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="RUNNING",
            trigger_event_details={"_conversation_id": str(row.id)},
        ),
    )
    crud.bind_execution(db_session, row, execution.id)
    db_session.commit()
    return row


@pytest.mark.parametrize("harness", ["deepseek", "pi", "codex", "claude"])
def test_unsupported_harness_explains_rejection(harness: str) -> None:
    """Only the tested OpenCode transport can be enabled."""
    with pytest.raises(ValueError, match="require hosted OpenCode"):
        service.interactive_settings(harness, {"interactive": {"enabled": True}})


def test_defaults_and_no_extra_turn_ceiling() -> None:
    """Defaults are explicit choices, with no smaller hidden conversation cap."""
    assert not service.interactive_settings("pi", {}).enabled
    policy = service.interactive_settings(
        "opencode", {"interactive": {"enabled": True}}
    )
    assert (policy.max_turns, policy.idle_timeout_seconds) == (30, 600)
    assert (
        InteractiveSettings(max_turns=100000, idle_timeout_seconds=100000).max_turns
        == 100000
    )


def test_working_waiting_and_durable_answer(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """Reload sees exactly the acknowledged input and current answer."""
    assert thread.notice == "Starting your conversation."
    crud.runtime_turn(
        db_session,
        thread,
        turn=1,
        answer="I remember amber.",
        finished=False,
        error=False,
    )
    assert thread.state == "working"
    assert service.admission_notice(thread) == service.BUSY_NOTICE
    crud.runtime_turn(
        db_session,
        thread,
        turn=1,
        answer="I remember amber.",
        finished=True,
        error=False,
    )
    assert thread.state == "waiting"
    assert "Waiting for you" in thread.notice
    assert thread.idle_deadline is not None
    projection = api.response(db_session, thread)
    assert projection.messages[0].text == "Remember the code word amber."
    assert projection.messages[0].answer == "I remember amber."


def test_turn_limit_text(db_session: Session, thread: models.FlowConversation) -> None:
    """The final answer is preserved before the configured limit ends the thread."""
    thread.max_turns = 1
    crud.runtime_turn(
        db_session, thread, turn=1, answer="Done.", finished=True, error=False
    )
    assert thread.end_requested
    assert "turn limit was reached" in thread.notice
    assert "Start a new conversation" in service.admission_notice(thread)


def test_idle_expiry_refuses_input_until_checkpoint_finishes(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """No message races into a container that has already begun stopping."""
    crud.runtime_turn(
        db_session, thread, turn=1, answer="Ready.", finished=True, error=False
    )
    thread.idle_deadline = datetime.now(UTC) - timedelta(seconds=1)
    assert "saving its checkpoint" in service.admission_notice(thread)
    reply = api.runtime_poll(row=thread, db=db_session)
    assert reply["stop"] is True
    assert "idle timeout passed" in thread.notice


def test_stopped_and_expired_checkpoint_notices(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """Only a verified checkpoint earns the stopped/resumable state."""
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    crud_flow_execution.update(
        db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
    )
    with patch.object(
        service, "resume_checkpoint", return_value={"session_id": "ses_example"}
    ):
        service.reconcile(db_session, thread)
    assert thread.state == "stopped"
    assert "Send a message to resume" in thread.notice
    thread.state = "working"
    with patch.object(
        service, "resume_checkpoint", side_effect=ValueError(service.EXPIRED_NOTICE)
    ):
        service.reconcile(db_session, thread)
    assert thread.state == "failed"
    assert "missing or expired" in thread.notice
    assert "Start a new conversation" in thread.notice


def test_resume_uses_prior_native_identity(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """A resumed execution may restore only its own conversation's prior run."""
    prior_id = thread.execution_id
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=thread.flow_id, status="PENDING")
    )
    crud.admit(
        db_session,
        thread,
        text="What was the code word?",
        request_id=uuid4(),
        starting=True,
    )
    crud.bind_execution(db_session, thread, execution.id)
    context = {
        "account_id": str(thread.account_id),
        "execution_id": str(execution.id),
        "trigger_event_data": {
            "_conversation_id": str(thread.id),
            "_resume": {"execution_id": str(prior_id)},
        },
    }
    with patch.object(
        service,
        "resume_checkpoint",
        return_value={
            "session_id": "ses_example",
            "artifact_reference": {"artifact_id": "example"},
        },
    ) as checkpoint:
        result = service.bound_resume(db_session, context)
    assert result["session_id"] == "ses_example"
    assert checkpoint.call_args.args[1].execution_id == prior_id
    assert context["checkpoint_resume_authorized"] is True
    context["trigger_event_data"]["_resume"]["execution_id"] = str(uuid4())
    with pytest.raises(ValueError, match="does not belong"):
        service.bound_resume(db_session, context)


def test_permission_denied_names_owner(
    db_session: Session, test_user: models.User, thread: models.FlowConversation
) -> None:
    """Other readers cannot send or end the conversation."""
    reader = SimpleNamespace(id=uuid4(), account_id=test_user.account_id)
    with (
        patch.object(api, "visible_flow"),
        patch.object(api, "ensure_permission_in_oss"),
    ):
        with pytest.raises(HTTPException) as error:
            api.conversation(db_session, reader, thread.id, write=True)
    assert error.value.status_code == 403
    assert "Only the person who started" in error.value.detail


def test_metering_for_each_turn_accumulates_across_resume(
    db_session: Session, test_user: models.User, thread: models.FlowConversation
) -> None:
    """Each gateway turn is attributed and the next container inherits spending."""
    for turn in range(1, 4):
        if turn > 1:
            execution = crud_flow_execution.create(
                db_session,
                FlowExecutionCreate(
                    flow_id=thread.flow_id,
                    status="RUNNING",
                    trigger_event_details={"_conversation_id": str(thread.id)},
                ),
            )
            crud.admit(
                db_session,
                thread,
                text=f"Question {turn}",
                request_id=uuid4(),
                starting=True,
            )
            crud.bind_execution(db_session, thread, execution.id)
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            flow_id=str(thread.flow_id),
            flow_execution_id=str(thread.execution_id),
            model_alias="deepseek/flash",
            provider_name="deepseek",
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
            estimated_cost=0.01,
            cost_source="catalog",
        )
    usage = get_execution_usage(db_session, thread.execution_id)
    assert usage.total_tokens == 300
    assert usage.turns == 3
    assert usage.cost_usd == pytest.approx(0.03)
    violation = evaluate_execution_limits(ExecutionLimits(max_total_tokens=300), usage)
    assert violation is not None
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    crud_flow_execution.update(
        db_session, db_obj=execution, obj_in={"status": "FAILED"}
    )
    service.reconcile(db_session, thread)
    assert "hit a limit" in thread.notice


def test_start_audit_attributes_initiator(
    db_session: Session, test_user: models.User, thread: models.FlowConversation
) -> None:
    """The durable audit row records the initiating human, not an account default."""
    # Other application lifecycle tests may have shut down the global service.
    with patch("preloop.utils.audit._get_audit_service", return_value=AuditService()):
        api.audit(db_session, test_user, thread, "started")
    row = (
        db_session.query(models.AuditLog)
        .filter_by(account_id=test_user.account_id, user_id=test_user.id)
        .order_by(models.AuditLog.timestamp.desc())
        .first()
    )
    assert row is not None
    assert row.user_id == test_user.id
    assert str(thread.id) in str(row.details)


def test_capability_cannot_cross_threads(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """Runtime capabilities bind both the execution and conversation."""
    env = service.runtime_environment(
        db_session,
        {
            "model_gateway_enabled": True,
            "account_id": str(thread.account_id),
            "flow_id": str(thread.flow_id),
            "execution_id": str(thread.execution_id),
            "trigger_event_data": {"_conversation_id": str(thread.id)},
        },
    )
    with pytest.raises(HTTPException) as error:
        api.runtime_conversation(
            uuid4(), "Bearer " + env["PRELOOP_CONVERSATION_TOKEN"], db_session
        )
    assert error.value.status_code == 401


def test_expired_native_artifact_refuses_resume(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """An explicit session ID cannot make an expired artifact resumable."""
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in={"cli_session": {"agent_type": "opencode", "session_id": "ses_example"}},
    )
    expired = SimpleNamespace(
        ciphertext=b"synthetic", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with patch.object(service.flow_artifact, "latest", return_value=expired):
        with pytest.raises(ValueError, match="missing or expired"):
            service.resume_checkpoint(db_session, thread)


def test_exhausted_wall_clock_refuses_resume_before_dispatch(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """A stopped conversation never receives a new full execution allowance."""
    flow = crud_flow.get(db_session, id=thread.flow_id)
    flow.timeout_seconds = 60
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    now = datetime.now(UTC)
    crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "start_time": now - timedelta(seconds=61),
            "end_time": now,
        },
    )
    assert service.consumed_seconds(db_session, thread.id) >= 61
    assert "wall-clock limit" in service.budget_notice(db_session, thread, flow)


def test_runtime_token_belongs_to_initiator_not_account_default(
    db_session: Session, test_user: models.User, thread: models.FlowConversation
) -> None:
    """Metering credentials retain the conversation owner on every container."""
    from preloop.models.crud import crud_api_key, crud_user
    from preloop.services.flow_runtime_token import create_flow_runtime_token

    owner = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": f"jane-{uuid4().hex}@example.com",
            "username": f"jane-{uuid4().hex}",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "synthetic",
            "user_source": "local",
        },
    )
    flow = crud_flow.get(db_session, id=thread.flow_id)
    token, key_id = create_flow_runtime_token(
        db_session, flow=flow, execution_id=thread.execution_id, owner_user_id=owner.id
    )
    assert token and key_id
    key = crud_api_key.get(db_session, id=key_id)
    assert key.user_id == owner.id


def test_start_reservation_reuses_one_identity(
    db_session: Session, test_user: models.User, thread: models.FlowConversation
) -> None:
    """A retried start cannot insert a second conversation or change ownership."""
    flow = crud_flow.get(db_session, id=thread.flow_id)
    repeated = crud.create(
        db_session,
        conversation_id=thread.id,
        flow=flow,
        owner=test_user,
        policy=InteractiveSettings(enabled=True),
    )
    assert repeated.id == thread.id
    assert repeated.turns == 1


@pytest.fixture
def conversation_client(db_session: Session, test_user: models.User) -> TestClient:
    """Exercise the public wire contract with the same database transaction."""
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    def database() -> Session:
        return db_session

    def user() -> models.User:
        return test_user

    app.dependency_overrides[get_db_session] = database
    app.dependency_overrides[get_current_active_user] = user
    return TestClient(app)


def test_http_read_busy_notice_and_runtime_auth(
    conversation_client: TestClient, thread: models.FlowConversation
) -> None:
    """The public API returns durable content and rejects busy or forged writes."""
    path = f"/api/v1/flow-conversations/{thread.id}"
    response = conversation_client.get(path)
    assert response.status_code == 200
    assert response.json()["can_write"] is True
    assert response.json()["messages"][0]["text"] == "Remember the code word amber."
    response = conversation_client.post(
        path + "/messages",
        json={"request_id": str(uuid4()), "message": "A follow-up too soon"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == service.BUSY_NOTICE
    response = conversation_client.post(
        path + "/runtime",
        headers={"Authorization": "Bearer synthetic-account-token"},
        json={"turn": 1, "answer": "Forged answer"},
    )
    assert response.status_code == 401


def test_http_expired_checkpoint_never_dispatches_new_session(
    conversation_client: TestClient,
    db_session: Session,
    thread: models.FlowConversation,
) -> None:
    """A visible 410 replaces silent history loss on an expired conversation."""
    crud.save(db_session, thread, state="stopped")
    before = crud.executions(db_session, thread.id)
    response = conversation_client.post(
        f"/api/v1/flow-conversations/{thread.id}/messages",
        json={
            "request_id": str(uuid4()),
            "message": "Continue with the earlier context",
        },
    )
    assert response.status_code == 410
    assert "missing or expired" in response.json()["detail"]
    assert crud.executions(db_session, thread.id) == before


def test_user_end_mid_turn_preserves_partial_reply(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """An explicit end is represented as ended, with the interrupted text intact."""
    crud.save(db_session, thread, end_requested=True, stop_reason="ended")
    crud.runtime_turn(
        db_session,
        thread,
        turn=1,
        answer="Partial answer",
        finished=True,
        error=False,
        interrupted=True,
    )
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    crud_flow_execution.update(
        db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
    )
    service.reconcile(db_session, thread)
    assert thread.state == "ended"
    assert "You ended this conversation" in thread.notice
    message = crud.messages(db_session, thread.id)[0]
    assert message.answer == "Partial answer"
    assert message.status == "interrupted"


def test_approval_keeps_interactive_runtime_live(
    db_session: Session, thread: models.FlowConversation
) -> None:
    """Approval polling keeps its decision gate without an unrelated park resume."""
    with patch.object(
        crud_flow_execution, "_approval_still_pending", return_value=True
    ):
        assert (
            crud_flow_execution.request_park(
                db_session,
                execution_id=thread.execution_id,
                approval_request_id=uuid4(),
            )
            is False
        )
    execution = crud_flow_execution.get(db_session, id=thread.execution_id)
    assert execution.status == "RUNNING"
    assert execution.park_request_id is None


def test_http_start_keeps_first_message_and_owner(
    conversation_client: TestClient,
    db_session: Session,
    test_user: models.User,
    thread: models.FlowConversation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting from any client produces the same durable thread and attribution."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(api.settings, "flow_artifact_direct_upload", True)

    async def launch(
        db: Session,
        row: models.FlowConversation,
        user: models.User,
        native: dict | None = None,
    ) -> None:
        assert user.id == test_user.id
        execution = crud_flow_execution.create(
            db, FlowExecutionCreate(flow_id=row.flow_id, status="PENDING")
        )
        crud.bind_execution(db, row, execution.id)
        db.commit()

    request_id = str(uuid4())
    body = {"request_id": request_id, "message": "Inspect the README first."}
    with patch.object(api, "dispatch", new=AsyncMock(side_effect=launch)) as dispatch:
        first = conversation_client.post(
            f"/api/v1/flows/{thread.flow_id}/conversations", json=body
        )
        retry = conversation_client.post(
            f"/api/v1/flows/{thread.flow_id}/conversations", json=body
        )
    assert first.status_code == 200, first.text
    assert retry.status_code == 200, retry.text
    assert dispatch.await_count == 1
    assert first.json()["id"] == request_id
    assert first.json()["owner_user_id"] == str(test_user.id)
    assert first.json()["messages"][0]["text"] == "Inspect the README first."
