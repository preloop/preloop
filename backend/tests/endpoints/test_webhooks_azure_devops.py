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
        patch(
            "preloop.api.endpoints.webhooks.project_scope_denial", return_value=None
        ) as scope_denial,
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
            "crud_tracker": crud_tracker,
            "scope_denial": scope_denial,
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


def test_unknown_repository_is_acknowledged_and_visible(harness) -> None:
    harness["crud_project"].get_for_tracker_by_identifier.return_value = None
    response = post(harness, fx("git.pullrequest.created"))
    assert response.status_code == 200
    assert response.json()["reason"] == "repository_not_imported"
    assert dispatched(harness) == []
    harness["crud_tracker"].record_ignored_webhook.assert_called_once()


def test_completed_pull_request_dispatches_a_distinct_close_event(harness) -> None:
    response = post(harness, fx("git.pullrequest.updated.completed"))
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    (call,) = dispatched(harness)
    assert call.kwargs["delivery_id"].endswith(":pull_request_merged")


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


# ----------------------------------------------------------------------
# Azure Boards work items
# ----------------------------------------------------------------------

WORK_ITEM_FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "azure_devops"
        / "work_items.json"
    ).read_text()
)["events"]
BOT_ID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"


def wi(key: str) -> Dict[str, Any]:
    return copy.deepcopy(WORK_ITEM_FIXTURES[key])


@pytest.fixture
def boards(harness):
    project = MagicMock()
    project.id = uuid.UUID("99999999-0000-0000-0000-000000000003")
    project.organization_id = uuid.UUID("99999999-0000-0000-0000-000000000001")
    project.identifier = "c2c2c2c2-dddd-eeee-ffff-a3a3a3a3a3a3"
    project.slug = "fabrikam/FabrikamCloud/FabrikamCloud"
    project.name = "FabrikamCloud"
    project.meta_data = {"repository": "FabrikamCloud", "project": "FabrikamCloud"}
    harness["tracker"].connection_details = {
        "organization": "fabrikam",
        "token_identity_id": BOT_ID,
        "token_identity_unique_name": "preloop-bot@example.com",
        "work_item_states": {
            "fabrikamcloud": {"bug": {"new": "Proposed", "approved": "Completed"}}
        },
    }
    with (
        patch(
            "preloop.api.endpoints.webhooks.resolve_work_item_project",
            return_value=project,
        ) as resolve,
        patch("preloop.api.endpoints.webhooks.crud_issue") as crud_issue,
        patch("preloop.api.endpoints.webhooks.crud_comment") as crud_comment,
    ):
        db_issue = MagicMock()
        db_issue.id = uuid.UUID("99999999-0000-0000-0000-0000000000aa")
        crud_issue.upsert.return_value = (db_issue, True)
        crud_comment.get_by_external_id.return_value = None
        crud_comment.create.return_value = MagicMock(id="c-1")
        yield {
            **harness,
            "project": project,
            "resolve": resolve,
            "crud_issue": crud_issue,
            "crud_comment": crud_comment,
        }


def test_work_item_created_is_stored_and_dispatched(boards) -> None:
    response = post(boards, wi("workitem.created"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    row = boards["crud_issue"].upsert.call_args.kwargs["obj_in"]
    assert row["external_id"] == "5"
    assert row["key"] == "fabrikam/FabrikamCloud#5"
    assert row["title"] == "Some great new idea!"
    assert row["description"] == "Steps: `login`"
    assert row["status"] == "open"
    assert row["meta_data"]["labels"] == ["triage"]
    assert row["project_id"] == boards["project"].id
    boards["resolve"].assert_called_once()
    assert boards["resolve"].call_args.args[2] == "FabrikamCloud"
    (call,) = dispatched(boards)
    kwargs = call.kwargs
    assert kwargs["event_type"] == "workitem.created"
    assert kwargs["delivery_id"] == (
        f"azure_devops:{TRACKER_ID}:wi:5:rev:1:workitem.created"
    )
    payload = kwargs["payload"]
    assert payload["project_id"] == str(boards["project"].id)
    assert payload["repository"]["id"] == boards["project"].identifier
    assert payload["object_attributes"]["title"] == "Some great new idea!"
    assert kwargs["embedding_requests"] == [
        {
            "issue_id": boards["crud_issue"].upsert.return_value[0].id,
            "force_update": True,
        }
    ]


def test_work_item_state_category_comes_from_the_snapshot(boards) -> None:
    # "Approved" is a Completed state in this (synthetic) process snapshot.
    post(boards, wi("workitem.updated"))
    row = boards["crud_issue"].upsert.call_args.kwargs["obj_in"]
    assert row["status"] == "closed"
    assert row["meta_data"]["state_name"] == "Approved"
    assert row["meta_data"]["state_category"] == "Completed"
    (call,) = dispatched(boards)
    assert call.kwargs["payload"]["object_attributes"]["state"] == "closed"


def test_work_item_comment_is_stored(boards) -> None:
    post(boards, wi("workitem.commented"))
    values = boards["crud_comment"].create.call_args.kwargs["obj_in"]
    assert values["external_id"] == "77"
    assert values["body"] == "This is a **great** new idea"
    assert values["meta_data"]["external_author"] == "Jane Doe"
    (call,) = dispatched(boards)
    assert call.kwargs["payload"]["comment"]["body"] == "This is a **great** new idea"


def test_own_work_item_update_is_marked_self_generated(boards) -> None:
    payload = wi("workitem.updated")
    payload["resource"]["revisedBy"]["id"] = BOT_ID
    post(boards, payload)
    (call,) = dispatched(boards)
    assert call.kwargs["payload"]["azure_devops"]["self_generated"] is True


def test_work_item_without_a_home_project_is_acknowledged(boards) -> None:
    boards["resolve"].return_value = None
    response = post(boards, wi("workitem.created"))
    assert response.json() == {
        "status": "ignored",
        "reason": "no_project_for_work_item",
        "tracker_id": str(TRACKER_ID),
    }
    assert dispatched(boards) == []
    boards["crud_issue"].upsert.assert_not_called()


def test_work_item_of_another_project_is_rejected(boards) -> None:
    boards["tracker"].connection_details = {
        "organization": "fabrikam",
        "project": "Other",
    }
    response = post(boards, wi("workitem.created"))
    assert response.status_code == 403
    assert dispatched(boards) == []


def test_work_item_needs_the_secret(boards) -> None:
    response = post(boards, wi("workitem.created"), secret="wrong")
    assert response.status_code == 403
    boards["crud_issue"].upsert.assert_not_called()


@pytest.mark.parametrize(
    "event", ["git.pullrequest.created", "git.pullrequest.updated"]
)
@pytest.mark.parametrize("secret", [SECRET, "wrong", None])
def test_subscription_test_checks_secret_and_never_dispatches(
    harness: Dict[str, Any], event: str, secret: Optional[str]
) -> None:
    samples = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "azure_devops"
            / "test_notifications.json"
        ).read_text()
    )["events"]
    harness["tracker"].connection_details = {"organization": "example-org"}
    response = post(harness, samples[event], secret=secret)
    assert response.status_code == (200 if secret == SECRET else 403)
    if secret == SECRET:
        assert response.json()["reason"] == "test_notification"
    assert dispatched(harness) == []


@pytest.mark.parametrize("binding", [{"project": "Other"}, {"repository": "Other"}])
def test_outside_bound_scope_is_acknowledged(
    harness: Dict[str, Any], binding: dict
) -> None:
    harness["tracker"].connection_details = {"organization": "fabrikam", **binding}
    response = post(harness, fx("git.pullrequest.created"))
    assert response.status_code == 200
    assert response.json()["reason"] == "repository_outside_scope"
    assert dispatched(harness) == []


def test_previously_imported_repository_excluded_by_scope_is_ignored(
    harness: Dict[str, Any],
) -> None:
    harness[
        "scope_denial"
    ].return_value = "Access denied: Project is explicitly excluded."
    response = post(harness, fx("git.pullrequest.created"))
    assert response.status_code == 200
    assert response.json()["reason"] == "repository_outside_scope"
    assert dispatched(harness) == []


def test_close_event_ignores_review_subscription_filter(
    harness: Dict[str, Any],
) -> None:
    harness["tracker"].subscribed_events = ["pull_request_opened"]
    response = post(harness, fx("git.pullrequest.updated.completed"))
    assert response.status_code == 200
    assert len(dispatched(harness)) == 1


@pytest.mark.parametrize(
    "body,accepted",
    [
        (" /PrElOoP ReViEw \n", True),
        ("Please /preloop review", False),
        ("/preloop review please", False),
        ("/preloop review\n<!-- preloop:comment -->", False),
        ("Looks good", False),
    ],
)
def test_exact_comment_command_and_own_comment_marker(
    harness: Dict[str, Any], body: str, accepted: bool
) -> None:
    sample = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "azure_devops"
            / "comment_notification.json"
        ).read_text()
    )
    sample["resource"]["content"] = body
    harness["tracker"].connection_details = {"organization": "example-org"}
    harness["crud_project"].get_for_tracker_by_identifier.return_value.meta_data = {
        "full_name": "example-org/Project/repo"
    }
    response = post(harness, sample)
    assert response.status_code == 200
    assert bool(dispatched(harness)) is accepted
    if accepted:
        assert (
            dispatched(harness)[0].kwargs["payload"]["azure_review_request"]["kind"]
            == "comment"
        )
