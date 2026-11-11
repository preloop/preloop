"""Ephemeral harness approval credentials cannot escape their execution session."""

from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models import models
from preloop.models.crud import crud_flow, crud_flow_execution, crud_runtime_session
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_runtime_token import create_flow_runtime_token


def test_minted_review_token_resolves_its_execution(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production minter and real authentication agree on review bindings."""
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Synthetic read-only review",
            prompt_template="Review this change",
            trigger_event_source="github",
            trigger_event_types=["pull_request"],
            agent_type="codex",
            agent_config={},
            runner_pool="server",
            git_clone_config={
                "enabled": True,
                "publication_mode": "read_only",
                "repositories": [
                    {
                        "tracker_id": str(uuid4()),
                        "repository_url": "https://example.com/repo.git",
                    }
                ],
            },
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
    )
    token, key_id = create_flow_runtime_token(
        db_session,
        flow=flow,
        execution_id=execution.id,
        runtime_session_id=session.id,
    )
    assert token is not None
    assert key_id is not None
    monkeypatch.setattr(
        endpoint, "get_session_factory", lambda: lambda: nullcontext(db_session)
    )
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity(token)
    assert exc.value.status_code == 403
    identity = endpoint._resolve_permission_identity(token, allow_read_only_review=True)
    assert identity.api_key_id == key_id
    assert identity.account_id == str(test_user.account_id)
    assert identity.runtime_session_id == session.id
    assert identity.flow_execution_id == execution.id
    assert identity.flow_id == flow.id
    assert identity.managed_agent_id is None


@pytest.mark.parametrize(
    "scenario",
    [
        "active",
        "ended",
        "different_execution",
        "ordinary_key",
        "missing_session",
        "active_managed",
        "ended_managed",
        "different_execution_managed",
    ],
)
def test_flow_permission_requires_own_active_execution(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    execution_id, session_id, account_id = uuid4(), uuid4(), uuid4()
    key = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        context_data={"flow_execution_id": str(execution_id)},
    )
    session = SimpleNamespace(
        id=session_id,
        session_source_type="flow_execution",
        session_source_id=str(execution_id),
        ended_at=None,
        runtime_principal_name="Test flow",
    )
    if scenario in {"ended", "ended_managed"}:
        session.ended_at = "2026-01-01"
    elif scenario in {"different_execution", "different_execution_managed"}:
        session.session_source_id = str(uuid4())
    elif scenario == "ordinary_key":
        key.context_data = {}
    elif scenario == "missing_session":
        session = None
    factory = MagicMock()
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        endpoint.crud_api_key, "get_by_key", lambda *args, **kwargs: key
    )
    monkeypatch.setattr(
        endpoint,
        "_authenticate_with_api_key",
        lambda *args, **kwargs: SimpleNamespace(id=uuid4()),
    )
    managed = (
        SimpleNamespace(
            id=uuid4(),
            display_name="Example employee",
            session_source_type="codex",
            session_source_id="employee-example",
        )
        if scenario.endswith("_managed")
        else None
    )
    monkeypatch.setattr(endpoint, "_managed_agent_for_api_key", lambda *args: managed)
    monkeypatch.setattr(
        endpoint, "_runtime_session_id_from_api_key", lambda *args: session_id
    )
    monkeypatch.setattr(
        endpoint.crud_runtime_session,
        "get_account_session",
        lambda *args, **kwargs: session,
    )
    if scenario in {"active", "active_managed"}:
        identity = endpoint._resolve_permission_identity("flow-token")
        assert identity.managed_agent_id == (managed.id if managed else None)
        assert identity.runtime_session_id == session_id
        assert identity.managed_agent_name == (
            "Example employee" if managed else "Test flow"
        )
    else:
        with pytest.raises(HTTPException) as exc:
            endpoint._resolve_permission_identity("flow-token")
        assert exc.value.status_code == 401


@pytest.fixture
def restricted_review(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Authenticate synthetic keys; keep real resolver and scope guards."""
    account_id, session_id, execution_id, flow_id = (uuid4() for _ in range(4))
    key = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        scopes=["mcp:read"],
        context_data={
            "read_only_review": True,
            "allowed_mcp_tools": [],
            "allowed_mcp_servers": [],
            "flow_id": str(flow_id),
            "flow_execution_id": str(execution_id),
            "runtime_principal": {"type": "flow_execution", "id": str(execution_id)},
        },
    )
    session = SimpleNamespace(
        id=session_id,
        session_source_type="flow_execution",
        session_source_id=str(execution_id),
        ended_at=None,
        runtime_principal_name="Synthetic review",
    )
    case = SimpleNamespace(key=key, session=session, managed=None)
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: MagicMock())
    monkeypatch.setattr(endpoint.crud_api_key, "get_by_key", lambda *a, **kw: key)
    case.authenticate = Mock(return_value=SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(endpoint, "_authenticate_with_api_key", case.authenticate)
    monkeypatch.setattr(endpoint, "_managed_agent_for_api_key", lambda *a: case.managed)
    monkeypatch.setattr(
        endpoint, "_runtime_session_id_from_api_key", lambda *a: session_id
    )
    case.lookup = Mock(side_effect=lambda *a, **kw: case.session)
    monkeypatch.setattr(
        endpoint.crud_runtime_session, "get_account_session", case.lookup
    )
    return case


def test_review_permission_identity_requires_explicit_opt_in(restricted_review) -> None:
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity("review-token")
    assert exc.value.status_code == 403
    identity = endpoint._resolve_permission_identity(
        "review-token", allow_read_only_review=True
    )
    assert identity.api_key_id == restricted_review.key.id
    assert identity.runtime_session_id == restricted_review.session.id
    assert (
        str(identity.flow_execution_id) == restricted_review.session.session_source_id
    )
    assert identity.managed_agent_id is None
    assert restricted_review.lookup.call_args.kwargs == {
        "account_id": restricted_review.key.account_id,
        "runtime_session_id": restricted_review.session.id,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_mcp_tools", None),
        ("allowed_mcp_tools", ["example-tool"]),
        ("allowed_mcp_servers", None),
        ("allowed_mcp_servers", ["example-server"]),
        ("flow_id", None),
        ("flow_id", "invalid"),
        ("flow_execution_id", None),
        ("flow_execution_id", "invalid"),
        ("runtime_principal", None),
        ("runtime_principal", {}),
        ("runtime_principal", {"type": "managed_agent", "id": "wrong"}),
        ("runtime_principal", {"type": "flow_execution", "id": "wrong"}),
    ],
)
def test_review_permission_rejects_invalid_bindings(
    restricted_review, field: str, value: object
) -> None:
    restricted_review.key.context_data[field] = value
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity(
            "review-token", allow_read_only_review=True
        )
    assert exc.value.status_code in {401, 403}


@pytest.mark.parametrize("scopes", [None, [], ["*"], ["mcp:read", "mcp:write"]])
def test_review_permission_rejects_scope_expansion(restricted_review, scopes) -> None:
    restricted_review.key.scopes = scopes
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity(
            "review-token", allow_read_only_review=True
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "scenario", ["missing", "ended", "wrong_execution", "wrong_source", "managed"]
)
def test_review_permission_requires_active_unmanaged_execution(
    restricted_review, scenario: str
) -> None:
    if scenario == "missing":
        # The account-scoped CRUD lookup also returns None for another account.
        restricted_review.session = None
    elif scenario == "ended":
        restricted_review.session.ended_at = "2026-01-01"
    elif scenario == "wrong_execution":
        restricted_review.session.session_source_id = str(uuid4())
    elif scenario == "wrong_source":
        restricted_review.session.session_source_type = "codex"
    else:
        restricted_review.managed = SimpleNamespace(id=uuid4())
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity(
            "review-token", allow_read_only_review=True
        )
    assert exc.value.status_code in {401, 403}


def test_review_permission_does_not_bypass_authentication(restricted_review) -> None:
    restricted_review.authenticate.side_effect = HTTPException(status_code=401)
    with pytest.raises(HTTPException) as exc:
        endpoint._resolve_permission_identity(
            "review-token", allow_read_only_review=True
        )
    assert exc.value.status_code == 401
    restricted_review.lookup.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [
        "active",
        "inactive",
        "expired",
        "ended_execution",
        "inactive_user",
        "wrong_account",
        "machine_authorization",
    ],
)
def test_review_permission_preserves_real_key_authentication(
    monkeypatch: pytest.MonkeyPatch, restricted_review, state: str
) -> None:
    from preloop.api.auth import jwt

    key = restricted_review.key
    key.is_active = state != "inactive"
    key.is_expired = state == "expired"
    key.requires_machine_authorization = state == "machine_authorization"
    key.user_id = uuid4()
    key.context_data["runtime_session_id"] = str(restricted_review.session.id)
    restricted_review.session.runtime_principal_type = "flow_execution"
    restricted_review.session.runtime_principal_id = key.context_data[
        "flow_execution_id"
    ]
    user = SimpleNamespace(
        id=key.user_id,
        account_id=uuid4() if state == "wrong_account" else key.account_id,
        is_active=state != "inactive_user",
    )
    monkeypatch.setattr(
        endpoint, "_authenticate_with_api_key", jwt._authenticate_with_api_key
    )
    monkeypatch.setattr(jwt.crud_user, "get", lambda *a, **kw: user)
    monkeypatch.setattr(jwt.crud_managed_agent, "get_by_source", lambda *a, **kw: None)
    monkeypatch.setattr(
        jwt.crud_flow_execution,
        "get_status",
        lambda *a, **kw: "SUCCEEDED" if state == "ended_execution" else "RUNNING",
    )
    if state == "active":
        with pytest.raises(HTTPException) as exc:
            endpoint._resolve_permission_identity("review-token")
        assert exc.value.status_code == 403
        identity = endpoint._resolve_permission_identity(
            "review-token", allow_read_only_review=True
        )
        assert identity.api_key_id == key.id
    else:
        with pytest.raises(HTTPException) as exc:
            endpoint._resolve_permission_identity(
                "review-token", allow_read_only_review=True
            )
        assert exc.value.status_code == (
            403 if state == "machine_authorization" else 401
        )


@pytest.mark.parametrize("scenario", ["own", "other_key", "other_account", "missing"])
def test_review_hook_expiry_requires_own_approval(
    monkeypatch: pytest.MonkeyPatch, restricted_review, scenario: str
) -> None:
    from preloop.models.crud.approval_request import crud_approval_request

    identity = endpoint._resolve_permission_identity(
        "review-token", allow_read_only_review=True
    )
    row = SimpleNamespace(
        account_id=restricted_review.key.account_id,
        api_key_id=restricted_review.key.id,
        managed_agent_id=None,
    )
    if scenario == "other_key":
        row.api_key_id = uuid4()
    elif scenario == "other_account":
        row.account_id = uuid4()
    elif scenario == "missing":
        row = None
    monkeypatch.setattr(crud_approval_request, "get", lambda *a, **kw: row)
    assert endpoint._owned_approval_exists(identity, uuid4()) is (scenario == "own")


@pytest.mark.parametrize("decision", ["allow", "deny", "park"])
def test_review_native_hook_retains_policy_decision(
    monkeypatch: pytest.MonkeyPatch, restricted_review, decision: str
) -> None:
    decide = AsyncMock(
        return_value=(decision, "Synthetic policy decision", None, False)
    )
    monkeypatch.setattr(endpoint, "request_agent_permission", decide)
    monkeypatch.setattr(endpoint, "_claim_operator_note", Mock(return_value=None))
    record = Mock()
    monkeypatch.setattr(endpoint, "_record_native_tool_call", record)
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as client:
        response = client.post(
            "/agents/permission-check",
            headers={"Authorization": "Bearer review-token"},
            json={
                "source": "codex_cli",
                "tool_name": "Bash",
                "tool_input": {"command": "example-review-helper"},
                "evaluation_phase": "pre_tool_use",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == ("deny" if decision == "park" else decision)
    assert response.json()["parked"] is (decision == "park")
    assert decide.await_args.kwargs["api_key_id"] == restricted_review.key.id
    assert str(decide.await_args.kwargs["flow_execution_id"]) == (
        restricted_review.session.session_source_id
    )
    record.assert_called_once()


def test_review_native_expiry_route_rejects_another_keys_approval(
    monkeypatch: pytest.MonkeyPatch, restricted_review
) -> None:
    from preloop.models.crud.approval_request import crud_approval_request

    other = SimpleNamespace(
        account_id=restricted_review.key.account_id,
        api_key_id=uuid4(),
        managed_agent_id=None,
    )
    lookup = Mock(return_value=other)
    monkeypatch.setattr(crud_approval_request, "get", lookup)
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as client:
        response = client.post(
            f"/approvals/{uuid4()}/expire",
            headers={"Authorization": "Bearer review-token"},
            json={"reason": "hook_deadline"},
        )
    assert response.status_code == 404
    lookup.assert_called_once()


def test_review_native_expiry_route_expires_only_own_request(
    monkeypatch: pytest.MonkeyPatch, restricted_review
) -> None:
    from preloop.models.crud.approval_request import crud_approval_request
    from preloop.models.db import session
    from preloop.services.approval_service import ApprovalService

    approval_id = uuid4()
    row = SimpleNamespace(
        id=approval_id,
        account_id=restricted_review.key.account_id,
        api_key_id=restricted_review.key.id,
        managed_agent_id=None,
        status="expired",
        approver_comment=None,
    )
    monkeypatch.setattr(crud_approval_request, "get", lambda *a, **kw: row)
    monkeypatch.setattr(session, "get_async_db_session", MagicMock())
    expire = AsyncMock(return_value=row)
    monkeypatch.setattr(ApprovalService, "expire_request", expire)
    app = FastAPI()
    app.include_router(endpoint.router)
    with TestClient(app) as client:
        response = client.post(
            f"/approvals/{approval_id}/expire",
            headers={"Authorization": "Bearer review-token"},
            json={"reason": "hook_deadline"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "expired"
    assert expire.await_args.args == (approval_id,)
    assert expire.await_args.kwargs["reason"] == "hook_deadline"
