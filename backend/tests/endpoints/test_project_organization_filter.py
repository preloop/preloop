"""Organization filters must compare UUIDs loaded from real database rows."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models


@pytest.fixture
def organization_projects(
    db_session: Session, test_tracker: models.Tracker
) -> tuple[models.Organization, list[models.Project]]:
    """Create matching, excluded and unrelated projects with database UUIDs."""
    organizations = [
        crud.crud_organization.create(
            db_session,
            obj_in={
                "name": name,
                "identifier": name,
                "tracker_id": test_tracker.id,
            },
        )
        for name in ("selected-org", "other-org")
    ]
    for organization in organizations:
        crud.crud_tracker_scope_rule.create(
            db_session,
            obj_in={
                "tracker_id": test_tracker.id,
                "scope_type": "ORGANIZATION",
                "rule_type": "INCLUDE",
                "identifier": organization.identifier,
            },
        )
    selected, other = organizations
    projects = [
        crud.crud_project.create(
            db_session,
            obj_in={
                "name": name,
                "identifier": name,
                "organization_id": organization.id,
            },
        )
        for name, organization in (
            ("selected-one", selected),
            ("selected-two", selected),
            ("excluded", selected),
            ("other-project", other),
        )
    ]
    crud.crud_tracker_scope_rule.create(
        db_session,
        obj_in={
            "tracker_id": test_tracker.id,
            "scope_type": "PROJECT",
            "rule_type": "EXCLUDE",
            "identifier": "excluded",
        },
    )
    assert isinstance(selected.id, UUID)
    assert isinstance(projects[0].organization_id, UUID)
    return selected, projects


@pytest.mark.parametrize("route", ["query", "path"])
@pytest.mark.parametrize("uppercase", [False, True])
def test_organization_filter_returns_only_accessible_projects(
    client: TestClient,
    organization_projects: tuple[models.Organization, list[models.Project]],
    route: str,
    uppercase: bool,
) -> None:
    """Both HTTP routes retain matching rows and paginate after scope filtering."""
    selected, projects = organization_projects
    organization_id = str(selected.id)
    if uppercase:
        organization_id = organization_id.upper()
    url = (
        f"/api/v1/projects?organization_id={organization_id}"
        if route == "query"
        else f"/api/v1/organizations/{organization_id}/projects"
    )
    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    items = body if route == "query" else body["items"]
    assert {item["id"] for item in items} == {str(p.id) for p in projects[:2]}
    if route == "path":
        assert body["total"] == 2

    separator = "&" if route == "query" else "?"
    page = client.get(f"{url}{separator}limit=1&offset=1")
    assert page.status_code == 200, page.text
    page_body = page.json()
    page_items = page_body if route == "query" else page_body["items"]
    assert len(page_items) == 1
    assert page_items[0]["id"] in {str(p.id) for p in projects[:2]}
    if route == "path":
        assert page_body["total"] == 2
