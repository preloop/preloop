"""Security-screen scoring endpoint implementing QM's external proxy contract.

A QM deployment configured with::

    securityScreen: {
      backend: "proxy",
      provider: "preloop",
      endpoint: "https://<preloop-host>/api/v1/security-screen/score",
      rollout: "shadow"
    }

POSTs every screened content chunk here with the operator's token in the
``x-api-key`` header and expects ``{score, threshold, primary_outcome}``
back. Shadow vs enforce rollout and fail-closed error handling live on the
QM side; this endpoint only scores. Screened text is never logged or
persisted.

Contract: https://github.com/yc-software/qm/blob/main/docs/deploy-directory.md
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.schemas.security_screen import (
    SecurityScreenRequest,
    SecurityScreenResponse,
)
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.services.security_screen import get_screen_threshold, score_text

logger = logging.getLogger(__name__)

router = APIRouter()


async def get_security_screen_auth_context(
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a security-screen request.

    QM sends the routed ``SECURITY_SCREEN_PROXY_TOKEN`` in the ``x-api-key``
    header; a plain ``Authorization: Bearer`` header is accepted as a
    fallback for non-QM callers. Tokens are validated with the same bearer
    authentication the model gateway uses (Preloop API keys, user JWTs, and
    OAuth tokens).

    Args:
        x_api_key: Token from the ``x-api-key`` header, if present.
        authorization: ``Authorization`` header value, if present.
        db: Database session.

    Returns:
        The authenticated request context.

    Raises:
        HTTPException: 401 when the credential is missing or invalid.
    """
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    auth_context = await authenticate_bearer_token(token, db)
    if not auth_context:
        raise HTTPException(
            status_code=401, detail="Invalid authentication credentials"
        )
    return auth_context


@router.post(
    "/security-screen/score",
    response_model=SecurityScreenResponse,
    response_model_exclude_none=True,
    summary="Score screened content (QM security-screen proxy contract)",
)
async def score_screened_content(
    payload: SecurityScreenRequest,
    auth_context: ModelGatewayAuthContext = Depends(get_security_screen_auth_context),
) -> SecurityScreenResponse:
    """Score one screened content chunk with the deterministic rule engine.

    Args:
        payload: Screened chunk (text, hook, opaque metadata).
        auth_context: Authenticated request context.

    Returns:
        The verdict in QM's expected shape; ``primary_outcome`` is omitted
        for benign content.
    """
    verdict = score_text(payload.text)
    threshold = get_screen_threshold()
    if verdict.score >= threshold:
        metadata = payload.metadata or {}
        # Log chunk coordinates and rule names only -- never the text.
        logger.info(
            "Security screen flagged chunk: outcome=%s score=%.2f rules=%s "
            "hook=%s account=%s coordinates=%s",
            verdict.primary_outcome,
            verdict.score,
            ",".join(verdict.matched_rules),
            payload.hook,
            auth_context.user.account_id,
            metadata.get("qm"),
        )
    return SecurityScreenResponse(
        score=verdict.score,
        threshold=threshold,
        primary_outcome=verdict.primary_outcome,
    )
