"""Durable operator history survives removal of a managed-agent registry row."""

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from starlette.requests import Request

from preloop.api.endpoints import account as endpoints
from preloop.schemas.gateway_usage import ManagedAgentUpdateRequest


@pytest.mark.asyncio
async def test_removal_records_actor_and_revocation_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion and its audit record share a transaction and retain identity."""
    account = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), account_id=account.id, is_superuser=True)
    agent = SimpleNamespace(
        id=uuid4(),
        display_name="Example Agent",
        session_source_type="codex",
        session_source_id="example-principal",
        lifecycle_state="active",
    )
    db = MagicMock()
    monkeypatch.setattr(
        endpoints.crud_managed_agent, "get_for_account", lambda *a, **k: agent
    )
    monkeypatch.setattr(
        endpoints.crud_api_key,
        "deactivate_unbound_runtime_keys_for_principal",
        lambda *a, **k: [object()],
    )
    monkeypatch.setattr(
        endpoints.crud_api_key,
        "deactivate_runtime_keys_for_managed_agent",
        lambda *a, **k: [object(), object()],
    )
    audit = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(endpoints.crud_audit_log, "log_action", audit)
    monkeypatch.setattr(endpoints.crud_managed_agent, "remove_registry_entry", remove)
    monkeypatch.setattr(endpoints, "emit_account_event", lambda *a: None)
    db.attach_mock(audit, "audit")
    db.attach_mock(remove, "remove")

    await inspect.unwrap(endpoints.delete_account_managed_agent)(
        agent_id=str(agent.id),
        account=account,
        current_user=user,
        request=Request(
            {
                "type": "http",
                "headers": [(b"user-agent", b"example-client/1.0")],
                "client": ("192.0.2.1", 1234),
            }
        ),
        db=db,
    )

    entry = audit.call_args.kwargs
    assert entry["account_id"] == account.id
    assert entry["user_id"] == user.id
    assert entry["user_agent"] == "example-client/1.0"
    assert entry["ip_address"] == "192.0.2.1"
    assert entry["resource_id"] == str(agent.id)
    assert entry["action"] == "managed_agent_removed"
    assert entry["commit"] is False
    assert entry["details"]["session_source_id"] == "example-principal"
    assert entry["details"]["revoked_credentials"] == 3
    assert entry["details"]["session_history_retained"] is True
    assert entry["details"]["local_configuration_restored"] is False
    assert [call[0] for call in db.mock_calls] == ["audit", "remove", "commit"]
    remove.assert_called_once_with(db, agent=agent, commit=False)


@pytest.mark.asyncio
async def test_removal_audit_failure_does_not_commit_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable audit store must not produce an unaudited removal."""
    agent = SimpleNamespace(
        id=uuid4(),
        display_name="Example Agent",
        session_source_type="codex",
        session_source_id="example-principal",
        lifecycle_state="active",
    )
    db = MagicMock()
    monkeypatch.setattr(
        endpoints.crud_managed_agent, "get_for_account", lambda *a, **k: agent
    )
    monkeypatch.setattr(
        endpoints.crud_api_key,
        "deactivate_unbound_runtime_keys_for_principal",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        endpoints.crud_api_key,
        "deactivate_runtime_keys_for_managed_agent",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        endpoints.crud_audit_log,
        "log_action",
        MagicMock(side_effect=RuntimeError("audit unavailable")),
    )
    remove = MagicMock()
    monkeypatch.setattr(endpoints.crud_managed_agent, "remove_registry_entry", remove)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await inspect.unwrap(endpoints.delete_account_managed_agent)(
            agent_id=str(agent.id),
            account=SimpleNamespace(id=uuid4()),
            current_user=SimpleNamespace(id=uuid4(), is_superuser=True),
            request=Request({"type": "http", "headers": []}),
            db=db,
        )
    db.commit.assert_not_called()
    remove.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,previous,next_state",
    [
        ("suspend", "active", "suspended"),
        ("decommission", "active", "decommissioned"),
        ("reenroll", "decommissioned", "active"),
    ],
)
async def test_lifecycle_change_persists_actor_and_previous_state(
    monkeypatch,
    action,
    previous,
    next_state,
):
    """The audit record distinguishes offboarding, suspension and reenrollment."""
    agent = SimpleNamespace(
        id=uuid4(),
        display_name="Example Agent",
        session_source_type="codex",
        session_source_id="example-principal",
        lifecycle_state=previous,
        lifecycle_reason="operator request",
        tags={},
        owner_user_id=None,
        others_may_start_sessions="ask_me",
        runtime_session_id=None,
    )
    db = MagicMock()
    account = SimpleNamespace(id=uuid4())
    user = SimpleNamespace(id=uuid4(), account_id=account.id, is_superuser=True)
    monkeypatch.setattr(
        endpoints.crud_managed_agent, "get_for_account", lambda *a, **k: agent
    )

    def update(*args: Any, **kwargs: Any) -> SimpleNamespace:
        agent.lifecycle_state = next_state
        return agent

    monkeypatch.setattr(endpoints.crud_managed_agent, "update_operator_state", update)
    for method in (
        "deactivate_runtime_keys_for_managed_agent",
        "deactivate_unbound_runtime_keys_for_principal",
        "reactivate_runtime_keys_for_managed_agent",
    ):
        monkeypatch.setattr(endpoints.crud_api_key, method, MagicMock())
    monkeypatch.setattr(
        endpoints.crud_runtime_session, "reopen_for_managed_agent", MagicMock()
    )
    summary = MagicMock()
    monkeypatch.setattr(
        endpoints,
        "_build_managed_agent_detail_response",
        lambda *a, **k: SimpleNamespace(agent=summary),
    )
    monkeypatch.setattr(endpoints, "emit_account_event", lambda *a: None)
    audit = MagicMock()
    monkeypatch.setattr(endpoints.crud_audit_log, "log_action", audit)
    await inspect.unwrap(endpoints.update_account_managed_agent)(
        agent_id=str(agent.id),
        update=ManagedAgentUpdateRequest(lifecycle_action=action),
        account=account,
        current_user=user,
        request=Request(
            {
                "type": "http",
                "headers": [(b"user-agent", b"example-client/1.0")],
                "client": ("192.0.2.1", 1234),
            }
        ),
        db=db,
    )
    entry = audit.call_args.kwargs
    assert entry["user_id"] == user.id
    assert entry["user_agent"] == "example-client/1.0"
    assert entry["ip_address"] == "192.0.2.1"
    assert entry["commit"] is False
    assert entry["details"]["lifecycle_action"] == action
    assert entry["details"]["previous_lifecycle_state"] == previous
    assert entry["details"]["lifecycle_state"] == next_state


def test_removed_agent_keeps_persisted_operator_audit(
    client: Any,
    db_session: Any,
    test_user: Any,
) -> None:
    """The operator can reconstruct removal after the registry row is gone."""
    token = client.post(
        "/api/v1/auth/runtime-sessions/token",
        json={
            "session_source_type": "codex",
            "session_source_id": "synthetic-audit-run",
            "runtime_principal_id": "synthetic-audit-principal",
            "runtime_principal_name": "Example Agent",
        },
    )
    assert token.status_code == 201
    agent_id = client.get("/api/v1/agents").json()["items"][0]["id"]
    update = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={
            "lifecycle_action": "decommission",
            "reason": "operator requested offboard",
        },
    )
    assert update.status_code == 200
    assert client.delete(f"/api/v1/agents/{agent_id}").status_code == 200
    assert (
        endpoints.crud_managed_agent.get_for_account(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=agent_id,
        )
        is None
    )
    audits = endpoints.crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        resource_type="managed_agent",
        resource_id=agent_id,
    )
    assert {row.action for row in audits} == {
        "managed_agent_updated",
        "managed_agent_removed",
    }
    assert all(row.user_id == test_user.id for row in audits)
    removal = next(row for row in audits if row.action == "managed_agent_removed")
    assert removal.details["session_source_id"] == "synthetic-audit-principal"
    assert removal.details["previous_lifecycle_state"] == "decommissioned"


def test_tag_changes_are_audited_and_noop_saves_are_skipped(
    client: Any,
    db_session: Any,
    test_user: Any,
) -> None:
    """Tag-only saves expose changes safely; repeating a save adds no fake edit."""
    response = client.post("/api/v1/agents", json={"display_name": "Example Agent"})
    assert response.status_code == 201
    agent_id = response.json()["id"]
    tags = {"environment": "test", "api_key": "synthetic-secret"}
    for _ in range(2):
        assert (
            client.patch(f"/api/v1/agents/{agent_id}", json={"tags": tags}).status_code
            == 200
        )
    rows = endpoints.crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        resource_type="managed_agent",
        resource_id=agent_id,
        action="managed_agent_updated",
    )
    assert len(rows) == 1
    assert rows[0].details["tags"]["environment"] == "test"
    assert rows[0].details["previous_tags"] == {}
    assert "synthetic-secret" not in str(rows[0].details)


def test_registry_removal_commits_when_requested(
    db_session: Any,
    test_user: Any,
) -> None:
    """The CRUD helper also supports standalone committed removal."""
    agent = endpoints.crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Example Agent",
        owner_user_id=test_user.id,
    )
    agent_id = str(agent.id)
    endpoints.crud_managed_agent.remove_registry_entry(db_session, agent=agent)
    assert (
        endpoints.crud_managed_agent.get_for_account(
            db_session,
            account_id=str(test_user.account_id),
            agent_id=agent_id,
        )
        is None
    )
