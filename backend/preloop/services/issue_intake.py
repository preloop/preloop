"""Turn a tracker issue payload into stored ``issue`` values.

Shared by the webhook endpoint and the triage controller so both write the
same row shape through ``crud_issue.upsert`` (#1194, #1195).
"""

from __future__ import annotations

import copy
from typing import Any

from preloop.models import models
from preloop.sync.scanner.core import TrackerClient


class IssuePayloadError(ValueError):
    """The payload lacks the data needed to identify the issue."""


def issue_values_from_payload(
    tracker: models.Tracker,
    project: models.Project,
    issue_data: dict[str, Any],
    *,
    tracker_type: str | None = None,
) -> dict[str, Any]:
    """Build ``issue`` column values from a provider issue payload.

    Args:
        tracker: Tracker that delivered the payload.
        project: Project the issue belongs to.
        issue_data: Provider issue object (GitHub ``issue``, GitLab
            ``object_attributes``, Jira ``issue``). Not mutated.
        tracker_type: Provider type; defaults to ``tracker.tracker_type``.

    Returns:
        Values accepted by ``crud_issue.upsert``.

    Raises:
        IssuePayloadError: When the issue key cannot be constructed.
    """
    issue_data = copy.deepcopy(issue_data)
    if tracker_type is None:
        tracker_type = str(getattr(tracker.tracker_type, "value", tracker.tracker_type))
    tracker_type = tracker_type.lower()
    if "key" not in issue_data:
        if tracker_type == "gitlab":
            number = issue_data.get("iid")
            if not (project.slug and number):
                raise IssuePayloadError("Missing data to construct GitLab issue key.")
            issue_data["key"] = f"{project.slug}#{number}"
        elif tracker_type == "github":
            number = issue_data.get("number")
            if not (project.slug and number):
                raise IssuePayloadError("Missing data to construct GitHub issue key.")
            issue_data["key"] = f"{project.slug}#{number}"

    client = TrackerClient(tracker, initialize_client=False).client
    if hasattr(client, "transform_issue_webhook"):
        values = client.transform_issue_webhook(issue_data, project)
    else:
        values = client.transform_issue(issue_data, project)

    # Fields that are not issue columns: dependencies live in meta_data.
    dependencies = values.pop("dependencies", [])
    values.pop("comments", None)
    if dependencies:
        values["meta_data"] = {
            **(values.get("meta_data") or {}),
            "dependencies": dependencies,
        }
    return values
