"""Explicit Azure requests are durable, bounded and never overlap a live review."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_azure_review_request as requests
from preloop.services.azure_review_requests import (
    flush_requests,
    poll_labels,
    request_enabled,
)
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.exceptions import TrackerPermissionError
from preloop.utils.azure_devops import normalize_pull_request
from tests.services.test_flow_trigger_pr_lifecycle_stop import (
    make_flow,
    make_execution,
    stubbed_runtime,
)
from tests.sync.trackers.test_azure_devops import PR, REPO_ID


@pytest.fixture
def setup_request(
    db_session: Session, test_tracker: models.Tracker
) -> tuple[models.Flow, dict[str, Any]]:
    """Create a repository review flow and an exact command request."""
    test_tracker.tracker_type = "azure_devops"
    test_tracker.connection_details = {"organization": "fabrikam"}
    flow = make_flow(
        db_session, test_tracker.account_id, types=["pull_request_updated"]
    )
    flow.trigger_event_source = str(test_tracker.id)
    db_session.flush()
    event = {
        "source": "azure_devops",
        "type": "pull_request_updated",
        "tracker_id": str(test_tracker.id),
        "account_id": str(test_tracker.account_id),
        "payload": {
            **normalize_pull_request(PR, "fabrikam"),
            "azure_review_request": {"kind": "comment", "signal": "1:1"},
        },
    }
    return flow, event


def test_requests_deduplicate_and_coalesce(
    db_session: Session, setup_request: Any
) -> None:
    """Duplicate signals never extend the quiet window; bursts keep one row."""
    flow, event = setup_request
    now = datetime.now(timezone.utc)
    assert requests.enqueue(db_session, flow=flow, event=event, now=now)
    assert not requests.enqueue(
        db_session, flow=flow, event=event, now=now + timedelta(seconds=5)
    )
    event["payload"]["azure_review_request"]["signal"] = "1:2"
    assert requests.enqueue(
        db_session, flow=flow, event=event, now=now + timedelta(seconds=5)
    )
    assert requests.claim_due(db_session, now=now + timedelta(seconds=16)) == []
    ids = requests.claim_due(db_session, now=now + timedelta(seconds=21))
    assert len(ids) == 1
    row = requests.get(db_session, ids[0])
    assert row.revision == 2
    assert requests.claim_due(db_session, now=now + timedelta(seconds=22)) == []
    # A late acknowledgment cannot discard a signal accepted since the lease.
    event["payload"]["azure_review_request"]["signal"] = "1:3"
    requests.enqueue(db_session, flow=flow, event=event, now=now)
    requests.finish(db_session, row_id=row.id, revision=2, done=True, now=now)
    assert requests.get(db_session, row.id).due_at is not None


def test_label_requires_removal_before_rearming(
    db_session: Session, setup_request: Any
) -> None:
    """An attached label requests once, including when removal is forbidden."""
    flow, event = setup_request
    event["payload"]["azure_review_request"] = {
        "kind": "label",
        "signal": "label-1",
        "label": "preloop-review",
    }
    assert requests.enqueue(db_session, flow=flow, event=event)
    assert not requests.enqueue(db_session, flow=flow, event=event)
    requests.observe_labels(
        db_session,
        tracker_id=event["tracker_id"],
        repository_id=REPO_ID,
        number=22,
        present={"label-1"},
    )
    assert not requests.enqueue(db_session, flow=flow, event=event)
    requests.observe_labels(
        db_session,
        tracker_id=event["tracker_id"],
        repository_id=REPO_ID,
        number=22,
        present=set(),
    )
    assert requests.enqueue(db_session, flow=flow, event=event)


@pytest.mark.asyncio
async def test_active_run_gets_exactly_one_pending_followup(
    db_session: Session, setup_request: Any
) -> None:
    """Two requests during a run produce one execution after it finishes."""
    flow, event = setup_request
    service = FlowTriggerService(db_session)
    active = make_execution(
        db_session,
        flow,
        source="azure_devops",
        event_type="pull_request_updated",
        payload=event["payload"],
    )
    now = datetime.now(timezone.utc)
    requests.enqueue(db_session, flow=flow, event=event, now=now - timedelta(minutes=1))
    event["payload"]["azure_review_request"]["signal"] = "1:2"
    requests.enqueue(db_session, flow=flow, event=event, now=now - timedelta(minutes=1))
    with (
        patch("preloop.services.azure_review_requests._project", return_value=True),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new=AsyncMock(return_value=PR),
        ),
        patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=[flow],
        ),
        stubbed_runtime(),
    ):
        assert await flush_requests(db_session, service, now) == 0
        active.status = "SUCCEEDED"
        db_session.commit()
        assert (
            await flush_requests(db_session, service, now + timedelta(seconds=16)) == 1
        )
        assert (
            await flush_requests(db_session, service, now + timedelta(seconds=40)) == 0
        )
    rows = db_session.query(models.FlowExecution).filter_by(flow_id=flow.id).all()
    assert len(rows) == 2
    assert (
        rows[-1].trigger_event_details["payload"]["azure_review_request"]["signal"]
        == "1:2"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["closed", "head", "disabled", "scope"])
async def test_pending_requests_recheck_current_state(
    db_session: Session, setup_request: Any, change: str
) -> None:
    """Queued work never dispatches stale or unauthorized repository state."""
    flow, event = setup_request
    now = datetime.now(timezone.utc)
    requests.enqueue(db_session, flow=flow, event=event, now=now - timedelta(minutes=1))
    current = copy.deepcopy(PR)
    if change == "closed":
        current["status"] = "abandoned"
    if change == "head":
        current["lastMergeSourceCommit"]["commitId"] = "e" * 40
    if change == "disabled":
        flow.is_enabled = False
        db_session.commit()
    service = FlowTriggerService(db_session)
    with (
        patch(
            "preloop.services.azure_review_requests._project",
            return_value=change != "scope",
        ),
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new=AsyncMock(return_value=current),
        ),
        patch.object(service, "_process_event", new_callable=AsyncMock) as dispatch,
        patch.object(
            service, "stop_executions_for_ended_pull_request", new_callable=AsyncMock
        ),
    ):
        assert await flush_requests(db_session, service, now) == 0
        dispatch.assert_not_awaited()
    assert requests.claim_due(db_session, now=now + timedelta(minutes=3)) == []


@pytest.mark.parametrize(
    "config,kind,label,expected",
    [
        ({}, "label", "preloop-review", True),
        ({}, "label", "other", False),
        ({"azure_review_label": "review-now"}, "label", "review-now", True),
        ({"azure_review_label": None}, "label", "preloop-review", False),
        ({}, "comment", None, True),
        ({"azure_review_comment_enabled": False}, "comment", None, False),
    ],
)
def test_flow_request_configuration(
    config: dict[str, Any], kind: str, label: str | None, expected: bool
) -> None:
    """Label and comment switches operate independently."""
    assert (
        request_enabled(
            SimpleNamespace(webhook_config=config), {"kind": kind, "label": label}
        )
        is expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_label_poll_accepts_before_removing(
    db_session: Session, setup_request: Any, denied: bool
) -> None:
    """Only accepted labels are removed; permission failure keeps dedupe armed."""
    flow, event = setup_request
    tracker = db_session.get(models.Tracker, flow.trigger_event_source)
    project = SimpleNamespace(
        id=flow.id,
        identifier=REPO_ID,
        organization=SimpleNamespace(tracker=tracker),
        meta_data={"full_name": "fabrikam/Fabrikam-Fiber-Git/Fabrikam-Fiber-Git"},
    )
    service = FlowTriggerService(db_session)
    with (
        patch.object(requests, "claim_projects", return_value=[(project.id, 1)]),
        patch(
            "preloop.services.azure_review_requests.crud_project.get",
            return_value=project,
        ),
        patch("preloop.services.azure_review_requests._project", return_value=project),
        patch(
            "preloop.services.azure_review_requests.crud_flow.get_by_trigger",
            return_value=[flow],
        ),
        patch("preloop.services.azure_review_requests.AzureDevOpsTracker") as factory,
        patch.object(requests, "advance_page") as advance,
        patch.object(requests, "observe_labels") as observe,
        patch.object(
            service, "_process_event", new=AsyncMock(return_value=True)
        ) as process,
    ):
        client = factory.return_value
        client.organization = "fabrikam"
        client.list_pull_requests = AsyncMock(
            return_value={"items": [{"number": 22}], "has_more": True}
        )
        client.list_pull_request_labels = AsyncMock(
            return_value=[
                {"id": "label-1", "name": "preloop-review"},
                {"id": "other", "name": "unrelated"},
            ]
        )
        client.get_pull_request = AsyncMock(return_value=PR)
        client.remove_pull_request_label = AsyncMock(
            side_effect=TrackerPermissionError("denied") if denied else None
        )
        await poll_labels(db_session, service, datetime.now(timezone.utc))
        process.assert_awaited_once()
        client.list_pull_requests.assert_awaited_once_with(
            limit=5, page=1, repo_full_name=project.meta_data["full_name"]
        )
        client.remove_pull_request_label.assert_awaited_once()
        assert observe.call_count == (1 if denied else 2)
        advance.assert_called_once_with(db_session, project_id=project.id, page=2)


def test_label_discovery_rotates_bounded_repository_pages(
    db_session: Session, setup_request: Any
) -> None:
    """Busy repositories cannot starve others; claims are persisted before I/O."""
    flow, event = setup_request
    organization = models.Organization(
        name="Example", identifier="example", tracker_id=flow.trigger_event_source
    )
    db_session.add(organization)
    db_session.flush()
    projects = [
        models.Project(
            name=f"Repository {i}",
            identifier=f"repo-{i}",
            organization_id=organization.id,
        )
        for i in range(3)
    ]
    db_session.add_all(projects)
    db_session.flush()
    now = datetime.now(timezone.utc)
    first = requests.claim_projects(db_session, now=now)
    assert len(first) == 2
    second = requests.claim_projects(db_session, now=now)
    assert len(second) == 1
    assert {x[0] for x in first}.isdisjoint({x[0] for x in second})
    assert requests.claim_projects(db_session, now=now) == []
    requests.advance_page(db_session, project_id=first[0][0], page=2)
    assert (
        dict(requests.claim_projects(db_session, now=now + timedelta(seconds=31)))[
            first[0][0]
        ]
        == 2
    )


@pytest.mark.asyncio
async def test_duplicate_label_can_retry_removal_without_another_request(
    db_session: Session, setup_request: Any
) -> None:
    """A transient label deletion failure does not turn into another review."""
    flow, event = setup_request
    event["payload"]["azure_review_request"] = {
        "kind": "label",
        "signal": "label-1",
        "label": "preloop-review",
    }
    service = FlowTriggerService(db_session)
    with (
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new=AsyncMock(return_value=PR),
        ),
        patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=[flow],
        ),
    ):
        assert await service.process_event(copy.deepcopy(event))
        assert await service.process_event(copy.deepcopy(event))
    row = db_session.query(models.AzureReviewRequest).filter_by(flow_id=flow.id).one()
    assert row.revision == 1


def test_changed_repository_binding_discards_pending_scope(
    db_session: Session, setup_request: Any
) -> None:
    """A new tracker binding cancels queued work instead of retrying forever."""
    from preloop.services.azure_review_requests import _project

    flow, _ = setup_request
    tracker = db_session.get(models.Tracker, flow.trigger_event_source)
    tracker.connection_details = {"organization": "another-org"}
    project = SimpleNamespace(
        is_active=True,
        organization=SimpleNamespace(is_active=True),
        meta_data={"full_name": "fabrikam/Project/repo"},
    )
    with (
        patch(
            "preloop.services.azure_review_requests.crud_project.get_for_tracker_by_identifier",
            return_value=project,
        ),
        patch(
            "preloop.services.azure_review_requests.project_scope_denial",
            return_value=None,
        ),
    ):
        assert _project(db_session, tracker, REPO_ID) is None
