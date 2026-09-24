"""Ingest browser steps reported by an agent runtime.

The route uses the same bearer check as the model gateway. It does not
import the gateway endpoint module: that module pulls the gateway stack
into the process, and the API role is required to stay free of it.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.schemas.browser_step import BrowserStepBatchIn, BrowserStepBatchOut
from preloop.services.browser_steps import BrowserStepsError, ingest_batch
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError

router = APIRouter()


async def get_model_gateway_auth_context(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a bearer token the same way the model gateway does.

    Defined here rather than imported from ``openai_gateway`` so the API
    process does not load the gateway stack. The checks match that
    dependency: a missing or unknown bearer is 401.

    Args:
        authorization: ``Authorization`` header.
        db: Database session.

    Returns:
        The authenticated gateway context.

    Raises:
        ModelGatewayAPIError: 401 when the bearer is missing or rejected.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=401,
            message="Missing bearer token",
        )

    token = authorization[7:]
    auth_context = await authenticate_bearer_token(token, db, owns_db_session=True)
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=401,
            message="Invalid authentication credentials",
        )
    return auth_context


@router.post(
    "/runtime-sessions/{runtime_session_id}/browser-steps",
    response_model=BrowserStepBatchOut,
)
def ingest_runtime_session_browser_steps(
    runtime_session_id: str,
    batch: BrowserStepBatchIn,
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
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
