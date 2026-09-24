"""Bearer authentication for routes that share the model gateway credential.

Lives outside the gateway endpoint module so the API process can authenticate
an agent key without importing the gateway stack. Gateway routes and browser
step ingestion both call :func:`authenticate_request`; only the ended-session
policy differs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError


async def authenticate_request(
    authorization: Optional[str],
    db: Session,
    *,
    allow_ended_runtime_session: bool,
) -> ModelGatewayAuthContext:
    """Resolve a bearer token or raise the gateway's 401.

    Args:
        authorization: ``Authorization`` header value.
        db: Database session.
        allow_ended_runtime_session: When true, a key pinned to a session
            that has already ended still authenticates. Model inference
            keeps the default and rejects that key.

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
    auth_context = await authenticate_bearer_token(
        token,
        db,
        owns_db_session=True,
        allow_ended_runtime_session=allow_ended_runtime_session,
    )
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=401,
            message="Invalid authentication credentials",
        )
    return auth_context


async def get_model_gateway_auth_context(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a bearer token for the model gateway.

    A key pinned to a session that has ended is rejected. Inference must
    not continue on a finished session.

    Args:
        authorization: ``Authorization`` header.
        db: Database session.

    Returns:
        The authenticated gateway context.

    Raises:
        ModelGatewayAPIError: 401 when the bearer is missing or rejected.
    """
    return await authenticate_request(
        authorization, db, allow_ended_runtime_session=False
    )


async def get_browser_step_auth_context(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a bearer token for browser step ingestion.

    Same credential and rejection rules as the model gateway, except a
    key pinned to a session may still flush steps after that session ends.

    Args:
        authorization: ``Authorization`` header.
        db: Database session.

    Returns:
        The authenticated gateway context.

    Raises:
        ModelGatewayAPIError: 401 when the bearer is missing or rejected.
    """
    return await authenticate_request(
        authorization, db, allow_ended_runtime_session=True
    )
