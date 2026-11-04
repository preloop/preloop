"""Let an agent runtime name the desktop endpoint it runs next to (#1568).

``preloop desktop enable`` exports ``PRELOOP_DESKTOP_ENDPOINT_ID`` into the
runtime environment. Adapters that already hold a runtime session send it
here so the console offers "Live desktop" on that session even when the
session's credential differs from the one the desktop tunnel registered with.
Authentication is the agent bearer, as for browser steps.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.gateway_auth_dependency import get_browser_step_auth_context
from preloop.models.crud import crud_desktop, crud_runtime_session
from preloop.models.db.session import get_db_session
from preloop.services.model_gateway_auth import ModelGatewayAuthContext

router = APIRouter()


class RuntimeSessionDesktopLink(BaseModel):
    """Desktop endpoint named by the adapter; null clears the link."""

    desktop_endpoint_id: Optional[str] = Field(default=None, max_length=128)


class RuntimeSessionDesktopLinkOut(BaseModel):
    """Stored link and whether it matches an endpoint in the account now."""

    runtime_session_id: str
    desktop_endpoint_id: Optional[str]
    linked: bool


@router.put(
    "/runtime-sessions/{runtime_session_id}/desktop-endpoint",
    response_model=RuntimeSessionDesktopLinkOut,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Credential is bound to a different runtime session"},
        404: {"description": "Runtime session not found"},
    },
)
def set_runtime_session_desktop_endpoint(
    runtime_session_id: str,
    body: RuntimeSessionDesktopLink,
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(get_browser_step_auth_context),
) -> RuntimeSessionDesktopLinkOut:
    """Record the desktop endpoint a runtime session runs next to.

    The value is stored as given; it only links endpoints in the session's
    own account, so naming another account's endpoint links nothing and
    ``linked`` is false. An endpoint that registers later is picked up then.

    Args:
        runtime_session_id: Session to link.
        body: Endpoint id (host ``endpoint_id`` or row id), or null to clear.
        db: Database session.
        auth: Agent credential from the bearer token.

    Returns:
        The stored value and whether it currently matches an endpoint.

    Raises:
        HTTPException: 403 when the credential is pinned to another session,
            404 when the session is not in the credential's account.
    """
    try:
        canonical = str(UUID(str(runtime_session_id).strip()))
    except ValueError:
        raise HTTPException(404, "Runtime session not found") from None
    pinned = auth.runtime_session_id
    if pinned is not None and pinned.lower() != canonical.lower():
        raise HTTPException(403, "Credential is bound to a different runtime session")
    session = crud_runtime_session.get_account_session(
        db, account_id=str(auth.account_id), runtime_session_id=canonical
    )
    if session is None:
        raise HTTPException(404, "Runtime session not found")
    session = crud_runtime_session.set_desktop_endpoint(
        db, runtime_session=session, desktop_endpoint_id=body.desktop_endpoint_id
    )
    linked = False
    if session.desktop_endpoint_id:
        linked = bool(
            crud_desktop.list_endpoints(
                db,
                account_id=session.account_id,
                endpoint_idents=[session.desktop_endpoint_id],
            )
        )
    return RuntimeSessionDesktopLinkOut(
        runtime_session_id=str(session.id),
        desktop_endpoint_id=session.desktop_endpoint_id,
        linked=linked,
    )
