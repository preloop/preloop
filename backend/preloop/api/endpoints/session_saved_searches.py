"""Saved session searches: save a question once, re-run it in one click.

The routes hang off ``/runtime-sessions/search/saved`` rather than
``/runtime-sessions/saved-searches`` because the account router already owns
``/runtime-sessions/{runtime_session_id}``, and a two segment sibling of it
would be matched as a session id before it ever reached this module. Sitting
under the search path also says what these are: the saved form of the search
one segment up.

The permission is ``view_runtime_sessions``, the same one the search endpoint
takes. Saving a query is not a new kind of access: a saved search can only
ever return what the person running it could have found by typing the query
themselves, and the account bound is applied in SQL on every read here as it
is there.

Sharing is explicit. A saved search is private until its author sets
``visibility`` to ``account``, and only its author may rename, edit, share or
delete it. Everyone else in the account may run a shared one, which is the
point of sharing it.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import crud_session_saved_search
from preloop.models.crud.session_saved_search import SessionSavedSearchNameConflictError
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.session_saved_search import (
    FILTER_SCHEMA_VERSION,
    VISIBILITY_ACCOUNT,
    VISIBILITY_PRIVATE,
    SessionSavedSearch,
)
from preloop.models.models.user import User
from preloop.schemas.session_saved_search import (
    SessionSavedSearchCreate,
    SessionSavedSearchList,
    SessionSavedSearchRead,
    SessionSavedSearchRunRequest,
    SessionSavedSearchRunResponse,
    SessionSavedSearchUpdate,
)
from preloop.services import session_saved_search as saved_search_service
from preloop.services.session_search_fusion import ranking_identity
from preloop.utils.permissions import require_permission

router = APIRouter()

#: Saved searches per page. A person's saved searches are a short list; this
#: is a bound on a pathological one, not a paging story.
MAX_SAVED_SEARCHES_PER_PAGE = 100


def _load_visible(
    db: Session, *, account: Account, user: User, saved_search_id: UUID
) -> SessionSavedSearch:
    """Load a saved search the caller may see, or refuse with a 404.

    A private saved search belonging to somebody else is not found rather
    than forbidden: its existence, and the name its author gave it, are not
    the caller's business.
    """
    saved = crud_session_saved_search.get_visible(
        db,
        account_id=account.id,
        user_id=user.id,
        saved_search_id=saved_search_id,
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found"
        )
    return saved


def _load_owned(
    db: Session, *, account: Account, user: User, saved_search_id: UUID
) -> SessionSavedSearch:
    """Load a saved search the caller wrote, or refuse.

    A shared search the caller did not write is refused with a 403: they can
    see it, so pretending it does not exist would be a worse answer than
    saying it is not theirs to change.
    """
    owned = crud_session_saved_search.get_owned(
        db,
        account_id=account.id,
        user_id=user.id,
        saved_search_id=saved_search_id,
    )
    if owned is not None:
        return owned
    visible = crud_session_saved_search.get_visible(
        db,
        account_id=account.id,
        user_id=user.id,
        saved_search_id=saved_search_id,
    )
    if visible is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the author of a saved search can change it",
        )
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Saved search not found"
    )


def _create(
    db: Session, *, account: Account, user: User, payload: SessionSavedSearchCreate
) -> SessionSavedSearchRead:
    """Store one saved search and return it as its author now sees it."""
    try:
        saved = crud_session_saved_search.create_for_user(
            db,
            account_id=account.id,
            owner_user_id=user.id,
            name=payload.name,
            query=payload.query,
            mode=payload.mode,
            filters=saved_search_service.filters_payload(payload.filters),
            filters_version=FILTER_SCHEMA_VERSION,
            ranking_identity=ranking_identity(),
            max_snippets_per_session=payload.max_snippets_per_session,
            include_snippet_text=payload.include_snippet_text,
            visibility=payload.visibility,
            commit=True,
        )
    except SessionSavedSearchNameConflictError as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(conflict)
        ) from conflict
    return saved_search_service.to_read(saved, caller_user_id=user.id)


def _update(
    db: Session,
    *,
    account: Account,
    user: User,
    saved_search_id: UUID,
    payload: SessionSavedSearchUpdate,
) -> SessionSavedSearchRead:
    """Apply an edit to a saved search the caller wrote."""
    saved = _load_owned(db, account=account, user=user, saved_search_id=saved_search_id)
    # ``None`` means "leave this alone" on every field here, because none of
    # them is nullable: a saved search always has a name, a query and a mode.
    values: Dict[str, Any] = {
        key: value
        for key, value in payload.model_dump(
            exclude_unset=True, exclude={"filters"}
        ).items()
        if value is not None
    }
    if payload.filters is not None:
        values["filters"] = saved_search_service.filters_payload(payload.filters)
        values["filters_version"] = FILTER_SCHEMA_VERSION
    if not values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No changes were requested",
        )
    visibility: Optional[str] = values.get("visibility")
    if visibility is not None and visibility != saved.visibility:
        # The share timestamp records the last time it became shared, and is
        # cleared when it stops being shared, so "shared since" is never a
        # date from a share that was already undone.
        values["shared_at"] = (
            saved_search_service.utc_now() if visibility == VISIBILITY_ACCOUNT else None
        )
    if _changes_the_question(values):
        # The ordering this search now produces is the one its constants
        # produce today, so the identity it is compared against moves with the
        # edit. Nothing is pinned either way.
        values["ranking_identity"] = ranking_identity()
    try:
        updated = crud_session_saved_search.update_owned(
            db, saved=saved, values=values, commit=True
        )
    except SessionSavedSearchNameConflictError as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(conflict)
        ) from conflict
    return saved_search_service.to_read(updated, caller_user_id=user.id)


def _changes_the_question(values: Dict[str, Any]) -> bool:
    """Whether an edit changed what is being asked, not just how it is labelled."""
    return bool({"query", "mode", "filters"} & set(values))


def _delete(
    db: Session, *, account: Account, user: User, saved_search_id: UUID
) -> None:
    """Delete a saved search the caller wrote. Sessions are untouched."""
    saved = _load_owned(db, account=account, user=user, saved_search_id=saved_search_id)
    crud_session_saved_search.delete_owned(db, saved=saved, commit=True)


def _run(
    db: Session,
    *,
    account: Account,
    user: User,
    saved_search_id: UUID,
    paging: SessionSavedSearchRunRequest,
) -> SessionSavedSearchRunResponse:
    """Re-run a saved search and commit its run counters."""
    saved = _load_visible(
        db, account=account, user=user, saved_search_id=saved_search_id
    )
    response = saved_search_service.run_saved_search(
        db,
        account_id=account.id,
        caller_user_id=user.id,
        saved=saved,
        paging=paging,
    )
    db.commit()
    return response


@router.post(
    "/runtime-sessions/search/saved",
    response_model=SessionSavedSearchRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save a session search under a name",
)
@require_permission("view_runtime_sessions")
async def create_saved_session_search(
    payload: SessionSavedSearchCreate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSavedSearchRead:
    """Save a query, mode, filters and snippet preferences under a name.

    The filters are validated here, so a saved search that cannot run cannot
    be stored. Visibility defaults to private; sharing is a separate edit.
    """
    return await run_db_off_loop(
        lambda: _create(db, account=account, user=current_user, payload=payload)
    )


@router.get(
    "/runtime-sessions/search/saved",
    response_model=SessionSavedSearchList,
    summary="List saved session searches",
)
@require_permission("view_runtime_sessions")
async def list_saved_session_searches(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=MAX_SAVED_SEARCHES_PER_PAGE),
    offset: int = Query(0, ge=0),
) -> SessionSavedSearchList:
    """Return the caller's saved searches plus the ones shared with the account.

    Most recently run first, so the searches somebody actually repeats are at
    the top of the list.
    """

    def _list() -> SessionSavedSearchList:
        rows, total = crud_session_saved_search.list_visible(
            db,
            account_id=account.id,
            user_id=current_user.id,
            limit=limit,
            offset=offset,
        )
        return SessionSavedSearchList(
            items=[
                saved_search_service.to_read(row, caller_user_id=current_user.id)
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    return await run_db_off_loop(_list)


@router.get(
    "/runtime-sessions/search/saved/{saved_search_id}",
    response_model=SessionSavedSearchRead,
    summary="Read one saved session search",
)
@require_permission("view_runtime_sessions")
async def get_saved_session_search(
    saved_search_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSavedSearchRead:
    """Return one saved search the caller may see."""
    return await run_db_off_loop(
        lambda: saved_search_service.to_read(
            _load_visible(
                db,
                account=account,
                user=current_user,
                saved_search_id=saved_search_id,
            ),
            caller_user_id=current_user.id,
        )
    )


@router.patch(
    "/runtime-sessions/search/saved/{saved_search_id}",
    response_model=SessionSavedSearchRead,
    summary="Rename, edit or share a saved session search",
)
@require_permission("view_runtime_sessions")
async def update_saved_session_search(
    saved_search_id: UUID,
    payload: SessionSavedSearchUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSavedSearchRead:
    """Change a saved search the caller wrote.

    Renaming, changing the question and sharing are one route because they are
    one thing: this saved question is not what it was. Only the author may do
    any of them.
    """
    return await run_db_off_loop(
        lambda: _update(
            db,
            account=account,
            user=current_user,
            saved_search_id=saved_search_id,
            payload=payload,
        )
    )


@router.delete(
    "/runtime-sessions/search/saved/{saved_search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved session search",
)
@require_permission("view_runtime_sessions")
async def delete_saved_session_search(
    saved_search_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Delete a saved search the caller wrote. The sessions are untouched."""
    await run_db_off_loop(
        lambda: _delete(
            db,
            account=account,
            user=current_user,
            saved_search_id=saved_search_id,
        )
    )
    return None


@router.post(
    "/runtime-sessions/search/saved/{saved_search_id}/run",
    response_model=SessionSavedSearchRunResponse,
    summary="Run a saved session search",
)
@require_permission("view_runtime_sessions")
async def run_saved_session_search(
    saved_search_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    paging: Optional[SessionSavedSearchRunRequest] = None,
) -> SessionSavedSearchRunResponse:
    """Run a saved search against the corpus as it is now.

    The answer is the search endpoint's answer, degraded block and all: a
    saved semantic search on a deployment whose semantic half cannot run comes
    back as keyword results with a reason, not as an error. Alongside it the
    response names every saved filter that no longer resolves, and says
    whether the ranking constants have moved since the search was saved.

    The body is optional: it carries paging only, because the saved search
    owns everything else about the question.
    """
    # An absent body is an empty body: the defaults on the paging model are
    # the first page.
    requested = paging or SessionSavedSearchRunRequest.model_validate({})
    return await run_db_off_loop(
        lambda: _run(
            db,
            account=account,
            user=current_user,
            saved_search_id=saved_search_id,
            paging=requested,
        )
    )


__all__ = ["router", "VISIBILITY_ACCOUNT", "VISIBILITY_PRIVATE"]
