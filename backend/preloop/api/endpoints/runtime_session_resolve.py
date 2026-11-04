"""Resolve a client session key to its runtime session id (#1358).

An adapter (Browser Use reporter, Skyvern importer) passes the same key it
sends the gateway as ``X-Preloop-Session-Id`` and gets back the runtime
session UUID, so its browser steps and the gateway's model turns land on
one session without ``PRELOOP_RUNTIME_SESSION_ID``. Authentication is the
gateway bearer, and the keying is the gateway's own, so both sides agree.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from preloop.api.gateway_auth_dependency import get_model_gateway_auth_context
from preloop.models.db.session import get_db_session
from preloop.services.agent_session_headers import normalize_session_id
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService

router = APIRouter()


class RuntimeSessionResolveOut(BaseModel):
    """The runtime session a client session key maps to."""

    runtime_session_id: str
    created: bool


@router.get(
    "/runtime-sessions/resolve",
    response_model=RuntimeSessionResolveOut,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No runtime session for this key in your account"},
        422: {"description": "client_session_id is not a valid session id"},
    },
)
def resolve_runtime_session(
    client_session_id: str = Query(
        ..., description="The value the client sends as X-Preloop-Session-Id"
    ),
    create: bool = Query(
        False, description="Create the session exactly as the gateway would"
    ),
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(get_model_gateway_auth_context),
) -> RuntimeSessionResolveOut:
    """Return the runtime session id for a client session key.

    Args:
        client_session_id: Key the client sends as ``X-Preloop-Session-Id``.
        create: Create the row when missing, with the gateway's keying.
        db: Database session.
        auth: Gateway credential from the bearer token.

    Returns:
        The runtime session id and whether this call created it.

    Raises:
        HTTPException: 422 for a malformed key, 404 when no session exists
            in the caller's account and ``create`` is false (or the
            credential cannot own a session).
    """
    normalized = normalize_session_id(client_session_id)
    if normalized is None:
        raise HTTPException(status_code=422, detail="Invalid client_session_id")
    service = OpenAIGatewayService(
        db,
        auth,
        client_session_id=normalized,
        client_session_id_is_explicit=True,
    )
    runtime_session_id, created = service.resolve_client_runtime_session(create=create)
    if runtime_session_id is None:
        raise HTTPException(status_code=404, detail="Runtime session not found")
    return RuntimeSessionResolveOut(
        runtime_session_id=runtime_session_id, created=created
    )
