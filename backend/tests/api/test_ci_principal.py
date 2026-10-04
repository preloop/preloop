"""Restricted identity lifecycle on synthetic resources and a local database."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.api.auth import jwt as jwt_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_principal import CiAction, CiGrant


@pytest.fixture(autouse=True)
def reset_ci_authority() -> Any:
    register_ci_administrator(None)
    yield
    register_ci_administrator(None)


@pytest.fixture
def ci_resources(
    db_session: Session,
) -> tuple[models.User, models.Project, models.Flow, CiGrant]:
    account = crud.crud_account.create(
        db_session, obj_in={"organization_name": "Synthetic CI", "is_active": True}
    )
    person = CRUDBase(models.Person).create(
        db_session, obj_in={"email_normalized": f"{uuid4()}@example.com"}
    )
    owner = crud.crud_user.create(
        db_session,
        obj_in={
            "username": f"operator-{uuid4()}",
            "email": person.email_normalized,
            "hashed_password": "fixture-only",
            "account_id": account.id,
            "person_id": person.id,
            "is_active": True,
        },
    )
    crud.crud_account.update(
        db_session, db_obj=account, obj_in={"primary_user_id": owner.id}
    )
    tracker = crud.crud_tracker.create(
        db_session,
        obj_in={
            "name": "Synthetic integration",
            "tracker_type": "github",
            "account_id": account.id,
            "is_active": True,
        },
    )
    organization = crud.crud_organization.create(
        db_session,
        obj_in={
            "name": "Example",
            "identifier": str(uuid4()),
            "tracker_id": tracker.id,
            "is_active": True,
        },
    )
    project = crud.crud_project.create(
        db_session,
        obj_in={
            "name": "Example repository",
            "identifier": str(uuid4()),
            "slug": "example/repository",
            "organization_id": organization.id,
            "is_active": True,
        },
    )
    flow = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": "Example review",
            "prompt_template": "Review approved changes",
            "agent_type": "openhands",
            "agent_config": {},
            "account_id": account.id,
            "is_enabled": True,
            "runner_pool": "server",
            "trigger_project_ids": [str(project.id)],
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {"project_id": str(project.id), "tracker_id": str(tracker.id)}
                ],
            },
        },
    )
    grant = CiGrant(
        version=1, project_id=project.id, flow_id=flow.id, actions=tuple(CiAction)
    )
    return owner, project, flow, grant


def provision(
    db: Session, resources: tuple[Any, ...]
) -> tuple[models.CiPrincipal, models.ApiKey, str]:
    owner, _, _, grant = resources
    return crud.crud_ci_principal.provision(
        db, actor=owner, name="Synthetic CI", grant=grant
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"actions": []},
        {"actions": ["admin:keys"]},
        {"actions": ["flow:trigger", "flow:trigger"]},
        {"version": 2},
        {"version": True},
        {"projects": []},
        {"project_id": None},
    ],
)
def test_grant_shape_fails_closed(invalid: dict[str, Any]) -> None:
    value = {
        "version": 1,
        "project_id": uuid4(),
        "flow_id": uuid4(),
        "actions": ["flow:trigger"],
    }
    value.update(invalid)
    with pytest.raises(ValidationError):
        CiGrant.model_validate(value)


def test_owner_provisions_hashed_key_without_human_authentication(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None and context.principal_id == principal.id
    assert not hasattr(context, "user")
    assert key.key is None and key.key_hash and key.key_hash != token
    assert crud.crud_api_key.get_by_key(db_session, key=token) is None
    assert crud.crud_api_key.validate_key(db_session, key=token) is None
    with pytest.raises(HTTPException) as denied:
        jwt_auth._authenticate_with_api_key(db_session, key)
    assert denied.value.status_code == 403
    assert jwt_auth.get_user_from_token_if_valid_sync(token, db_session) is None
    audit = crud.crud_audit_log.get_by_account(
        db_session, account_id=principal.account_id, action="ci_identity_provision"
    )
    assert len(audit) == 1 and audit[0].user_id == ci_resources[0].id
    assert token not in str(audit[0].details)


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
def test_partial_rollout_never_inherits_owner(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    _, _, token = provision(db_session, ci_resources)
    monkeypatch.setattr(jwt_auth.settings, "api_key_scope_enforcement", mode)
    with pytest.raises(HTTPException) as denied:
        jwt_auth.get_current_user(token=token, db=db_session)
    assert denied.value.status_code in (401, 403)


def test_rotation_preserves_ownership_and_revokes_old_key(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner, _, flow, _ = ci_resources
    principal, key, token = provision(db_session, ci_resources)
    execution = CRUDBase(models.FlowExecution).create(
        db_session,
        obj_in={
            "flow_id": flow.id,
            "ci_principal_id": principal.id,
            "initiating_ci_key_id": key.id,
        },
    )
    endpoint = CRUDBase(models.WebhookEndpoint).create(
        db_session,
        obj_in={
            "account_id": owner.account_id,
            "url": "https://example.com/completed",
            "secret_encrypted": "fixture-ciphertext",
            "event_types": ["flow.execution.finished"],
            "ci_principal_id": principal.id,
            "initiating_ci_key_id": key.id,
        },
    )
    new_key, new_token = crud.crud_ci_principal.rotate(
        db_session, actor=owner, principal_id=principal.id, key_id=key.id
    )
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None
    context = crud.crud_ci_principal.authenticate(db_session, token=new_token)
    assert context is not None and context.principal_id == principal.id
    db_session.refresh(execution)
    db_session.refresh(endpoint)
    assert (
        execution.ci_principal_id == endpoint.ci_principal_id == new_key.ci_principal_id
    )
    assert execution.initiating_ci_key_id == endpoint.initiating_ci_key_id == key.id
    assert execution.end_time is None
    crud.crud_ci_principal.revoke_key(
        db_session, actor=owner, principal_id=principal.id, key_id=new_key.id
    )
    assert crud.crud_ci_principal.authenticate(db_session, token=new_token) is None
    assert crud.crud_ci_principal.get(
        db_session, account_id=owner.account_id, principal_id=principal.id
    ).is_active


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "type",
        "scope",
        "actions",
        "empty_actions",
        "grant",
        "expired",
        "inactive_account",
        "disabled",
    ],
)
def test_fresh_state_and_malformed_markers_deny(
    db_session: Session, ci_resources: tuple[Any, ...], mutation: str
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    assert crud.crud_ci_principal.authenticate(db_session, token=token)
    if mutation == "version":
        key.credential_version = 2
    elif mutation == "type":
        key.credential_type = "unknown"
    elif mutation == "scope":
        key.scopes = ["mcp:read", "owner"]
    elif mutation == "actions":
        key.ci_actions = ["admin:keys"]
    elif mutation == "empty_actions":
        key.ci_actions = []
    elif mutation == "grant":
        principal.grant = {}
    elif mutation == "expired":
        key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    elif mutation == "inactive_account":
        account = crud.crud_account.get(db_session, id=key.account_id)
        account.is_active = False
    elif mutation == "disabled":
        principal.is_active = False
    db_session.commit()
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None
    assert crud.crud_api_key.get_by_key(db_session, key=token) is None


def test_grant_and_key_ceiling_intersection_and_rotation(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner, _, _, grant = ci_resources
    principal, key, token = crud.crud_ci_principal.provision(
        db_session,
        actor=owner,
        name="Narrow CI",
        grant=grant,
        key_actions=(CiAction.TRIGGER, CiAction.READ_EXECUTION),
    )
    narrower = grant.model_copy(
        update={"actions": (CiAction.READ_EXECUTION, CiAction.STOP_EXECUTION)}
    )
    crud.crud_ci_principal.change(
        db_session, actor=owner, principal_id=principal.id, grant=narrower
    )
    assert crud.crud_ci_principal.authenticate(
        db_session, token=token
    ).actions == frozenset({CiAction.READ_EXECUTION})
    new, new_token = crud.crud_ci_principal.rotate(
        db_session, actor=owner, principal_id=principal.id, key_id=key.id
    )
    assert new.ci_actions == [CiAction.READ_EXECUTION.value]
    assert crud.crud_ci_principal.authenticate(
        db_session, token=new_token
    ).actions == frozenset({CiAction.READ_EXECUTION})


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_flow",
        "foreign_project",
        "shared_flow",
        "misbound",
        "repository_override",
        "multiple_repositories",
        "member",
        "plugin_denial",
        "excess_key",
    ],
)
def test_unauthorized_provision_has_no_mutation(
    db_session: Session, ci_resources: tuple[Any, ...], mutation: str
) -> None:
    owner, project, flow, grant = ci_resources
    account = crud.crud_account.get(db_session, id=owner.account_id)
    if mutation in {"foreign_flow", "foreign_project"}:
        other = crud.crud_account.create(
            db_session, obj_in={"organization_name": "Other synthetic account"}
        )
        if mutation == "foreign_flow":
            flow.account_id = other.id
        else:
            tracker = crud.crud_tracker.get(
                db_session, id=project.organization.tracker_id
            )
            tracker.account_id = other.id
    elif mutation == "shared_flow":
        flow.account_id = None
    elif mutation == "misbound":
        flow.trigger_project_ids = [str(uuid4())]
    elif mutation == "repository_override":
        flow.git_clone_config = {
            "repositories": [
                {
                    "project_id": str(project.id),
                    "tracker_id": str(project.organization.tracker_id),
                    "repository_url": "https://example.com/other.git",
                }
            ]
        }
    elif mutation == "multiple_repositories":
        flow.git_clone_config = {"repositories": [{}, {}]}
    elif mutation == "member":
        account.primary_user_id = None
    elif mutation == "plugin_denial":
        register_ci_administrator(lambda *_: False)
    db_session.commit()
    kwargs = {"key_actions": ("admin:keys",)} if mutation == "excess_key" else {}
    before = (
        db_session.query(models.ApiKey).count(),
        db_session.query(models.CiPrincipal).count(),
    )
    with pytest.raises((PermissionError, ValueError)):
        crud.crud_ci_principal.provision(
            db_session, actor=owner, name="Denied CI", grant=grant, **kwargs
        )
    assert before == (
        db_session.query(models.ApiKey).count(),
        db_session.query(models.CiPrincipal).count(),
    )


def test_binding_move_invalidates_grant_and_machine_cannot_administer(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner, project, _, grant = ci_resources
    principal, key, token = provision(db_session, ci_resources)
    project.identifier = "different-synthetic-repository"
    db_session.commit()
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None
    owner._auth_api_key = key
    with pytest.raises(PermissionError):
        crud.crud_ci_principal.provision(
            db_session, actor=owner, name="Denied", grant=grant
        )


def test_null_history_and_legacy_runtime_contracts(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner, _, flow, _ = ci_resources
    execution = CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": flow.id}
    )
    assert execution.ci_principal_id is None and execution.initiating_ci_key_id is None
    legacy = crud.crud_api_key.create_with_owner(
        db_session, obj_in={"name": "Legacy"}, owner_username=owner.username
    )
    assert not legacy.requires_machine_authorization
    assert crud.crud_api_key.get_by_key(db_session, key=legacy.key).id == legacy.id
    runtime, token = crud.crud_api_key.create_runtime_key(
        db_session,
        name="Runtime",
        account_id=owner.account_id,
        user_id=owner.id,
        scopes=["mcp:read"],
    )
    assert not runtime.requires_machine_authorization
    assert crud.crud_api_key.get_by_key(db_session, key=token).id == runtime.id


def test_authentication_refreshes_another_sessions_changes(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner = ci_resources[0]
    principal, key, token = provision(db_session, ci_resources)
    connection = db_session.connection()
    with Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as first:
        cached = crud.crud_ci_principal.get(
            first, account_id=owner.account_id, principal_id=principal.id
        )
        assert cached.is_active
        assert crud.crud_ci_principal.authenticate(first, token=token)
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as second:
            changed = crud.crud_ci_principal.get(
                second, account_id=owner.account_id, principal_id=principal.id
            )
            changed.is_active = False
            second.commit()
        assert cached.is_active  # Deliberately stale identity map.
        assert crud.crud_ci_principal.authenticate(first, token=token) is None
        assert not cached.is_active


def test_expiry_failure_rolls_back_identity_key_and_audit(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    before = (
        db_session.query(models.CiPrincipal).count(),
        db_session.query(models.ApiKey).count(),
        db_session.query(models.AuditLog).count(),
    )
    with pytest.raises(ValueError, match="expiry"):
        crud.crud_ci_principal.provision(
            db_session,
            actor=ci_resources[0],
            name="Expired",
            grant=ci_resources[3],
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    assert before == (
        db_session.query(models.CiPrincipal).count(),
        db_session.query(models.ApiKey).count(),
        db_session.query(models.AuditLog).count(),
    )


def test_rotation_does_not_claim_another_principals_key(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    principal_a, _, _ = provision(db_session, ci_resources)
    principal_b, key_b, token_b = provision(db_session, ci_resources)
    with pytest.raises(ValueError):
        crud.crud_ci_principal.rotate(
            db_session,
            actor=ci_resources[0],
            principal_id=principal_a.id,
            key_id=key_b.id,
        )
    assert (
        crud.crud_ci_principal.authenticate(db_session, token=token_b).principal_id
        == principal_b.id
    )


@pytest.mark.parametrize("field", ["slug", "host", "provider", "lineage"])
def test_actual_repository_target_changes_invalidate_binding(
    db_session: Session, ci_resources: tuple[Any, ...], field: str
) -> None:
    _, project, _, _ = ci_resources
    _, _, token = provision(db_session, ci_resources)
    tracker = crud.crud_tracker.get(db_session, id=project.organization.tracker_id)
    if field == "slug":
        project.slug = "example/other-repository"
    elif field == "host":
        tracker.url = "https://different.example.com"
    elif field == "provider":
        tracker.tracker_type = "gitlab"
    else:
        organization = crud.crud_organization.create(
            db_session,
            obj_in={
                "name": "Other synthetic lineage",
                "identifier": str(uuid4()),
                "tracker_id": tracker.id,
                "is_active": True,
            },
        )
        project.organization_id = organization.id
    db_session.commit()
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None


@pytest.mark.parametrize("disable", [True, False])
def test_owner_can_restrict_an_unusable_binding(
    db_session: Session, ci_resources: tuple[Any, ...], disable: bool
) -> None:
    owner, _, flow, _ = ci_resources
    principal, key, token = provision(db_session, ci_resources)
    flow.is_enabled = False
    db_session.commit()
    if disable:
        crud.crud_ci_principal.change(
            db_session, actor=owner, principal_id=principal.id, enabled=False
        )
        assert not principal.is_active
    else:
        crud.crud_ci_principal.revoke_key(
            db_session, actor=owner, principal_id=principal.id, key_id=key.id
        )
        assert not key.is_active
    assert crud.crud_ci_principal.authenticate(db_session, token=token) is None


def test_legacy_human_key_management_cannot_bypass_ci_authority(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner = ci_resources[0]
    _, key, _ = provision(db_session, ci_resources)
    register_ci_administrator(lambda *_: False)
    assert (
        crud.crud_api_key.get_by_id_and_user(
            db_session, key_id=key.id, username=owner.username
        )
        is None
    )
    assert crud.crud_api_key.get_by_user(db_session, username=owner.username) == []
    assert (
        crud.crud_api_key.get_active_by_user(db_session, username=owner.username) == []
    )
    assert key.is_active


def test_internal_runtime_gateway_context_cannot_inherit_owner(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    from preloop.services.model_gateway_auth import build_runtime_key_auth_context

    _, key, token = provision(db_session, ci_resources)
    assert (
        build_runtime_key_auth_context(db_session, token=token, api_key_id=str(key.id))
        is None
    )


def test_model_copy_cannot_bypass_grant_validation(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    owner, _, _, grant = ci_resources
    forged = grant.model_copy(update={"actions": ("admin:keys",)})
    with pytest.raises(ValidationError):
        crud.crud_ci_principal.provision(
            db_session, actor=owner, name="Denied", grant=forged
        )


def test_aware_expiry_is_persisted_as_utc_without_database_timezone_conversion(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).astimezone(
        timezone(timedelta(hours=5))
    )
    _, key, _ = crud.crud_ci_principal.provision(
        db_session,
        actor=ci_resources[0],
        name="Short-lived CI",
        grant=ci_resources[3],
        expires_at=expiry,
    )
    db_session.refresh(key)
    assert key.expires_at == expiry.astimezone(timezone.utc).replace(tzinfo=None)


def test_restricted_key_cannot_invoke_new_human_logout_hook(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from preloop.api.app import create_app
    from preloop.models.db.session import get_db_session
    from preloop.plugins import account_hooks

    _, _, token = provision(db_session, ci_resources)
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    calls = []
    monkeypatch.setattr(
        account_hooks, "run_logout_hook", lambda *args: calls.append(args)
    )
    response = TestClient(app).post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code in (401, 403)
    assert calls == []
