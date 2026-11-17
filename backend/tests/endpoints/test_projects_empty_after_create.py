"""An empty project list right after a create in its scope is flagged."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints import projects
from preloop.models import models


def _project(db: Session, tracker: Any, *, age: timedelta) -> models.Organization:
    org = models.Organization(
        name="example", identifier=f"example-{uuid4().hex[:6]}", tracker_id=tracker.id
    )
    db.add(org)
    db.flush()
    db.add(
        models.Project(
            name="api",
            identifier=f"api-{uuid4().hex[:6]}",
            organization_id=org.id,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None) - age,
        )
    )
    db.flush()
    return org


@pytest.fixture
def flagged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    # Scope rules hide every project, as when a new project is not visible yet.
    monkeypatch.setattr(projects, "get_accessible_projects", lambda **_: [])
    monkeypatch.setattr(projects, "mark_empty_after_create", calls.append)
    return calls


@pytest.mark.parametrize(
    "path",
    ["/api/v1/projects?organization_id={org}", "/api/v1/organizations/{org}/projects"],
)
def test_empty_list_right_after_a_create_is_flagged(
    client: TestClient,
    db_session: Session,
    test_tracker: Any,
    flagged: list[Any],
    path: str,
) -> None:
    org = _project(db_session, test_tracker, age=timedelta(seconds=5))
    response = client.get(path.format(org=org.id))
    assert response.status_code == 200
    assert len(flagged) == 1


def test_empty_list_long_after_the_last_create_is_not_flagged(
    client: TestClient, db_session: Session, test_tracker: Any, flagged: list[Any]
) -> None:
    org = _project(db_session, test_tracker, age=timedelta(hours=2))
    assert client.get(f"/api/v1/projects?organization_id={org.id}").status_code == 200
    assert flagged == []
