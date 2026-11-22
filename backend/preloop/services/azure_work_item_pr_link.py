"""Link a pull request a flow opened to the Azure Boards work item behind it.

When a run triggered by a work item event records an opened Azure Repos pull
request, Preloop adds the pull request to the work item's Development
section (an ``ArtifactLink`` relation with the ``vstfs:///Git/PullRequestId``
URI, https://learn.microsoft.com/azure/devops/boards/queries/
link-type-reference#external-link-type). It costs one read of the pull
request (for the project and repository GUIDs) and one JSON Patch.

Runs that review a pull request are triggered by the pull request, not by a
work item, so they have no work item to link and are left alone.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from preloop.utils.azure_devops import AzureDevOpsConfigError, is_guid, parse_azure_url

logger = logging.getLogger(__name__)


def azure_work_item_key(trigger: Optional[Dict[str, Any]]) -> Optional[str]:
    """Key of the work item a trigger snapshot refers to, if any.

    Args:
        trigger: Execution trigger snapshot (event envelope).

    Returns:
        ``org/project#id``, or None when the trigger is not a work item
        event of an ``azure_devops`` tracker.
    """
    if not isinstance(trigger, dict):
        return None
    if str(trigger.get("source") or "").lower() != "azure_devops":
        return None
    payload = trigger.get("payload")
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issue, dict) or not issue.get("id"):
        return None
    return str(issue.get("key") or issue["id"])


async def link_pull_request_to_work_item(
    *, client: Any, issue_key: str, pr_url: str, execution_id: str
) -> bool:
    """Add the work item -> pull request link. Never raises.

    Args:
        client: ``AzureDevOpsTracker`` (or a ``TrackerClient`` wrapping one).
        issue_key: Work item key or id.
        pr_url: Web URL of the opened pull request.
        execution_id: Execution id, for logs.

    Returns:
        True when the link was added or already present.
    """
    tracker = getattr(client, "client", client)
    if not callable(getattr(tracker, "link_pull_request", None)):
        return False
    try:
        parsed = parse_azure_url(pr_url)
    except AzureDevOpsConfigError:
        parsed = None
    if not parsed or parsed[3] is None:
        return False
    organization, project, repository, pr_id = parsed
    try:
        pr = await tracker.get_pull_request(
            pr_id, f"{organization}/{project}/{repository}"
        )
        repo = pr.get("repository") or {}
        repo_id = repo.get("id")
        project_id = (repo.get("project") or {}).get("id")
        if not (is_guid(repo_id) and is_guid(project_id)):
            return False
        await tracker.link_pull_request(
            issue_key,
            project_id=str(project_id),
            repository_id=str(repo_id),
            pr_id=int(pr_id),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - best effort, status is persisted
        logger.warning(
            "Could not link the pull request to Azure work item %s (execution %s): %s",
            issue_key,
            execution_id,
            type(exc).__name__,
        )
        return False
