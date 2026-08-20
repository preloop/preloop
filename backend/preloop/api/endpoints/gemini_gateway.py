"""Gemini-compatible gateway endpoints."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Query
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from preloop.api.deps import get_budget_enforcer
from preloop.models.db.session import get_db_session
from preloop.services.gateway_streaming import GatewayStreamingResponse
from preloop.services.gemini_gateway import GeminiGatewayService
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    authenticate_bearer_token,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError

router = APIRouter(include_in_schema=False)


async def get_gemini_gateway_auth_context(
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    api_key: Optional[str] = Header(None, alias="api-key"),
    authorization: Optional[str] = Header(None),
    key: Optional[str] = Query(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a Gemini-compatible gateway request."""
    token = x_goog_api_key or key or api_key
    if not token and authorization:
        if authorization.lower().startswith("bearer "):
            token = authorization[7:]
        else:
            token = authorization
    if not token:
        raise ModelGatewayAPIError(
            provider="gemini",
            status_code=401,
            message="Missing API key",
        )

    auth_context = await authenticate_bearer_token(token, db)
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="gemini",
            status_code=401,
            message="Invalid API key",
        )
    return auth_context


@router.get("/models")
def list_models(
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_gemini_gateway_auth_context),
) -> Dict[str, Any]:
    """List Gemini-compatible model aliases."""
    return GeminiGatewayService(db, auth_context).list_models()


@router.get("/models/{model_name:path}")
def get_model(
    model_name: str,
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_gemini_gateway_auth_context),
) -> Dict[str, Any]:
    """Return Gemini-compatible metadata for one model alias."""
    return GeminiGatewayService(db, auth_context).get_model(model_name)


@router.post("/models/{model_name:path}:generateContent")
def generate_content(
    model_name: str,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_gemini_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Dict[str, Any]:
    """Generate Gemini-compatible content from the shared gateway."""
    return GeminiGatewayService(
        db,
        auth_context,
        client_session_id=x_preloop_session_id,
        budget_enforcer=budget_enforcer,
    ).generate_content(model_name, payload)


@router.post("/models/{model_name:path}:streamGenerateContent")
def stream_generate_content(
    model_name: str,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_gemini_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> StreamingResponse:
    """Stream Gemini-compatible content from the shared gateway."""
    service = GeminiGatewayService(
        db,
        auth_context,
        client_session_id=x_preloop_session_id,
        budget_enforcer=budget_enforcer,
    )
    return GatewayStreamingResponse(
        service.stream_generate_content(model_name, payload),
        media_type="text/event-stream",
        on_complete=service.flush_deferred_stream_record,
    )
