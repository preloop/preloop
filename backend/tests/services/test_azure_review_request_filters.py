"""Explicit requests honor the configured copy of the real reviewer preset."""

import copy
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, PropertyMock, patch

from fastapi.testclient import TestClient

import pytest
from sqlalchemy.orm import Session

from preloop.flow_presets import load_flow_presets
from preloop.models import models, schemas
from preloop.models.crud import crud_flow, crud_project, crud_tracker
from preloop.models.crud import crud_azure_review_request as requests
from preloop.services.azure_review_requests import flush_requests, poll_labels
from preloop.services.flow_presets_service import clone_preset_for_account
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.event_normalizer import extract_filter_fields
from preloop.sync.services.event_bus import get_task_publisher
from preloop.sync.tasks import process_webhook_event
from preloop.utils.azure_devops import PULL_REQUEST_COMMENT_EVENT, normalize_delivery
from tests.services.test_azure_review_requests import setup_request  # noqa: F401
from tests.services.test_flow_trigger_pr_lifecycle_stop import stubbed_runtime
from tests.sync.trackers.test_azure_devops import PR, REPO_ID


@pytest.fixture
def reviewer(db_session: Session, setup_request: Any) -> models.Flow:  # noqa: F811
    """Clone the shipped preset, then configure the reported ready/open filter."""
    old_flow, event = setup_request
    old_flow.is_enabled = False
    old_flow.trigger_event_types = []
    definition = next(
        item for item in load_flow_presets() if item["name"] == "Pull Request Reviewer"
    )
    preset = crud_flow.create(
        db_session,
        flow_in=schemas.FlowCreate(**{**definition, "is_enabled": False}),
        account_id=None,
    )
    with patch(
        "preloop.api.endpoints.flows._resolve_clone_model_binding", return_value=None
    ):
        flow = clone_preset_for_account(db_session, preset, old_flow.account_id)
    return crud_flow.update(
        db_session,
        db_obj=flow,
        account_id=old_flow.account_id,
        flow_in=schemas.FlowUpdate(
            **{
                "trigger_event_source": event["tracker_id"],
                "trigger_event_types": ["pull_request_opened", "pull_request_updated"],
                "trigger_config": {"draft": False, "state": "open"},
                "is_enabled": True,
            }
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "label"])
@pytest.mark.parametrize(
    "state", ["active", "draft", "abandoned", "completed", "paused"]
)
async def test_request_runs_configured_preset(
    db_session: Session,
    reviewer: models.Flow,
    client: TestClient,
    kind: str,
    state: str,
) -> None:
    """Both entry points create an execution only for a live, ready PR."""
    current = copy.deepcopy(PR)
    current["isDraft"] = state == "draft"
    current["status"] = "active" if state in {"draft", "paused"} else state
    if state == "paused":
        reviewer.is_enabled = False
        db_session.commit()
    project = crud_project.get_for_tracker_by_identifier(
        db_session, tracker_id=reviewer.trigger_event_source, identifier=REPO_ID
    )
    service = FlowTriggerService(db_session)
    remove = AsyncMock()
    with (
        patch.object(db_session, "close"),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new=AsyncMock(return_value=current),
        ),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.list_pull_requests",
            new=AsyncMock(return_value={"items": [{"number": 22}], "has_more": False}),
        ),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.list_pull_request_labels",
            new=AsyncMock(return_value=[{"id": "label-1", "name": "preloop-review"}]),
        ),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.remove_pull_request_label",
            new=remove,
        ),
        stubbed_runtime(),
    ):
        now = datetime.now(timezone.utc)
        if kind == "label":
            await poll_labels(db_session, service, now)
        else:
            repo_url = (
                f"https://dev.azure.com/fabrikam/_apis/git/repositories/{REPO_ID}"
            )
            raw = {
                "eventType": PULL_REQUEST_COMMENT_EVENT,
                "resource": {
                    "content": "/preloop review",
                    "_links": {
                        "repository": {"href": repo_url},
                        "self": {
                            "href": f"{repo_url}/pullRequests/22/threads/1/comments/1"
                        },
                    },
                },
            }
            delivery = normalize_delivery(raw, organization="fabrikam")
            assert (
                extract_filter_fields(
                    "azure_devops", PULL_REQUEST_COMMENT_EVENT, delivery.payload
                )["state"]
                is None
            )
            publisher = AsyncMock()
            client.app.dependency_overrides[get_task_publisher] = lambda: publisher
            with patch.object(
                models.Tracker,
                "resolved_webhook_secret",
                new_callable=PropertyMock,
                return_value="test-webhook-secret",
            ):
                response = client.post(
                    f"/api/v1/private/webhooks/azure_devops/{reviewer.trigger_event_source}",
                    json=raw,
                    headers={"X-Preloop-Webhook-Secret": "test-webhook-secret"},
                )
            assert response.status_code == 200
            tasks = [
                call
                for call in publisher.publish_task.await_args_list
                if call.args[0] == "process_webhook_event"
            ]
            assert len(tasks) == 1
            with (
                patch(
                    "preloop.sync.tasks.get_db_session", return_value=iter([db_session])
                ),
                patch.object(db_session, "close"),
            ):
                await process_webhook_event(**tasks[0].kwargs)
        count = await flush_requests(db_session, service, now + timedelta(seconds=16))
    rows = db_session.query(models.FlowExecution).filter_by(flow_id=reviewer.id).all()
    assert count == len(rows) == int(state == "active")
    if rows:
        payload = rows[0].trigger_event_details["payload"]
        assert payload["state"] == payload["pull_request"]["state"] == "open"
        assert payload["draft"] is payload["pull_request"]["draft"] is False
        assert payload["azure_review_request"]["kind"] == kind
        assert rows[0].flow.source_preset_id == reviewer.source_preset_id
    assert remove.await_count == int(kind == "label" and state == "active")
    status = crud_tracker.get(db_session, id=reviewer.trigger_event_source).meta_data[
        "azure_review_status"
    ]
    reason = {
        "active": "run_created",
        "draft": "filter_mismatch",
        "paused": "flow_paused",
    }.get(state, "pull_request_not_active")
    assert status["recent"][0]["reason"] == reason
    if kind == "label":
        assert status["last_poll_at"]


def test_label_poll_includes_any_source_and_paused_flows(
    db_session: Session, reviewer: models.Flow
) -> None:
    """Polling uses the same source/account/project scope as webhook routing."""
    tracker_id = reviewer.trigger_event_source
    project = crud_project.get_for_tracker_by_identifier(
        db_session, tracker_id=tracker_id, identifier=REPO_ID
    )
    reviewer.trigger_event_source = None
    reviewer.is_enabled = False
    reviewer.trigger_project_ids = [str(project.id)]
    db_session.commit()
    now = datetime.now(timezone.utc)
    assert requests.claim_projects(db_session, now=now) == [(project.id, 1)]
    assert requests.claim_projects(db_session, now=now + timedelta(seconds=15)) == []
    assert requests.claim_projects(db_session, now=now + timedelta(seconds=30)) == [
        (project.id, 1)
    ]
    reviewer.trigger_project_ids = ["00000000-0000-0000-0000-000000000000"]
    db_session.commit()
    assert requests.claim_projects(db_session, now=now + timedelta(seconds=60)) == []


@pytest.mark.asyncio
async def test_queued_request_rechecks_draft_filter(
    db_session: Session, reviewer: models.Flow
) -> None:
    """A PR becoming draft in the quiet window must not leave an endless retry."""
    from preloop.utils.azure_devops import normalize_pull_request

    service = FlowTriggerService(db_session)
    payload = normalize_pull_request(PR, "fabrikam")
    payload.update(
        extract_filter_fields("azure_devops", "git.pullrequest.updated", payload)
    )
    payload["azure_review_request"] = {"kind": "comment", "signal": "2:1"}
    event = {
        "source": "azure_devops",
        "type": "pull_request_updated",
        "tracker_id": reviewer.trigger_event_source,
        "account_id": str(reviewer.account_id),
        "payload": payload,
    }
    now = datetime.now(timezone.utc)
    requests.enqueue(
        db_session, flow=reviewer, event=event, now=now - timedelta(seconds=16)
    )
    with (
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new=AsyncMock(return_value={**PR, "isDraft": True}),
        ),
        stubbed_runtime(),
    ):
        assert await flush_requests(db_session, service, now) == 0
    assert requests.claim_due(db_session, now=now + timedelta(minutes=3)) == []
    assert (
        crud_tracker.get(db_session, id=reviewer.trigger_event_source).meta_data[
            "azure_review_status"
        ]["recent"][0]["reason"]
        == "filter_mismatch"
    )


@pytest.mark.parametrize(
    "source,event_type,payload,expected_state",
    [
        (
            "github",
            "pull_request",
            {
                "action": "review_requested",
                "pull_request": {"state": "open", "draft": False},
            },
            "open",
        ),
        (
            "github",
            "pull_request",
            {
                "action": "labeled",
                "label": {"name": "preloop-review"},
                "pull_request": {"state": "open", "draft": False},
            },
            "open",
        ),
        (
            "gitlab",
            "Merge Request Hook",
            {
                "object_kind": "merge_request",
                "object_attributes": {
                    "state": "opened",
                    "draft": False,
                    "action": "update",
                },
            },
            "opened",
        ),
        (
            "bitbucket",
            "pullrequest:updated",
            {"pullrequest": {"state": "OPEN", "draft": False}},
            "open",
        ),
        (
            "bitbucket_dc",
            "pr:reviewer:updated",
            {"pull_request": {"state": "open", "draft": False}},
            "open",
        ),
    ],
)
def test_other_forge_pr_events_carry_filters(
    source: str, event_type: str, payload: dict[str, Any], expected_state: str
) -> None:
    """Other forge PR deliveries already extract ready/state before matching."""
    fields = extract_filter_fields(source, event_type, payload)
    assert fields["draft"] is False
    assert fields["state"] == expected_state


def test_review_status_history_is_bounded(
    db_session: Session, reviewer: models.Flow
) -> None:
    """Recent outcomes replace matching entries and never persist raw payloads."""
    for number in range(12):
        event = {
            "payload": {
                "repository": {"id": REPO_ID},
                "pull_request": {"number": number},
                "azure_review_request": {"kind": "comment", "signal": "1:1"},
                "comment": {"body": "not retained"},
            }
        }
        requests.record_status(
            db_session,
            tracker_id=reviewer.trigger_event_source,
            event=event,
            reason="filter_mismatch",
            flow_id=reviewer.id,
        )
    requests.record_status(
        db_session,
        tracker_id=reviewer.trigger_event_source,
        event=event,
        reason="accepted",
        flow_id=reviewer.id,
    )
    recent = crud_tracker.get(db_session, id=reviewer.trigger_event_source).meta_data[
        "azure_review_status"
    ]["recent"]
    assert len(recent) == 10
    assert [row["number"] for row in recent] == list(range(11, 1, -1))
    assert recent[0]["reason"] == "accepted"
    assert all(
        set(row) == {"repository_id", "number", "kind", "flow_id", "reason", "at"}
        for row in recent
    )


@pytest.mark.asyncio
async def test_label_poll_failure_is_visible(
    db_session: Session, reviewer: models.Flow
) -> None:
    """A failed provider read leaves an actionable tracker status."""
    from preloop.sync.exceptions import TrackerPermissionError

    with patch(
        "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.list_pull_requests",
        new=AsyncMock(side_effect=TrackerPermissionError("denied")),
    ):
        await poll_labels(
            db_session, FlowTriggerService(db_session), datetime.now(timezone.utc)
        )
    status = crud_tracker.get(db_session, id=reviewer.trigger_event_source).meta_data[
        "azure_review_status"
    ]
    assert status["poll_error"] == "poll_failed"
    assert status["last_poll_at"]


def test_any_source_poll_cannot_cross_accounts(
    db_session: Session, reviewer: models.Flow
) -> None:
    """An unrelated account's wildcard flow cannot cause provider polling."""
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Example Account"}
    )
    reviewer.trigger_event_source = None
    reviewer.account_id = other.id
    db_session.commit()
    assert requests.claim_projects(db_session, now=datetime.now(timezone.utc)) == []
