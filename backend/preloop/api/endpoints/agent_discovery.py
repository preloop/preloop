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
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status, Header
from preloop.api.auth.jwt import get_current_active_user
from preloop.api.auth.key_scopes import DISCOVERY_REPORT_SCOPE
from preloop.api.common import get_account_for_user
from preloop.services.fleet_findings import evaluate_fleet_findings
from preloop.models import models
from preloop.models.crud import (
    crud_account_discovery_salt,
    crud_account,
    crud_flow_runner,
    crud_user,
    crud_discovery_observation,
    crud_discovered_extension,
    crud_discovered_agent_candidate,
)
from preloop.models.crud.discovered_agent_candidate import (
    CANDIDATE_RETENTION_DAYS,
    ReportedCandidate,
)
from preloop.models.crud.discovery_observation import ObservationConflictError
from preloop.models.db.session import get_db_session
from preloop.plugins.base import get_plugin_manager
from preloop.schemas.agent_discovery import (
    DiscoveredAgentCandidateList,
    DiscoveredAgentCandidateSummary,
    DiscoveredAgentCandidateUpdate,
    DiscoveryReportRequest,
    DiscoveryReportResponse,
    DiscoverySaltResponse,
)
from preloop.services.event_webhooks.emitters import emit_agent_discovered
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from preloop.plugins.account_hooks import resolve_runner_policy
from preloop.services.runner_service import hash_runner_token

logger = logging.getLogger(__name__)

router = APIRouter()
runner_router = APIRouter()

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
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
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
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoveryReportResponse:
    """Record one workstation's discovery report.

    New candidate rows emit ``agent.discovered`` in the same transaction;
    known rows only get a fresh ``last_seen_at``.
    """
    _require_report_access(db, current_user)
    return record_discovery_report(db, payload, account, current_user)


def record_discovery_report(
    db: Session,
    payload: DiscoveryReportRequest,
    account: models.Account,
    current_user: models.User,
    *,
    source_ref: Optional[UUID] = None,
) -> DiscoveryReportResponse:
    """Ingest manual and runner reports with identical events and first-seen rules."""
    manager = get_plugin_manager()
    authorizer = manager.get_service("discovery_report_authorizer")
    key = getattr(current_user, "_auth_api_key", None)
    context = getattr(key, "context_data", None)
    if authorizer is None and isinstance(context, dict) and "device_report" in context:
        # A credential with persisted identity restrictions must not silently
        # become an unrestricted reporter if its authorization plugin is absent.
        raise HTTPException(status_code=503, detail="Report authorization unavailable")
    if authorizer is not None:
        authorizer.authorize_report(
            db, account_id=account.id, current_user=current_user, payload=payload
        )
    if payload.evidence is not None:
        service = get_plugin_manager().get_service("discovery_evidence")
        if service is None and payload.evidence.schema_version < 3:
            raise HTTPException(
                status_code=503, detail="Discovery evidence service unavailable"
            )
        try:
            if payload.evidence.schema_version >= 3:
                # Core owns v3+ observations, including machine-wide configuration
                # and runner attribution. The legacy writer owns v1/v2.
                now = datetime.now(UTC)
                if payload.evidence.ended_at > now + timedelta(minutes=5):
                    raise HTTPException(422, "Collection window is in the future")
                if service is not None:
                    # Preserve eager cleanup for legacy installations. The
                    # discovery sweeper enforces retention for every account.
                    crud_discovery_observation.purge_expired(
                        db, account_id=account.id, now=now
                    )
                observation = crud_discovery_observation.record(
                    db,
                    account_id=account.id,
                    workstation_fingerprint=payload.workstation_fingerprint,
                    source_ref=source_ref
                    or (key.id if key is not None else current_user.id),
                    evidence=payload.evidence.model_dump(mode="json"),
                )
                crud_discovered_extension.upsert_from_report(
                    db,
                    account_id=account.id,
                    workstation_fingerprint=payload.workstation_fingerprint,
                    evidence=payload.evidence,
                    observation_id=observation.id,
                )
            else:
                service.record(
                    db,
                    account_id=account.id,
                    current_user=current_user,
                    payload=payload,
                )
        except ObservationConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Observation identifier already contains different evidence",
            ) from exc
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
    evaluate_fleet_findings(db, account_id=account.id)
    return DiscoveryReportResponse(
        received=len(outcomes), created=created, updated=len(outcomes) - created
    )


@router.get(
    "/agents/discovery-candidates",
    response_model=DiscoveredAgentCandidateList,
)
@require_permission("view_agents")
def list_discovery_candidates(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> DiscoveredAgentCandidateList:
    """List reported candidates, most recently seen first.

    The row list is capped. ``total`` is the full matching count and
    ``truncated`` is true when some rows are not in ``items``.
    """
    page = crud_discovered_agent_candidate.list_for_account(
        db, account_id=account.id, statuses=status_filter
    )
    return DiscoveredAgentCandidateList(
        items=[_summary(row) for row in page.items],
        total=page.total,
        truncated=page.truncated,
    )


@router.patch(
    "/agents/discovery-candidates/{candidate_id}",
    response_model=DiscoveredAgentCandidateSummary,
)
@require_permission("manage_agents")
def update_discovery_candidate(
    candidate_id: UUID,
    payload: DiscoveredAgentCandidateUpdate,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
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
    evaluate_fleet_findings(db, account_id=account.id)
    return _summary(candidate)


class RunnerDiscoveryPolicy(BaseModel):
    """Account controls for automatic discovery; null interval uses device config."""

    enabled: bool = True
    interval_seconds: Optional[int] = Field(default=None, ge=3600, le=604800)


class RunnerDiscoveryConfiguration(RunnerDiscoveryPolicy):
    """Reporting policy and the salt, present only when reporting is allowed."""

    salt: Optional[str] = None


def _runner_discovery_policy(account: models.Account) -> RunnerDiscoveryPolicy:
    """Read the account's validated discovery policy."""
    return RunnerDiscoveryPolicy.model_validate(
        (account.meta_data or {}).get("runner_discovery", {})
    )


def _runner_report_context(
    runner_id: UUID,
    authorization: Annotated[str, Header()] = "",
    db: Session = Depends(get_db_session),
) -> tuple[models.FlowRunner, models.Account, models.User]:
    """Grant report_discovery only to an active runner with inventory capability.

    Runner credentials remain separate from user/API-key authentication. Only
    the two routes below accept them, so they cannot list or read any reports.
    """
    runner = crud_flow_runner.get(db, id=runner_id)
    scheme, _, token = authorization.partition(" ")
    if (
        runner is None
        or scheme.lower() != "bearer"
        or not token
        or not hmac.compare_digest(
            str(runner.token_hash or ""), hash_runner_token(token)
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid runner credentials")
    account = crud_account.get(db, id=runner.account_id)
    user = crud_user.get(db, id=runner.registered_by_user_id)
    if (
        account is None
        or not account.is_active
        or user is None
        or not user.is_active
        or user.account_id != account.id
        or runner.ephemeral
    ):
        raise HTTPException(status_code=403, detail="Runner discovery unavailable")
    policy = resolve_runner_policy(db, user)
    if policy.get("requirement") == "forbidden" or "inventory" not in policy.get(
        "capabilities", []
    ):
        raise HTTPException(
            status_code=403, detail="Runner lacks report_discovery capability"
        )
    return runner, account, user


@runner_router.get(
    "/runners/{runner_id}/discovery-policy", response_model=RunnerDiscoveryConfiguration
)
def get_runner_discovery_configuration(
    context: Annotated[
        tuple[models.FlowRunner, models.Account, models.User],
        Depends(_runner_report_context),
    ],
    db: Session = Depends(get_db_session),
) -> RunnerDiscoveryConfiguration:
    """Return automatic reporting controls using only the runner credential."""
    _, account, _ = context
    policy = _runner_discovery_policy(account)
    salt = (
        crud_account_discovery_salt.get_or_create(db, account_id=account.id)
        if policy.enabled
        else None
    )
    return RunnerDiscoveryConfiguration(**policy.model_dump(), salt=salt)


@runner_router.post(
    "/runners/{runner_id}/discovery-reports",
    response_model=DiscoveryReportResponse,
    status_code=202,
)
def create_runner_discovery_report(
    payload: DiscoveryReportRequest,
    context: Annotated[
        tuple[models.FlowRunner, models.Account, models.User],
        Depends(_runner_report_context),
    ],
    db: Session = Depends(get_db_session),
) -> DiscoveryReportResponse:
    """Accept the runner's device report with no enrollment or read authority."""
    runner, account, user = context
    if not _runner_discovery_policy(account).enabled:
        raise HTTPException(status_code=403, detail="Automatic discovery disabled")
    if not crud_flow_runner.bind_discovery_fingerprint(
        db, runner_id=runner.id, fingerprint=payload.workstation_fingerprint
    ):
        raise HTTPException(
            status_code=403, detail="Report must describe this runner's device"
        )
    return record_discovery_report(db, payload, account, user, source_ref=runner.id)


@router.get("/account/runner-discovery", response_model=RunnerDiscoveryPolicy)
@require_permission("manage_policies")
def get_runner_discovery_policy(
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RunnerDiscoveryPolicy:
    """Read account-wide automatic reporting controls."""
    ensure_permission_in_oss(db, current_user, "manage_policies")
    return _runner_discovery_policy(account)


@router.put("/account/runner-discovery", response_model=RunnerDiscoveryPolicy)
@require_permission("manage_policies")
def update_runner_discovery_policy(
    payload: RunnerDiscoveryPolicy,
    account: Annotated[models.Account, Depends(get_account_for_user)],
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RunnerDiscoveryPolicy:
    """Set account-wide reporting controls without changing other settings."""
    ensure_permission_in_oss(db, current_user, "manage_policies")
    crud_account.set_runner_discovery_policy(
        db, account_id=account.id, policy=payload.model_dump()
    )
    return payload
