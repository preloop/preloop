"""OpenAI-compatible gateway endpoints."""

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


async def get_model_gateway_auth_context(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Authenticate a bearer token for the model gateway."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=401,
            message="Missing bearer token",
        )

    token = authorization[7:]
    auth_context = await authenticate_bearer_token(token, db)
    if not auth_context:
        raise ModelGatewayAPIError(
            provider="openai",
            status_code=401,
            message="Invalid authentication credentials",
        )
    return auth_context


@router.get("/models")
def list_models(
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
) -> Dict[str, Any]:
    """List models available via the Preloop gateway."""
    return OpenAIGatewayService(db, auth_context).list_models()


@router.post("/chat/completions")
def create_chat_completion(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible chat completion."""
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        client_session_id=x_preloop_session_id,
    )
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_chat_completion(payload),
            media_type="text/event-stream",
        )
    return service.create_chat_completion(payload)


@router.post("/responses")
def create_response(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible response."""
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        client_session_id=x_preloop_session_id,
    )
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_response(payload),
            media_type="text/event-stream",
        )
    return service.create_response(payload)
