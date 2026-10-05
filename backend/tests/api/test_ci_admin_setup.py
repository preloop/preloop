"""Independent HTTP contracts for human-only restricted CI setup."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import ci_identities
from preloop.api.middleware import ci_auth
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from preloop.plugins.ci_authorization import register_ci_administrator
from preloop.schemas.ci_principal import CiAction
from tests.api.test_ci_execution import review_binding
from tests.api.test_ci_principal import ci_resources as make_resources
from tests.api.test_ci_principal import provision

BASE = "/api/v1/ci-identities"


@pytest.fixture(autouse=True)
def authority() -> Any:
    register_ci_administrator(None)
    yield
    register_ci_administrator(None)


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(
    db_session: Session, ci_resources: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    def sessions() -> Any:
        yield db_session

    app = FastAPI()
    app.include_router(ci_identities.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = sessions
    app.dependency_overrides[get_current_active_user] = lambda: ci_resources[0]
    monkeypatch.setattr(ci_auth, "get_db_session", sessions)
    app.add_middleware(ci_auth.RestrictedCiAuthMiddleware)
    return TestClient(app)


def create(client: TestClient, resources: tuple[Any, ...]) -> dict[str, Any]:
    response = client.post(
        BASE,
        json={
            "name": "Synthetic review",
            "grant": resources[3].model_dump(mode="json"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def safe(value: Any, tokens: tuple[str, ...]) -> None:
    for token in tokens:
        assert token not in json.dumps(value)
    if isinstance(value, dict):
        assert not {"token", "key", "key_hash", "key_prefix", "secret"} & value.keys()
        for child in value.values():
            safe(child, tokens)
    elif isinstance(value, list):
        for child in value:
            safe(child, tokens)


def test_owner_preview_metadata_rotation_and_replacement(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    capabilities = client.get(BASE + "/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["available"] and capabilities.json()["can_manage"]
    preview = client.post(
        BASE + "/preview", json={"grant": ci_resources[3].model_dump(mode="json")}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["runner_pool"] == "server"
    own = create(client, ci_resources)
    pid, kid = own["identity"]["id"], own["key_id"]
    expiry = own["identity"]["keys"][0]["expires_at"]
    assert expiry
    rotate = client.post(f"{BASE}/{pid}/keys/{kid}/rotate", json={})
    assert rotate.status_code == 200, rotate.text
    new = rotate.json()
    assert new["token"] != own["token"]
    assert crud.crud_ci_principal.authenticate(db_session, token=own["token"]) is None
    assert (
        str(
            crud.crud_ci_principal.authenticate(
                db_session, token=new["token"]
            ).principal_id
        )
        == pid
    )
    for path in (BASE, f"{BASE}/{pid}"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        safe(response.json(), (own["token"], new["token"]))
    metadata = client.get(f"{BASE}/{pid}").json()
    assert (
        next(
            key["expires_at"] for key in metadata["keys"] if key["id"] == new["key_id"]
        )
        == expiry
    )
    assert client.delete(f"{BASE}/{pid}/keys/{new['key_id']}").status_code == 204
    assert crud.crud_ci_principal.authenticate(db_session, token=new["token"]) is None
    replacement = client.post(f"{BASE}/{pid}/keys", json={})
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["principal_id"] == pid
    audit = crud.crud_audit_log.get_by_account(
        db_session, account_id=ci_resources[0].account_id
    )
    assert audit
    for row in audit:
        assert row.user_id == ci_resources[0].id
        assert own["token"] not in str(row.details)
        assert new["token"] not in str(row.details)


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("GET", "/capabilities", None),
        ("POST", "/preview", "grant"),
        ("GET", "", None),
        ("POST", "", "create"),
        ("GET", "/owned", None),
        ("PATCH", "/owned", {"enabled": False}),
        ("POST", "/owned/keys", {}),
        ("POST", "/owned/keys/anchor/rotate", {}),
        ("DELETE", "/owned/keys/anchor", None),
        ("POST", "/owned/subscriptions", "subscription"),
    ],
)
def test_machine_denied_all_human_operations(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    method: str,
    suffix: str,
    body: Any,
) -> None:
    from preloop.api.auth import jwt as jwt_auth

    principal, key, token = provision(db_session, ci_resources)
    monkeypatch.setattr(jwt_auth.settings, "api_key_scope_enforcement", mode)
    grant = ci_resources[3].model_dump(mode="json")
    if body == "grant":
        body = {"grant": grant}
    elif body == "create":
        body = {"name": "forbidden", "grant": grant}
    elif body == "subscription":
        body = {"key_id": str(key.id), "url": "https://example.com/completed"}
    response = client.request(
        method,
        BASE
        + suffix.replace("owned", str(principal.id)).replace("anchor", str(key.id)),
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text
    db_session.refresh(principal)
    db_session.refresh(key)
    assert principal.is_active and key.is_active


@pytest.mark.parametrize("mutation", ["unknown", "action", "null", "empty", "owner"])
def test_closed_requests_no_mutation(
    ci_resources: tuple[Any, ...], client: TestClient, mutation: str
) -> None:
    own = create(client, ci_resources)
    payload: dict[str, Any] = {
        "name": "invalid",
        "grant": ci_resources[3].model_dump(mode="json"),
    }
    path, method = BASE, "POST"
    if mutation == "unknown":
        payload["grant"]["projects"] = []
    elif mutation == "action":
        payload["grant"]["actions"] = ["admin:keys"]
    elif mutation == "owner":
        payload["account_id"] = str(ci_resources[0].account_id)
    else:
        method, path = "PATCH", f"{BASE}/{own['identity']['id']}"
        payload = {"enabled": None} if mutation == "null" else {}
    response = client.request(method, path, json=payload)
    assert response.status_code == 422, response.text
    assert len(client.get(BASE).json()) == 1
    assert client.get(f"{BASE}/{own['identity']['id']}").json()["is_active"]


def test_view_only_and_fresh_revocation(
    ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    pid = own["identity"]["id"]
    register_ci_administrator(
        lambda _actor, operation, _grant: operation in {"view", "capabilities"}
    )
    assert client.get(BASE).status_code == 200
    assert client.get(f"{BASE}/{pid}").status_code == 200
    assert client.get(BASE + "/capabilities").json()["can_manage"] is False
    assert client.patch(f"{BASE}/{pid}", json={"enabled": False}).status_code == 403
    assert client.post(f"{BASE}/{pid}/keys", json={}).status_code == 403
    register_ci_administrator(lambda *_: False)
    assert client.get(BASE).status_code == 403
    assert client.get(f"{BASE}/{pid}").status_code == 403


def test_inactive_binding_recovery(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    pid, kid = own["identity"]["id"], own["key_id"]
    CRUDBase(models.Flow).update(
        db_session, db_obj=ci_resources[2], obj_in={"is_enabled": False}
    )
    assert client.get(f"{BASE}/{pid}").status_code == 200
    assert client.post(f"{BASE}/{pid}/keys", json={}).status_code == 400
    assert client.patch(f"{BASE}/{pid}", json={"enabled": False}).status_code == 200
    grant = ci_resources[3].model_copy(update={"actions": (CiAction.READ_EXECUTION,)})
    assert (
        client.patch(
            f"{BASE}/{pid}", json={"grant": grant.model_dump(mode="json")}
        ).status_code
        == 200
    )
    assert client.delete(f"{BASE}/{pid}/keys/{kid}").status_code == 204
    assert client.patch(f"{BASE}/{pid}", json={"enabled": True}).status_code == 400
    CRUDBase(models.Flow).update(
        db_session, db_obj=ci_resources[2], obj_in={"is_enabled": True}
    )
    recovered = client.patch(f"{BASE}/{pid}", json={"enabled": True})
    assert recovered.status_code == 200, recovered.text
    issued = client.post(f"{BASE}/{pid}/keys", json={})
    assert issued.status_code == 201, issued.text
    context = crud.crud_ci_principal.authenticate(
        db_session, token=issued.json()["token"]
    )
    assert context is not None
    assert str(context.principal_id) == pid


def test_partial_rollout_denies_usable_issuance(
    ci_resources: tuple[Any, ...], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = create(client, ci_resources)
    monkeypatch.setattr(ci_auth, "CI_ROUTE_POLICIES", {})
    assert client.get(BASE + "/capabilities").json()["available"] is False
    assert (
        client.post(
            BASE,
            json={"name": "blocked", "grant": ci_resources[3].model_dump(mode="json")},
        ).status_code
        == 503
    )
    assert (
        client.post(f"{BASE}/{own['identity']['id']}/keys", json={}).status_code == 503
    )
    assert client.get(f"{BASE}/{own['identity']['id']}").status_code == 200


def test_human_callback_stable_anchor_survives_revocation(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    pid, kid = own["identity"]["id"], own["key_id"]
    context = crud.crud_ci_principal.authenticate(db_session, token=own["token"])
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "SUCCEEDED"}
    )
    response = client.post(
        f"{BASE}/{pid}/subscriptions",
        json={"key_id": kid, "url": "https://example.com/completed"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["event_types"] == ["flow.execution.finished"]
    endpoint = CRUDBase(models.WebhookEndpoint).get(
        db_session, id=response.json()["id"]
    )
    assert str(endpoint.initiating_ci_key_id) == kid
    assert str(endpoint.ci_principal_id) == pid
    rotate = client.post(f"{BASE}/{pid}/keys/{kid}/rotate", json={})
    assert rotate.status_code == 200, rotate.text
    assert (
        client.delete(f"{BASE}/{pid}/keys/{rotate.json()['key_id']}").status_code == 204
    )
    payload = crud.crud_ci_subscription.callback_payload(
        db_session,
        endpoint=endpoint,
        account_id=ci_resources[0].account_id,
        event_type="flow.execution.finished",
        subject_id=execution.id,
    )
    assert payload is not None and payload["execution_id"] == str(execution.id)


@pytest.mark.parametrize("foreign_account", [False, True])
def test_foreign_anchor_rejection(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    foreign_account: bool,
) -> None:
    own = create(client, ci_resources)
    resources = (
        make_resources.__wrapped__(db_session) if foreign_account else ci_resources
    )
    other, key, token = provision(db_session, resources)
    pid = own["identity"]["id"]
    assert (
        client.post(
            f"{BASE}/{pid}/subscriptions",
            json={"key_id": str(key.id), "url": "https://example.com/completed"},
        ).status_code
        == 404
    )
    assert client.post(f"{BASE}/{pid}/keys/{key.id}/rotate", json={}).status_code == 400
    assert client.delete(f"{BASE}/{pid}/keys/{key.id}").status_code == 400
    assert (
        crud.crud_ci_principal.authenticate(db_session, token=token).principal_id
        == other.id
    )
    if foreign_account:
        assert client.get(f"{BASE}/{other.id}").status_code == 404
        assert all(row["id"] != str(other.id) for row in client.get(BASE).json())


def test_malformed_stored_key_actions_safe_metadata(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    key = CRUDBase(models.ApiKey).get(db_session, id=own["key_id"])
    CRUDBase(models.ApiKey).update(
        db_session,
        db_obj=key,
        obj_in={
            "ci_actions": [
                {"unexpected": "element"},
                "admin:keys",
                CiAction.READ_EXECUTION.value,
            ]
        },
    )
    for path in (BASE, f"{BASE}/{own['identity']['id']}"):
        response = client.get(path)
        assert response.status_code == 200, response.text
        safe(response.json(), (own["token"],))
        identities = response.json() if path == BASE else [response.json()]
        assert identities[0]["keys"][0]["actions"] == [CiAction.READ_EXECUTION.value]


def test_administrator_extension_error_denies_safely(
    ci_resources: tuple[Any, ...], client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    own = create(client, ci_resources)

    def unavailable(_actor: models.User, _operation: str, _grant: Any) -> bool:
        raise RuntimeError("synthetic-sensitive-extension-error")

    register_ci_administrator(unavailable)
    response = client.get(BASE + "/capabilities")
    assert response.status_code == 200, response.text
    assert response.json()["can_view"] is False
    assert response.json()["can_manage"] is False
    for method, path, body in [
        ("GET", BASE, None),
        ("GET", f"{BASE}/{own['identity']['id']}", None),
        ("PATCH", f"{BASE}/{own['identity']['id']}", {"enabled": False}),
        ("POST", f"{BASE}/{own['identity']['id']}/keys", {}),
    ]:
        denied = client.request(method, path, json=body)
        assert denied.status_code == 403, denied.text
        assert "synthetic-sensitive-extension-error" not in denied.text
    assert "synthetic-sensitive-extension-error" not in caplog.text


@pytest.mark.parametrize("operation", ["issue", "rotate"])
def test_repository_snapshot_change_denies_issuance_without_key_mutation(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    operation: str,
) -> None:
    own = create(client, ci_resources)
    pid, kid = own["identity"]["id"], own["key_id"]
    CRUDBase(models.Project).update(
        db_session,
        db_obj=ci_resources[1],
        obj_in={"slug": "example/changed-repository"},
    )
    before = db_session.query(models.ApiKey).count()
    suffix = "/keys" if operation == "issue" else f"/keys/{kid}/rotate"
    response = client.post(f"{BASE}/{pid}{suffix}", json={})
    assert response.status_code == 400, response.text
    assert db_session.query(models.ApiKey).count() == before
    db_session.refresh(CRUDBase(models.ApiKey).get(db_session, id=kid))
    assert CRUDBase(models.ApiKey).get(db_session, id=kid).is_active is True


@pytest.mark.parametrize("operation", ["issue", "rotate", "subscription"])
@pytest.mark.parametrize(
    "field", ["account_id", "ci_principal_id", "secret", "resource_scope"]
)
def test_management_control_overrides_are_closed(
    ci_resources: tuple[Any, ...], client: TestClient, operation: str, field: str
) -> None:
    own = create(client, ci_resources)
    pid, kid = own["identity"]["id"], own["key_id"]
    path = f"{BASE}/{pid}/keys"
    payload: dict[str, Any] = {}
    if operation == "rotate":
        path += f"/{kid}/rotate"
    elif operation == "subscription":
        path = f"{BASE}/{pid}/subscriptions"
        payload = {"key_id": kid, "url": "https://example.com/completed"}
    payload[field] = "forged"
    assert client.post(path, json=payload).status_code == 422
    metadata = client.get(f"{BASE}/{pid}").json()
    assert len(metadata["keys"]) == 1 and metadata["keys"][0]["is_active"]


@pytest.mark.parametrize(
    "events",
    [
        [],
        ["approval.requested"],
        ["flow.execution.finished", "approval.requested"],
        None,
    ],
)
def test_human_callback_cannot_broaden_fixed_filter(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient, events: Any
) -> None:
    own = create(client, ci_resources)
    response = client.post(
        f"{BASE}/{own['identity']['id']}/subscriptions",
        json={
            "key_id": own["key_id"],
            "url": "https://example.com/completed",
            "event_types": events,
        },
    )
    assert response.status_code == 422, response.text
    assert (
        not db_session.query(models.WebhookEndpoint)
        .filter_by(ci_principal_id=own["identity"]["id"])
        .all()
    )


def test_expired_key_replacement_retains_principal_binding(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    key = CRUDBase(models.ApiKey).get(db_session, id=own["key_id"])
    CRUDBase(models.ApiKey).update(
        db_session,
        db_obj=key,
        obj_in={
            "expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1)
        },
    )
    assert crud.crud_ci_principal.authenticate(db_session, token=own["token"]) is None
    response = client.post(f"{BASE}/{own['identity']['id']}/keys", json={})
    assert response.status_code == 201, response.text
    context = crud.crud_ci_principal.authenticate(
        db_session, token=response.json()["token"]
    )
    assert str(context.principal_id) == own["identity"]["id"]
    assert context.project_id == ci_resources[1].id
    assert context.flow_id == ci_resources[2].id
    metadata = client.get(f"{BASE}/{own['identity']['id']}").json()
    safe(metadata, (own["token"], response.json()["token"]))
    assert len(metadata["keys"]) == 2


def test_permission_denial_audit_contains_safe_attribution(
    db_session: Session, ci_resources: tuple[Any, ...], client: TestClient
) -> None:
    own = create(client, ci_resources)
    register_ci_administrator(lambda *_: False)
    response = client.post(
        f"{BASE}/{own['identity']['id']}/keys/{own['key_id']}/rotate", json={}
    )
    assert response.status_code == 403
    records = crud.crud_audit_log.get_by_account(
        db_session,
        account_id=ci_resources[0].account_id,
        action="ci_identity_administration_denied",
    )
    assert len(records) == 1
    record = records[0]
    assert record.user_id == ci_resources[0].id
    assert record.account_id == ci_resources[0].account_id
    assert record.details["principal_id"] == own["identity"]["id"]
    assert record.details["key_id"] == own["key_id"]
    assert record.details["operation"] == "rotate"
    assert own["token"] not in json.dumps(record.details)


def test_router_auth_protects_future_route_without_actor_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import APIRouter, HTTPException

    from preloop.api.app import create_app

    router = APIRouter()
    router.include_router(ci_identities.router)

    @router.get(BASE.removeprefix("/api/v1") + "/synthetic-auth-probe/without-actor")
    def probe() -> dict[str, bool]:
        return {"reached": True}

    def unauthenticated() -> None:
        raise HTTPException(status_code=401, detail="Synthetic unauthenticated")

    monkeypatch.setattr(ci_identities, "router", router)
    app = create_app()
    app.dependency_overrides[get_current_active_user] = unauthenticated
    response = TestClient(app).get(BASE + "/synthetic-auth-probe/without-actor")
    assert response.status_code == 401


def test_v1_machine_route_expansion_requires_explicit_rollout_review(
    ci_resources: tuple[Any, ...], client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = create(client, ci_resources)
    policies = dict(ci_auth.CI_ROUTE_POLICIES)
    policies[("GET", "/api/v1/synthetic-new-machine-route")] = CiAction.READ_EXECUTION
    monkeypatch.setattr(ci_auth, "CI_ROUTE_POLICIES", policies)
    assert client.get(BASE + "/capabilities").json()["available"] is False
    assert (
        client.post(f"{BASE}/{own['identity']['id']}/keys", json={}).status_code == 503
    )
    assert client.get(f"{BASE}/{own['identity']['id']}").status_code == 200
