"""Azure DevOps service hook intake: authentication, normalization, dedupe.

Deliveries are the Microsoft samples from
https://learn.microsoft.com/azure/devops/service-hooks/events, stored in
``tests/fixtures/azure_devops/service_hooks.json``. No Azure DevOps
organization is contacted.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import get_task_publisher
from preloop.utils.azure_devops import WEBHOOK_SECRET_HEADER

SECRET = "azure-webhook-secret"
TRACKER_ID = uuid.UUID("11111111-2222-3333-4444-666666666666")
URL = f"/api/v1/private/webhooks/azure_devops/{TRACKER_ID}"
FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "azure_devops"
        / "service_hooks.json"
    ).read_text()
)["events"]


def fx(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES[key])


@pytest.fixture(autouse=True)
def no_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")


@pytest.fixture
def harness():
    with (
        patch("preloop.api.app.connect_nats", new_callable=AsyncMock),
        patch("preloop.api.app.close_nats", new_callable=AsyncMock),
        patch("preloop.api.endpoints.webhooks.crud_tracker") as crud_tracker,
        patch("preloop.api.endpoints.webhooks.crud_project") as crud_project,
        patch("preloop.api.endpoints.webhooks.crud_organization"),
        patch(
            "preloop.api.endpoints.webhooks.crud_forge_webhook_delivery"
        ) as deliveries,
    ):
        app = create_app()
        session = MagicMock(spec=Session)
        publisher = AsyncMock()

        def override_db():
            yield session

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_task_publisher] = lambda: publisher
        tracker = MagicMock()
        tracker.id = TRACKER_ID
        tracker.tracker_type = "azure_devops"
        tracker.is_active = True
        tracker.is_deleted = False
        tracker.subscribed_events = None
        tracker.resolved_webhook_secret = SECRET
        tracker.connection_details = {"organization": "fabrikam"}
        crud_tracker.get.return_value = tracker
        project = MagicMock()
        project.id = uuid.UUID("99999999-0000-0000-0000-000000000002")
        project.organization_id = uuid.UUID("99999999-0000-0000-0000-000000000001")
        crud_project.get_for_tracker_by_identifier.return_value = project
        deliveries.claim.return_value = True
        yield {
            "client": TestClient(app),
            "publisher": publisher,
            "tracker": tracker,
            "crud_project": crud_project,
            "deliveries": deliveries,
        }
        app.dependency_overrides.clear()


def post(
    harness: Dict[str, Any],
    payload: Any,
    *,
    secret: Optional[str] = SECRET,
    headers: Optional[Dict[str, str]] = None,
):
    sent = {"Content-Type": "application/json", **(headers or {})}
    if secret is not None:
        sent[WEBHOOK_SECRET_HEADER] = secret
    return harness["client"].post(URL, content=json.dumps(payload), headers=sent)


def dispatched(harness: Dict[str, Any]) -> list:
    return [
        c
        for c in harness["publisher"].publish_task.await_args_list
        if c.args[0] == "process_webhook_event"
    ]


def test_authenticated_created_event_is_dispatched(harness) -> None:
    response = post(harness, fx("git.pullrequest.created"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    (call,) = dispatched(harness)
    kwargs = call.kwargs
    assert kwargs["tracker_type"] == "azure_devops"
    assert kwargs["event_type"] == "git.pullrequest.created"
    assert kwargs["delivery_id"].endswith(
        ":pr:1:4444eeee455ff5aaaaabb66ccccccccc7777cccc"
    )
    attrs = kwargs["payload"]["object_attributes"]
    assert attrs["url"].endswith("/fabrikam/Fabrikam/_git/Fabrikam/pullrequest/1")
    harness["crud_project"].get_for_tracker_by_identifier.assert_called_with(
        harness["crud_project"].get_for_tracker_by_identifier.call_args.args[0],
        tracker_id=TRACKER_ID,
        identifier="c2c2c2c2-dddd-eeee-ffff-a3a3a3a3a3a3",
    )


@pytest.mark.parametrize("secret", [None, "wrong", ""])
def test_unauthenticated_deliveries_are_rejected(harness, secret) -> None:
    response = post(harness, fx("git.pullrequest.created"), secret=secret)
    assert response.status_code == 403
    assert dispatched(harness) == []


def test_tracker_without_secret_rejects_everything(harness) -> None:
    harness["tracker"].resolved_webhook_secret = None
    assert post(harness, fx("git.pullrequest.created")).status_code == 403


def test_other_tracker_types_are_not_found(harness) -> None:
    harness["tracker"].tracker_type = "github"
    assert post(harness, fx("git.pullrequest.created")).status_code == 404


def test_foreign_organization_is_rejected(harness) -> None:
    harness["tracker"].connection_details = {"organization": "contoso"}
    assert post(harness, fx("git.pullrequest.created")).status_code == 403
    assert dispatched(harness) == []


def test_unknown_repository_is_rejected(harness) -> None:
    harness["crud_project"].get_for_tracker_by_identifier.return_value = None
    assert post(harness, fx("git.pullrequest.created")).status_code == 403


def test_completed_pull_request_is_acknowledged_without_dispatch(harness) -> None:
    response = post(harness, fx("git.pullrequest.updated.completed"))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert dispatched(harness) == []


def test_update_on_seen_head_is_a_duplicate(harness) -> None:
    harness["deliveries"].claim.return_value = False
    response = post(harness, fx("git.pullrequest.updated"))
    assert response.json()["status"] == "duplicate"
    assert dispatched(harness) == []


def test_invalid_json_is_rejected(harness) -> None:
    response = harness["client"].post(
        URL, content=b"{not json", headers={WEBHOOK_SECRET_HEADER: SECRET}
    )
    assert response.status_code == 400
