"""Core audit wiring must work without optional plugins or user administration."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models import models
from preloop.models.crud import crud_account, crud_audit_log, crud_user
from preloop.plugins.base import PluginManager
from preloop.plugins import account_hooks
from preloop.services import session_search_audit
from preloop.services.audit import get_audit_service
from preloop.schemas.session_search import SessionSearchRequest
from preloop.utils.audit import log_config_change


def test_plugin_free_manager_has_one_core_collector() -> None:
    manager = PluginManager()
    assert manager._plugins == {}
    assert manager.get_service("audit_service") is get_audit_service()
    assert manager.get_enabled_features()["features"]["audit_logs"] is True
    manager._services["audit_service"] = object()
    assert manager.get_service("audit_service") is get_audit_service()


def test_audit_routes_are_registered_exactly_once() -> None:
    app = create_app()
    from fastapi.routing import iter_route_contexts

    operations = [
        (context.path, method)
        for context in iter_route_contexts(app.routes)
        for method in (context.methods or set())
        if context.path.startswith("/api/v1/audit-logs")
    ]
    assert operations
    assert len(operations) == len(set(operations))
    assert ("/api/v1/audit-logs/actors", "GET") in operations


def test_configuration_helper_persists_in_plugin_free_core(
    db_session: Session, test_user: models.User
) -> None:
    service = get_audit_service()
    service.enable()
    log_config_change(
        db_session,
        user=test_user,
        config_type="mcp_server",
        action="created",
        new_value={"name": "example", "password": "never-keep-this-secret"},
    )
    rows = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="configuration_change"
    )
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id
    assert "never-keep-this-secret" not in str(rows[0].details)


def test_actor_display_is_account_scoped_and_minimal(
    audit_client: TestClient, db_session: Session, test_user: models.User
) -> None:
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    foreign = crud_user.create(
        db_session,
        obj_in={
            "account_id": other.id,
            "username": "other-actor",
            "email": "other@example.com",
            "hashed_password": "synthetic",
            "full_name": "Other Actor",
            "is_active": True,
        },
    )
    response = audit_client.get("/api/v1/audit-logs/actors")
    assert response.status_code == 200
    actors = response.json()
    assert str(test_user.id) in {row["id"] for row in actors}
    assert str(foreign.id) not in {row["id"] for row in actors}
    assert all(set(row) == {"id", "username", "full_name"} for row in actors)
    assert audit_client.get("/api/v1/audit-logs/actors?limit=1001").status_code == 422


def test_foreign_entry_is_not_readable(
    audit_client: TestClient, db_session: Session, test_user: models.User
) -> None:
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    row = crud_audit_log.log_action(
        db_session, account_id=other.id, action="foreign_event", status="success"
    )
    assert audit_client.get(f"/api/v1/audit-logs/{row.id}").status_code == 404


def test_audit_read_requires_authentication() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/audit-logs").status_code == 401
        assert client.get("/api/v1/audit-logs/actors").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/audit-logs",
        "/api/v1/audit-logs/actors",
        "/api/v1/audit-logs/grouped",
        "/api/v1/audit-logs/overview",
        "/api/v1/audit-logs/stats",
        "/api/v1/audit-logs/denials",
    ],
)
def test_account_authorizer_enforces_audit_permission(
    audit_client: TestClient, path: str
) -> None:
    seen = []
    previous = account_hooks.get_authorizer()

    def deny(ctx: Any, action: str, resource: Any) -> account_hooks.Decision:
        seen.append(action)
        return account_hooks.Decision("deny")

    account_hooks.register_authorizer(deny)
    try:
        assert audit_client.get(path).status_code == 403
        assert seen == ["view_audit_logs"]
    finally:
        account_hooks.register_authorizer(previous)


def test_denied_search_uses_actor_tenant_and_hash_privacy(
    db_session: Session, test_user: models.User
) -> None:
    payload = SessionSearchRequest(query="synthetic private query")
    session_search_audit.record_denied_endpoint_search(
        permission="view_runtime_sessions",
        kwargs={
            "current_user": test_user,
            "db": db_session,
            "payload": payload,
            "account": type("Foreign", (), {"id": uuid4()})(),
        },
    )
    rows = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action=session_search_audit.AUDIT_ACTION,
    )
    assert len(rows) == 1
    assert rows[0].status == "denied"
    assert rows[0].details["query_hash"] == session_search_audit.query_hash(
        payload.query
    )
    assert rows[0].details["query_text_stored"] is False
    assert "synthetic private query" not in str(rows[0].details)


def test_core_authorizer_denied_search_writes_exactly_one_row(
    audit_client: TestClient, db_session: Session, test_user: models.User
) -> None:
    previous = account_hooks.get_authorizer()
    account_hooks.register_authorizer(
        lambda ctx, action, resource: account_hooks.Decision("deny")
    )
    try:
        response = audit_client.post(
            "/api/v1/runtime-sessions/search", json={"query": "private sample query"}
        )
        assert response.status_code == 403
        rows = crud_audit_log.get_by_account(
            db_session,
            account_id=test_user.account_id,
            action=session_search_audit.AUDIT_ACTION,
        )
        assert len(rows) == 1
        assert rows[0].status == "denied"
        assert rows[0].details["query_text_stored"] is False
        assert "private sample query" not in str(rows[0].details)
    finally:
        account_hooks.register_authorizer(previous)


def test_default_timeline_exposes_every_family_without_duplicate_children(
    db_session: Session, test_user: models.User
) -> None:
    actions = [
        "query",
        "user.login",
        "audit_exported",
        "audit_chain_verified",
        "future_action",
    ]
    for action in actions:
        crud_audit_log.log_action(
            db_session,
            account_id=test_user.account_id,
            action=action,
            status="success",
            details={"correlation_id": "example-chain"},
        )
    for action, details in [
        ("tool_call", {"correlation_id": "example-chain"}),
        ("policy_allow", {"correlation_id": "example-chain", "decision": "allow"}),
        (
            "approval_created",
            {"correlation_id": "example-chain", "approval_id": "example-approval"},
        ),
        ("approval_approved", {"approval_id": "example-approval"}),
        ("approval_expired", {"approval_id": "orphan-approval"}),
    ]:
        crud_audit_log.log_action(
            db_session,
            account_id=test_user.account_id,
            action=action,
            status="success",
            details=details,
        )
    groups, total = crud_audit_log.get_grouped_by_correlation(
        db_session,
        account_id=test_user.account_id,
    )
    assert total == len(actions) + 2  # Tool chain + orphan approval.
    primary = {group["primary_event"].action for group in groups}
    assert set(actions) <= primary
    assert "approval_expired" in primary
    visible_ids = [
        row.id
        for group in groups
        for row in [group["primary_event"], *group["sub_events"]]
    ]
    assert len(visible_ids) == len(set(visible_ids)) == len(actions) + 5
    assert all(
        not group["sub_events"]
        for group in groups
        if group["primary_event"].action in actions
    )


@pytest.mark.asyncio
async def test_sync_worker_owns_core_audit_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    import asyncio
    from preloop.sync.services import nats_worker
    from preloop.sync.services.event_bus import event_bus_service

    service = get_audit_service()
    service.disable()
    observations = []

    class Worker:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def start_listening(self) -> None:
            observations.append(service.enabled)

        async def stop(self) -> None:
            observations.append(service.enabled)

    monkeypatch.setattr(nats_worker, "PreloopSyncNatsWorker", Worker)
    monkeypatch.setattr(nats_worker, "_run_boot_recovery", AsyncMock())
    monkeypatch.setattr(event_bus_service, "connect", AsyncMock())
    monkeypatch.setattr(event_bus_service, "close", AsyncMock())
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *args: None
    )
    try:
        await nats_worker.main(tasks_allowlist=["synthetic"])
        assert observations == [True, True]
        assert service.enabled is False
    finally:
        service.enable()
