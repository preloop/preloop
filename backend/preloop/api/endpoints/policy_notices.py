"""Policy notice summary for the Attention page (#959).

Model I/O rules with the ``notify`` action record a hit per match. This
router returns those hits grouped by rule so the console can show one
Attention card per rule. Dismissal goes through the existing Attention
dismissal API; the card's fingerprint includes the latest hit id, so a new
hit brings a dismissed card back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.services.policy_notices import summarize_policy_notices
from preloop.utils.permissions import require_permission

router = APIRouter()


class PolicyNoticeRuleSummaryResponse(BaseModel):
    """Hits of one notify rule inside the window."""

    rule_id: str = Field(..., description="Model I/O rule id")
    rule_description: Optional[str] = Field(None, description="Rule description")
    target: str = Field(..., description="model.request or model.response")
    count: int = Field(..., description="Hits in the window")
    last_hit_id: UUID = Field(..., description="Newest hit id (card fingerprint)")
    last_hit_at: datetime = Field(..., description="Newest hit time (UTC)")
    last_excerpt: Optional[str] = Field(
        None, description="Newest redacted excerpt (at most 280 characters)"
    )
    last_user_id: Optional[UUID] = Field(None, description="Newest hit's user")
    last_username: Optional[str] = Field(None, description="Newest hit's username")


class PolicyNoticeSummaryResponse(BaseModel):
    """Notify rule hits grouped by rule, newest first."""

    days: int = Field(..., description="Window length in days")
    rules: List[PolicyNoticeRuleSummaryResponse]


@router.get(
    "/policies/notices/summary",
    response_model=PolicyNoticeSummaryResponse,
    summary="Summarize notify rule hits by rule",
)
@require_permission("view_policies")
def get_policy_notice_summary(
    days: int = Query(7, ge=1, le=90, description="Window length in days"),
    account: Account = Depends(get_account_for_user),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PolicyNoticeSummaryResponse:
    """Return notify rule hits of the last ``days`` days, one row per rule."""
    rows = summarize_policy_notices(
        db,
        account.id,
        now=datetime.now(timezone.utc),
        window=timedelta(days=days),
    )
    return PolicyNoticeSummaryResponse(
        days=days,
        rules=[
            PolicyNoticeRuleSummaryResponse(
                rule_id=row.rule_id,
                rule_description=row.rule_description,
                target=row.target,
                count=row.count,
                last_hit_id=row.last_hit_id,
                last_hit_at=row.last_hit_at,
                last_excerpt=row.last_excerpt,
                last_user_id=row.last_user_id,
                last_username=row.last_username,
            )
            for row in rows
        ],
    )
