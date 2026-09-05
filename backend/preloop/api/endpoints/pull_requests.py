"""Live pull request / merge request lists for a project."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple
from uuid import UUID

import gitlab.exceptions
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_tracker_client
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User
from preloop.schemas.pull_request import PullRequestListResponse
from preloop.services.preset_runner import PresetRunnerError, _load_visible_project
from preloop.sync.exceptions import TrackerError
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

_PR_LIST_CACHE: Dict[Tuple[str, str, str, int, int], Tuple[float, dict]] = {}
_PR_LIST_CACHE_LOCK = threading.Lock()
_PR_LIST_TTL_SECONDS = 60.0


def clear_pull_request_list_cache() -> None:
    """Drop the in-process PR list cache (tests)."""
    with _PR_LIST_CACHE_LOCK:
        _PR_LIST_CACHE.clear()


def _cache_key(
    account_id: Any, project_id: Any, state: str, page: int, limit: int
) -> Tuple[str, str, str, int, int]:
    return (str(account_id), str(project_id), state, page, limit)


def _cache_get(key: Tuple[str, str, str, int, int]) -> Optional[dict]:
    with _PR_LIST_CACHE_LOCK:
        entry = _PR_LIST_CACHE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            _PR_LIST_CACHE.pop(key, None)
            return None
        return payload


def _cache_set(key: Tuple[str, str, str, int, int], payload: dict) -> None:
    with _PR_LIST_CACHE_LOCK:
        now = time.monotonic()
        expired = [
            cached
            for cached, (expires_at, _) in list(_PR_LIST_CACHE.items())
            if expires_at <= now
        ]
        for cached in expired:
            _PR_LIST_CACHE.pop(cached, None)
        _PR_LIST_CACHE[key] = (now + _PR_LIST_TTL_SECONDS, payload)


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


@router.get(
    "/projects/{project_id}/pull-requests",
    response_model=PullRequestListResponse,
)
@require_permission("view_trackers")
def list_project_pull_requests(
    project_id: UUID,
    state: Literal["open"] = Query("open"),
    limit: int = Query(20, ge=1, le=50),
    page: int = Query(1, ge=1),
    refresh: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PullRequestListResponse:
    """List open pull requests (GitHub) or merge requests (GitLab) for a project."""
    try:
        project, tracker, organization = _load_visible_project(
            db, project_id=project_id, account_id=current_user.account_id
        )
    except PresetRunnerError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    kind = _tracker_kind(tracker.tracker_type)
    if kind not in ("github", "gitlab"):
        return _unsupported_response(page, limit)

    cache_key = _cache_key(current_user.account_id, project.id, state, page, limit)
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return PullRequestListResponse.model_validate(cached)

    async def _fetch() -> dict:
        client = await get_tracker_client(organization.id, project.id, db, current_user)
        if kind == "gitlab":
            return await client.list_merge_requests(state=state, limit=limit, page=page)
        return await client.list_pull_requests(state=state, limit=limit, page=page)

    try:
        listed = asyncio.run(_fetch())
    except HTTPException:
        raise
    except TrackerError as exc:
        logger.warning("Tracker request failed listing pull requests: %s", exc)
        raise HTTPException(status_code=502, detail="Tracker request failed") from exc
    except (httpx.HTTPError, gitlab.exceptions.GitlabError) as exc:
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
