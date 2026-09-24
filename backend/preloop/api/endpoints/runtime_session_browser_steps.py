"""Ingest browser steps reported by an agent runtime.

Authentication is the shared gateway bearer dependency. This route allows
a key pinned to a session to flush after that session has ended; model
inference does not.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.gateway_auth_dependency import get_browser_step_auth_context
from preloop.models.db.session import get_db_session
from preloop.schemas.browser_step import BrowserStepBatchIn, BrowserStepBatchOut
from preloop.services.browser_steps import BrowserStepsError, ingest_batch
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

router = APIRouter()


@router.post(
    "/runtime-sessions/{runtime_session_id}/browser-steps",
    response_model=BrowserStepBatchOut,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Credential is bound to a different runtime session"},
        404: {"description": "Runtime session not found"},
    },
)
def ingest_runtime_session_browser_steps(
    runtime_session_id: str,
    batch: BrowserStepBatchIn,
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(get_browser_step_auth_context),
) -> BrowserStepBatchOut:
    """Store a batch of browser step observations on a runtime session.

    Args:
        runtime_session_id: Session the steps attach to.
        batch: One to 200 steps.
        db: Database session.
        auth: Agent credential from the bearer token.

    Returns:
        How many steps were stored, repeated, or refused.

    Raises:
        HTTPException: 403 when the credential is pinned to another session,
            404 when the session is not in the credential's account.
    """
    try:
        return ingest_batch(
            db,
            auth=auth,
            runtime_session_id=runtime_session_id,
            batch=batch,
        )
    except BrowserStepsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
