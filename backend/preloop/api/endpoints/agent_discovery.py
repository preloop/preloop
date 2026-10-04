"""Opt-in agent discovery reporting.

``preloop agents discover --report`` (or ``PRELOOP_DISCOVERY_REPORT=1``)
fetches the account's discovery salt, keys a workstation fingerprint and
per-agent config path hashes with it, and posts the result here. The server
never sees the machine id, a hostname, a user name or a clear path. New
candidates fire ``agent.discovered``; re-reports only move ``last_seen_at``.

Reporting needs ``report_discovery``. That permission is meant to be held by
a device-scoped API key (scopes ``["report_discovery"]``) which can reach
nothing but the two reporting routes, so an MDM job can run the report
without a token that could do anything else in the account.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.auth.key_scopes import DISCOVERY_REPORT_SCOPE
from preloop.api.common import get_account_for_user
from preloop.models.crud import (
    crud_account_discovery_salt,
    crud_discovered_agent_candidate,
)
from preloop.models.crud.discovered_agent_candidate import (
    CANDIDATE_RETENTION_DAYS,
    ReportedCandidate,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User as UserModel
from preloop.schemas.agent_discovery import (
    DiscoveredAgentCandidateSummary,
    DiscoveredAgentCandidateUpdate,
    DiscoveryReportRequest,
    DiscoveryReportResponse,
    DiscoverySaltResponse,
)
from preloop.services.event_webhooks.emitters import emit_agent_discovered
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)

router = APIRouter()

REPORT_PERMISSION = "report_discovery"


def _require_report_access(db: Session, current_user: Any) -> None:
    """Enforce ``report_discovery`` for the caller, key scopes included.

    Role check: :func:`ensure_permission_in_oss` (the decorator covers EE).
    Key check: an API key that carries scopes must carry this one (or
    ``*``). Personal keys without scopes fall back to the role check.

    Raises:
        HTTPException: 403 when either check fails.
    """
    api_key = getattr(current_user, "_auth_api_key", None)
    scopes = getattr(api_key, "scopes", None) if api_key is not None else None
    if isinstance(scopes, (list, tuple)) and scopes:
        if DISCOVERY_REPORT_SCOPE not in scopes and "*" not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required: {REPORT_PERMISSION}",
            )
    ensure_permission_in_oss(db, current_user, REPORT_PERMISSION)


def _summary(candidate: Any) -> DiscoveredAgentCandidateSummary:
    return DiscoveredAgentCandidateSummary(
        id=candidate.id,
        agent_kind=candidate.agent_kind,
        agent_version=candidate.agent_version,
        workstation_fingerprint=candidate.workstation_fingerprint,
        config_path_hash=candidate.config_path_hash,
        mcp_server_count=candidate.mcp_server_count,
        enrolled=bool(candidate.reported_enrolled),
        os_family=candidate.os_family,
        status=candidate.status,
        managed_agent_id=candidate.managed_agent_id,
        first_seen_at=candidate.first_seen_at,
        last_seen_at=candidate.last_seen_at,
    )


@router.get("/agents/discovery-salt", response_model=DiscoverySaltResponse)
@require_permission(REPORT_PERMISSION)
def get_discovery_salt(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoverySaltResponse:
    """Return the account's discovery salt, issuing it on first use."""
    _require_report_access(db, current_user)
    salt = crud_account_discovery_salt.get_or_create(db, account_id=account.id)
    return DiscoverySaltResponse(salt=salt, retention_days=CANDIDATE_RETENTION_DAYS)


@router.post(
    "/agents/discovery-reports",
    response_model=DiscoveryReportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission(REPORT_PERMISSION)
def create_discovery_report(
    payload: DiscoveryReportRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoveryReportResponse:
    """Record one workstation's discovery report.

    New candidate rows emit ``agent.discovered`` in the same transaction;
    known rows only get a fresh ``last_seen_at``.
    """
    _require_report_access(db, current_user)
    outcomes = crud_discovered_agent_candidate.record_report(
        db,
        account_id=account.id,
        workstation_fingerprint=payload.workstation_fingerprint,
        os_family=payload.os,
        cli_version=payload.cli_version,
        candidates=[
            ReportedCandidate(
                agent_kind=item.agent_kind,
                config_path_hash=item.config_path_hash,
                agent_version=item.agent_version,
                mcp_server_count=item.mcp_server_count,
                enrolled=item.enrolled,
            )
            for item in payload.candidates
        ],
    )
    created = 0
    for outcome in outcomes:
        if outcome.created:
            created += 1
            emit_agent_discovered(db, outcome.candidate)
    db.commit()
    return DiscoveryReportResponse(
        received=len(outcomes), created=created, updated=len(outcomes) - created
    )


@router.get(
    "/agents/discovery-candidates",
    response_model=list[DiscoveredAgentCandidateSummary],
)
@require_permission("view_agents")
def list_discovery_candidates(
    account: Annotated[Account, Depends(get_account_for_user)],
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[DiscoveredAgentCandidateSummary]:
    """List reported candidates, most recently seen first."""
    rows = crud_discovered_agent_candidate.list_for_account(
        db, account_id=account.id, statuses=status_filter
    )
    return [_summary(row) for row in rows]


@router.patch(
    "/agents/discovery-candidates/{candidate_id}",
    response_model=DiscoveredAgentCandidateSummary,
)
@require_permission("manage_agents")
def update_discovery_candidate(
    candidate_id: UUID,
    payload: DiscoveredAgentCandidateUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoveredAgentCandidateSummary:
    """Mark a candidate ignored, or put an ignored one back to ``new``."""
    ensure_permission_in_oss(db, current_user, "manage_agents")
    candidate = crud_discovered_agent_candidate.set_status(
        db,
        account_id=account.id,
        candidate_id=candidate_id,
        status=payload.status,
    )
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Discovery candidate not found",
        )
    return _summary(candidate)
