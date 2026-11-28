"""Single-use provisioning and least-privilege per-user app enrollment."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from preloop.api.auth.key_scopes import (
    ENROLLMENT_PROVISION_SCOPE,
    api_key_allowed_on_channel,
    enforce_api_key_route_scope,
    is_single_purpose_api_key,
)
from preloop.api.endpoints.agent_enrollment import create_authority, exchange
from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.schemas.agent_enrollment import (
    EnrollmentExchangeRequest,
    EnrollmentGrantRequest,
)
from preloop.services.agent_enrollment import exchange_authority, issue_authority


def _request(method: str, path: str) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


def _payload(**changes: object) -> EnrollmentExchangeRequest:
    values = {
        "workstation_fingerprint": "a" * 64,
        "user_ref": "b" * 64,
        "device_ref_hash": "c" * 64,
        "apps": ["claude_code", "cursor"],
    }
    values.update(changes)
    return EnrollmentExchangeRequest.model_validate(values)


@pytest.mark.parametrize("mode", ["off", "audit", "enforce"])
@pytest.mark.parametrize(
    "scopes", [[ENROLLMENT_PROVISION_SCOPE], [ENROLLMENT_PROVISION_SCOPE, "*"]]
)
def test_provisioning_authority_has_exact_one_route(
    mode: str, scopes: list[str]
) -> None:
    key = SimpleNamespace(scopes=scopes)
    with patch("preloop.api.auth.key_scopes.settings.api_key_scope_enforcement", mode):
        for method, path in [
            ("GET", "/api/v1/agents"),
            ("GET", "/api/v1/fleet/devices"),
            ("POST", "/api/v1/agents/discovery-reports"),
            ("GET", "/api/v1/agents/discovery-salt"),
            ("POST", "/api/v1/agents/enrollment/authorities"),
            ("GET", "/api/v1/agents/enrollment/exchange"),
            ("POST", "/api/v1/agents/enrollment/exchange/other"),
            ("POST", "/mcp/v1"),
        ]:
            with pytest.raises(HTTPException) as exc:
                enforce_api_key_route_scope(key, _request(method, path))
            assert exc.value.status_code == 403
        if len(scopes) == 1:
            enforce_api_key_route_scope(
                key, _request("POST", "/api/v1/agents/enrollment/exchange")
            )
        else:
            with pytest.raises(HTTPException):
                enforce_api_key_route_scope(
                    key, _request("POST", "/api/v1/agents/enrollment/exchange")
                )
        with pytest.raises(HTTPException):
            enforce_api_key_route_scope(key, None)
    assert is_single_purpose_api_key(key)
    assert not api_key_allowed_on_channel(key, "console")


@pytest.mark.parametrize("scope", ["report_discovery", "mcp:read", "telemetry:ingest"])
def test_report_and_runtime_keys_cannot_enroll_or_issue(scope: str) -> None:
    key = SimpleNamespace(scopes=[scope], context_data={})
    user = SimpleNamespace(_auth_api_key=key)
    with pytest.raises(HTTPException):
        exchange(_payload(), Response(), user, MagicMock())
    with pytest.raises(HTTPException):
        create_authority(
            EnrollmentGrantRequest(apps=["cursor"]), Response(), user, MagicMock()
        )


def test_app_runtime_never_gains_account_rest_access_even_in_off_mode() -> None:
    key = SimpleNamespace(
        scopes=["mcp:read", "mcp:write"], context_data={"unattended_enrollment": {}}
    )
    with patch("preloop.api.auth.key_scopes.settings.api_key_scope_enforcement", "off"):
        with pytest.raises(HTTPException):
            enforce_api_key_route_scope(key, _request("GET", "/api/v1/agents"))
        assert not api_key_allowed_on_channel(key, "console")


@pytest.mark.parametrize(
    "extra",
    [
        {"user_ref": "Jane Doe"},
        {"workstation_fingerprint": "raw-device"},
        {"device_ref": "raw-mdm-id"},
        {"apps": ["cursor", "cursor"]},
        {"apps": ["all"]},
        {"account_id": str(uuid4())},
        {"apps": []},
    ],
)
def test_exchange_schema_refuses_raw_identity_and_unselected_scope(extra: dict) -> None:
    with pytest.raises(ValidationError):
        _payload(**extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"expires_minutes": 61},
        {"credential_hours": 2161},
        {"allowed_mcp_tools": ["", ""]},
        {"apps": ["cursor", "cursor"]},
    ],
)
def test_grants_are_bounded(extra: dict) -> None:
    with pytest.raises(ValidationError):
        EnrollmentGrantRequest.model_validate({"apps": ["cursor"], **extra})


def test_atomic_exchange_creates_only_bound_credentials(
    db_session: Session, test_user: models.User
) -> None:
    grant = EnrollmentGrantRequest(
        apps=["claude_code", "cursor"], allowed_mcp_tools=["example_tool"]
    )
    key, secret = issue_authority(db_session, user=test_user, grant=grant)
    assert key.key is None and key.scopes == [ENROLLMENT_PROVISION_SCOPE]
    result = exchange_authority(db_session, authenticated_key=key, payload=_payload())
    db_session.flush()
    assert not key.is_active
    assert len(result.apps) == 2
    assert (
        db_session.query(models.ManagedAgent)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 2
    )
    for credential in result.apps:
        runtime = crud_api_key.get_by_key(db_session, key=credential.token)
        assert runtime.key is None
        assert runtime.scopes == ["mcp:read", "mcp:write"]
        assert runtime.context_data["unattended_enrollment"] == {
            "workstation_fingerprint": "a" * 64,
            "user_ref": "b" * 64,
            "device_ref_hash": "c" * 64,
            "app": credential.app,
        }
        assert runtime.context_data["allowed_mcp_tools"] == ["example_tool"]
        assert runtime.context_data["managed_agent_id"] == str(credential.agent_id)
        assert (
            runtime.context_data["runtime_principal"]["id"]
            == credential.runtime_principal_id
        )
    audits = (
        db_session.query(models.AuditLog)
        .filter_by(account_id=test_user.account_id)
        .all()
    )
    assert len(audits) == 2
    assert all(secret not in str(row.details) for row in audits)
    with pytest.raises(HTTPException) as exc:
        exchange_authority(db_session, authenticated_key=key, payload=_payload())
    assert exc.value.status_code == 403


@pytest.mark.parametrize("state", ["revoked", "expired", "broadened", "invalid_grant"])
def test_revoked_expired_and_malformed_authority_never_mints(
    db_session: Session, test_user: models.User, state: str
) -> None:
    key, _ = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    mutation = {
        "revoked": {"is_active": False},
        "expired": {
            "expires_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        },
        "broadened": {"scopes": [ENROLLMENT_PROVISION_SCOPE, "*"]},
        "invalid_grant": {"context_data": {"app_enrollment_grant": None}},
    }[state]
    crud_api_key.update(db_session, db_obj=key, obj_in=mutation, commit=False)
    with pytest.raises(HTTPException) as exc:
        exchange_authority(
            db_session, authenticated_key=key, payload=_payload(apps=["cursor"])
        )
    assert exc.value.status_code == 403
    assert (
        db_session.query(models.ManagedAgent)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 0
    )


def test_selection_mismatch_does_not_consume_grant(
    db_session: Session, test_user: models.User
) -> None:
    key, _ = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    with pytest.raises(HTTPException):
        exchange_authority(db_session, authenticated_key=key, payload=_payload())
    assert key.is_active
    assert (
        db_session.query(models.ManagedAgent)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 0
    )


def test_distinct_os_users_are_distinct_apps_and_existing_identity_never_reissues(
    db_session: Session, test_user: models.User
) -> None:
    grant = EnrollmentGrantRequest(apps=["cursor"])
    first, _ = issue_authority(db_session, user=test_user, grant=grant)
    result = exchange_authority(
        db_session, authenticated_key=first, payload=_payload(apps=["cursor"])
    )
    second, _ = issue_authority(db_session, user=test_user, grant=grant)
    other = exchange_authority(
        db_session,
        authenticated_key=second,
        payload=_payload(apps=["cursor"], user_ref="d" * 64),
    )
    assert result.apps[0].agent_id != other.apps[0].agent_id
    runtime = crud_api_key.get_by_key(db_session, key=result.apps[0].token)
    crud_api_key.update(
        db_session, db_obj=runtime, obj_in={"is_active": False}, commit=False
    )
    fresh, _ = issue_authority(db_session, user=test_user, grant=grant)
    with pytest.raises(HTTPException) as exc:
        exchange_authority(
            db_session, authenticated_key=fresh, payload=_payload(apps=["cursor"])
        )
    assert exc.value.status_code == 409
    assert not runtime.is_active
    assert fresh.is_active


def test_exchange_route_rolls_back_failed_bundle_before_returning() -> None:
    db = MagicMock()
    user = SimpleNamespace(
        _auth_api_key=SimpleNamespace(scopes=[ENROLLMENT_PROVISION_SCOPE])
    )
    with patch(
        "preloop.api.endpoints.agent_enrollment.exchange_authority",
        side_effect=HTTPException(409, "synthetic collision"),
    ):
        with pytest.raises(HTTPException):
            exchange(_payload(), Response(), user, db)
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_concurrent_exchange_only_one_mints(db_engine: Engine) -> None:
    """Independent PostgreSQL sessions serialize one leaked/retried authority."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from preloop.models.crud import crud_account, crud_user

    with Session(db_engine) as setup:
        account = crud_account.create(
            setup, obj_in={"organization_name": "Synthetic enrollment"}
        )
        account_id = account.id
        user = crud_user.create(
            setup,
            obj_in={
                "account_id": account.id,
                "username": f"synthetic-{uuid4().hex}",
                "email": f"{uuid4().hex}@example.com",
                "is_active": True,
                "hashed_password": "synthetic-test-only",
            },
        )
        _, token = issue_authority(
            setup, user=user, grant=EnrollmentGrantRequest(apps=["cursor"])
        )
        setup.commit()
    barrier = Barrier(2)

    def attempt() -> int:
        with Session(db_engine) as session:
            key = crud_api_key.get_by_key(session, key=token)
            barrier.wait(timeout=10)
            try:
                exchange_authority(
                    session, authenticated_key=key, payload=_payload(apps=["cursor"])
                )
                session.commit()
                return 200
            except HTTPException as exc:
                session.rollback()
                return exc.status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _: attempt(), range(2))) == [200, 403]
        with Session(db_engine) as check:
            assert (
                check.query(models.ManagedAgent)
                .filter_by(account_id=account_id)
                .count()
                == 1
            )
            assert (
                check.query(models.ManagedAgentCredential)
                .filter_by(account_id=account_id)
                .count()
                == 1
            )
    finally:
        with Session(db_engine) as cleanup:
            cleanup.query(models.Account).filter_by(id=account_id).delete()
            cleanup.commit()


def test_exchange_reloads_revocation_after_authentication(db_engine: Engine) -> None:
    """An authenticated stale ORM object must not bypass later revocation."""
    from preloop.models.crud import crud_account, crud_user

    with Session(db_engine) as setup:
        account = crud_account.create(
            setup, obj_in={"organization_name": "Synthetic revocation"}
        )
        account_id = account.id
        user = crud_user.create(
            setup,
            obj_in={
                "account_id": account_id,
                "username": f"synthetic-{uuid4().hex}",
                "email": f"{uuid4().hex}@example.com",
                "is_active": True,
                "hashed_password": "synthetic-test-only",
            },
        )
        _, token = issue_authority(
            setup, user=user, grant=EnrollmentGrantRequest(apps=["cursor"])
        )
        setup.commit()
    try:
        with Session(db_engine) as authenticated:
            stale = crud_api_key.get_by_key(authenticated, key=token)
            assert stale.is_active
            with Session(db_engine) as admin:
                current = crud_api_key.get_by_key(admin, key=token)
                crud_api_key.update(admin, db_obj=current, obj_in={"is_active": False})
            with pytest.raises(HTTPException) as exc:
                exchange_authority(
                    authenticated,
                    authenticated_key=stale,
                    payload=_payload(apps=["cursor"]),
                )
            assert exc.value.status_code == 403
            authenticated.rollback()
    finally:
        with Session(db_engine) as cleanup:
            cleanup.query(models.Account).filter_by(id=account_id).delete()
            cleanup.commit()


@pytest.mark.asyncio
async def test_authority_and_app_credentials_cannot_call_gateway(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.model_gateway_auth import authenticate_bearer_token
    from preloop.services.mcp_http import PreloopBearerAuthBackend

    key, token = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    db_session.flush()
    assert await authenticate_bearer_token(token, db_session) is None
    assert PreloopBearerAuthBackend._build_auth_result(token, test_user, key) is None
    result = exchange_authority(
        db_session, authenticated_key=key, payload=_payload(apps=["cursor"])
    )
    assert await authenticate_bearer_token(result.apps[0].token, db_session) is None


def test_app_can_acknowledge_only_its_own_active_enrollment(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.agent_enrollment import acknowledge_apply

    key, _ = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    result = exchange_authority(
        db_session, authenticated_key=key, payload=_payload(apps=["cursor"])
    )
    runtime = crud_api_key.get_by_key(db_session, key=result.apps[0].token)
    enrollment = db_session.get(
        models.ManagedAgentEnrollment, result.apps[0].enrollment_id
    )
    assert enrollment.status == "pending"
    acknowledge_apply(db_session, authenticated_key=runtime)
    assert enrollment.status == "active"
    assert enrollment.validation_result == {
        "state": "configured",
        "application_login": "unknown",
        "attestation": False,
    }
    applied_at = enrollment.last_applied_at
    acknowledge_apply(db_session, authenticated_key=runtime)
    assert enrollment.last_applied_at == applied_at
    crud_api_key.update(
        db_session, db_obj=runtime, obj_in={"is_active": False}, commit=False
    )
    with pytest.raises(HTTPException):
        acknowledge_apply(db_session, authenticated_key=runtime)


def test_app_acknowledgment_cannot_restore_archived_enrollment(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.agent_enrollment import acknowledge_apply

    key, _ = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    result = exchange_authority(
        db_session, authenticated_key=key, payload=_payload(apps=["cursor"])
    )
    runtime = crud_api_key.get_by_key(db_session, key=result.apps[0].token)
    enrollment = db_session.get(
        models.ManagedAgentEnrollment, result.apps[0].enrollment_id
    )
    enrollment.status = "restored"
    db_session.flush()
    with pytest.raises(HTTPException):
        acknowledge_apply(db_session, authenticated_key=runtime)
    assert enrollment.status == "restored"


def test_app_rest_exception_is_only_its_config_acknowledgment() -> None:
    key = SimpleNamespace(
        scopes=["mcp:read", "mcp:write"], context_data={"unattended_enrollment": {}}
    )
    enforce_api_key_route_scope(
        key, _request("POST", "/api/v1/agents/enrollment/applied")
    )
    for method, path in [
        ("GET", "/api/v1/agents/enrollment/applied"),
        ("POST", "/api/v1/agents/enrollment/exchange"),
        ("POST", "/api/v1/agents/enrollment/authorities"),
    ]:
        with pytest.raises(HTTPException):
            enforce_api_key_route_scope(key, _request(method, path))


def test_real_bearer_exchange_and_self_acknowledgment(
    db_session: Session, test_user: models.User
) -> None:
    """Exercise FastAPI authentication and scope gates without auth overrides."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.endpoints.agent_enrollment import router
    from preloop.models.db.session import get_db_session

    key, token = issue_authority(
        db_session, user=test_user, grant=EnrollmentGrantRequest(apps=["cursor"])
    )
    db_session.flush()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        denied = client.post(
            "/api/v1/agents/enrollment/authorities",
            headers=headers,
            json={"apps": ["cursor"]},
        )
        assert denied.status_code == 403
        response = client.post(
            "/api/v1/agents/enrollment/exchange",
            headers=headers,
            json=_payload(apps=["cursor"]).model_dump(),
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        runtime = response.json()["apps"][0]["token"]
        again = client.post(
            "/api/v1/agents/enrollment/exchange",
            headers=headers,
            json=_payload(apps=["cursor"]).model_dump(),
        )
        assert again.status_code in {401, 403}
        runtime_headers = {"Authorization": f"Bearer {runtime}"}
        identity_path = "/api/v1/agents/enrollment/identity"
        identity = client.get(identity_path, headers=runtime_headers)
        assert identity.status_code == 200
        assert identity.json() == {"agent_id": response.json()["apps"][0]["agent_id"]}
        assert client.get(identity_path, headers=headers).status_code in {401, 403}
        ack = client.post("/api/v1/agents/enrollment/applied", headers=runtime_headers)
        assert ack.status_code == 200, ack.text
        assert ack.json() == {"state": "configured"}
        no_mint = client.post(
            "/api/v1/agents/enrollment/exchange",
            headers=runtime_headers,
            json=_payload(apps=["cursor"]).model_dump(),
        )
        assert no_mint.status_code == 403
