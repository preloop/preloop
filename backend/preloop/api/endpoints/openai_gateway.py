"""OpenAI-compatible gateway endpoints."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, Request
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from preloop.models.db.session import get_db_session
from preloop.services.agent_session_headers import native_session_id_from_headers
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
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible chat completion.

    OpenCode (``x-session-id``) and Codex (``session-id``/``thread-id``) both
    stamp their own conversation id on every request; reading it is what stops
    every conversation on a machine collapsing onto one eternal runtime session.
    Preloop's own header still wins, and agent-native headers are only trusted
    for the agent the credential identifies — see
    :mod:`preloop.services.agent_session_headers`.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        client_session_id=x_preloop_session_id
        or native_session_id_from_headers(request.headers, auth_context=auth_context),
    )
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_chat_completion(payload),
            media_type="text/event-stream",
        )
    return service.create_chat_completion(payload)


@router.post("/responses")
def create_response(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db_session),
    auth_context: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
    budget_enforcer: Any = Depends(get_budget_enforcer),
    x_preloop_session_id: Optional[str] = Header(None, alias="X-Preloop-Session-Id"),
) -> Any:
    """Create an OpenAI-compatible response.

    This is the route Codex uses (``wire_api = "responses"``). Codex sends its
    conversation uuid as ``session-id``/``thread-id`` headers and again as the
    body's ``prompt_cache_key``; both are read, so the fix survives either
    signal being dropped. See :func:`create_chat_completion` for precedence.
    """
    service = OpenAIGatewayService(
        db,
        auth_context,
        budget_enforcer=budget_enforcer,
        client_session_id=x_preloop_session_id
        or native_session_id_from_headers(request.headers, auth_context=auth_context),
    )
    if payload.get("stream"):
        return StreamingResponse(
            service.stream_response(payload),
            media_type="text/event-stream",
        )
    return service.create_response(payload)
