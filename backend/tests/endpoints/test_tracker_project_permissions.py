"""Wrapped tracker, organization and project permission checks in OSS builds."""

from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import (
    bitbucket_dc_webhooks,
    organizations,
    projects,
    trackers,
)
from preloop.config import settings
from preloop.models import crud, models
from preloop.models.crud import readiness
from preloop.models.db.session import get_db_session
from preloop.plugins import account_hooks
from preloop.utils import permissions


ROUTES = [
    (
        organizations,
        "POST",
        "/organizations",
        "create_organizations",
        201,
        "org_create",
    ),
    (
        organizations,
        "PUT",
        "/organizations/{id}",
        "edit_organizations",
        200,
        "org_update",
    ),
    (
        organizations,
        "DELETE",
        "/organizations/{id}",
        "delete_organizations",
        204,
        "org_delete",
    ),
    (projects, "POST", "/projects", "create_projects", 201, "project_create"),
    (projects, "PUT", "/projects/{id}", "edit_projects", 200, "project_update"),
    (projects, "DELETE", "/projects/{id}", "delete_projects", 204, "project_delete"),
    (
        projects,
        "PUT",
        "/projects/{id}/readiness-policy",
        "edit_projects",
        200,
        "activate_policy",
    ),
    (
        projects,
        "DELETE",
        "/projects/{id}/readiness-policy",
        "edit_projects",
        200,
        "activate_policy",
    ),
    (trackers, "POST", "/trackers", "create_trackers", 201, "tracker_create"),
    (trackers, "PUT", "/trackers/{id}", "edit_trackers", 200, "tracker_update"),
    (trackers, "POST", "/trackers/{id}/sync", "edit_trackers", 202, "publish"),
    (trackers, "DELETE", "/trackers/{id}", "delete_trackers", 200, "publish"),
    (
        trackers,
        "DELETE",
        "/trackers/{id}?hard_delete=true",
        "delete_trackers",
        200,
        "publish",
    ),
    (
        bitbucket_dc_webhooks,
        "POST",
        "/trackers/{id}/bitbucket-dc/webhook/secret",
        "edit_trackers",
        200,
        "tracker_update",
    ),
    (
        bitbucket_dc_webhooks,
        "POST",
        "/trackers/{id}/bitbucket-dc/webhook/register",
        "edit_trackers",
        200,
        "register_webhook",
    ),
]


@pytest.fixture
def permission_user(db_session: Session) -> tuple[models.Account, models.User]:
    """Persist an ordinary active user without any role assignments."""
    account = crud.crud_account.create(
        db_session,
        obj_in={"organization_name": "Example Organization", "is_active": True},
    )
    primary = crud.crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "primary@example.com",
            "username": "example-primary",
            "is_active": True,
            "hashed_password": "unused-test-password",
            "user_source": "local",
        },
    )
    crud.crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": primary.id}
    )
    user = crud.crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "member@example.com",
            "username": "example-member",
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": False,
            "hashed_password": "unused-test-password",
            "user_source": "local",
        },
    )
    return account, user


@pytest.fixture
def route_operations(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    permission_user: tuple[models.Account, models.User],
) -> SimpleNamespace:
    """Use real permission reads while recording every downstream operation."""
    account, _ = permission_user
    now = datetime.now(UTC)
    tracker = SimpleNamespace(
        id=uuid4(),
        account_id=account.id,
        name="Example Tracker",
        tracker_type="gitlab",
        url="https://gitlab.example.com",
        auth_type="api_token",
        connection_details={},
        meta_data={},
        scope_rules=[],
        is_active=True,
        is_deleted=False,
        is_valid=True,
        created=now,
        last_updated=now,
        webhook_secret_id=uuid4(),
        resolved_webhook_secret="example-webhook-secret",
        resolved_api_key="example-token",
    )
    org = SimpleNamespace(
        id=uuid4(),
        tracker_id=tracker.id,
        tracker=tracker,
        name="Example Organization",
        identifier="example-org",
        description=None,
        settings={},
        meta_data={},
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    project = SimpleNamespace(
        id=uuid4(),
        organization_id=org.id,
        name="Example Project",
        identifier="example-project",
        description=None,
        settings={},
        tracker_settings={},
        created_at=now,
        updated_at=now,
    )
    effects: dict[str, MagicMock] = {}

    def record(target: object, name: str, key: str, result: object = None) -> MagicMock:
        operation = MagicMock(return_value=result)
        monkeypatch.setattr(target, name, operation)
        effects[key] = operation
        return operation

    for module in (organizations, projects):
        record(module.crud_organization, "get", f"{module.__name__}_org_get", org)
        record(
            module.crud_organization,
            "get_by_identifier",
            f"{module.__name__}_org_lookup",
        )
    record(organizations.crud_tracker, "get_for_account", "account_trackers", [tracker])
    record(organizations.crud_organization, "create", "org_create", org)
    record(organizations.crud_organization, "update", "org_update", org)
    record(organizations.crud_organization, "delete", "org_delete")
    record(projects, "_get_organization_in_account", "project_org", org)
    record(projects, "_get_project_in_account", "project_get", project)
    record(projects, "lock_repository_identity", "repository_lock")
    record(projects, "find_same_repository_projects", "repository_lookup", [])
    record(projects.crud_project, "get_by_identifier", "project_lookup")
    record(projects.crud_project, "create", "project_create", project)
    record(projects.crud_project, "update", "project_update", project)
    record(projects.crud_project, "delete", "project_delete")
    record(readiness, "activate_policy", "activate_policy")
    record(projects.crud_audit_log, "log_action", "policy_audit")
    record(crud.crud_tracker, "get_by_id_and_account", "tracker_get", tracker)
    record(crud.crud_tracker, "get_by_name", "tracker_lookup")
    record(crud.crud_tracker, "create", "tracker_create", tracker)
    record(crud.crud_tracker, "update", "tracker_update", tracker)
    record(crud.crud_tracker, "delete_scope_rules", "scope_delete")
    record(crud.crud_tool_configuration, "get_multi_by_account", "tool_configs", [])
    record(trackers, "has_tracker", "has_tracker", False)
    record(trackers, "get_tracker_types", "tracker_types", set())
    record(trackers, "log_config_change", "tracker_audit")
    record(trackers, "send_tracker_registered_email", "email")
    record(
        bitbucket_dc_webhooks,
        "generate_webhook_secret",
        "generate_secret",
        "new-example-secret",
    )
    remote = SimpleNamespace(
        test_connection=AsyncMock(
            return_value=SimpleNamespace(
                connected=True, success=True, message="Connected", details={}
            )
        ),
        get_organizations=AsyncMock(return_value=[]),
        get_projects=AsyncMock(return_value=[]),
        ensure_repository_webhook=AsyncMock(return_value={"id": 1, "created": True}),
    )
    for module in (trackers, bitbucket_dc_webhooks, projects):
        factory = AsyncMock(return_value=remote)
        monkeypatch.setattr(module, "create_tracker_client", factory)
        effects[f"{module.__name__}_client"] = factory
    effects["connection"] = remote.test_connection
    effects["register_webhook"] = remote.ensure_repository_webhook
    effects["publish"] = AsyncMock()
    monkeypatch.setattr(trackers.event_bus_service, "publish_task", effects["publish"])
    db = MagicMock(spec=Session, wraps=db_session)
    for name in ("add", "delete", "commit", "flush", "refresh", "rollback", "close"):
        operation = MagicMock()
        monkeypatch.setattr(db, name, operation)
        effects[f"db_{name}"] = operation
    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    monkeypatch.setattr(account_hooks, "_authorizer", None)
    monkeypatch.setattr(settings, "disable_rbac", False)
    monkeypatch.setattr(settings, "ticket_readiness_enabled", True)
    monkeypatch.setenv("DISABLE_RBAC", "false")
    monkeypatch.setenv("DISABLE_PROPRIETARY_PLUGINS", "true")
    monkeypatch.setenv("PRELOOP_BITBUCKET_DC_ENABLED", "true")
    monkeypatch.setenv("PRELOOP_URL", "https://preloop.example.com")
    return SimpleNamespace(
        db=db, tracker=tracker, org=org, project=project, effects=effects
    )


@pytest.mark.parametrize(
    "actor", ["non_holder", "primary", "holder", "superuser", "foreign_role"]
)
@pytest.mark.parametrize(
    ("module", "method", "path", "permission", "success", "effect"),
    ROUTES,
    ids=[f"{method}-{path}" for _, method, path, *_ in ROUTES],
)
def test_wrapped_route_permission_consistency(
    db_session: Session,
    permission_user: tuple[models.Account, models.User],
    route_operations: SimpleNamespace,
    actor: str,
    module: ModuleType,
    method: str,
    path: str,
    permission: str,
    success: int,
    effect: str,
) -> None:
    """Primary users, superusers and local permission holders reach the operation."""
    account, user = permission_user
    if actor == "primary":
        crud.crud_account.update(
            db_session, db_obj=account, obj_in={"primary_user_id": user.id}
        )
    elif actor == "superuser":
        crud.crud_user.update(db_session, db_obj=user, obj_in={"is_superuser": True})
    elif actor in {"holder", "foreign_role"}:
        granted = crud.crud_permission.get_by_name(db_session, name=permission)
        if granted is None:
            granted = crud.crud_permission.create(
                db_session,
                obj_in={
                    "name": permission,
                    "description": "Example permission",
                    "category": "example",
                },
            )
        role_account = account
        if actor == "foreign_role":
            role_account = crud.crud_account.create(
                db_session, obj_in={"organization_name": "Another Organization"}
            )
        role = crud.crud_role.create(
            db_session,
            obj_in={
                "account_id": role_account.id,
                "name": "example-permission-holder",
                "is_system_role": False,
            },
        )
        crud.crud_role.assign_permission(
            db_session, role_id=role.id, permission_id=granted.id
        )
        crud.crud_user_role.create(
            db_session, obj_in={"user_id": user.id, "role_id": role.id}
        )
    assert user.is_active
    assert user.is_superuser == (actor == "superuser")
    assert (str(account.primary_user_id) == str(user.id)) == (actor == "primary")
    assert permissions.user_holds_permission(db_session, user, permission) == (
        actor == "holder"
    )

    ops = route_operations
    payload: dict[str, object] = {"name": "Example", "identifier": "example"}
    resource_id = ops.org.id if module is organizations else ops.project.id
    if module is projects:
        payload["organization_id"] = str(ops.org.id)
        if "readiness-policy" in path:
            payload = {
                "required_build_keys": [],
                "minimum_approvals": 1,
                "changes_requests_block": True,
                "unresolved_tasks_block": True,
            }
    elif module is trackers:
        resource_id = ops.tracker.id
        payload = {
            "name": "Example Tracker",
            "type": "gitlab",
            "api_key": "example-token",
            "url": "https://gitlab.example.com",
            "scope_rules": [],
        }
    elif module is bitbucket_dc_webhooks:
        resource_id = ops.tracker.id
        ops.tracker.tracker_type = "bitbucket_dc"
        payload = {}

    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: ops.db
    app.dependency_overrides[get_current_active_user] = lambda: user
    with TestClient(app) as client:
        response = client.request(
            method, "/api/v1" + path.format(id=resource_id), json=payload
        )
    if actor in {"non_holder", "foreign_role"}:
        assert response.status_code == 403, response.text
        assert (
            response.json()["detail"]
            == f"Insufficient permissions. Required: {permission}"
        )
        for operation in ops.effects.values():
            operation.assert_not_called()
        assert ops.tracker.is_deleted is False
        assert ops.tracker.is_active is True
        assert ops.tracker.scope_rules == []
    else:
        assert response.status_code == success, response.text
        ops.effects[effect].assert_called_once()
        if module is trackers and method == "DELETE":
            if "hard_delete=true" in path:
                ops.db.delete.assert_called_once_with(ops.tracker)
            else:
                assert ops.tracker.is_deleted is True
                assert ops.tracker.is_active is False


@pytest.mark.parametrize(
    ("module", "path"),
    [
        (projects, "/projects/test-connection"),
        (trackers, "/trackers/test-and-list-orgs"),
        (trackers, "/trackers/list-projects-for-org"),
    ],
    ids=["project-connection", "tracker-connection", "tracker-projects"],
)
def test_connection_previews_remain_reads(
    permission_user: tuple[models.Account, models.User],
    route_operations: SimpleNamespace,
    module: ModuleType,
    path: str,
) -> None:
    """An authenticated preview does not require a tracker or project write grant."""
    _, user = permission_user
    ops = route_operations
    if module is projects:
        payload = {"organization": str(ops.org.id), "project": str(ops.project.id)}
    else:
        payload = {
            "tracker_type": "gitlab",
            "url": "https://gitlab.example.com",
            "api_key": "example-token",
            "organization_identifier": "example-org",
        }
    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: ops.db
    app.dependency_overrides[get_current_active_user] = lambda: user
    with TestClient(app) as client:
        response = client.post("/api/v1" + path, json=payload)
    assert response.status_code == 200, response.text
    ops.effects[f"{module.__name__}_client"].assert_awaited_once()
    for name in (
        "org_create",
        "org_update",
        "org_delete",
        "project_create",
        "project_update",
        "project_delete",
        "tracker_create",
        "tracker_update",
        "scope_delete",
        "activate_policy",
        "policy_audit",
        "tracker_audit",
        "publish",
        "email",
        "db_add",
        "db_delete",
        "db_commit",
        "generate_secret",
        "register_webhook",
    ):
        ops.effects[name].assert_not_called()
