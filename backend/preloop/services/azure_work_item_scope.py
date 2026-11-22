"""Which Preloop project an Azure Boards work item attaches to.

Preloop projects of an ``azure_devops`` tracker are git repositories; a
work item belongs to an Azure DevOps project. Each Azure DevOps project's
work items attach to the Preloop project of one of its repositories, chosen
by :func:`preloop.utils.azure_devops_work_items.work_item_home`.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_project
from preloop.utils.azure_devops_work_items import work_item_home

# More repositories than this per tracker is unusual; the lookup is bounded.
_MAX_PROJECTS = 1000


def resolve_work_item_project(
    db: Session, tracker: models.Tracker, board_project: str
) -> Optional[models.Project]:
    """The Preloop project that holds ``board_project``'s work items.

    Args:
        db: Database session.
        tracker: The ``azure_devops`` tracker.
        board_project: Azure DevOps project name of the work item.

    Returns:
        The project, or None when the Azure DevOps project has several
        repositories and no rule selects one (or none is imported).
    """
    candidates = [
        project
        for project in crud_project.get_for_tracker(
            db, tracker_id=str(tracker.id), limit=_MAX_PROJECTS
        )
        if str((project.meta_data or {}).get("project") or "").casefold()
        == board_project.casefold()
        and getattr(project, "is_active", True) is not False
    ]
    by_repo = {
        str((p.meta_data or {}).get("repository") or p.name).casefold(): p
        for p in candidates
    }
    details: dict[str, Any] = dict(tracker.connection_details or {})
    home = work_item_home(
        board_project,
        [str((p.meta_data or {}).get("repository") or p.name) for p in candidates],
        bound_repository=details.get("repository") or None,
        overrides=details.get("work_item_repositories"),
    )
    return by_repo.get(home.casefold()) if home else None
