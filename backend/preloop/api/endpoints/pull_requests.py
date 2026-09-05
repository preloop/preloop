"""Live pull request / merge request lists for a project."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_tracker_client
from preloop.models.crud import crud_organization, crud_project, crud_tracker
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User
from preloop.schemas.pull_request import PullRequestListResponse
from preloop.sync.exceptions import TrackerError
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

_PR_LIST_CACHE: Dict[Tuple[str, str, str, int, int], Tuple[float, dict]] = {}
_PR_LIST_TTL_SECONDS = 60.0


def clear_pull_request_list_cache() -> None:
    """Drop the in-process PR list cache (tests)."""
    _PR_LIST_CACHE.clear()


def _cache_key(
    account_id: Any, project_id: Any, state: str, page: int, limit: int
) -> Tuple[str, str, str, int, int]:
    return (str(account_id), str(project_id), state, page, limit)


def _cache_get(key: Tuple[str, str, str, int, int]) -> Optional[dict]:
    entry = _PR_LIST_CACHE.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if time.monotonic() >= expires_at:
        _PR_LIST_CACHE.pop(key, None)
        return None
    return payload


def _cache_set(key: Tuple[str, str, str, int, int], payload: dict) -> None:
    _PR_LIST_CACHE[key] = (time.monotonic() + _PR_LIST_TTL_SECONDS, payload)


def _unsupported_response(page: int, limit: int) -> PullRequestListResponse:
    return PullRequestListResponse(
        items=[],
        page=page,
        limit=limit,
        has_more=False,
        supported=False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _tracker_kind(tracker_type: str) -> str:
    lowered = (tracker_type or "").lower()
    if "jira" in lowered:
        return "jira"
    if "gitlab" in lowered:
        return "gitlab"
    if "github" in lowered:
        return "github"
    return "other"


def _load_visible_project_and_tracker(
    db: Session, project_id: UUID, account_id: UUID
) -> Tuple[Any, Any, Any]:
    """Return ``(project, organization, tracker)`` if the project is visible."""
    project = crud_project.get(db, id=str(project_id), account_id=str(account_id))
    if project is None:
        project = crud_project.get(db, id=str(project_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    user_trackers = crud_tracker.get_for_account(db, account_id=account_id)
    tracker_ids = {str(item.id) for item in user_trackers}
    organization = crud_organization.get(db, id=project.organization_id)
    if not organization or str(organization.tracker_id) not in tracker_ids:
        raise HTTPException(status_code=404, detail="Project not found")

    tracker = next(
        (
            item
            for item in user_trackers
            if str(item.id) == str(organization.tracker_id)
        ),
        None,
    )
    if tracker is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project, organization, tracker


@router.get(
    "/projects/{project_id}/pull-requests",
    response_model=PullRequestListResponse,
)
@require_permission("view_trackers")
async def list_project_pull_requests(
    project_id: UUID,
    state: Literal["open"] = Query("open"),
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PullRequestListResponse:
    """List open pull requests (GitHub) or merge requests (GitLab) for a project."""
    project, organization, tracker = _load_visible_project_and_tracker(
        db, project_id, current_user.account_id
    )
    kind = _tracker_kind(tracker.tracker_type)
    if kind not in ("github", "gitlab"):
        return _unsupported_response(page, limit)

    cache_key = _cache_key(current_user.account_id, project.id, state, page, limit)
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return PullRequestListResponse.model_validate(cached)

    try:
        client = await get_tracker_client(organization.id, project.id, db, current_user)
        if kind == "gitlab":
            listed = await client.list_merge_requests(
                state=state, limit=limit, page=page
            )
        else:
            listed = await client.list_pull_requests(
                state=state, limit=limit, page=page
            )
    except HTTPException:
        raise
    except TrackerError as exc:
        logger.warning("Tracker request failed listing pull requests: %s", exc)
        raise HTTPException(status_code=502, detail="Tracker request failed") from exc
    except Exception as exc:
        logger.exception("Unexpected tracker error listing pull requests")
        raise HTTPException(status_code=502, detail="Tracker request failed") from exc

    payload = {
        "items": listed.get("items") or [],
        "page": page,
        "limit": limit,
        "has_more": bool(listed.get("has_more")),
        "supported": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _cache_set(cache_key, payload)
    return PullRequestListResponse.model_validate(payload)
