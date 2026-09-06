"""Endpoint tests for the account kill switch (#157)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_account_halt, crud_audit_log
from preloop.models.db.session import get_db_session as get_db
from preloop.services.kill_switch import invalidate_kill_switch_cache

ALL_SCOPES = {"gateway", "tools", "flows"}


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache():
    """Halt state is cached in-process; keep tests independent."""
    invalidate_kill_switch_cache()
    yield
    invalidate_kill_switch_cache()


def test_status_inactive_by_default(client):
    """An account that never halted reads as inactive."""
    response = client.get("/api/v1/account/kill-switch/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is False
    assert payload["scopes"] == []


def test_activate_defaults_to_full_halt(client, test_user):
    """POST /activate without scopes halts gateway, tools, and flows."""
    response = client.post(
        "/api/v1/account/kill-switch/activate",
        json={"reason": "runaway spend loop"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is True
    assert {entry["scope"] for entry in payload["scopes"]} == ALL_SCOPES
    for entry in payload["scopes"]:
        assert entry["reason"] == "runaway spend loop"
        assert entry["activated_at"] is not None
        assert entry["activated_by_user_id"] == str(test_user.id)


def test_activate_is_audited(client, db_session, test_user):
    """Every activated scope lands in the audit log with actor and reason."""
    client.post(
        "/api/v1/account/kill-switch/activate",
        json={"reason": "prompt injection suspected"},
    )
    entries = crud_audit_log.get_by_account(
        db_session, account_id=test_user.account_id, action="kill_switch_activated"
    )
    assert {entry.details["scope"] for entry in entries} == ALL_SCOPES
    for entry in entries:
        assert entry.user_id == test_user.id
        assert entry.details["reason"] == "prompt injection suspected"
        assert entry.status == "success"


def test_staged_reenable_lifts_scopes_independently(client):
    """Deactivating a subset is the staged recovery path."""
    client.post("/api/v1/account/kill-switch/activate", json={"reason": "halt"})

    response = client.post(
        "/api/v1/account/kill-switch/deactivate",
        json={"scopes": ["gateway"]},
    )
    assert response.status_code == 200
    assert {entry["scope"] for entry in response.json()["scopes"]} == {
        "tools",
        "flows",
    }

    response = client.post(
        "/api/v1/account/kill-switch/deactivate",
        json={"scopes": ["tools", "flows"]},
    )
    assert response.status_code == 200
    assert response.json()["active"] is False
    assert response.json()["scopes"] == []


def test_deactivate_is_audited(client, db_session, test_user):
    client.post("/api/v1/account/kill-switch/activate", json={"reason": "halt"})
    client.post("/api/v1/account/kill-switch/deactivate", json={"scopes": ["gateway"]})
    entries = crud_audit_log.get_by_account(
        db_session,
        account_id=test_user.account_id,
        action="kill_switch_deactivated",
    )
    assert [entry.details["scope"] for entry in entries] == ["gateway"]


def test_activate_subset_only_halts_requested_scopes(client):
    """A partial activation only touches the requested scopes."""
    response = client.post(
        "/api/v1/account/kill-switch/activate",
        json={"scopes": ["tools"], "reason": "tool abuse"},
    )
    assert response.status_code == 200
    assert {entry["scope"] for entry in response.json()["scopes"]} == {"tools"}


def test_repeated_activation_keeps_original_attribution(client, db_session, test_user):
    """Confirming an active halt must not rewrite who activated it."""
    client.post("/api/v1/account/kill-switch/activate", json={"reason": "first"})
    first = crud_account_halt.get_for_scope(
        db_session, account_id=test_user.account_id, scope="gateway"
    )
    original_at = first.activated_at
    original_reason = first.reason

    client.post("/api/v1/account/kill-switch/activate", json={"reason": "second"})
    second = crud_account_halt.get_for_scope(
        db_session, account_id=test_user.account_id, scope="gateway"
    )
    assert second.is_active is True
    assert second.activated_at == original_at
    assert second.reason == original_reason


def test_unknown_scope_rejected(client):
    response = client.post(
        "/api/v1/account/kill-switch/activate",
        json={"scopes": ["not_a_scope"]},
    )
    assert response.status_code == 422


def test_toggle_requires_elevated_role_for_non_owner(db_session, test_viewer_user):
    """A viewer cannot halt the account, even with RBAC disabled (OSS)."""
    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    with TestClient(app) as viewer_client:
        response = viewer_client.post(
            "/api/v1/account/kill-switch/activate", json={"reason": "oops"}
        )
    assert response.status_code == 403
    assert "manage_kill_switch" in response.json()["detail"]

    # And the halt state is untouched.
    assert (
        crud_account_halt.active_scopes(
            db_session, account_id=test_viewer_user.account_id
        )
        == set()
    )


def test_viewer_can_read_status(db_session, test_viewer_user):
    """Any authenticated user can see that the account is halted."""
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_viewer_user.account_id,
        scopes=["gateway"],
        active=True,
        user_id=test_viewer_user.id,
        reason="incident",
    )
    invalidate_kill_switch_cache(test_viewer_user.account_id)

    app: FastAPI = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    with TestClient(app) as viewer_client:
        response = viewer_client.get("/api/v1/account/kill-switch/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is True
    assert [entry["scope"] for entry in payload["scopes"]] == ["gateway"]


def test_halt_state_survives_across_accounts(client, db_session, test_viewer_user):
    """One account's halt never leaks into another account's status."""
    client.post("/api/v1/account/kill-switch/activate", json={"reason": "halt"})
    other = crud_account_halt.snapshot_for_account(
        db_session, account_id=test_viewer_user.account_id
    )
    assert other["active"] is False


def test_ee_plugin_does_not_bypass_toggle_authorization_when_rbac_disabled(
    db_session, test_viewer_user, monkeypatch
):
    from preloop.api.endpoints import kill_switch
    from fastapi import HTTPException

    monkeypatch.setattr(kill_switch, "_plugin_require_permission", object())
    monkeypatch.setattr(kill_switch, "_rbac_checks_enabled", lambda: False)
    with pytest.raises(HTTPException) as denied:
        kill_switch._ensure_toggle_authorized(db_session, test_viewer_user)
    assert denied.value.status_code == 403


def test_custom_team_permission_allows_toggle_with_rbac_disabled(
    db_session, test_viewer_user, monkeypatch
):
    from preloop.api.endpoints import kill_switch
    from preloop.models.crud import (
        crud_permission,
        crud_role,
        crud_team,
        crud_team_role,
    )

    permission = crud_permission.get_by_name(db_session, name="manage_kill_switch")
    role = crud_role.create(
        db_session,
        obj_in={
            "name": "incident_responder",
            "account_id": test_viewer_user.account_id,
            "is_system_role": False,
        },
    )
    crud_role.assign_permission(
        db_session, role_id=role.id, permission_id=permission.id
    )
    team = crud_team.create(
        db_session,
        obj_in={
            "name": "incident_response",
            "account_id": test_viewer_user.account_id,
        },
    )
    crud_team.add_member(db_session, team_id=team.id, user_id=test_viewer_user.id)
    crud_team_role.assign_role(db_session, team_id=team.id, role_id=role.id)
    monkeypatch.setattr(kill_switch, "_plugin_require_permission", object())
    monkeypatch.setattr(kill_switch, "_rbac_checks_enabled", lambda: False)
    kill_switch._ensure_toggle_authorized(db_session, test_viewer_user)
