"""Hook permission checks feed `preloop sessions attach` (#1149).

A durable hook credential names an agent but no session. These tests prove
that such a check is attributed to the agent's open session, that a note sent
to that session (or to the hook's own conversation session) is delivered, and
that the call lands on the timeline and the live stream with an argument
summary that never carries argument values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.orm import sessionmaker

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_agent_control_command,
    crud_api_key,
    crud_managed_agent,
    crud_user,
)
from preloop.services import account_realtime


@pytest.fixture
def hook_agent(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    factory = sessionmaker(bind=db_engine)
    unique = uuid4().hex
    now = datetime.now(timezone.utc)
    with factory() as db:
        account = crud_account.create(db, obj_in={"organization_name": unique})
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account.id,
                "email": f"{unique}@example.com",
                "username": unique,
                "full_name": "Admin User",
                "hashed_password": "unused-test-password",
                "is_active": True,
                "email_verified": True,
            },
        )
        agent = crud_managed_agent.create(
            db,
            obj_in={
                "account_id": account.id,
                "owner_user_id": user.id,
                "agent_kind": "claude_code",
                "session_source_type": "claude_code",
                "session_source_id": unique,
                "display_name": "Claude Code",
                "lifecycle_updated_at": now,
                "last_seen_at": now,
            },
        )
        session = models.RuntimeSession(
            account_id=account.id,
            session_source_type="claude_code",
            session_source_id=f"{unique}-identity",
            runtime_principal_type="claude_code",
            runtime_principal_id=unique,
            started_at=now.replace(tzinfo=None),
        )
        db.add(session)
        db.flush()
        agent.runtime_session_id = session.id
        db.add(agent)
        # The durable hook credential: an agent, no session.
        _, token = crud_api_key.create_runtime_key(
            db,
            name=unique,
            account_id=account.id,
            user_id=user.id,
            context_data={"managed_agent_id": str(agent.id)},
        )
        db.commit()
        ids = {
            "account_id": account.id,
            "user_id": user.id,
            "agent_id": agent.id,
            "session_id": session.id,
            "principal_id": unique,
            "token": token,
        }
    events: list[dict] = []
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: factory)
    monkeypatch.setattr(endpoint.settings, "preloop_url", "https://example.invalid")
    monkeypatch.setattr(account_realtime, "emit_account_event", events.append)
    try:
        yield {"factory": factory, "events": events, **ids}
    finally:
        with factory() as db:
            db.execute(
                delete(models.Account).where(models.Account.id == ids["account_id"])
            )
            db.commit()


def _note(hook_agent: dict, session_id: Any, body: str) -> None:
    with hook_agent["factory"]() as db:
        crud_agent_control_command.create_note(
            db,
            account_id=hook_agent["account_id"],
            managed_agent_id=hook_agent["agent_id"],
            runtime_session_id=session_id,
            note_id=uuid4().hex[:16],
            body=body,
            envelope={},
            author_display="Admin User",
            author_auth_method="jwt",
            created_by_user_id=hook_agent["user_id"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def _decide(captured: dict, decision: str = "allow", request_id: Any = None):
    async def decide(**kwargs: Any) -> tuple[str, str, Any, bool]:
        captured.update(kwargs)
        return decision, "Approved via Preloop.", request_id, False

    return decide


def _tool_rows(
    hook_agent: dict, session_id: Any
) -> list[models.RuntimeSessionActivity]:
    with hook_agent["factory"]() as db:
        return (
            db.query(models.RuntimeSessionActivity)
            .filter(
                models.RuntimeSessionActivity.runtime_session_id == session_id,
                models.RuntimeSessionActivity.activity_type == "tool_call",
            )
            .all()
        )


@pytest.mark.asyncio
async def test_durable_hook_check_uses_the_agents_session_end_to_end(
    hook_agent: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    monkeypatch.setattr(endpoint, "request_agent_permission", _decide(captured))
    _note(hook_agent, hook_agent["session_id"], "Stop refactoring the tests.")

    response = await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(
            tool_name="Bash",
            tool_input={"command": "export TOKEN=super-secret-value && ls"},
            source="claude_code",
            session_id="conv-1",
            cwd="/work/alpha",
        ),
        authorization="Bearer " + hook_agent["token"],
    )

    session_id = hook_agent["session_id"]
    assert response.decision == "allow"
    assert captured["runtime_session_id"] == session_id
    assert (
        response.operator_note
        and "Stop refactoring the tests." in response.operator_note
    )

    (row,) = _tool_rows(hook_agent, session_id)
    assert (row.tool_name, row.server_name, row.status) == (
        "Bash",
        "claude_code",
        "allowed",
    )
    assert row.metadata_["origin"] == "native_hook"
    assert set(row.metadata_["arguments_summary"]) == {"command"}
    assert "super-secret-value" not in str(row.metadata_)

    with hook_agent["factory"]() as db:
        assert db.get(models.RuntimeSession, session_id).cwd == "/work/alpha"

    live = [e for e in hook_agent["events"] if e["type"] == "runtime_session_updated"]
    tool_events = [e for e in live if e["payload"].get("tool_name") == "Bash"]
    note_events = [
        e for e in live if e["payload"].get("activity_type") == "agent_control_message"
    ]
    assert len(tool_events) == 1 and len(note_events) == 1
    assert tool_events[0]["runtime_session_id"] == str(session_id)
    assert tool_events[0]["payload"]["activity_id"] == str(row.id)
    assert "super-secret-value" not in str(tool_events[0])
    assert note_events[0]["payload"]["metadata"]["author_display"] == "Admin User"


@pytest.mark.asyncio
async def test_note_to_the_hooks_own_conversation_session_is_delivered(
    hook_agent: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(endpoint, "request_agent_permission", _decide({}))
    with hook_agent["factory"]() as db:
        origin = models.RuntimeSession(
            account_id=hook_agent["account_id"],
            session_source_type="claude_code",
            session_source_id=f"{hook_agent['principal_id']}:conv-2",
            runtime_principal_type="claude_code",
            runtime_principal_id=hook_agent["principal_id"],
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(origin)
        db.commit()
        origin_id = origin.id
    _note(hook_agent, origin_id, "Use the eu-west-1 cluster.")

    response = await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(
            tool_name="Read", source="claude_code", session_id="conv-2"
        ),
        authorization="Bearer " + hook_agent["token"],
    )

    assert response.operator_note and "eu-west-1" in response.operator_note
    # The call is recorded on the conversation the hook named.
    assert len(_tool_rows(hook_agent, origin_id)) == 1
    assert _tool_rows(hook_agent, hook_agent["session_id"]) == []


@pytest.mark.asyncio
async def test_an_ended_agent_session_is_not_used(
    hook_agent: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    monkeypatch.setattr(endpoint, "request_agent_permission", _decide(captured))
    with hook_agent["factory"]() as db:
        session = db.get(models.RuntimeSession, hook_agent["session_id"])
        session.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()

    await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(tool_name="Read", source="claude_code"),
        authorization="Bearer " + hook_agent["token"],
    )

    assert captured["runtime_session_id"] is None
    assert _tool_rows(hook_agent, hook_agent["session_id"]) == []


@pytest.mark.asyncio
async def test_codex_permission_request_after_pre_tool_use_is_not_recorded_twice(
    hook_agent: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(endpoint, "request_agent_permission", _decide({}))
    for phase in ("pre_tool_use", "permission_request"):
        await endpoint.agent_permission_check(
            endpoint.AgentPermissionCheckRequest(
                tool_name="shell", source="codex_cli", evaluation_phase=phase
            ),
            authorization="Bearer " + hook_agent["token"],
        )

    rows = _tool_rows(hook_agent, hook_agent["session_id"])
    assert [row.metadata_["evaluation_phase"] for row in rows] == ["pre_tool_use"]


@pytest.mark.parametrize(
    ("decision", "request_id", "timed_out", "status"),
    [
        ("allow", None, False, "allowed"),
        ("deny", None, False, "denied"),
        ("allow", "approval-1", False, "approved"),
        ("deny", "approval-1", False, "declined"),
        ("deny", "approval-1", True, "timed_out"),
    ],
)
def test_native_tool_status(
    decision: str, request_id: Any, timed_out: bool, status: str
) -> None:
    assert endpoint._native_tool_status(decision, request_id, timed_out) == status


def test_a_failed_second_claim_keeps_the_notes_the_first_one_took(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim commits its notes as delivered, so they must reach the agent."""

    class _Session:
        def __enter__(self) -> "_Session":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    calls: list[Any] = []

    def claim(db: Any, **kwargs: Any) -> list[str]:
        calls.append(kwargs["runtime_session_id"])
        if len(calls) == 2:
            raise RuntimeError("store unavailable")
        return ["note-from-first-session"]

    monkeypatch.setattr(endpoint, "get_session_factory", lambda: _Session)
    monkeypatch.setattr(endpoint.operator_notes, "claim_pending_notes", claim)
    monkeypatch.setattr(
        endpoint.operator_notes, "render_notes_block", lambda notes: "|".join(notes)
    )
    identity = endpoint.PermissionIdentity(
        account_id=str(uuid4()),
        user_id=uuid4(),
        api_key_id=uuid4(),
        managed_agent_id=uuid4(),
        runtime_session_id=uuid4(),
        managed_agent_name="Agent",
    )
    origin = str(uuid4())

    rendered = endpoint._claim_operator_note(identity, origin_session_id=origin)

    assert calls == [str(identity.runtime_session_id), origin]
    assert rendered == "note-from-first-session"
