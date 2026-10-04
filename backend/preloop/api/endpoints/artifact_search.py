"""Account-wide artifact search: ``GET /api/v1/artifacts`` (#1086).

Users with ``view_runtime_sessions`` search every session's artifacts in
their account. Items are the session artifact descriptor (#1080) plus the
session title and agent name; ``q`` adds a redacted excerpt with hit offsets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User as UserModel
from preloop.schemas.runtime_session_artifact import ArtifactSearchOut
from preloop.services import artifact_search
from preloop.services.analytics_history import history_cutoff
from preloop.services.artifact_deposit import LIST_LIMIT_DEFAULT, ArtifactDepositError
from preloop.utils.permissions import require_permission

router = APIRouter()


@router.get(
    "/artifacts",
    response_model=ArtifactSearchOut,
    responses={
        401: {"description": "Missing or invalid credentials"},
        403: {"description": "User lacks view_runtime_sessions"},
        422: {"description": "Bad kind, label, id, date, limit or cursor"},
    },
)
@require_permission("view_runtime_sessions")
def search_artifacts(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    q: Optional[str] = Query(
        None, description="Full text over artifact text chunks, plus name."
    ),
    kind: list[str] = Query(default_factory=list, description="Repeatable."),
    label: list[str] = Query(
        default_factory=list,
        description="Repeatable `key:value`; all must match.",
    ),
    agent_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None, max_length=255),
    producer: Optional[str] = Query(None, max_length=64),
    runtime_session_id: Optional[str] = Query(None),
    created_from: Optional[datetime] = Query(
        None, alias="from", description="ISO 8601; `created_at >= from`."
    ),
    created_to: Optional[datetime] = Query(
        None, alias="to", description="ISO 8601; `created_at < to`."
    ),
    held: Optional[bool] = Query(None, description="Legal hold state."),
    availability: Optional[str] = Query(
        None, description="available, evicted or expired."
    ),
    limit: int = Query(LIST_LIMIT_DEFAULT, description="1 to 200."),
    cursor: Optional[str] = Query(None),
) -> ArtifactSearchOut:
    """Search artifacts across sessions, newest first, with facets.

    Ordered by ``created_at`` then id (also with ``q``) so the cursor is
    stable while artifacts arrive. Facets count kinds and ``labels.site``
    over the whole filter, up to 10000 rows (``facets_truncated``).
    """
    try:
        return artifact_search.search(
            db,
            account_id=account.id,
            q=q,
            kinds=kind,
            labels=label,
            agent_id=agent_id,
            tool_name=tool_name,
            producer=producer,
            runtime_session_id=runtime_session_id,
            created_from=created_from,
            created_to=created_to,
            held=held,
            availability=availability,
            limit=limit,
            cursor=cursor,
            session_cutoff=history_cutoff(db, account=account),
        )
    except ArtifactDepositError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
