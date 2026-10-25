"""Copilot hook sessions resolve to the runner session hosting them (#1482)."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_runtime_session_activity
from preloop.services import runner_sessions as svc
from preloop.services.agent_session_lineage import (
    caller_session_ids,
    register_session_start,
    resolve_external_session,
)

PRINCIPAL_TYPE = "agent"


@pytest.fixture
def runner(db_session: Session, test_user: models.User) -> models.FlowRunner:
    row = models.FlowRunner(
        account_id=test_user.account_id,
        registered_by_user_id=test_user.id,
        name="Jane laptop",
        hostname="jane-laptop.example.com",
        token_hash="test-token-hash",
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _start(
    db: Session,
    runner: models.FlowRunner,
    user: models.User,
    *,
    harness: str = "copilot_cli",
) -> models.RunnerRemoteSession:
    return svc.request_session(
        db,
        runner=runner,
        actor=user,
        harness=harness,
        model="auto",
        workspace={"kind": "authorized_directory", "id": "dir_1"},
        workspace_label="ims",
    )


def _state(row: models.RunnerRemoteSession, state: str, **extra: Any) -> dict:
    return {
        "type": "session_state",
        "remote_session_id": str(row.id),
        "state": state,
        **extra,
    }


def _go_idle(
    db: Session, runner: models.FlowRunner, row: models.RunnerRemoteSession, hs: str
) -> None:
    svc.apply_runner_session_message(db, runner, _state(row, "starting"))
    svc.apply_runner_session_message(
        db, runner, _state(row, "idle", harness_session_id=hs)
    )
    db.refresh(row)


def _hook_start(db: Session, account_id: Any, hs: str, **kwargs: Any) -> Any:
    return register_session_start(
        db,
        account_id=account_id,
        principal_type=PRINCIPAL_TYPE,
        principal_id="agent-1",
        principal_name="Copilot",
        external_session_id=hs,
        agent_kind="Copilot CLI",
        cwd="/home/jane/ims",
        **kwargs,
    )


def _account_sessions(db: Session, account_id: Any) -> list[models.RuntimeSession]:
    return (
        db.query(models.RuntimeSession)
        .filter(models.RuntimeSession.account_id == account_id)
        .all()
    )


def test_state_then_hook_resolves_to_runner_session(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)

    result = _hook_start(db_session, test_user.account_id, hs)

    assert result.runtime_session_id == str(row.runtime_session_id)
    assert result.created is False
    [only] = _account_sessions(db_session, test_user.account_id)
    assert only.id == row.runtime_session_id
    assert only.session_reference == hs
    # The workspace label stays; the host path is not stored.
    assert only.cwd == "ims"
    assert resolve_external_session(
        db_session,
        account_id=test_user.account_id,
        external_session_id=hs,
        principal_type=PRINCIPAL_TYPE,
        principal_id="agent-1",
    ) == [only]
    # Registration can attach hooks to a hosted session without changing its
    # principal. An MCP caller must still authenticate as that principal.
    assert (
        caller_session_ids(
            db_session,
            account_id=test_user.account_id,
            principal_type=PRINCIPAL_TYPE,
            principal_id="agent-1",
            headers={"x-mcp-client-session-id": hs},
        )
        == []
    )
    assert caller_session_ids(
        db_session,
        account_id=test_user.account_id,
        principal_type="user",
        principal_id=str(test_user.id),
        headers={"x-mcp-client-session-id": hs},
    ) == [only.id]


def test_hook_then_state_folds_hook_session(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    hook = _hook_start(db_session, test_user.account_id, hs)
    hook_id = uuid.UUID(hook.runtime_session_id)
    assert hook_id != row.runtime_session_id
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=hook_id,
        server_name="copilot_cli",
        tool_name="bash",
        status="approved",
    )
    child = _hook_start(
        db_session,
        test_user.account_id,
        str(uuid.uuid4()),
        parent_session_id=hook_id,
    )
    db_session.commit()

    _go_idle(db_session, runner, row, hs)

    sessions = _account_sessions(db_session, test_user.account_id)
    assert {s.id for s in sessions} == {
        row.runtime_session_id,
        uuid.UUID(child.runtime_session_id),
    }
    activity = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(models.RuntimeSessionActivity.activity_type == "tool_call")
        .one()
    )
    assert activity.runtime_session_id == row.runtime_session_id
    child_row = db_session.get(
        models.RuntimeSession, uuid.UUID(child.runtime_session_id)
    )
    db_session.refresh(child_row)
    assert child_row.parent_session_id == row.runtime_session_id
    # A later hook call lands on the runner session, not a new row.
    again = _hook_start(db_session, test_user.account_id, hs)
    assert again.runtime_session_id == str(row.runtime_session_id)
    started = [a for a in svc_actions(db_session, row)]
    assert "runner_session.started" in started


def svc_actions(db: Session, row: models.RunnerRemoteSession) -> list[str]:
    return [
        a.action
        for a in db.query(models.AuditLog)
        .filter(models.AuditLog.resource_id == str(row.id))
        .all()
    ]


def test_other_account_does_not_link(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )

    result = _hook_start(db_session, other.id, hs)

    assert result.runtime_session_id != str(row.runtime_session_id)
    assert result.created is True


def test_ended_runner_session_does_not_link(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    svc.apply_runner_session_message(
        db_session, runner, _state(row, "ended", end_reason="stopped_by_actor")
    )
    db_session.refresh(row)
    assert not row.is_live

    result = _hook_start(db_session, test_user.account_id, hs)

    assert result.runtime_session_id != str(row.runtime_session_id)
    assert result.created is True


@pytest.fixture
def endpoint_db(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    """Point the permission endpoint's own sessions at the test transaction."""
    from sqlalchemy.orm import sessionmaker

    from preloop.api.endpoints import agent_permission

    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    monkeypatch.setattr(agent_permission, "get_session_factory", lambda: factory)
    return db_session


def _identity(db: Session, user: models.User) -> Any:
    from preloop.api.endpoints import agent_permission

    key = models.ApiKey(
        account_id=user.account_id,
        user_id=user.id,
        name=f"Copilot hook {uuid.uuid4().hex[:8]}",
        key=f"synthetic-{uuid.uuid4().hex}",
        is_active=True,
    )
    db.add(key)
    db.flush()
    return agent_permission.PermissionIdentity(
        account_id=str(user.account_id),
        user_id=user.id,
        api_key_id=key.id,
        managed_agent_id=None,
        runtime_session_id=None,
        managed_agent_name="Copilot",
        runtime_principal_type=PRINCIPAL_TYPE,
        runtime_principal_id="agent-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "source", "hook_event_name", "carries_notes"),
    [
        ("copilot_cli", "copilot_cli", None, False),
        ("cursor_cli", "cursor", "preToolUse", True),
    ],
)
async def test_permission_hook_lands_on_runner_session(
    endpoint_db,
    runner,
    test_user,
    monkeypatch,
    harness,
    source,
    hook_event_name,
    carries_notes,
):
    from preloop.api.endpoints import agent_permission

    db_session = endpoint_db
    row = _start(db_session, runner, test_user, harness=harness)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    identity = _identity(db_session, test_user)
    monkeypatch.setattr(
        agent_permission, "_resolve_permission_identity", lambda token: identity
    )
    from fastapi import Response

    from preloop.api.endpoints.approval_requests import list_approval_requests
    from preloop.models.crud import crud_agent_control_command

    note = crud_agent_control_command.create_note(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=None,
        runtime_session_id=row.runtime_session_id,
        note_id=uuid.uuid4().hex[:16],
        body="check the tests first",
        envelope={},
        author_display="Admin User",
        author_auth_method="jwt",
        created_by_user_id=test_user.id,
        expires_at=datetime.now(timezone.utc).replace(year=2100),
        commit=False,
    )
    db_session.flush()
    seen: dict = {}

    async def decide(**kwargs: Any) -> tuple:
        # Stands in for the policy engine: record the approval the way it
        # does, on the session it was given.
        seen.update(kwargs)
        workflow = models.ApprovalWorkflow(
            account_id=test_user.account_id, name=f"wf-{uuid.uuid4().hex[:6]}"
        )
        tool = models.ToolConfiguration(
            account_id=test_user.account_id, tool_name="bash", tool_source="builtin"
        )
        db_session.add_all([workflow, tool])
        db_session.flush()
        approval = models.ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool.id,
            approval_workflow_id=workflow.id,
            tool_name="bash",
            tool_args={},
            status="approved",
            runtime_session_id=kwargs["runtime_session_id"],
        )
        db_session.add(approval)
        db_session.flush()
        return ("allow", "approved", str(approval.id), False)

    monkeypatch.setattr(agent_permission, "request_agent_permission", decide)

    result = await agent_permission.agent_permission_check(
        agent_permission.AgentPermissionCheckRequest(
            source=source,
            hook_event_name=hook_event_name,
            tool_name="bash",
            tool_input={"command": "ls"},
            session_id=hs,
            cwd="/home/jane/ims",
        ),
        authorization="Bearer token",
    )

    assert seen["runtime_session_id"] == row.runtime_session_id
    listed = list_approval_requests(
        Response(),
        status=None,
        execution_id=None,
        runtime_session_id=row.runtime_session_id,
        tool_name=None,
        q=None,
        limit=50,
        skip=0,
        current_user=test_user,
        db=db_session,
    )
    assert [str(a.id) for a in listed] == [result.request_id]
    db_session.refresh(note)
    if carries_notes:
        assert result.operator_note and "check the tests first" in result.operator_note
        assert note.status == "delivered"
        assert note.delivered_at is not None
        assert note.delivery_channel == "hook"
    else:
        # Copilot's permission response has no model-visible note carrier.
        # Keep the note available for a later carrying model transport.
        assert result.operator_note is None
        assert note.status == "pending"
        assert note.claimed_at is None
        assert note.delivered_at is None
        assert note.acked_at is None
        assert note.delivery_channel is None
    activity = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(models.RuntimeSessionActivity.activity_type == "tool_call")
        .one()
    )
    assert activity.runtime_session_id == row.runtime_session_id
    assert activity.metadata_["approval_request_id"]
    assert len(_account_sessions(db_session, test_user.account_id)) == 1
    runtime = db_session.get(models.RuntimeSession, row.runtime_session_id)
    db_session.refresh(runtime)
    assert runtime.cwd == "ims"
    # Another user's hook in the same account does not reach this session.
    other = models.User(
        account_id=test_user.account_id,
        email="other@example.com",
        username="other",
        hashed_password="x",
        user_source="local",
    )
    db_session.add(other)
    db_session.flush()
    assert (
        agent_permission._origin_runtime_session_id(
            _identity(db_session, other), "copilot_cli", hs
        )
        is None
    )


def _user(db: Session, account_id: Any, name: str) -> models.User:
    user = models.User(
        account_id=account_id,
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        username=f"{name}-{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        user_source="local",
    )
    db.add(user)
    db.flush()
    return user


def test_admin_started_session_links_runner_owner_hook_only(
    db_session, runner, test_user
):
    from preloop.services.agent_session_lineage import runner_session_for_external

    owner = _user(db_session, test_user.account_id, "owner")
    runner.registered_by_user_id = owner.id
    db_session.flush()
    row = _start(db_session, runner, test_user)  # the admin starts it
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    stranger = _user(db_session, test_user.account_id, "member")

    linked = runner_session_for_external(
        db_session,
        account_id=test_user.account_id,
        external_session_id=hs,
        actor_user_id=owner.id,
    )
    refused = runner_session_for_external(
        db_session,
        account_id=test_user.account_id,
        external_session_id=hs,
        actor_user_id=stranger.id,
    )

    assert linked is not None and linked.id == row.runtime_session_id
    assert refused is None
    start = _hook_start(db_session, test_user.account_id, hs, actor_user_id=owner.id)
    assert start.runtime_session_id == str(row.runtime_session_id)


def test_session_event_requires_runner_actor_or_owner(
    endpoint_db: Session,
    runner: models.FlowRunner,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.api.endpoints import agent_permission
    from preloop.services import session_hook_events

    db = endpoint_db
    owner = _user(db, test_user.account_id, "hook-owner")
    runner.registered_by_user_id = owner.id
    row = _start(db, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db, runner, row, hs)
    stranger = _user(db, test_user.account_id, "hook-stranger")
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_hook_events, "emit_account_event", events.append)
    payload = agent_permission.AgentSessionEventRequest(
        session_id=hs, hook_event_name="SessionEnd"
    )
    assert not agent_permission._record_session_event(_identity(db, stranger), payload)
    runtime = db.get(models.RuntimeSession, row.runtime_session_id)
    db.refresh(runtime)
    assert runtime.ended_at is None
    assert events == []
    assert agent_permission._record_session_event(_identity(db, owner), payload)
    db.refresh(runtime)
    assert runtime.ended_at is not None
    assert [event["type"] for event in events] == ["runtime_session_hook_ended"]


def _row_fields(db: Session, row: models.RunnerRemoteSession) -> tuple:
    runtime = db.get(models.RuntimeSession, row.runtime_session_id)
    db.refresh(runtime)
    return (
        runtime.session_reference == row.harness_session_id,
        runtime.cwd,
        runtime.runtime_principal_name,
        runtime.title,
    )


def test_late_hook_row_is_folded_by_a_later_frame(db_session, runner, test_user):
    """A sibling made after the first frame is folded by the next one."""
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    late = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type=PRINCIPAL_TYPE,
        session_source_id=f"agent-1:{hs}",
        runtime_principal_type=PRINCIPAL_TYPE,
        runtime_principal_id=f"agent-1:{hs}",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(late)
    db_session.commit()

    svc.apply_runner_session_message(
        db_session, runner, _state(row, "running", harness_session_id=hs)
    )

    assert [s.id for s in _account_sessions(db_session, test_user.account_id)] == [
        row.runtime_session_id
    ]


def test_both_orders_leave_the_same_runner_session_fields(
    db_session, runner, test_user
):
    state_first = _start(db_session, runner, test_user)
    hs1 = str(uuid.uuid4())
    _go_idle(db_session, runner, state_first, hs1)
    _hook_start(db_session, test_user.account_id, hs1)

    hook_first = _start(db_session, runner, test_user)
    hs2 = str(uuid.uuid4())
    _hook_start(db_session, test_user.account_id, hs2)
    _go_idle(db_session, runner, hook_first, hs2)

    assert _row_fields(db_session, state_first) == _row_fields(db_session, hook_first)
    assert _row_fields(db_session, state_first)[0] is True


def _late_sibling(db: Session, account_id: Any, hs: str) -> None:
    db.add(
        models.RuntimeSession(
            account_id=account_id,
            session_source_type=PRINCIPAL_TYPE,
            session_source_id=f"agent-1:{hs}",
            runtime_principal_type=PRINCIPAL_TYPE,
            runtime_principal_id=f"agent-1:{hs}",
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def _only_session_ended(db: Session, row: models.RunnerRemoteSession) -> None:
    sessions = _account_sessions(db, row.account_id)
    assert [s.id for s in sessions] == [row.runtime_session_id]
    db.refresh(sessions[0])
    assert sessions[0].ended_at is not None


def test_ended_frame_folds_a_late_sibling_first(db_session, runner, test_user):
    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    _late_sibling(db_session, test_user.account_id, hs)

    svc.apply_runner_session_message(
        db_session,
        runner,
        _state(row, "ended", harness_session_id=hs, end_reason="stopped_by_actor"),
    )

    _only_session_ended(db_session, row)


def test_sweeper_end_folds_a_late_sibling(db_session, runner, test_user):
    from datetime import timedelta

    row = _start(db_session, runner, test_user)
    hs = str(uuid.uuid4())
    _go_idle(db_session, runner, row, hs)
    _late_sibling(db_session, test_user.account_id, hs)
    runner.last_heartbeat = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(runner)
    db_session.commit()
    later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)

    assert svc.sweep_runner_sessions(db_session, now=later) == 1

    db_session.refresh(row)
    assert row.end_reason == "runner_offline"
    _only_session_ended(db_session, row)
