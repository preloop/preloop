"""Scope confinement at REST and runtime authentication boundaries."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth.jwt import authenticate_runtime_bearer_token
from preloop.api.endpoints import (
    agent_discovery,
    agent_enrollment,
    agent_permission,
    desktop,
    telemetry_otlp,
)
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session


@pytest.fixture
def scope_client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    for module in (agent_discovery, agent_enrollment, telemetry_otlp):
        app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


def _key(db: Session, user: models.User, scopes: list[str], **kwargs: Any) -> str:
    _, token = crud_api_key.create_runtime_key(
        db,
        name="Scope regression",
        account_id=user.account_id,
        user_id=user.id,
        scopes=scopes,
        **kwargs,
    )
    return token


def _other_user(db: Session) -> models.User:
    account = crud_account.create(db, obj_in={"organization_name": "Example account"})
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": "jane",
            "email": "jane@example.com",
            "hashed_password": "unused",
            "is_active": True,
            "email_verified": True,
        },
    )


@pytest.mark.parametrize("signal", ["logs", "metrics", "traces"])
@pytest.mark.parametrize(
    "scopes,allowed",
    [
        (["telemetry:ingest"], True),
        ([], False),
        (["*"], False),
        (["report_discovery"], False),
        (["mcp:read", "mcp:write"], False),
        (["enrollment:provision", "telemetry:ingest"], False),
    ],
)
def test_telemetry_requires_explicit_ingest_and_preserves_enrollment_confinement(
    scope_client: TestClient,
    db_session: Session,
    test_user: models.User,
    signal: str,
    scopes: list[str],
    allowed: bool,
) -> None:
    token = _key(db_session, test_user, scopes)
    response = scope_client.post(
        f"/api/v1/telemetry/otlp/v1/{signal}",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert response.status_code == (200 if allowed else 403), response.text


@pytest.mark.parametrize(
    "scopes,allowed",
    [
        (["report_discovery"], True),
        (["model_gateway:trusted_upstream"], False),
        (["mcp:read"], False),
        (["telemetry:ingest"], False),
        (["enrollment:provision"], False),
    ],
)
def test_discovery_requires_its_scope_with_real_authentication(
    scope_client: TestClient,
    db_session: Session,
    test_user: models.User,
    scopes: list[str],
    allowed: bool,
) -> None:
    # Scope checking is independent of role policy; the account owner passes it.
    crud_account.update(
        db_session,
        db_obj=crud_account.get(db_session, id=test_user.account_id),
        obj_in={"primary_user_id": test_user.id},
    )
    token = _key(db_session, test_user, scopes)
    response = scope_client.get(
        "/api/v1/agents/discovery-salt", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == (200 if allowed else 403), response.text


def test_discovery_keys_get_only_their_own_account_salt(
    scope_client: TestClient,
    db_session: Session,
    test_user: models.User,
) -> None:
    salts = []
    for user in (test_user, _other_user(db_session)):
        crud_account.update(
            db_session,
            db_obj=crud_account.get(db_session, id=user.account_id),
            obj_in={"primary_user_id": user.id},
        )
        token = _key(db_session, user, ["report_discovery"])
        response = scope_client.get(
            "/api/v1/agents/discovery-salt",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        salts.append(response.json()["salt"])
    assert salts[0] != salts[1]


@pytest.mark.parametrize("surface", ["control", "permission", "desktop"])
@pytest.mark.parametrize(
    "scopes",
    [
        ["report_discovery"],
        ["telemetry:ingest"],
        ["enrollment:provision"],
        ["enrollment:provision", "mcp:read", "mcp:write"],
    ],
)
def test_single_purpose_keys_cannot_use_runtime_bindings(
    db_session: Session,
    test_user: models.User,
    scopes: list[str],
    surface: str,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    agent = CRUDBase(models.ManagedAgent).create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "agent_kind": "hermes",
            "session_source_type": "hermes",
            "session_source_id": str(uuid4()),
            "display_name": "Example agent",
            "enrolled_via": "runtime_session_token",
            "lifecycle_state": "active",
            "lifecycle_updated_at": now,
            "last_seen_at": now,
        },
    )
    token = _key(
        db_session, test_user, scopes, context_data={"managed_agent_id": str(agent.id)}
    )
    with pytest.raises(HTTPException) as error:
        if surface == "control":
            authenticate_runtime_bearer_token(
                db_session, token, enforce_current_binding=False
            )
        elif surface == "desktop":
            desktop._authenticate_endpoint(db_session, token)
        else:
            with (
                patch(
                    "preloop.api.endpoints.agent_permission.get_session_factory",
                    return_value=lambda: db_session,
                ),
                patch.object(db_session, "close"),
            ):
                agent_permission._resolve_permission_identity(token)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "api_key_scope_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes,allowed",
    [
        ([], True),
        (["mcp:read"], False),
        (["mcp:write"], False),
        (["report_discovery"], False),
        (["telemetry:ingest"], False),
        (["enrollment:provision"], False),
    ],
)
async def test_desktop_viewer_uses_console_scope_confinement(
    db_session: Session,
    test_user: models.User,
    scopes: list[str],
    allowed: bool,
) -> None:
    token = _key(db_session, test_user, scopes)

    async def run_db(operation: Any) -> Any:
        return operation(db_session)

    with (
        patch.object(desktop, "_run_db", new=run_db),
        patch(
            "preloop.services.db_executor.detach_user",
            side_effect=lambda db, user: user,
        ),
    ):
        user = await desktop._resolve_viewer(token)
    assert (user is not None) is allowed


@pytest.mark.parametrize(
    "scope,path",
    [
        ("report_discovery", "/api/v1/agents/discovery-salt"),
        ("enrollment:provision", "/api/v1/agents/enrollment/exchange"),
        ("telemetry:ingest", "/api/v1/telemetry/otlp/v1/logs"),
    ],
)
def test_rest_scopes_do_not_allow_mismatched_key_owner_accounts(
    scope_client: TestClient,
    db_session: Session,
    test_user: models.User,
    scope: str,
    path: str,
) -> None:
    other = _other_user(db_session)
    _, token = crud_api_key.create_runtime_key(
        db_session,
        name="Mismatched account",
        account_id=test_user.account_id,
        user_id=other.id,
        scopes=[scope],
    )
    method = "GET" if scope == "report_discovery" else "POST"
    response = scope_client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json={}
    )
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    "scopes",
    [
        ["mcp:read"],
        ["mcp:write"],
        ["report_discovery"],
        ["telemetry:ingest"],
        ["enrollment:provision"],
    ],
)
def test_all_account_rest_operations_confine_scoped_keys(
    db_session: Session,
    test_user: models.User,
    scopes: list[str],
) -> None:
    """Exercise every mounted operation using the shared REST bearer contract."""
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    token = _key(db_session, test_user, scopes)
    allowed_paths = {
        "/api/v1/agents/discovery-salt",
        "/api/v1/agents/discovery-reports",
        "/api/v1/agents/enrollment/exchange",
    }
    tested = 0
    with TestClient(app) as client:
        schema = get_openapi(title="Scope inventory", version="1", routes=app.routes)
        for path, operations in schema["paths"].items():
            if path in allowed_paths:
                continue  # These have separate positive and negative tests above.
            for method, operation in operations.items():
                if not isinstance(operation, dict) or not any(
                    "OAuth2PasswordBearer" in requirement
                    for requirement in operation.get("security", [])
                ):
                    continue
                url = re.sub(r"\{[^}]+\}", str(uuid4()), path)
                response = client.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, json={}
                )
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["detail"]["code"] == "api_key_scope_denied", (
                    method,
                    path,
                    response.text,
                )
                tested += 1
    assert tested > 400  # Fail visibly if discovery stops seeing mounted routes.
