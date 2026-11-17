"""List endpoints report a total and filter visibility before paging."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.schemas.flow import FlowCreate
from preloop.plugins import account_hooks


@pytest.fixture
def many_projects(
    db_session: Session, test_tracker: models.Tracker
) -> list[models.Project]:
    """Create more accessible projects than one default page holds."""
    organization = crud.crud_organization.create(
        db_session,
        obj_in={
            "name": "example-org",
            "identifier": "example-org",
            "tracker_id": test_tracker.id,
        },
    )
    crud.crud_tracker_scope_rule.create(
        db_session,
        obj_in={
            "tracker_id": test_tracker.id,
            "scope_type": "ORGANIZATION",
            "rule_type": "INCLUDE",
            "identifier": organization.identifier,
        },
    )
    return [
        crud.crud_project.create(
            db_session,
            obj_in={
                "name": f"project-{index:03d}",
                "identifier": f"project-{index:03d}",
                "organization_id": organization.id,
            },
        )
        for index in range(105)
    ]


def test_project_list_reports_total_and_pages_completely(
    client: TestClient, many_projects: list[models.Project]
) -> None:
    """The default page is capped, but the header says how many exist."""
    first = client.get("/api/v1/projects")
    assert first.status_code == 200, first.text
    assert len(first.json()) == 100
    assert first.headers["X-Total-Count"] == "105"

    rest = client.get("/api/v1/projects", params={"offset": 100})
    assert rest.status_code == 200, rest.text
    assert rest.headers["X-Total-Count"] == "105"
    seen = [row["id"] for row in first.json()] + [row["id"] for row in rest.json()]
    assert len(seen) == len(set(seen))
    assert set(seen) == {str(project.id) for project in many_projects}


@pytest.fixture
def account_flows(db_session: Session, test_user: models.User) -> list[models.Flow]:
    """Create six flows owned by the test user's account."""
    return [
        crud.crud_flow.create(
            db=db_session,
            flow_in=FlowCreate(
                name=f"{'hidden' if index % 2 else 'shown'}-flow-{index}",
                description="Example flow",
                trigger_event_source="github",
                trigger_event_types=["push"],
                prompt_template="Summarize the change",
                agent_type="openhands",
                agent_config={},
                account_id=test_user.account_id,
            ),
            account_id=test_user.account_id,
        )
        for index in range(6)
    ]


@pytest.fixture
def hide_odd_flows() -> Iterator[None]:
    """Register an authorizer that denies viewing flows named ``hidden-*``."""

    def authorizer(
        ctx: account_hooks.AuthorizationContext, action: str, resource: Any
    ) -> account_hooks.Decision | None:
        """Deny view on hidden flows and defer everything else."""
        name = getattr(resource, "name", "") or ""
        if action == account_hooks.ACTION_RESOURCE_VIEW and name.startswith("hidden"):
            return account_hooks.Decision(effect="deny")
        return None

    previous = account_hooks.get_authorizer()
    account_hooks.register_authorizer(authorizer)
    try:
        yield
    finally:
        account_hooks.register_authorizer(previous)


@pytest.mark.parametrize("path", ["/api/v1/flows", "/api/v1/flows/summary"])
def test_flow_lists_filter_visibility_before_paging(
    client: TestClient,
    account_flows: list[models.Flow],
    hide_odd_flows: None,
    path: str,
) -> None:
    """A full page comes back while visible flows remain, with their total."""
    visible = {str(flow.id) for flow in account_flows if flow.name.startswith("shown")}
    assert all(isinstance(flow.id, UUID) for flow in account_flows)

    first = client.get(path, params={"skip": 0, "limit": 2})
    assert first.status_code == 200, first.text
    assert first.headers["X-Total-Count"] == "3"
    assert len(first.json()) == 2

    second = client.get(path, params={"skip": 2, "limit": 2})
    assert second.status_code == 200, second.text
    assert second.headers["X-Total-Count"] == "3"
    ids = [row["id"] for row in first.json() + second.json()]
    assert len(ids) == 3
    assert set(ids) == visible
