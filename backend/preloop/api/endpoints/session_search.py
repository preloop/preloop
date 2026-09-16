"""Ranked keyword, semantic and hybrid search across session content.

``POST``, not ``GET``, and deliberately so. The two search surfaces that
already exist (``GET /account/gateway-usage/search`` and ``GET /search``) put
the query text in the request path, where every proxy, load balancer and
access log on the way keeps a copy. The text an operator types here is what
they are hunting for in their own agent transcripts, which is the last kind of
string that should be sitting in a log file. A body costs a caller nothing and
keeps it out.

The permission is ``view_runtime_sessions``: this reads session content, so it
takes the permission that already guards reading sessions rather than the cost
permission the older account wide search happens to sit behind.

The semantic half embeds the query, which spends money under the account's own
opt in and daily cap. It is still one route and one body: a mode the account
cannot serve is answered with what it can serve plus a marker saying what is
missing, so a client never has to ask which modes this deployment supports.

The similar sessions route beside it is a ``GET``, and for the same reason the
search is a ``POST``: it carries no free text at all. Its only input is a
session id the caller already has, so nothing an operator typed can end up in
an access log, and a list that depends only on a path is one a browser may
cache and revisit.
"""

from __future__ import annotations

from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import crud_runtime_session
from preloop.models.crud.session_search_document import (
    MAX_SIMILAR_MATCHES_PER_SESSION,
    MAX_SIMILAR_SESSIONS,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.session_search import (
    SessionSearchRequest,
    SessionSearchResponse,
)
from preloop.schemas.session_similarity import (
    DEFAULT_SIMILAR_MATCHES_PER_SESSION,
    DEFAULT_SIMILAR_SESSIONS,
    MAX_SIMILAR_WINDOW_DAYS,
    SimilarSessionsResponse,
)
from preloop.services import session_search, session_similarity
from preloop.utils.permissions import require_permission

router = APIRouter()


@router.post(
    "/runtime-sessions/search",
    response_model=SessionSearchResponse,
    summary="Search session content by relevance",
)
@require_permission("view_runtime_sessions")
async def search_runtime_sessions(
    payload: SessionSearchRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSearchResponse:
    """Rank the caller's runtime sessions by relevance to a query.

    Returns sessions ordered by relevance, each with snippets that name the
    turn the match came from and a reason saying whether the words, the
    vector or both put it there. ``semantic`` and ``hybrid`` embed the query
    and search the corpus vectors; when that half cannot run, the answer is
    keyword results with a degraded marker naming the cause, never an error.
    """
    return await run_db_off_loop(
        lambda: session_search.search_sessions(
            db,
            account_id=account.id,
            request=payload,
        )
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/similar",
    response_model=SimilarSessionsResponse,
    summary="List sessions similar to this one",
)
@require_permission("view_runtime_sessions")
async def similar_runtime_sessions(
    runtime_session_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    limit: int = Query(
        DEFAULT_SIMILAR_SESSIONS,
        ge=1,
        le=MAX_SIMILAR_SESSIONS,
        description="Similar sessions to return.",
    ),
    max_matches_per_session: int = Query(
        DEFAULT_SIMILAR_MATCHES_PER_SESSION,
        ge=0,
        le=MAX_SIMILAR_MATCHES_PER_SESSION,
        description=(
            "Matching passages returned per session. Zero returns the ranking "
            "with no captured content at all."
        ),
    ),
    window_days: Optional[int] = Query(
        None,
        ge=1,
        le=MAX_SIMILAR_WINDOW_DAYS,
        description=(
            "Only compare content this recent. Unset compares the whole "
            "corpus, which is the default: the session worth finding is often "
            "an old one."
        ),
    ),
    include_match_text: bool = Query(
        True,
        description=(
            "When false no chunk text is read, so no captured content leaves "
            "the database; matches still name the turn they came from."
        ),
    ),
) -> SimilarSessionsResponse:
    """Rank other sessions of this account by similarity to this one.

    The comparison reads vectors the indexing worker already wrote: nothing is
    embedded, no provider is called and no spend is recorded, so an account at
    its daily embedding cap still gets this list. A session with no vectors of
    its own, or an account whose corpus holds no other session embedded with
    the same model, is answered with an empty list and a degraded reason
    naming which of those it was, never with an error.

    Raises:
        HTTPException: 404 when the session does not belong to this account,
            so a probe cannot tell an existing session of another account
            apart from one that never existed.
    """
    session = await run_db_off_loop(
        lambda: crud_runtime_session.get_account_session(
            db,
            account_id=str(account.id),
            runtime_session_id=str(runtime_session_id),
        )
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime session not found",
        )
    return await run_db_off_loop(
        lambda: session_similarity.similar_sessions(
            db,
            account_id=account.id,
            runtime_session_id=runtime_session_id,
            limit=limit,
            max_matches_per_session=max_matches_per_session,
            window_days=window_days,
            include_match_text=include_match_text,
        )
    )
