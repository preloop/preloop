"""Runtime triage credentials cannot escape scoped MCP through ordinary REST."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from preloop.api.auth.jwt import (
    create_access_token,
    get_current_user,
    get_user_from_token_if_valid_sync,
)
from preloop.api.auth.router import router as auth_router
from preloop.api.endpoints import flows, issue_lifecycle, issues, projects
from preloop.flow_presets import PRESET_SLUGS
from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_issue,
    crud_organization,
    crud_project,
)
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session


def _credential(db_session: Session, user: models.User, kind: str) -> str:
    """Use production minting and real persisted execution provenance."""
    if kind == "human":
        return create_access_token({"sub": str(user.id)})
    flow = CRUDBase(models.Flow).create(
        db_session,
        obj_in={
            "name": PRESET_SLUGS["issue-triage-assistant"]
            if kind == "triage"
            else kind.title(),
            "account_id": user.account_id,
            "agent_type": "codex",
            "agent_config": {},
            "prompt_template": "Assess the issue",
            "is_enabled": True,
        },
    )
    execution = CRUDBase(models.FlowExecution).create(
        db_session, obj_in={"flow_id": flow.id, "status": "RUNNING"}
    )
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        name="triage-rest-regression",
        account_id=user.account_id,
        user_id=user.id,
        context_data={}
        if kind == "personal"
        else {"flow_execution_id": str(execution.id)},
    )
    return secret


@pytest.mark.parametrize(
    "kind", ["triage", "implementation", "reviewer", "personal", "human"]
)
def test_authenticated_flow_key_cannot_bypass_triage_with_rest_issue_write(
    db_session: Session,
    test_user: models.User,
    test_tracker: models.Tracker,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Mint and authenticate a real flow key before reaching the real REST route."""
    secret = _credential(db_session, test_user, kind)
    organization = crud_organization.create(
        db_session,
        obj_in={
            "name": "example",
            "identifier": "example",
            "tracker_id": test_tracker.id,
        },
    )
    project = crud_project.create(
        db_session,
        obj_in={
            "name": "project",
            "identifier": "example/project",
            "organization_id": organization.id,
        },
    )
    issue = crud_issue.create(
        db_session,
        obj_in={
            "title": "Keep human requirements",
            "description": "Original human requirements",
            "status": "open",
            "external_id": "1001",
            "key": "example/project#1",
            "project_id": project.id,
            "tracker_id": test_tracker.id,
        },
    )
    provider = AsyncMock()
    monkeypatch.setattr(issues, "get_tracker_client", AsyncMock(return_value=provider))
    app = FastAPI()
    app.include_router(issues.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/issues/{issue.id}",
            headers={"Authorization": f"Bearer {secret}"},
            json={"description": "Replace all human requirements", "status": "closed"},
        )
    if kind == "triage":
        assert response.status_code == 403, response.text
        provider.update_issue.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        provider.update_issue.assert_awaited_once()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/issues"),
        ("POST", f"/api/v1/issues/{uuid4()}/lifecycle/ready"),
        ("POST", f"/api/v1/flows/{uuid4()}/trigger"),
        ("POST", "/api/v1/flows/run-preset"),
        ("PUT", f"/api/v1/flows/{uuid4()}"),
        ("PUT", f"/api/v1/projects/{uuid4()}"),
        ("POST", "/api/v1/auth/api-keys"),
    ],
)
def test_triage_cannot_escalate_via_other_rest_mutations(
    db_session: Session, test_user: models.User, method: str, path: str
) -> None:
    """Use the actual routers to block follow-ups, dispatch and new credentials."""
    secret = _credential(db_session, test_user, "triage")
    app = FastAPI()
    for router in (
        issues.router,
        issue_lifecycle.router,
        flows.router,
        projects.router,
    ):
        app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as client:
        response = client.request(
            method, path, headers={"Authorization": f"Bearer {secret}"}, json={}
        )
    assert response.status_code == 403, response.text


def test_triage_rest_reads_and_separate_tool_auth_remain_available(
    db_session: Session, test_user: models.User
) -> None:
    """The REST guard must not block MCP's separate authentication helper."""
    secret = _credential(db_session, test_user, "triage")
    request = Request(
        {"type": "http", "method": "GET", "path": "/api/v1/flows/executions"}
    )
    assert (
        get_current_user(token=secret, db=db_session, request=request).id
        == test_user.id
    )
    principal = get_user_from_token_if_valid_sync(secret, db_session)
    assert principal is not None and principal.id == test_user.id
    assert principal._auth_api_key.context_data["flow_execution_id"]


@pytest.mark.parametrize(
    "failure",
    [ValueError("triage_execution_not_found"), SQLAlchemyError("unavailable")],
)
def test_rest_scope_classification_fails_closed(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Invalid provenance or unavailable storage never grants general writes."""
    from preloop.services import issue_triage_controller

    secret = _credential(db_session, test_user, "triage")

    def fail(*args: object, **kwargs: object) -> bool:
        raise failure

    monkeypatch.setattr(issue_triage_controller, "is_triage_execution", fail)
    request = Request(
        {"type": "http", "method": "PUT", "path": "/api/v1/issues/example"}
    )
    with pytest.raises(HTTPException) as caught:
        get_current_user(token=secret, db=db_session, request=request)
    assert caught.value.status_code == (403 if isinstance(failure, ValueError) else 503)


@pytest.mark.parametrize("execution_id", ["not-a-uuid", str(uuid4())])
def test_real_unbound_flow_credentials_cannot_write_rest(
    db_session: Session, test_user: models.User, execution_id: str
) -> None:
    """A minted credential with invalid server provenance is never a human key."""
    _, secret = crud_api_key.create_runtime_key(
        db_session,
        name="unbound-triage-regression",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={"flow_execution_id": execution_id},
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/api-keys"}
    )
    with pytest.raises(HTTPException) as caught:
        get_current_user(token=secret, db=db_session, request=request)
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_mcp_scope_survives_missing_fastmcp_request_context(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authenticated key remains authoritative when middleware context is absent."""
    from preloop.api.endpoints import mcp
    from preloop.services import dynamic_fastmcp_http

    secret = _credential(db_session, test_user, "triage")
    principal = get_user_from_token_if_valid_sync(secret, db_session)
    assert principal is not None
    monkeypatch.setattr(dynamic_fastmcp_http, "get_current_user_context", lambda: None)
    monkeypatch.setattr(mcp, "_get_tool_db", lambda: db_session)
    monkeypatch.setattr(mcp, "_tool_user", AsyncMock(return_value=principal))
    with pytest.raises(HTTPException) as caught:
        await mcp.update_issue.__wrapped__(
            issue="example/project#1", description="Replace human requirements"
        )
    assert caught.value.status_code == 403
