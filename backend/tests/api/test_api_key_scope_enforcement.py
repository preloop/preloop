"""API keys that carry only MCP scopes stay on the MCP surface.

Flow execution credentials and runtime session tokens are minted with
``mcp:read`` and ``mcp:write``. Those scopes are enforced on the REST
authentication path: such a key authenticates MCP (and the runtime routes
that verify their own credentials), but not the general account API.
"""

import logging
from typing import Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    create_access_token,
    get_current_active_user_optional,
    get_current_user,
    get_user_from_token_if_valid_sync,
)
from preloop.api.auth.router import router as auth_router
from preloop.api.endpoints import ai_models, flows, mcp_servers, trackers
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from preloop.services.flow_runtime_token import create_flow_runtime_token

SCOPE_DENIED = "api_key_scope_denied"
KEY_SCOPES_LOGGER = "preloop.api.auth.key_scopes"


def _flow_execution(
    db_session: Session, user: models.User, status: str = "RUNNING"
) -> tuple[models.Flow, models.FlowExecution]:
    flow = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": f"scope-regression-{uuid4().hex[:8]}",
            "account_id": user.account_id,
            "agent_type": "codex",
            "agent_config": {},
            "prompt_template": "Do the work",
            "is_enabled": True,
            "allowed_mcp_servers": ["preloop-mcp"],
            "allowed_mcp_tools": [
                {"server_name": "preloop-mcp", "tool_name": "get_issue"}
            ],
        },
    )
    execution = CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": flow.id, "status": status}
    )
    return flow, execution


def _flow_token(db_session: Session, user: models.User, status: str = "RUNNING") -> str:
    """Mint through the production path the orchestrator and runners use."""
    flow, execution = _flow_execution(db_session, user, status)
    token, key_id = create_flow_runtime_token(
        db_session, flow=flow, execution_id=execution.id
    )
    assert token and key_id
    return token


def _personal_key(db_session: Session, user: models.User) -> str:
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        name=f"personal-{uuid4().hex[:8]}",
        account_id=user.account_id,
        user_id=user.id,
    )
    return secret


def _mcp_scoped_personal_key(db_session: Session, user: models.User) -> str:
    """A key without flow context whose only scopes are MCP scopes."""
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        name=f"mcp-only-{uuid4().hex[:8]}",
        account_id=user.account_id,
        user_id=user.id,
        scopes=["mcp:read"],
    )
    return secret


@pytest.fixture
def api_client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    for router in (ai_models.router, trackers.router, mcp_servers.router, flows.router):
        app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        yield client


@pytest.fixture
def enforcement_mode(monkeypatch: pytest.MonkeyPatch):
    def set_mode(mode: str) -> None:
        monkeypatch.setattr(settings, "api_key_scope_enforcement", mode, raising=False)

    set_mode("enforce")
    return set_mode


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/ai-models"),
        ("GET", "/api/v1/trackers"),
        ("GET", "/api/v1/mcp-servers"),
        ("GET", "/api/v1/auth/api-keys"),
        ("POST", "/api/v1/auth/api-keys"),
        ("POST", "/api/v1/auth/runtime-sessions/token"),
        ("GET", "/api/v1/flows"),
    ],
)
def test_flow_token_cannot_call_account_rest_routes(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
    method: str,
    path: str,
) -> None:
    """A flow token is denied the general API, including credential minting."""
    token = _flow_token(db_session, test_user)
    response = api_client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json={}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == SCOPE_DENIED


def test_flow_token_cannot_list_ai_models(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
) -> None:
    """Regression: the model list (and its provider configuration) is out of reach."""
    CRUDBase(models.AIModel).create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "scope-regression-model",
            "provider_name": "openai",
            "model_identifier": "gpt-4o",
        },
    )
    token = _flow_token(db_session, test_user)
    response = api_client.get(
        "/api/v1/ai-models", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403, response.text
    assert "scope-regression-model" not in response.text


def test_runtime_session_token_is_mcp_only(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
) -> None:
    """Runtime session tokens carry the same scopes and get the same limit."""
    human = create_access_token({"sub": str(test_user.id)})
    minted = api_client.post(
        "/api/v1/auth/runtime-sessions/token",
        headers={"Authorization": f"Bearer {human}"},
        json={
            "session_source_type": "hermes",
            "session_source_id": f"scope-{uuid4().hex[:8]}",
            "runtime_principal_name": "Hermes",
        },
    )
    assert minted.status_code == 201, minted.text
    runtime_token = minted.json()["token"]
    response = api_client.get(
        "/api/v1/ai-models", headers={"Authorization": f"Bearer {runtime_token}"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == SCOPE_DENIED


def test_mcp_scoped_key_without_flow_context_is_mcp_only(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
) -> None:
    """The limit follows the scopes, not the key's name or context."""
    secret = _mcp_scoped_personal_key(db_session, test_user)
    response = api_client.get(
        "/api/v1/ai-models", headers={"Authorization": f"Bearer {secret}"}
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("kind", ["personal", "human"])
def test_unscoped_credentials_keep_rest_access(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
    kind: str,
) -> None:
    """Personal API keys (no scopes) and console JWTs are unchanged."""
    token = (
        _personal_key(db_session, test_user)
        if kind == "personal"
        else create_access_token({"sub": str(test_user.id)})
    )
    response = api_client.get(
        "/api/v1/ai-models", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text


def test_flow_token_still_authenticates_mcp(
    db_session: Session, test_user: models.User, enforcement_mode
) -> None:
    """MCP's own authentication helper accepts a live flow token."""
    token = _flow_token(db_session, test_user)
    principal = get_user_from_token_if_valid_sync(token, db_session)
    assert principal is not None and principal.id == test_user.id
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/v1",
            "headers": [],
            "query_string": b"",
        }
    )
    assert get_current_user(token=token, db=db_session, request=request).id == (
        test_user.id
    )


def test_direct_callers_without_a_request_are_denied(
    db_session: Session, test_user: models.User, enforcement_mode
) -> None:
    """Callers that cannot name the route fail closed for MCP-only keys."""
    token = _flow_token(db_session, test_user)
    with pytest.raises(HTTPException) as caught:
        get_current_user(token=token, db=db_session)
    assert caught.value.status_code == 403
    assert get_current_active_user_optional(token=token, db=db_session) is None


def test_audit_mode_logs_and_allows(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit mode records the would-be denial without blocking the call."""
    enforcement_mode("audit")
    token = _flow_token(db_session, test_user)
    with caplog.at_level(logging.WARNING, logger=KEY_SCOPES_LOGGER):
        response = api_client.get(
            "/api/v1/ai-models", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200, response.text
    assert any("/api/v1/ai-models" in record.getMessage() for record in caplog.records)


def test_off_mode_allows(
    db_session: Session,
    test_user: models.User,
    api_client: TestClient,
    enforcement_mode,
) -> None:
    enforcement_mode("off")
    token = _flow_token(db_session, test_user)
    response = api_client.get(
        "/api/v1/ai-models", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text


def test_unified_websocket_rejects_mcp_only_keys(
    db_session: Session,
    test_user: models.User,
    enforcement_mode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console event stream is account data, not an MCP surface."""
    import asyncio

    from preloop.api.endpoints import websockets

    async def run_inline(fn):
        return fn(db_session)

    monkeypatch.setattr(websockets, "run_db_async", run_inline)
    flow_token = _flow_token(db_session, test_user)
    personal = _personal_key(db_session, test_user)
    assert asyncio.run(websockets._resolve_token_user(flow_token)) is None
    assert asyncio.run(websockets._resolve_token_user(personal)) is not None


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (["mcp:read", "mcp:write"], True),
        (["mcp:read"], True),
        ([], False),
        (None, False),
        (["*"], False),
        (["mcp:read", "admin"], False),
        ([{"device_token": "x"}], False),
    ],
)
def test_mcp_only_classification(scopes, expected) -> None:
    from preloop.api.auth import key_scopes

    class Key:
        pass

    key = Key()
    key.scopes = scopes
    assert key_scopes.is_mcp_only_api_key(key) is expected
