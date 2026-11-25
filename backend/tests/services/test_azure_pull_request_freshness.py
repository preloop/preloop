"""Delayed Azure deliveries reconcile with the current PR before starting work."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.exceptions import TrackerConnectionError


def event() -> dict[str, Any]:
    """Return an active delivery for a synthetic repository."""
    return {
        "source": "azure_devops",
        "type": "pull_request_updated",
        "tracker_id": "11111111-1111-4111-8111-111111111111",
        "account_id": "22222222-2222-4222-8222-222222222222",
        "payload": {
            "repository": {
                "id": "33333333-3333-4333-8333-333333333333",
                "full_name": "example-org/Project/repo",
            },
            "pull_request": {"number": 7, "head": {"sha": "a" * 40}},
            "azure_devops": {
                "project": "Project",
                "repository_id": "33333333-3333-4333-8333-333333333333",
            },
        },
    }


@pytest.fixture(autouse=True)
def imported_repository() -> Any:
    """Bind freshness reads to the imported repository, never delivery names."""
    project = SimpleNamespace(
        is_active=True,
        organization=SimpleNamespace(is_active=True),
        meta_data={"full_name": "example-org/Project/repo"},
    )
    with (
        patch(
            "preloop.models.crud.crud_project.get_for_tracker_by_identifier",
            return_value=project,
        ),
        patch("preloop.api.common.project_scope_denial", return_value=None),
    ):
        yield project


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,sha,stop",
    [
        ("abandoned", "a" * 40, "pull_request_closed"),
        ("completed", "a" * 40, "pull_request_merged"),
        ("active", "b" * 40, None),
        ("active", None, None),
    ],
)
async def test_delayed_event_cannot_start_a_stale_review(
    status: str, sha: str | None, stop: str | None
) -> None:
    service = FlowTriggerService(MagicMock())
    with (
        patch("preloop.models.crud.crud_tracker.get") as get_tracker,
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new_callable=AsyncMock,
        ) as read,
        patch.object(
            service, "stop_executions_for_ended_pull_request", new_callable=AsyncMock
        ) as stop_runs,
        patch("preloop.services.flow_trigger_service.crud_flow") as flows,
        patch("preloop.services.flow_feedback.ingest_feedback"),
        patch("preloop.services.pull_request_refresh.accelerate"),
    ):
        tracker = get_tracker.return_value
        tracker.connection_details = {"organization": "example-org"}
        tracker.resolved_api_key = "test-pat"
        tracker.is_active = True
        tracker.is_deleted = False
        read.return_value = {
            "status": status,
            "repository": {"id": event()["payload"]["repository"]["id"]},
            "lastMergeSourceCommit": {"commitId": sha},
        }
        await service.process_event(event())
        read.assert_awaited_once_with(7, "example-org/Project/repo")
        flows.get_by_trigger.assert_not_called()
        if stop:
            assert stop_runs.await_args.args[0]["type"] == stop
        else:
            stop_runs.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_state_read_failure_retries_instead_of_starting_review() -> None:
    service = FlowTriggerService(MagicMock())
    with (
        patch("preloop.models.crud.crud_tracker.get") as get_tracker,
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new_callable=AsyncMock,
        ) as read,
    ):
        get_tracker.return_value.connection_details = {"organization": "example-org"}
        get_tracker.return_value.resolved_api_key = "test-pat"
        get_tracker.return_value.is_active = True
        get_tracker.return_value.is_deleted = False
        read.side_effect = TrackerConnectionError("unavailable")
        with pytest.raises(TrackerConnectionError):
            await service.process_event(event())


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["payload_name", "response_id"])
async def test_repository_identity_cannot_redirect_freshness_read(
    mismatch: str,
) -> None:
    """An imported GUID cannot authorize reads from another repository."""
    service = FlowTriggerService(MagicMock())
    delivery = event()
    if mismatch == "payload_name":
        delivery["payload"]["repository"]["full_name"] = "example-org/Other/secret"
    with (
        patch("preloop.models.crud.crud_tracker.get") as get_tracker,
        patch(
            "preloop.sync.trackers.azure_devops.AzureDevOpsTracker.get_pull_request",
            new_callable=AsyncMock,
        ) as read,
    ):
        tracker = get_tracker.return_value
        tracker.connection_details = {"organization": "example-org"}
        tracker.is_active = True
        tracker.is_deleted = False
        read.return_value = {
            "status": "active",
            "repository": {"id": "other-id"},
            "lastMergeSourceCommit": {"commitId": "a" * 40},
        }
        assert (
            await service._azure_pull_request_skip_reason(delivery)
            == "repository_identity_mismatch"
        )
        if mismatch == "payload_name":
            read.assert_not_awaited()
        else:
            read.assert_awaited_once_with(7, "example-org/Project/repo")
