"""Restricted REST authorization cannot adopt human authority or stale grants."""

from dataclasses import replace
from typing import Any
from uuid import uuid4
import pytest
from sqlalchemy.orm import Session
from preloop.models import crud
from preloop.schemas.ci_principal import CiAction
from tests.api.test_ci_principal import ci_resources as create_ci_resources, provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    """Reuse the tested synthetic identity binding without importing a fixture name."""
    return create_ci_resources.__wrapped__(db_session)


def test_downstream_authorization_rechecks_revoked_key(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    principal, key, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    assert (
        crud.crud_ci_principal.authorize(
            db_session, context=context, action=CiAction.TRIGGER
        )
        == context
    )
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    with pytest.raises(PermissionError):
        crud.crud_ci_principal.authorize(
            db_session, context=context, action=CiAction.TRIGGER
        )


def test_forged_context_cannot_change_account_or_binding(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    assert context is not None
    for field in ("account_id", "principal_id", "project_id", "flow_id"):
        with pytest.raises(PermissionError):
            crud.crud_ci_principal.authorize(
                db_session,
                context=replace(context, **{field: uuid4()}),
                action=CiAction.TRIGGER,
            )


@pytest.fixture
def ci_http(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    """The middleware's worker owns a borrowed isolated fixture session."""
    from preloop.api.middleware import ci_auth

    def sessions() -> Any:
        yield db_session

    monkeypatch.setattr(ci_auth, "get_db_session", sessions)


@pytest.mark.parametrize("transport", ["bearer", "header", "query", "duplicate"])
def test_valid_forbidden_write_denies_before_handler(
    db_session: Session, ci_resources: tuple[Any, ...], ci_http: None, transport: str
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware

    _, _, token = provision(db_session, ci_resources)
    project = ci_resources[1]
    app = FastAPI()
    calls = []

    @app.put("/api/v1/projects/{project_id}")
    def edit_project(project_id: str, payload: dict) -> Any:
        calls.append(project_id)
        crud.crud_project.update(db_session, db_obj=project, obj_in=payload)
        return {"name": project.name}

    app.add_middleware(RestrictedCiAuthMiddleware)
    headers = [("Authorization", f"Bearer {token}")]
    query = ""
    if transport == "header":
        headers = [("x-api-key", token)]
    elif transport == "query":
        headers = []
        query = f"?token={token}"
    elif transport == "duplicate":
        headers.append(("Authorization", "Bearer unrelated-human-token"))
    client = TestClient(app)
    before = project.name
    response = client.put(
        f"/api/v1/projects/{project.id}{query}",
        headers=headers,
        json={"name": "Changed by unauthorized caller"},
    )
    assert response.status_code in (401, 403)
    assert not calls
    assert crud.crud_project.get(db_session, id=project.id).name == before


def test_machine_websocket_is_denied_before_accept(
    db_session: Session, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware

    _, _, token = provision(db_session, ci_resources)
    app = FastAPI()
    calls = []

    @app.websocket("/custom-stream")
    async def stream(socket: WebSocket) -> Any:
        calls.append(True)
        await socket.accept()

    app.add_middleware(RestrictedCiAuthMiddleware)
    with pytest.raises(WebSocketDisconnect) as denied:
        with TestClient(app).websocket_connect(f"/custom-stream?token={token}"):
            pass
    assert denied.value.code == 1008
    assert not calls


def test_unknown_routes_deny_machine_but_preserve_public_behavior(
    db_session: Session, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware

    _, _, token = provision(db_session, ci_resources)
    app = FastAPI()
    app.add_middleware(RestrictedCiAuthMiddleware)
    client = TestClient(app)
    assert client.get("/new-unclassified-route").status_code == 404
    assert (
        client.get(
            "/new-unclassified-route", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )


@pytest.fixture(autouse=True)
def reset_machine_policy() -> Any:
    from preloop.plugins.ci_authorization import register_ci_machine_authorizer

    register_ci_machine_authorizer(None)
    yield
    register_ci_machine_authorizer(None)


def test_route_inventory_includes_lazy_hidden_and_new_routes(app: Any) -> None:
    import json
    from pathlib import Path
    from preloop.api.auth.ci_policy import restricted_route_inventory
    from preloop.api.middleware.ci_auth import CI_ROUTE_POLICIES

    declared = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "ci_route_inventory.json").read_text()
    )
    actual = restricted_route_inventory(app)
    assert actual == declared
    assert actual["GET /api/v1/auth/api-keys"] == "deny"
    assert actual["GET /docs/api"] == "deny"
    assert actual["MOUNT /mcp"] == "deny"
    assert {
        (name.split(" ", 1)[0], name.split(" ", 1)[1]): policy
        for name, policy in actual.items()
        if policy != "deny"
    } == {key: action.value for key, action in CI_ROUTE_POLICIES.items()}

    @app.get("/api/v1/new-administrative-route")
    def added() -> Any:
        return {"protected": True}

    with pytest.raises(AssertionError):
        assert restricted_route_inventory(app) == declared


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize("rbac_disabled", [True, False])
def test_real_routes_deny_valid_requests_without_side_effects(
    app: Any,
    db_session: Session,
    ci_resources: tuple[Any, ...],
    ci_http: None,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    rbac_disabled: bool,
) -> None:
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    from preloop.api.auth import get_current_active_user
    from preloop.config import settings
    from preloop.models.crud.base import CRUDBase
    from preloop.models import models
    from preloop.plugins.ci_authorization import register_ci_machine_authorizer
    from preloop.services.flow_trigger_service import FlowTriggerService

    _, key, token = provision(db_session, ci_resources)
    owner, project, flow, _ = ci_resources
    app.dependency_overrides[get_current_active_user] = lambda: owner
    monkeypatch.setattr(settings, "api_key_scope_enforcement", mode)
    monkeypatch.setattr(settings, "disable_rbac", rbac_disabled)
    register_ci_machine_authorizer(lambda context, action: True)
    trigger = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "trigger_flow", trigger)
    from preloop.schemas.auth import ApiKeyCreate, RefreshRequest
    from preloop.schemas.project import ProjectUpdate

    ApiKeyCreate.model_validate({"name": "Forbidden issuance", "scopes": []})
    ProjectUpdate.model_validate({"name": "Forbidden edit"})
    RefreshRequest.model_validate({"refresh_token": token})
    before_name = project.name
    before = len(CRUDBase(models.ApiKey).get_multi(db_session, limit=10000))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    requests = [
        ("GET", "/api/v1/auth/api-keys", None),
        ("POST", "/api/v1/auth/api-keys", {"name": "Forbidden issuance", "scopes": []}),
        ("DELETE", f"/api/v1/auth/api-keys/{key.id}", None),
        ("PUT", f"/api/v1/projects/{project.id}", {"name": "Forbidden edit"}),
        (
            "POST",
            f"/api/v1/flows/{flow.id}/trigger",
            {"pull_request_number": 1, "head_sha": "a" * 40},
        ),
        ("GET", "/api/v1/runtime-sessions", None),
        ("GET", "/api/v1/runners", None),
        ("POST", "/api/v1/auth/refresh", {"refresh_token": token}),
    ]
    for method, path, payload in requests:
        response = client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == {"detail": "Restricted CI authorization denied"}
    assert len(CRUDBase(models.ApiKey).get_multi(db_session, limit=10000)) == before
    assert crud.crud_api_key.get(db_session, id=key.id).is_active
    assert crud.crud_project.get(db_session, id=project.id).name == before_name
    trigger.assert_not_called()


def test_explicit_machine_handler_uses_context_and_fresh_grant(
    db_session: Session, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.ci import get_current_actor
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware
    from preloop.models import models
    from preloop.models.crud.ci_principal import CiAuthorizationContext
    from preloop.models.db.session import get_db_session
    from preloop.plugins.ci_authorization import register_ci_machine_authorizer
    from preloop.utils.permissions import require_permission

    principal, key, token = provision(db_session, ci_resources)
    app = FastAPI()
    app.dependency_overrides[get_db_session] = lambda: db_session

    @app.post("/machine-probe")
    @require_permission("execute_flows", ci_action=CiAction.TRIGGER)
    def probe(
        db: Session = Depends(get_db_session),
        current_user: models.User | CiAuthorizationContext = Depends(get_current_actor),
    ) -> Any:
        assert isinstance(current_user, CiAuthorizationContext)
        assert not hasattr(current_user, "id")
        return {"principal": str(current_user.principal_id)}

    app.add_middleware(
        RestrictedCiAuthMiddleware,
        policies={("POST", "/machine-probe"): CiAction.TRIGGER},
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/machine-probe", headers=headers).json() == {
        "principal": str(principal.id)
    }
    register_ci_machine_authorizer(lambda context, action: False)
    assert client.post("/machine-probe", headers=headers).status_code == 403
    register_ci_machine_authorizer(lambda context, action: True)
    assert client.post("/machine-probe", headers=headers).status_code == 200
    grant = ci_resources[3].model_copy(update={"actions": (CiAction.READ_EXECUTION,)})
    crud.crud_ci_principal.change(
        db_session, actor=ci_resources[0], principal_id=principal.id, grant=grant
    )
    assert client.post("/machine-probe", headers=headers).status_code == 403
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    assert client.post("/machine-probe", headers=headers).status_code == 401


def test_unmarked_permission_decorator_denies_positional_machine_context(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    from fastapi import HTTPException
    from preloop.utils.permissions import require_permission

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)

    @require_permission("manage_users")
    def administrative(db: Any, current_user: Any) -> Any:
        raise AssertionError("Machine reached human administrative code")

    with pytest.raises(HTTPException) as denied:
        administrative(db_session, context)
    assert denied.value.status_code == 403

    @require_permission("manage_users")
    def variadic(**kwargs: Any) -> None:
        raise AssertionError("Machine reached variadic human administrative code")

    with pytest.raises(HTTPException) as denied:
        variadic(db=db_session, current_user=context)
    assert denied.value.status_code == 403


def test_new_mounted_authenticated_route_changes_inventory() -> None:
    from fastapi import Depends, FastAPI
    from preloop.api.auth import get_current_active_user
    from preloop.api.auth.ci_policy import restricted_route_inventory

    app, child = (FastAPI(), FastAPI())
    from starlette.middleware.authentication import AuthenticationMiddleware
    from preloop.services.mcp_http import PreloopBearerAuthBackend

    app.mount(
        "/mounted", AuthenticationMiddleware(child, backend=PreloopBearerAuthBackend())
    )
    before = restricted_route_inventory(app)

    @child.get("/protected", dependencies=[Depends(get_current_active_user)])
    def protected() -> Any:
        return {"protected": True}

    after = restricted_route_inventory(app)
    assert before != after
    assert after["GET /mounted/protected"] == "deny"


def test_overlapping_allow_pattern_cannot_enable_public_handler(
    db_session: Session, ci_resources: tuple[Any, ...], ci_http: None
) -> None:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth.ci import get_current_actor
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware
    from preloop.models.crud.ci_principal import CiAuthorizationContext
    from preloop.models.db.session import get_db_session
    from preloop.utils.permissions import require_permission

    _, _, token = provision(db_session, ci_resources)
    app = FastAPI()
    calls = []

    @app.get("/probe/administration")
    def public_alias() -> Any:
        calls.append(True)
        return {"protected": True}

    @app.get("/probe/{resource}")
    @require_permission("view_flows", ci_action=CiAction.READ_EXECUTION)
    def machine_probe(
        resource: str,
        db: Session = Depends(get_db_session),
        current_user: Any = Depends(get_current_actor),
    ) -> Any:
        assert isinstance(current_user, CiAuthorizationContext)
        return {"resource": resource}

    app.add_middleware(
        RestrictedCiAuthMiddleware,
        policies={("GET", "/probe/{resource}"): CiAction.READ_EXECUTION},
    )
    response = TestClient(app).get(
        "/probe/administration", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert not calls


def test_machine_policy_error_denies_without_logging_sensitive_error(
    db_session: Session, ci_resources: tuple[Any, ...], caplog: Any
) -> None:
    from preloop.plugins.ci_authorization import register_ci_machine_authorizer

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)

    def broken(context: Any, action: Any) -> Any:
        raise RuntimeError("sensitive-extension-detail")

    register_ci_machine_authorizer(broken)
    with pytest.raises(PermissionError):
        crud.crud_ci_principal.authorize(
            db_session, context=context, action=CiAction.TRIGGER
        )
    assert "sensitive-extension-detail" not in caplog.text
    assert token not in caplog.text


def test_lookup_database_error_never_logs_token_parameters(
    monkeypatch: Any, caplog: Any
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import ProgrammingError
    from preloop.api.middleware import ci_auth

    synthetic_token = "synthetic-sensitive-bearer-for-error-test"

    def failed(tokens: Any, action: Any) -> Any:
        raise ProgrammingError(
            "SELECT api_key WHERE key = :key",
            {"key": synthetic_token},
            RuntimeError("synthetic database failure"),
        )

    monkeypatch.setattr(ci_auth, "_inspect", failed)
    app = FastAPI()
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    response = TestClient(app).get(
        "/protected", headers={"Authorization": f"Bearer {synthetic_token}"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Credential verification unavailable"}
    assert synthetic_token not in response.text
    assert synthetic_token not in caplog.text
    assert "SELECT api_key" not in caplog.text


def test_revoked_key_denial_retains_safe_attribution(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    ci_http: None,
    caplog: Any,
) -> None:
    import logging
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.middleware.ci_auth import RestrictedCiAuthMiddleware

    principal, key, token = provision(db_session, ci_resources)
    crud.crud_ci_principal.revoke_key(
        db_session, actor=ci_resources[0], principal_id=principal.id, key_id=key.id
    )
    app = FastAPI()
    app.add_middleware(RestrictedCiAuthMiddleware)
    with caplog.at_level(logging.INFO, logger="preloop.api.middleware.ci_auth"):
        response = TestClient(app).get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 401
    decisions = [
        r for r in caplog.records if r.message == "Restricted CI request denied"
    ]
    assert decisions[-1].ci_principal_id == str(principal.id)
    assert decisions[-1].ci_key_id == str(key.id)
    assert decisions[-1].ci_project_id == str(ci_resources[1].id)
    assert decisions[-1].ci_flow_id == str(ci_resources[2].id)
    assert token not in caplog.text
