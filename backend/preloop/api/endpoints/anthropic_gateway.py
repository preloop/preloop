"""Anthropic-compatible gateway endpoints."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from preloop.models.db.session import get_db_session
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.api.deps import get_budget_enforcer
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService

router = APIRouter(include_in_schema=False)


async def get_anthropic_gateway_auth_context(
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate an Anthropic-compatible gateway request."""
    if not anthropic_version:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=400,
            message="Missing anthropic-version header",
        )

    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    if not token:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=401,
            message="Missing API key",
        )

    auth_context = await authenticate_bearer_token(token, db)
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="anthropic",
            status_code=401,
            message="Invalid authentication credentials",
        )
    return auth_context


@router.post("/messages")
def create_message(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_anthropic_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
    anthropic_version: Optional[str] = Header(None, alias="anthropic-version"),
    anthropic_beta: Optional[str] = Header(None, alias="anthropic-beta"),
) -> Any:
    """Create an Anthropic-compatible message.

    The ``anthropic-version`` and ``anthropic-beta`` headers are forwarded to
    the service so the subscription-OAuth passthrough can preserve the
    client's requested API surface (e.g. prompt-caching betas) upstream.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        client_session_id=x_preloop_session_id,
    )
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_message(
                payload,
                anthropic_version=anthropic_version,
                anthropic_beta=anthropic_beta,
            ),
            media_type="text/event-stream",
        )
    return service.create_message(
        payload,
        anthropic_version=anthropic_version,
        anthropic_beta=anthropic_beta,
    )
