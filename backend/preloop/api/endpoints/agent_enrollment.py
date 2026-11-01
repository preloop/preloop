"""Generic single-use app enrollment authority, separate from report credentials."""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    get_current_active_user,
    oauth2_scheme,
    _authenticate_with_api_key,
)
from preloop.api.auth.key_scopes import is_single_purpose_api_key
from preloop.models import models
from preloop.models.crud import crud_account_discovery_salt, crud_api_key
from preloop.models.db.session import get_db_session
from preloop.schemas.agent_enrollment import (
    EnrollmentApplyResponse,
    EnrollmentExchangeRequest,
    EnrollmentExchangeResponse,
    EnrollmentGrantRequest,
    EnrollmentGrantResponse,
)
from preloop.services.agent_enrollment import (
    acknowledge_apply,
    exchange_authority,
    issue_authority,
)
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter(prefix="/agents/enrollment")


def _no_cache(response: Response) -> None:
    """Prevent caching of one-time plaintext token responses."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/authorities", response_model=EnrollmentGrantResponse, status_code=201)
@require_permission("manage_agents")
def create_authority(
    payload: EnrollmentGrantRequest,
    response: Response,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> EnrollmentGrantResponse:
    """Issue a short-lived token once to an authenticated administrator."""
    api_key = getattr(current_user, "_auth_api_key", None)
    if api_key is not None and (
        is_single_purpose_api_key(api_key)
        or api_key.scopes
        or (api_key.context_data or {}).get("managed_agent_id")
    ):
        raise HTTPException(403, "Administrative authentication required")
    ensure_permission_in_oss(db, current_user, "manage_agents")
    salt = crud_account_discovery_salt.get_or_create(
        db, account_id=current_user.account_id
    )
    try:
        key, token = issue_authority(db, user=current_user, grant=payload)
        assert key.expires_at is not None
        result = EnrollmentGrantResponse(
            token=token, salt=salt, expires_at=key.expires_at.replace(tzinfo=UTC)
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    _no_cache(response)
    return result


@router.post("/exchange", response_model=EnrollmentExchangeResponse)
def exchange(
    payload: EnrollmentExchangeRequest,
    response: Response,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> EnrollmentExchangeResponse:
    """Accept provisioning authority only, never report or app runtime keys."""
    key = getattr(current_user, "_auth_api_key", None)
    if key is None or key.scopes != ["enrollment:provision"]:
        raise HTTPException(403, "Enrollment authority required")
    try:
        result = exchange_authority(db, authenticated_key=key, payload=payload)
        db.commit()
    except Exception:
        db.rollback()
        raise
    _no_cache(response)
    return result


def _app_credential(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db_session),
) -> models.ApiKey:
    """Authenticate the app itself for its sole configuration acknowledgment."""
    key = crud_api_key.get_by_key(db, key=token)
    _authenticate_with_api_key(db, key, allow_managed_agent=True)
    if (
        key is None
        or (key.context_data or {}).get("credential_kind") != "managed_agent_durable"
    ):
        raise HTTPException(403, "App credential required")
    return key


@router.post("/applied", response_model=EnrollmentApplyResponse)
def applied(
    key: models.ApiKey = Depends(_app_credential),
    db: Session = Depends(get_db_session),
) -> EnrollmentApplyResponse:
    """Acknowledge local config using the app's own active credential only."""
    try:
        acknowledge_apply(db, authenticated_key=key)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return EnrollmentApplyResponse()
