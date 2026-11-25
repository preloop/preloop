"""Bounded label discovery and durable, coalesced explicit Azure PR reviews."""

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.common import azure_devops_repo_full_name, project_scope_denial
from preloop.models import models
from preloop.models.crud import (
    crud_azure_review_request as requests,
    crud_flow,
    crud_project,
    crud_tracker,
)
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.webhook_delivery_dedupe import (
    delivery_key_for_event,
    find_execution_for_delivery,
)
from preloop.sync.exceptions import TrackerError
from preloop.sync.trackers.azure_devops import AzureDevOpsTracker
from preloop.utils.azure_devops import normalize_pull_request

logger = logging.getLogger(__name__)


def request_enabled(flow: models.Flow, request: dict[str, Any]) -> bool:
    """Match explicit requests to the flow's configured label or command."""
    config = flow.webhook_config or {}
    if request.get("kind") == "comment":
        return config.get("azure_review_comment_enabled", True) is True
    label = config.get("azure_review_label", "preloop-review")
    return (
        request.get("kind") == "label" and bool(label) and request.get("label") == label
    )


def _project(db: Session, tracker: models.Tracker, repo_id: str) -> Any:
    """Recheck imported scope before any provider access or dispatch."""
    project = crud_project.get_for_tracker_by_identifier(
        db, tracker_id=tracker.id, identifier=repo_id
    )
    if (
        not project
        or not tracker.is_active
        or tracker.is_deleted
        or not project.is_active
        or not project.organization.is_active
        or project_scope_denial(db, tracker, project.organization, project)
    ):
        return None
    try:
        azure_devops_repo_full_name(tracker.connection_details or {}, project)
    except HTTPException:
        return None
    return project


async def poll_labels(db: Session, service: FlowTriggerService, now: datetime) -> None:
    """Rotate two repositories per tick, reading at most five open PRs each."""
    for project_id, page in requests.claim_projects(db, now=now):
        try:
            project = crud_project.get(db, id=project_id)
            tracker = project.organization.tracker
            if not _project(db, tracker, project.identifier):
                continue
            full_name = azure_devops_repo_full_name(
                tracker.connection_details or {}, project
            )
            client = AzureDevOpsTracker(
                str(tracker.id),
                tracker.resolved_api_key or "",
                dict(tracker.connection_details or {}),
            )
            flows = crud_flow.get_by_trigger(
                db,
                event_source=str(tracker.id),
                event_type="pull_request_updated",
                project_id=str(project.id),
                account_id=tracker.account_id,
            )
            labels_wanted = {
                (f.webhook_config or {}).get("azure_review_label", "preloop-review")
                for f in flows
                if f.is_enabled
            }
            if not any(labels_wanted):
                continue
            result = await client.list_pull_requests(
                limit=5, page=page, repo_full_name=full_name
            )
            for pr in result["items"]:
                number = pr["number"]
                async with requests.serialized(
                    db, tracker.id, project.identifier, number
                ):
                    labels = await client.list_pull_request_labels(number, full_name)
                    present = {str(label["id"]) for label in labels}
                    requests.observe_labels(
                        db,
                        tracker_id=tracker.id,
                        repository_id=project.identifier,
                        number=number,
                        present=present,
                    )
                    for label in labels:
                        if label.get("name") not in labels_wanted:
                            continue
                        current = await client.get_pull_request(number, full_name)
                        event = {
                            "source": "azure_devops",
                            "type": "pull_request_updated",
                            "tracker_id": str(tracker.id),
                            "account_id": str(tracker.account_id),
                            "payload": {
                                **normalize_pull_request(current, client.organization),
                                "project_id": str(project.id),
                                "azure_review_request": {
                                    "kind": "label",
                                    "signal": str(label["id"]),
                                    "label": label["name"],
                                },
                            },
                        }
                        if await service._process_event(event):
                            try:
                                await client.remove_pull_request_label(
                                    number, full_name, str(label["id"])
                                )
                            except TrackerError:
                                logger.info(
                                    "Azure request label could not be removed; remove and re-add it manually (tracker %s)",
                                    tracker.id,
                                )
                            else:
                                present.discard(str(label["id"]))
                                requests.observe_labels(
                                    db,
                                    tracker_id=tracker.id,
                                    repository_id=project.identifier,
                                    number=number,
                                    present=present,
                                )
            requests.advance_page(
                db, project_id=project_id, page=page + 1 if result["has_more"] else 1
            )
        except Exception:
            db.rollback()
            logger.error("Azure review label polling failed for project %s", project_id)


async def flush_requests(
    db: Session, service: FlowTriggerService, now: datetime
) -> int:
    """Dispatch one pending follow-up only after this flow's active run ends."""
    dispatched = 0
    for row_id in requests.claim_due(db, now=now):
        row = requests.get(db, row_id)
        if row is None:
            continue
        try:
            async with requests.serialized(
                db, row.tracker_id, row.repository_id, row.number
            ):
                row = requests.get(db, row_id)
                if row is None or row.due_at is None or row.due_at > now:
                    continue
                revision = row.revision
                flow = crud_flow.get(db, id=row.flow_id)
                tracker = crud_tracker.get(db, id=row.tracker_id)
                event = copy.deepcopy(row.event)
                request = event["payload"]["azure_review_request"]
                done = not (
                    flow
                    and tracker
                    and flow.is_enabled
                    and flow.trigger_event_source in (None, str(tracker.id))
                    and "pull_request_updated" in (flow.trigger_event_types or [])
                    and service._matches_trigger_config(flow, event)
                    and (
                        not flow.trigger_project_ids
                        or event["payload"].get("project_id")
                        in flow.trigger_project_ids
                    )
                    and str(flow.account_id) == str(tracker.account_id)
                    and request_enabled(flow, request)
                    and _project(db, tracker, row.repository_id)
                )
                if not done:
                    reason = await service._azure_pull_request_skip_reason(event)
                    done = reason is not None
                if not done and not service.find_active_execution_for_event_object(
                    flow, event, tracker.account_id
                ):
                    event["_azure_review_flow_id"] = str(flow.id)
                    event["delivery_id"] = f"azure-review:{row.id}:{revision}"
                    await service._process_event(event)
                    done = (
                        find_execution_for_delivery(
                            db,
                            flow_id=flow.id,
                            delivery_key=delivery_key_for_event(event),
                        )
                        is not None
                    )
                    dispatched += int(done)
                requests.finish(
                    db, row_id=row_id, revision=revision, done=done, now=now
                )
        except Exception:
            db.rollback()
            logger.error("Azure explicit review retry deferred for request %s", row_id)
    return dispatched


async def run_review_request_tick(db: Session) -> int:
    """Discover labels and flush due requests without keeping a runner idle."""
    service = FlowTriggerService(db)
    now = datetime.now(timezone.utc)
    await poll_labels(db, service, now)
    dispatched = await flush_requests(db, service, now)
    requests.purge_stale(db, now=now)
    return dispatched
