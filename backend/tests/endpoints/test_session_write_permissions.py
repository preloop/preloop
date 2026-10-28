"""Permissions for personal searches, sharing, analysis and replay.

The core edition has no access controls: every account member may share
searches, run analysis and replay regardless of role. The sharing check that
runs inside the handler body is still pinned with an RBAC plugin stand-in.
"""

from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from preloop.api.auth import get_current_active_user
from preloop.models import crud, models
from preloop.utils import permissions

SAVED_URL = "/api/v1/runtime-sessions/search/saved"
OPTIMIZATION_URL = "/api/v1/billing/cost/runtime-sessions"


@pytest.fixture
def role_user(app: Any, db_session: Any, test_user: Any, monkeypatch: Any) -> Any:
    """Use a non-primary colleague with a real seeded role in the OSS build."""
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)

    def create(role_name: str) -> models.User:
        user = crud.crud_user.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "email": f"{role_name}@example.com",
                "username": role_name,
                "full_name": "Jane Doe",
                "is_active": True,
                "email_verified": True,
                "hashed_password": "testpassword",
                "user_source": "local",
            },
        )
        role = crud.crud_role.get_by_name(db_session, name=role_name)
        assert role is not None
        crud.crud_user_role.assign_role(db_session, user_id=user.id, role_id=role.id)
        app.dependency_overrides[get_current_active_user] = lambda: user
        return user

    return create


@pytest.mark.parametrize("role_name", ["viewer", "analyst"])
def test_read_roles_keep_personal_saved_searches(
    client: Any, role_user: Any, role_name: str
) -> None:
    role_user(role_name)
    created = client.post(SAVED_URL, json={"name": "personal", "query": "ledger"})
    assert created.status_code == 201
    saved_id = created.json()["id"]
    assert created.json()["visibility"] == "private"
    edited = client.patch(
        f"{SAVED_URL}/{saved_id}", json={"name": "renamed", "query": "billing"}
    )
    assert edited.status_code == 200
    assert edited.json()["query"] == "billing"
    assert client.delete(f"{SAVED_URL}/{saved_id}").status_code == 204


@pytest.mark.parametrize("role_name", ["viewer", "analyst"])
@pytest.mark.parametrize("plugin_present", [False, True])
def test_sharing_permission_applies_only_with_plugin(
    client: Any,
    db_session: Any,
    role_user: Any,
    role_name: str,
    plugin_present: bool,
    monkeypatch: Any,
) -> None:
    """Core members share freely; with the plugin, read roles are refused."""
    user = role_user(role_name)
    private = client.post(
        SAVED_URL, json={"name": "personal", "query": "ledger"}
    ).json()
    if plugin_present:
        # The OSS helper returns early with the plugin enabled; the body must
        # still check the permission for a payload that requests sharing.
        monkeypatch.setattr(permissions, "_plugin_require_permission", object())
        monkeypatch.setattr(permissions, "_rbac_checks_enabled", lambda: True)
    created = client.post(
        SAVED_URL,
        json={"name": "shared", "query": "billing", "visibility": "account"},
    )
    edited = client.patch(
        f"{SAVED_URL}/{private['id']}",
        json={"name": "changed", "query": "changed", "visibility": "account"},
    )
    if not plugin_present:
        assert created.status_code == 201, created.text
        assert edited.status_code == 200, edited.text
        assert edited.json()["visibility"] == "account"
        return
    assert created.status_code == edited.status_code == 403
    assert "share_session_searches" in created.json()["detail"]
    assert "share_session_searches" in edited.json()["detail"]
    rows, total = crud.crud_session_saved_search.list_visible(
        db_session, account_id=user.account_id, user_id=user.id, limit=100, offset=0
    )
    assert total == 1
    assert rows[0].name == "personal"
    assert rows[0].query == "ledger"
    assert rows[0].visibility == "private"
    assert rows[0].shared_at is None


@pytest.mark.parametrize("role_name", ["owner", "admin", "editor", "executor"])
def test_sharing_roles_can_create_and_share_saved_searches(
    client: Any, role_user: Any, role_name: str
) -> None:
    role_user(role_name)
    shared = client.post(
        SAVED_URL,
        json={"name": "shared", "query": "ledger", "visibility": "account"},
    )
    assert shared.status_code == 201
    assert shared.json()["shared_at"] is not None
    private = client.post(
        SAVED_URL, json={"name": "personal", "query": "billing"}
    ).json()
    edited = client.patch(
        f"{SAVED_URL}/{private['id']}", json={"visibility": "account"}
    )
    assert edited.status_code == 200
    assert edited.json()["visibility"] == "account"


@pytest.mark.parametrize("operation", ["unshare", "delete", "edit", "edit_shared"])
def test_author_can_manage_shared_search_after_sharing_permission_is_removed(
    client: Any, db_session: Any, role_user: Any, operation: str
) -> None:
    user = role_user("editor")
    created = client.post(
        SAVED_URL,
        json={"name": "shared", "query": "ledger", "visibility": "account"},
    )
    assert created.status_code == 201
    saved_id = created.json()["id"]
    editor = crud.crud_role.get_by_name(db_session, name="editor")
    viewer = crud.crud_role.get_by_name(db_session, name="viewer")
    crud.crud_user_role.remove_role(db_session, user_id=user.id, role_id=editor.id)
    crud.crud_user_role.assign_role(db_session, user_id=user.id, role_id=viewer.id)
    if operation == "delete":
        response = client.delete(f"{SAVED_URL}/{saved_id}")
        assert response.status_code == 204
    else:
        payload = (
            {"visibility": "private"} if operation == "unshare" else {"name": "new"}
        )
        if operation == "edit_shared":
            payload["visibility"] = "account"
        response = client.patch(f"{SAVED_URL}/{saved_id}", json=payload)
        assert response.status_code == 200
        if operation == "unshare":
            assert response.json()["shared_at"] is None


def test_member_without_roles_keeps_saved_search_access(
    client: Any, db_session: Any, role_user: Any
) -> None:
    """Losing every role does not cut a core member off from saved searches."""
    user = role_user("viewer")
    saved = client.post(SAVED_URL, json={"name": "personal", "query": "ledger"}).json()
    role = crud.crud_role.get_by_name(db_session, name="viewer")
    crud.crud_user_role.remove_role(db_session, user_id=user.id, role_id=role.id)
    assert (
        client.post(SAVED_URL, json={"name": "new", "query": "ledger"}).status_code
        == 201
    )
    assert (
        client.patch(f"{SAVED_URL}/{saved['id']}", json={"name": "renamed"}).status_code
        == 200
    )
    assert client.get(SAVED_URL).status_code == 200
    assert client.get(f"{SAVED_URL}/{saved['id']}").status_code == 200
    assert client.post(f"{SAVED_URL}/{saved['id']}/run").status_code != 403
    assert client.delete(f"{SAVED_URL}/{saved['id']}").status_code == 204
    assert (
        crud.crud_session_saved_search.get_owned(
            db_session,
            account_id=user.account_id,
            user_id=user.id,
            saved_search_id=UUID(saved["id"]),
        )
        is None
    )


@pytest.mark.parametrize(
    "role_name", ["owner", "admin", "editor", "executor", "viewer", "analyst"]
)
@pytest.mark.parametrize("suffix", ["optimizations", "optimizations/jobs"])
def test_every_role_reaches_submission_service(
    client: Any, role_user: Any, role_name: str, suffix: str
) -> None:
    """Read-only roles reach analysis too: core has no access controls."""
    from fastapi import HTTPException

    role_user(role_name)
    target = (
        "run_session_optimization"
        if suffix == "optimizations"
        else "submit_session_optimization_job"
    )
    with patch(
        f"preloop.api.endpoints.session_optimization.{target}",
        side_effect=HTTPException(404, "Runtime session not found"),
    ) as service:
        response = client.post(
            f"{OPTIMIZATION_URL}/00000000-0000-0000-0000-000000000001/{suffix}", json={}
        )
    assert response.status_code == 404
    service.assert_called_once()


@pytest.mark.parametrize(
    "role_name", ["owner", "admin", "editor", "executor", "viewer", "analyst"]
)
def test_every_role_reaches_replay(client: Any, role_user: Any, role_name: str) -> None:
    """Replay no longer requires a model edit grant in the core edition."""
    from preloop.services.replay_savings_service import ConsentRequiredError

    role_user(role_name)
    with patch(
        "preloop.api.endpoints.session_optimization.run_session_replay",
        side_effect=ConsentRequiredError("Explicit consent required"),
    ) as replay:
        response = client.post(
            f"{OPTIMIZATION_URL}/00000000-0000-0000-0000-000000000001/replay", json={}
        )
    assert response.status_code == 400
    replay.assert_called_once()
