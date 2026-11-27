"""HTTP authorization, account boundaries and readiness request validation."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.endpoints import account, approval_requests, fleet_map
from preloop.models import models
from preloop.models.crud import crud_account
from preloop.models.db.session import get_db_session
from preloop.plugins import account_hooks
from preloop.schemas.approval_readiness import ApprovalReadiness


@pytest.fixture
def readiness_client(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Mount the real routes with authenticated membership and a local database."""
    app = FastAPI()
    for router in (account.router, approval_requests.router, fleet_map.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_account_for_user] = lambda: crud_account.get(
        db_session, id=test_user.account_id
    )
    monkeypatch.setattr(account, "native_workflow", lambda *a, **k: None)
    monkeypatch.setattr(
        account, "describe_readiness", lambda *a, **k: ApprovalReadiness()
    )
    monkeypatch.setattr(account_hooks, "_authorizer", None)
    with TestClient(app) as client:
        yield client


def make_agent(db: Session, account_id: UUID) -> models.ManagedAgent:
    """Persist an active synthetic enrollment."""
    now = datetime.now(UTC)
    agent = models.ManagedAgent(
        account_id=account_id,
        display_name="Example agent",
        agent_kind="claude_code",
        session_source_type="test",
        session_source_id=str(uuid4()),
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now,
    )
    db.add(agent)
    db.flush()
    return agent


def test_foreign_and_hidden_agent_return_404(
    readiness_client: TestClient,
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account boundary and the resource authorizer both hide the agent."""
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other example"}
    )
    other_agent = make_agent(db_session, foreign.id)
    path = "/api/v1/account/approval-readiness"
    assert (
        readiness_client.get(path, params={"agent_id": str(other_agent.id)}).status_code
        == 404
    )
    own = make_agent(db_session, test_user.account_id)
    monkeypatch.setattr(account_hooks, "filter_viewable", lambda *a: [])
    assert (
        readiness_client.get(path, params={"agent_id": str(own.id)}).status_code == 404
    )


@pytest.mark.parametrize(
    "default,override,expected",
    [
        ("off", None, False),
        ("off", "enforce", True),
        ("enforce", "off", False),
    ],
)
def test_agent_mode_overrides_account_default(
    readiness_client: TestClient,
    db_session: Session,
    test_user: models.User,
    default: str,
    override: str | None,
    expected: bool,
) -> None:
    """The HTTP response describes the persisted effective mode."""
    agent = make_agent(db_session, test_user.account_id)
    row = crud_account.get(db_session, id=test_user.account_id)
    row.meta_data = {
        "subject_governance": {
            "account_defaults": {"native_tool_approvals": default},
            "managed_agents": {
                str(agent.id): {"native_tool_approvals": override} if override else {}
            },
        }
    }
    db_session.flush()
    response = readiness_client.get(
        "/api/v1/account/approval-readiness", params={"agent_id": str(agent.id)}
    )
    assert response.status_code == 200, response.text
    assert response.json()["enforced"] is expected


def test_foreign_pending_request_returns_404(
    readiness_client: TestClient,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request lookup always receives the authenticated account boundary."""
    get = MagicMock(return_value=None)
    monkeypatch.setattr(approval_requests.crud_approval_request, "get", get)
    response = readiness_client.get(f"/api/v1/approval-requests/{uuid4()}/readiness")
    assert response.status_code == 404
    assert get.call_args.kwargs["account_id"] == test_user.account_id


@pytest.mark.parametrize(
    "path,permission,method",
    [
        ("/api/v1/account/approval-readiness", "view_policies", "GET"),
        (
            f"/api/v1/approval-requests/{UUID(int=42)}/readiness",
            "view_approvals",
            "GET",
        ),
        ("/api/v1/fleet/identities", "view_device_fleet", "POST"),
    ],
)
def test_readiness_routes_enforce_permission(
    readiness_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    permission: str,
    method: str,
) -> None:
    """The endpoint permission gate runs before any readiness service."""

    def deny(ctx: object, action: str, resource: object) -> account_hooks.Decision:
        return account_hooks.Decision(
            effect="deny" if action == permission else "allow", reason="Test denial"
        )

    monkeypatch.setattr(account_hooks, "_authorizer", deny)
    response = readiness_client.request(
        method, path, json={} if method == "POST" else None
    )
    assert response.status_code == 403


@pytest.mark.parametrize("field", ["agent_ids", "observer_ids"])
def test_fleet_identity_bound_is_enforced_at_http_boundary(
    readiness_client: TestClient,
    field: str,
) -> None:
    """Oversized input is rejected before any projection read."""
    response = readiness_client.post(
        "/api/v1/fleet/identities", json={field: [str(UUID(int=42))] * 1001}
    )
    assert response.status_code == 422
