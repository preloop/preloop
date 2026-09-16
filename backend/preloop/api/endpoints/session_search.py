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

Every call writes one audit row (#688). A content search is a read across
every captured prompt, response and tool call the account holds, which is the
broadest read the product offers, and the record of who ran it is what makes
the feature answerable to whoever is asked whether anyone grepped the
transcripts. The row never carries snippet text, and carries the query text
only when the account opted in; see
``preloop.services.session_search_audit``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.session_search import (
    SessionSearchRequest,
    SessionSearchResponse,
)
from preloop.services import session_search_audit
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
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSearchResponse:
    """Rank the caller's runtime sessions by relevance to a query.

    Returns sessions ordered by relevance, each with snippets that name the
    turn the match came from and a reason saying whether the words, the
    vector or both put it there. ``semantic`` and ``hybrid`` embed the query
    and search the corpus vectors; when that half cannot run, the answer is
    keyword results with a degraded marker naming the cause, never an error.

    The call is audited either way. A refusal raised inside the handler is
    recorded as denied and a broken search as failed, so a trail with no row
    for a search means no search ran, not that one was hidden.
    """
    actor = session_search_audit.user_actor(
        current_user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return await run_db_off_loop(
        lambda: session_search_audit.audited_search(
            db,
            account_id=account.id,
            request=payload,
            actor=actor,
        )
    )
