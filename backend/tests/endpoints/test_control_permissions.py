"""Permission consistency for account, agent, desktop and runner controls."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import (
    account,
    agent_control,
    desktop,
    runner_sessions,
    runners,
    websockets,
)
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.plugins import account_hooks
from preloop.utils import permissions
from tests.endpoints.test_websocket_command_auth import (
    _execution_socket_command,
    _ws_env,
)
from tests.permission_helpers import grant_permission

RESOURCE_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"

# Valid request bodies ensure these requests reach the permission decision.
CONTROL_ROUTES = [
    (
        "PUT",
        "/account/session-artifacts/settings",
        {"audio_storage_enabled": True},
        "manage_policies",
    ),
    ("PUT", f"/agents/{RESOURCE_ID}/model-bindings", {"bindings": []}, "manage_agents"),
    ("PUT", "/account/governance-defaults", {}, "manage_policies"),
    ("PUT", f"/account/governance/flows/{RESOURCE_ID}", {}, "edit_flows"),
    ("DELETE", f"/account/governance/flows/{RESOURCE_ID}", None, "edit_flows"),
    ("PUT", f"/agents/{RESOURCE_ID}/governance", {}, "manage_agents"),
    ("POST", "/agents", {"display_name": "Example agent"}, "manage_agents"),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/credentials",
        {"name": "Example credential"},
        "manage_agents",
    ),
    ("DELETE", f"/agents/{RESOURCE_ID}/credentials/{OTHER_ID}", None, "manage_agents"),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/enrollments",
        {"enrollment_type": "manual"},
        "manage_agents",
    ),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/enrollments/{OTHER_ID}/validate",
        {},
        "manage_agents",
    ),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/enrollments/{OTHER_ID}/restore",
        {},
        "manage_agents",
    ),
    (
        "PATCH",
        f"/agents/{RESOURCE_ID}",
        {"lifecycle_action": "suspend"},
        "manage_agents",
    ),
    ("DELETE", f"/agents/{RESOURCE_ID}", None, "manage_agents"),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/rekey",
        {"new_session_source_id": "example-agent"},
        "manage_agents",
    ),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/merge",
        {"duplicate_agent_id": OTHER_ID},
        "manage_agents",
    ),
    (
        "PATCH",
        f"/runtime-sessions/{RESOURCE_ID}",
        {"action": "end"},
        "manage_runtime_sessions",
    ),
    (
        "PUT",
        "/attention/dismissals/example-item",
        {"fingerprint": "example", "reason": "expected"},
        "manage_agents",
    ),
    ("DELETE", "/attention/dismissals/example-item", None, "manage_agents"),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/control/commands",
        {"message": "Continue"},
        "control_managed_agent",
    ),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/control/prompts",
        {"message": "Continue"},
        "control_managed_agent",
    ),
    (
        "POST",
        f"/agents/{RESOURCE_ID}/control/voice-transcripts",
        {"transcript": "Continue"},
        "control_managed_agent",
    ),
    ("POST", f"/agents/{RESOURCE_ID}/control/takeover", {}, "control_managed_agent"),
    ("POST", f"/agents/{RESOURCE_ID}/control/release", {}, "control_managed_agent"),
    ("POST", "/desktop/endpoints/example-host/views", {}, "control_managed_agent"),
    ("POST", f"/agents/{RESOURCE_ID}/desktop-views", {}, "control_managed_agent"),
    ("DELETE", f"/desktop/views/{RESOURCE_ID}", None, "control_managed_agent"),
    (
        "DELETE",
        f"/agents/{OTHER_ID}/desktop-views/{RESOURCE_ID}",
        None,
        "control_managed_agent",
    ),
    (
        "POST",
        f"/desktop/views/{RESOURCE_ID}/control",
        {"reason": "Example operation"},
        "control_managed_agent",
    ),
    (
        "POST",
        f"/agents/{OTHER_ID}/desktop-views/{RESOURCE_ID}/control",
        {"reason": "Example operation"},
        "control_managed_agent",
    ),
    (
        "POST",
        f"/desktop/views/{RESOURCE_ID}/control/release",
        None,
        "control_managed_agent",
    ),
    (
        "POST",
        f"/agents/{OTHER_ID}/desktop-views/{RESOURCE_ID}/control/release",
        None,
        "control_managed_agent",
    ),
    (
        "POST",
        f"/runners/{RESOURCE_ID}/sessions",
        {
            "harness": "copilot_cli",
            "workspace": {"kind": "authorized_directory", "id": "dir_example"},
        },
        "execute_flows",
    ),
    (
        "POST",
        f"/runner-sessions/{RESOURCE_ID}/turns",
        {"text": "Continue"},
        "execute_flows",
    ),
    ("POST", f"/runner-sessions/{RESOURCE_ID}/stop", {"mode": "kill"}, "execute_flows"),
    ("POST", "/runners/register", {"name": "Example runner"}, "execute_flows"),
    (
        "POST",
        f"/runners/{RESOURCE_ID}/models",
        {
            "name": "Example model",
            "provider_name": "openai",
            "model_identifier": "example-model",
            "api_endpoint": "http://localhost:11434/v1",
        },
        "create_ai_models",
    ),
    (
        "PATCH",
        f"/runners/{RESOURCE_ID}/consent",
        {"flow_execution_consent": "never"},
        "execute_flows",
    ),
    ("PATCH", f"/runners/{RESOURCE_ID}/scheduling", {"paused": True}, "execute_flows"),
    (
        "PATCH",
        f"/runners/{RESOURCE_ID}/concurrency",
        {"concurrency": 2},
        "execute_flows",
    ),
    ("DELETE", f"/runners/{RESOURCE_ID}?force=true", None, "execute_flows"),
    ("POST", f"/runners/{RESOURCE_ID}/token", None, "execute_flows"),
]


@pytest.fixture
def oss_checks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "true")
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(settings, "desktop_views_enabled", True)
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    account_hooks.reset_account_hooks()
    yield
    account_hooks.reset_account_hooks()


def _identity_plugin(permission_name: str) -> Any:
    """Stand in for the RBAC plugin decorator without enforcing anything."""

    def decorate(func: Any) -> Any:
        return func

    return decorate


def use_ee_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the RBAC plugin stand-in with its checks disabled.

    This is the only configuration in which the role-matrix fallback still
    runs. In the core edition (no plugin) every account member is admitted.
    """
    monkeypatch.setenv("DISABLE_RBAC", "true")
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr(permissions, "_plugin_require_permission", _identity_plugin)


@pytest.fixture
def member(db_session: Session, test_user: models.User) -> models.User:
    row = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session, db_obj=row, obj_in={"primary_user_id": test_user.id}
    )
    return crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "member@example.com",
            "username": "example-member",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "unused",
            "user_source": "local",
        },
    )


@pytest.fixture
def control_client(
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    oss_checks: None,
) -> Iterator[TestClient]:
    app = FastAPI()
    for module in (
        account,
        agent_control,
        desktop,
        runner_sessions,
        runners,
        websockets,
    ):
        app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: member
    app.dependency_overrides[get_db_session] = lambda: db_session

    async def run_db(operation: Any) -> Any:
        return operation(db_session)

    monkeypatch.setattr(desktop, "_run_db", run_db)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("edition", ["core", "ee_fallback"])
@pytest.mark.parametrize("role_scope", ["none", "other_account"])
@pytest.mark.parametrize("method,path,body,permission_name", CONTROL_ROUTES)
def test_control_without_permission_precedes_effects(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    permission_name: str,
    role_scope: str,
    edition: str,
) -> None:
    """With RBAC, refusal precedes effects; core admits roleless members."""
    if edition == "ee_fallback":
        use_ee_fallback(monkeypatch)
    if role_scope == "other_account":
        other_account = crud_account.create(
            db_session, obj_in={"organization_name": "Example organization"}
        )
        grant_permission(
            db_session, member, permission_name, role_account_id=other_account.id
        )
    holds = permissions.user_holds_permission(db_session, member, permission_name)
    assert holds is (edition == "core")
    writes: list[str] = []

    def record_write(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    nats = AsyncMock()
    delivery = AsyncMock()
    desktop_bus = AsyncMock()
    service = MagicMock()
    monkeypatch.setattr(agent_control, "get_nats_client", nats)
    monkeypatch.setattr(agent_control, "persist_and_deliver_command", delivery)
    monkeypatch.setattr(desktop.broker, "get_desktop_bus", desktop_bus)
    monkeypatch.setattr(runner_sessions, "_service", service)
    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", record_write)
    try:
        response = control_client.request(method, f"/api/v1{path}", json=body)
    finally:
        event.remove(connection, "before_cursor_execute", record_write)
    if edition == "core":
        # Referenced resources are absent, so the route fails later on its
        # own lookups; it must not be refused for lack of a role.
        assert response.status_code != 403, response.text
        return
    assert response.status_code == 403, response.text
    assert permission_name in response.text
    assert writes == []
    nats.assert_not_called()
    delivery.assert_not_called()
    desktop_bus.assert_not_called()
    assert service.mock_calls == []


ALLOWED_CONTROL_ROUTES = [
    pytest.param(
        "PUT",
        "/account/session-artifacts/settings",
        {"audio_storage_enabled": True},
        "manage_policies",
        id="session-artifact-settings",
    ),
    pytest.param(
        "PUT",
        "/account/governance-defaults",
        {},
        "manage_policies",
        id="governance-defaults",
    ),
    pytest.param(
        "POST",
        "/agents",
        {"display_name": "Example agent"},
        "manage_agents",
        id="create-agent",
    ),
    pytest.param(
        "PUT",
        "/attention/dismissals/example-item",
        {"fingerprint": "example", "reason": "expected"},
        "manage_agents",
        id="dismiss-attention-item",
    ),
    pytest.param(
        "POST",
        "/runners/register",
        {"name": "Example runner"},
        "execute_flows",
        id="register-runner",
    ),
]


@pytest.mark.parametrize("identity", ["primary", "holder", "superuser"])
@pytest.mark.parametrize(
    "method,path,body,permission_name",
    ALLOWED_CONTROL_ROUTES,
)
def test_allowed_user_can_update_controls(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    method: str,
    path: str,
    body: dict[str, Any],
    permission_name: str,
) -> None:
    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    elif identity == "superuser":
        member.is_superuser = True
        db_session.flush()
    else:
        grant_permission(db_session, member, permission_name)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners, "evaluate_fleet_findings", MagicMock())
    response = control_client.request(method, f"/api/v1{path}", json=body)
    assert response.status_code in {200, 201}, response.text


@pytest.mark.parametrize("identity", ["primary", "holder", "superuser"])
@pytest.mark.parametrize("transport", ["desktop", "execution"])
def test_shared_control_helpers_allow_permitted_users(
    db_session: Session,
    member: models.User,
    oss_checks: None,
    identity: str,
    transport: str,
) -> None:
    permission_name = (
        "control_managed_agent" if transport == "desktop" else "execute_flows"
    )
    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    elif identity == "superuser":
        member.is_superuser = True
    else:
        grant_permission(db_session, member, permission_name)
    if transport == "desktop":
        desktop._authorize_operator(db_session, member)
    else:
        assert websockets._execution_command_permission(
            current_user=member, db=db_session
        )


def test_desktop_operator_is_admitted_in_core(
    db_session: Session,
    member: models.User,
    oss_checks: None,
) -> None:
    """Core lets a roleless member operate desktop views."""
    desktop._authorize_operator(db_session, member)


def test_desktop_uses_declared_control_permission(
    db_session: Session,
    member: models.User,
    oss_checks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With RBAC, the desktop checks the permission it declares."""
    use_ee_fallback(monkeypatch)
    grant_permission(db_session, member, "control_managed_agent")
    monkeypatch.setattr(desktop, "PERMISSION", "manage_agents")
    with pytest.raises(HTTPException) as exc:
        desktop._authorize_operator(db_session, member)
    assert exc.value.status_code == 403
    assert "manage_agents" in exc.value.detail
    grant_permission(db_session, member, "manage_agents")
    desktop._authorize_operator(db_session, member)


@pytest.mark.parametrize("identity", ["primary", "holder"])
@pytest.mark.parametrize("method,path,body,permission_name", CONTROL_ROUTES)
def test_control_permission_allows_primary_and_holder(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    permission_name: str,
) -> None:
    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    else:
        grant_permission(db_session, member, permission_name)
    passed: list[str] = []

    def check(db: Session, user: models.User, name: str) -> None:
        permissions.ensure_permission_in_oss(db, user, name)
        passed.append(name)

    for module in (account, agent_control, desktop, runner_sessions, runners):
        monkeypatch.setattr(module, "ensure_permission_in_oss", check)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners, "evaluate_fleet_findings", MagicMock())
    monkeypatch.setattr(runner_sessions, "_service", None)
    response = control_client.request(method, f"/api/v1{path}", json=body)
    assert passed == [permission_name]
    # Referenced resources are deliberately absent; the permission must pass
    # before the route's existing resource and service availability checks.
    assert response.status_code in {200, 201, 204, 400, 404, 409, 503}, response.text


@pytest.mark.parametrize("edition", ["core", "ee_fallback"])
def test_desktop_viewer_without_permission_precedes_view_lookup(
    control_client: TestClient,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    edition: str,
) -> None:
    """With RBAC, refusal precedes the view lookup; core reaches the lookup."""
    if edition == "ee_fallback":
        use_ee_fallback(monkeypatch)
    lookup = MagicMock(return_value=None)
    bus = AsyncMock()
    monkeypatch.setattr(desktop, "_resolve_viewer", AsyncMock(return_value=member))
    monkeypatch.setattr(desktop.crud_desktop, "get_view", lookup)
    monkeypatch.setattr(desktop.broker, "get_desktop_bus", bus)
    with control_client.websocket_connect(
        f"/api/v1/ws/desktop/{RESOURCE_ID}?token=example"
    ) as ws:
        message = ws.receive()
    assert message["type"] == "websocket.close"
    if edition == "core":
        assert message["code"] != 4403
        lookup.assert_called_once()
        return
    assert message["code"] == 4403
    lookup.assert_not_called()
    bus.assert_not_called()


@pytest.mark.parametrize(
    "identity", ["roleless", "denied_with_rbac", "primary", "holder", "superuser"]
)
@pytest.mark.parametrize("transport", ["execution", "unified", "helper"])
@pytest.mark.parametrize("command", ["stop", "send_message"])
@pytest.mark.asyncio
async def test_execution_commands_check_permission_before_publication(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    transport: str,
    command: str,
) -> None:
    """With RBAC, refusal precedes publication; core admits roleless members."""
    from preloop.models.crud import crud_flow, crud_flow_execution

    if identity == "denied_with_rbac":
        use_ee_fallback(monkeypatch)
    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    elif identity == "superuser":
        member.is_superuser = True
    elif identity == "holder":
        grant_permission(db_session, member, "execute_flows")
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Example flow",
            trigger_event_source="github",
            trigger_event_types=["push"],
            prompt_template="Review",
            agent_type="openhands",
            agent_config={},
            account_id=member.account_id,
        ),
        account_id=member.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    with _ws_env(db_session, member) as nats:
        if transport == "execution":
            reply = _execution_socket_command(control_client, execution.id, command)
        elif transport == "unified":
            with control_client.websocket_connect("/api/v1/ws/unified") as ws:
                assert ws.receive_json()["type"] == "handshake"
                ws.send_json({"type": "authenticate", "token": "example"})
                assert ws.receive_json()["type"] == "authenticated"
                ws.send_json(
                    {
                        "type": "command",
                        "command": command,
                        "execution_id": str(execution.id),
                    }
                )
                reply = ws.receive_json()
        else:
            reply = await websockets._run_execution_command(
                member, execution.id, {"command": command, "message": "Continue"}
            )
    if identity == "denied_with_rbac":
        if transport == "helper":
            assert reply is None
        else:
            assert reply["type"] == "command_error"
            assert reply["error"] == "unauthorized"
        nats.publish.assert_not_called()
        db_session.refresh(execution)
        assert execution.status == "RUNNING"
    else:
        assert reply["status"] == ("stopped" if command == "stop" else "command_sent")
        nats.publish.assert_awaited_once()


@pytest.mark.parametrize(
    "resource", ["agent", "agent_control", "flow", "runtime", "runner", "desktop"]
)
def test_permission_holder_keeps_reference_scope(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    from preloop.models.crud import crud_flow, crud_managed_agent

    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Example organization"}
    )
    other_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "email": "other@example.com",
            "username": "example-other",
            "hashed_password": "unused",
        },
    )
    if resource in {"agent", "agent_control"}:
        row = crud_managed_agent.create_custom_agent(
            db_session, account_id=other_account.id, display_name="Example agent"
        )
        if resource == "agent":
            method, path, body, permission_name = (
                "DELETE",
                f"/agents/{row.id}",
                None,
                "manage_agents",
            )
        else:
            method, path, body, permission_name = (
                "POST",
                f"/agents/{row.id}/control/prompts",
                {"message": "Continue"},
                "control_managed_agent",
            )
    elif resource == "flow":
        row = crud_flow.create(
            db_session,
            flow_in=FlowCreate(
                name="Example flow",
                trigger_event_source="github",
                trigger_event_types=["push"],
                prompt_template="Review",
                agent_type="openhands",
                agent_config={},
                account_id=other_account.id,
            ),
            account_id=other_account.id,
        )
        method, path, body, permission_name = (
            "PUT",
            f"/account/governance/flows/{row.id}",
            {},
            "edit_flows",
        )
    elif resource == "runtime":
        row = models.RuntimeSession(
            account_id=other_account.id,
            session_source_type="custom",
            session_source_id="example-session",
            started_at=datetime.now(UTC),
        )
        db_session.add(row)
        db_session.flush()
        method, path, body, permission_name = (
            "PATCH",
            f"/runtime-sessions/{row.id}",
            {"action": "end"},
            "manage_runtime_sessions",
        )
    elif resource == "runner":
        row = models.FlowRunner(
            account_id=other_account.id,
            registered_by_user_id=other_user.id,
            name="Example runner",
            token_hash="example-hash",
        )
        db_session.add(row)
        db_session.flush()
        method, path, body, permission_name = (
            "DELETE",
            f"/runners/{row.id}?force=true",
            None,
            "execute_flows",
        )
    else:
        endpoint = models.DesktopEndpoint(
            account_id=other_account.id, endpoint_id="example-host"
        )
        db_session.add(endpoint)
        db_session.flush()
        row = models.DesktopView(
            account_id=other_account.id,
            endpoint_pk=endpoint.id,
            user_id=other_user.id,
            expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
        )
        db_session.add(row)
        db_session.flush()
        method, path, body, permission_name = (
            "POST",
            f"/desktop/views/{row.id}/control",
            {"reason": "Example operation"},
            "control_managed_agent",
        )
    grant_permission(db_session, member, permission_name)
    publish = AsyncMock()
    monkeypatch.setattr(agent_control, "get_nats_client", publish)
    monkeypatch.setattr(desktop.broker, "get_desktop_bus", publish)
    response = control_client.request(method, f"/api/v1{path}", json=body)
    assert response.status_code == 404, response.text
    publish.assert_not_called()
    assert db_session.get(type(row), row.id) is not None


@pytest.mark.parametrize("identity", ["primary", "holder"])
@pytest.mark.parametrize(
    "permission_name", ["edit_flows", "manage_runtime_sessions", "create_ai_models"]
)
def test_permitted_user_can_update_referenced_control(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    identity: str,
    permission_name: str,
) -> None:
    from preloop.models.crud import crud_flow

    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    else:
        grant_permission(db_session, member, permission_name)
    if permission_name == "edit_flows":
        row = crud_flow.create(
            db_session,
            flow_in=FlowCreate(
                name="Example flow",
                trigger_event_source="github",
                trigger_event_types=["push"],
                prompt_template="Review",
                agent_type="openhands",
                agent_config={},
                account_id=member.account_id,
            ),
            account_id=member.account_id,
        )
        response = control_client.put(
            f"/api/v1/account/governance/flows/{row.id}", json={}
        )
    elif permission_name == "manage_runtime_sessions":
        row = models.RuntimeSession(
            account_id=member.account_id,
            session_source_type="custom",
            session_source_id="example-session",
            started_at=datetime.now(UTC),
        )
        db_session.add(row)
        db_session.flush()
        response = control_client.patch(
            f"/api/v1/runtime-sessions/{row.id}", json={"action": "end"}
        )
        db_session.refresh(row)
        assert row.ended_at is not None
    else:
        row = models.FlowRunner(
            account_id=member.account_id,
            registered_by_user_id=member.id,
            name="Example runner",
            token_hash="example-hash",
        )
        db_session.add(row)
        db_session.flush()
        response = control_client.post(
            f"/api/v1/runners/{row.id}/models",
            json={
                "name": "Example model",
                "provider_name": "openai",
                "model_identifier": "example-model",
                "api_endpoint": "http://localhost:11434/v1",
            },
        )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "identity", ["roleless", "denied_with_rbac", "primary", "holder"]
)
@pytest.mark.parametrize(
    "action,body",
    [
        ("commands", {"message": "Continue"}),
        ("prompts", {"message": "Continue"}),
        ("voice-transcripts", {"transcript": "Continue"}),
        ("takeover", {}),
        ("release", {}),
    ],
)
def test_agent_control_permission_precedes_command_delivery(
    control_client: TestClient,
    db_session: Session,
    member: models.User,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    action: str,
    body: dict[str, Any],
) -> None:
    """With RBAC, refusal precedes delivery; core admits roleless members."""
    from preloop.models.crud import crud_managed_agent, crud_managed_agent_enrollment

    if identity == "denied_with_rbac":
        use_ee_fallback(monkeypatch)
    if identity == "primary":
        row = crud_account.get(db_session, id=member.account_id)
        crud_account.update(
            db_session, db_obj=row, obj_in={"primary_user_id": member.id}
        )
    elif identity == "holder":
        grant_permission(db_session, member, "control_managed_agent")
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=member.account_id,
        owner_user_id=member.id,
        display_name="Example agent",
        agent_kind="openclaw",
    )
    runtime = models.RuntimeSession(
        account_id=member.account_id,
        session_source_type="custom",
        session_source_id=agent.session_source_id,
        started_at=datetime.now(UTC),
    )
    db_session.add(runtime)
    db_session.flush()
    agent.runtime_session_id = runtime.id
    agent.control_last_heartbeat_at = datetime.now(UTC)
    crud_managed_agent_enrollment.create_for_agent(
        db_session,
        account_id=member.account_id,
        agent_id=agent.id,
        created_by_user_id=member.id,
        enrollment_type="runtime_plugin_control",
        adapter_key="openclaw",
        validation_result={"control_channel_configured": True},
    )
    nats = MagicMock(is_connected=True, publish=AsyncMock())
    monkeypatch.setattr(agent_control, "get_nats_client", AsyncMock(return_value=nats))
    response = control_client.post(
        f"/api/v1/agents/{agent.id}/control/{action}", json=body
    )
    if identity == "denied_with_rbac":
        assert response.status_code == 403, response.text
        nats.publish.assert_not_called()
    else:
        assert response.status_code == 202, response.text
        nats.publish.assert_awaited_once()
