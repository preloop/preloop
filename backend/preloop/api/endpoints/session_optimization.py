"""Runtime-session optimization endpoints: analyze, apply, verify by replay.

The full value loop is open capability: waste findings, one-click apply, and
replay verification run in every edition on the account's own models (BYOK).
Deployments that meter built-in hosted models register an analysis-model
authorizer (see :mod:`preloop.services.optimization_gating`) which may reject
hosted-model analysis; nothing here imports billing concepts.

Mounted under ``/billing/cost`` to keep the API paths stable for existing
console clients (the endpoints previously shipped in the billing plugin).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.deps import get_budget_enforcer
from preloop.models.crud import crud_audit_log
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.gateway_usage import (
    RuntimeSessionOptimizationActionListResponse,
    RuntimeSessionOptimizationAppliedAction,
    RuntimeSessionOptimizationApplyRequest,
    RuntimeSessionOptimizationRequest,
    RuntimeSessionOptimizationResponse,
)
from preloop.services.example_optimization import (
    ExampleSessionUnavailableError,
    build_example_optimization_response,
)
from preloop.services.replay_savings_service import (
    ConsentRequiredError,
    run_session_replay,
)
from preloop.services.session_optimization import SessionOptimizationService
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing/cost", tags=["Session Optimization"])


@router.get(
    "/runtime-sessions/example/optimization",
    response_model=RuntimeSessionOptimizationResponse,
)
@require_permission("view_cost")
def get_example_runtime_session_optimization(
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> RuntimeSessionOptimizationResponse:
    """Return the bundled example session's optimization suggestions.

    Lets a brand-new account see what the Optimize tab produces before its own
    agents have generated any traffic. The response is computed live by the
    production deterministic analyzers over a bundled transcript and is flagged
    ``is_example`` so the console can label it as sample data.

    Deliberately side-effect free: no database writes, no LLM call, and — unlike
    :func:`optimize_account_runtime_session` — no ``optimization_result_viewed``
    audit event, because viewing a bundled sample is not the account "reaching
    their number" and must not enter launch telemetry.

    Raises:
        HTTPException: 404 if the bundled transcript is unavailable, so the
            console falls back to its normal empty state.
    """
    try:
        return build_example_optimization_response(db)
    except ExampleSessionUnavailableError:
        logger.warning("Bundled example session unavailable", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Example session is not available",
        ) from None


@router.post(
    "/runtime-sessions/{runtime_session_id}/optimizations",
    response_model=RuntimeSessionOptimizationResponse,
)
@require_permission("view_cost")
def optimize_account_runtime_session(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    request: RuntimeSessionOptimizationRequest | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
    budget_enforcer: Any = Depends(get_budget_enforcer),
) -> RuntimeSessionOptimizationResponse:
    """Suggest cost and context optimizations for one runtime session."""
    response = SessionOptimizationService(
        db
    ).get_account_session_optimization_suggestions(
        account=account,
        runtime_session_id=runtime_session_id,
        request=request or RuntimeSessionOptimizationRequest(),
        current_user=current_user,
        budget_enforcer=budget_enforcer,
    )
    # Cohort telemetry: record that a user actually viewed optimization results,
    # so the "reached-their-number" launch metric is queryable — in every
    # edition, since the value loop is open capability. Emitted here at the
    # endpoint layer (not the service) so internal/cache-warm calls stay
    # silent. NOTE: audit_log is compliance-shaped (append-only, retention/PII
    # rules); a dedicated user_activity_event table is the eventual clean home
    # (deferred).
    try:
        crud_audit_log.log_action(
            db,
            account_id=account.id,
            user_id=current_user.id,
            action="optimization_result_viewed",
            resource_type="runtime_session",
            resource_id=runtime_session_id,
            status="success",
            details={"runtime_session_id": runtime_session_id},
        )
    except Exception:
        logger.debug("Failed to audit optimization result view", exc_info=True)
    return response


@router.post(
    "/runtime-sessions/{runtime_session_id}/optimizations/apply",
    response_model=RuntimeSessionOptimizationAppliedAction,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("edit_ai_models")
def apply_account_runtime_session_optimization(
    runtime_session_id: str,
    request: RuntimeSessionOptimizationApplyRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> RuntimeSessionOptimizationAppliedAction:
    """Apply one optimization suggestion action for a runtime session.

    Applying writes governance/budget configuration only — it spends no model
    budget — so it is never gated behind an entitlement.
    """
    return SessionOptimizationService(db).apply_account_session_optimization_action(
        account=account,
        runtime_session_id=runtime_session_id,
        request=request,
        current_user=current_user,
    )


class RuntimeSessionReplayCandidate(BaseModel):
    """The candidate optimization to measure by re-execution replay."""

    removed_tool_names: list[str] = Field(default_factory=list)
    filtered_output_fields: dict[str, list[str]] = Field(default_factory=dict)


class RuntimeSessionReplayRequest(BaseModel):
    """Request to verify a candidate's savings by replaying a stored request."""

    candidate: RuntimeSessionReplayCandidate = Field(
        default_factory=RuntimeSessionReplayCandidate
    )
    suggestion_id: Optional[str] = None
    # A replay re-sends the stored request upstream and spends the user's
    # budget, so consent is explicit and required.
    consented: bool = False
    n_runs: int = Field(default=3, ge=1, le=10)


class RuntimeSessionReplayResponse(BaseModel):
    """Outcome of a replay: the exact input delta plus the banded end-to-end."""

    id: str
    runtime_session_id: str
    status: str
    input_delta_tokens: int
    input_pct_saved: float
    end_to_end_delta_median: Optional[float] = None
    end_to_end_delta_low: Optional[float] = None
    end_to_end_delta_high: Optional[float] = None
    inconclusive: bool
    cost_spent: Optional[float] = None
    n_runs: int


@router.post(
    "/runtime-sessions/{runtime_session_id}/replay",
    response_model=RuntimeSessionReplayResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("edit_ai_models")
def replay_account_runtime_session(
    runtime_session_id: str,
    request: RuntimeSessionReplayRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
    budget_enforcer: Any = Depends(get_budget_enforcer),
) -> RuntimeSessionReplayResponse:
    """Verify a candidate optimization's savings by re-executing a stored request.

    Re-runs the session's stored request with and without the candidate applied,
    under a hard spend cap, and records the exact deterministic input-token delta
    together with the noise-banded end-to-end delta. Requires explicit consent.
    """
    try:
        row = run_session_replay(
            db,
            account=account,
            current_user=current_user,
            runtime_session_id=runtime_session_id,
            candidate={
                "removed_tool_names": request.candidate.removed_tool_names,
                "filtered_output_fields": request.candidate.filtered_output_fields,
            },
            consented=request.consented,
            n_runs=request.n_runs,
            suggestion_id=request.suggestion_id,
            budget_enforcer=budget_enforcer,
        )
    except ConsentRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return RuntimeSessionReplayResponse(
        id=str(row.id),
        runtime_session_id=str(row.runtime_session_id),
        status=row.status,
        input_delta_tokens=row.input_delta_tokens,
        input_pct_saved=row.input_pct_saved,
        end_to_end_delta_median=row.end_to_end_delta_median,
        end_to_end_delta_low=row.end_to_end_delta_low,
        end_to_end_delta_high=row.end_to_end_delta_high,
        inconclusive=row.inconclusive,
        cost_spent=row.cost_spent,
        n_runs=row.n_runs,
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/optimizations/actions",
    response_model=RuntimeSessionOptimizationActionListResponse,
)
@require_permission("view_cost")
def list_account_runtime_session_optimization_actions(
    runtime_session_id: str,
    account: Annotated[Account, Depends(get_account_for_user)],
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> RuntimeSessionOptimizationActionListResponse:
    """List applied optimization actions and their measured outcomes."""
    return SessionOptimizationService(db).list_account_session_optimization_actions(
        account=account,
        runtime_session_id=runtime_session_id,
    )
