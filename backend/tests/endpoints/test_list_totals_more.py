"""More list endpoints report a total and filter visibility before paging."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import legal_hold as crud_legal_hold
from preloop.plugins import account_hooks
from preloop.services.runner_service import hash_runner_token


@pytest.fixture
def hide_hidden_names() -> Iterator[None]:
    """Register an authorizer that denies viewing rows named ``hidden-*``."""

    def authorizer(
        ctx: account_hooks.AuthorizationContext, action: str, resource: Any
    ) -> account_hooks.Decision | None:
        """Deny view on hidden rows and defer everything else."""
        if isinstance(resource, dict):
            name = resource.get("display_name") or resource.get("name") or ""
        else:
            name = getattr(resource, "display_name", None) or getattr(
                resource, "name", ""
            )
        if action == account_hooks.ACTION_RESOURCE_VIEW and str(name).startswith(
            "hidden"
        ):
            return account_hooks.Decision(effect="deny")
        return None

    previous = account_hooks.get_authorizer()
    account_hooks.register_authorizer(authorizer)
    try:
        yield
    finally:
        account_hooks.register_authorizer(previous)


def _name(index: int) -> str:
    """Return ``hidden-NNN`` for odd and ``shown-NNN`` for even indexes."""
    return f"{'hidden' if index % 2 else 'shown'}-{index:03d}"


def test_mcp_server_list_is_not_capped(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    hide_hidden_names: None,
) -> None:
    """Every visible server comes back, past the old 100-row cap."""
    for index in range(210):
        db_session.add(
            models.MCPServer(
                name=_name(index),
                url="http://localhost:8080/mcp",
                transport="http-streaming",
                auth_type="none",
                account_id=test_user.account_id,
                status="active",
            )
        )
    db_session.commit()

    response = client.get("/api/v1/mcp-servers")
    assert response.status_code == 200, response.text
    names = {row["name"] for row in response.json()}
    assert names == {_name(index) for index in range(0, 210, 2)}
    assert response.headers["X-Total-Count"] == "105"


def test_approval_workflow_list_is_not_capped(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """Every workflow comes back, past the old 100-row cap."""
    for index in range(103):
        db_session.add(
            models.ApprovalWorkflow(
                account_id=test_user.account_id,
                name=f"workflow-{index:03d}",
                workflow_type="simple",
            )
        )
    db_session.commit()

    response = client.get("/api/v1/approval-workflows")
    assert response.status_code == 200, response.text
    names = {row["name"] for row in response.json()}
    assert {f"workflow-{index:03d}" for index in range(103)} <= names
    assert response.headers["X-Total-Count"] == str(len(response.json()))


def test_runner_list_filters_visibility_before_paging(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    hide_hidden_names: None,
) -> None:
    """Pages are full while visible runners remain, and the header counts them."""
    for index in range(6):
        db_session.add(
            models.FlowRunner(
                account_id=test_user.account_id,
                name=_name(index),
                token_hash=hash_runner_token(f"token-{index}"),
                labels=[],
                status="offline",
            )
        )
    db_session.commit()

    first = client.get("/api/v1/runners", params={"skip": 0, "limit": 2})
    assert first.status_code == 200, first.text
    assert first.headers["X-Total-Count"] == "3"
    assert len(first.json()) == 2
    second = client.get("/api/v1/runners", params={"skip": 2, "limit": 2})
    assert second.status_code == 200, second.text
    assert second.headers["X-Total-Count"] == "3"
    names = [row["name"] for row in first.json() + second.json()]
    assert sorted(names) == ["shown-000", "shown-002", "shown-004"]


def test_legal_hold_list_pages_with_offset_and_total(
    client: TestClient, db_session: Session, test_user: models.User
) -> None:
    """``offset`` reaches holds past ``limit`` and the header counts them all."""
    start = datetime.now(UTC)
    for index in range(5):
        crud_legal_hold.create(
            db_session,
            account_id=test_user.account_id,
            resource_type="flow_execution",
            resource_id=str(uuid.uuid4()),
            reason=f"hold-{index}",
            placed_by_user_id=test_user.id,
            placed_at=start - timedelta(minutes=index),
        )
    db_session.commit()

    first = client.get("/api/v1/retention/holds", params={"limit": 3})
    assert first.status_code == 200, first.text
    assert first.headers["X-Total-Count"] == "5"
    assert [row["reason"] for row in first.json()] == ["hold-0", "hold-1", "hold-2"]
    rest = client.get("/api/v1/retention/holds", params={"limit": 3, "offset": 3})
    assert rest.status_code == 200, rest.text
    assert rest.headers["X-Total-Count"] == "5"
    assert [row["reason"] for row in rest.json()] == ["hold-3", "hold-4"]


@pytest.fixture
def account_agents(db_session: Session, test_user: models.User) -> None:
    """Create six managed agents, odd ones named ``hidden-*``."""
    now = datetime.now(UTC).replace(tzinfo=None)
    for index in range(6):
        db_session.add(
            models.ManagedAgent(
                id=uuid.uuid4(),
                account_id=test_user.account_id,
                agent_kind="codex",
                session_source_type="codex",
                session_source_id=f"agent-{index}",
                display_name=_name(index),
                enrolled_via="runtime_session_token",
                lifecycle_state="active",
                lifecycle_updated_at=now,
                last_seen_at=now - timedelta(minutes=index),
                tags={},
            )
        )
    db_session.commit()


def test_agent_list_filters_visibility_before_paging(
    client: TestClient, account_agents: None, hide_hidden_names: None
) -> None:
    """``total`` counts visible agents and each page is full."""
    first = client.get("/api/v1/agents", params={"limit": 2})
    assert first.status_code == 200, first.text
    assert first.json()["total"] == 3
    assert [row["display_name"] for row in first.json()["items"]] == [
        "shown-000",
        "shown-002",
    ]
    second = client.get("/api/v1/agents", params={"limit": 2, "offset": 2})
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 3
    assert [row["display_name"] for row in second.json()["items"]] == ["shown-004"]


def test_agent_list_without_authorizer_still_pages_in_sql(
    client: TestClient, account_agents: None
) -> None:
    """With nothing hidden, paging and the total are unchanged."""
    response = client.get("/api/v1/agents", params={"limit": 4})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 6
    assert len(response.json()["items"]) == 4
