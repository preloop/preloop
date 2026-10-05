"""Human-only native restricted CI provisioning and safe metadata/recovery."""

import logging
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import crud, models
from preloop.models.db.session import get_db_session
from preloop.schemas.ci_administration import (
    CiAdminCapabilities,
    CiAdminCreate,
    CiAdminIdentityRead,
    CiAdminIssue,
    CiAdminIssued,
    CiAdminKeyIssued,
    CiAdminPreview,
    CiAdminRotate,
    CiAdminSubscription,
    CiAdminUpdate,
)
from preloop.schemas.webhook_endpoint import WebhookEndpointCreated

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ci-identities", tags=["Restricted CI administration"])
T = TypeVar("T")


def _call(operation: Callable[[], T]) -> T:
    """Keep public errors generic and prevent secrets in error/log projections."""
    try:
        return operation()
    except PermissionError:
        raise HTTPException(403, "CI administration denied") from None
    except LookupError:
        raise HTTPException(404, "CI resource not found") from None
    except ValueError:
        raise HTTPException(
            400, "Invalid CI administration request or resource binding"
        ) from None
    except (RuntimeError, SQLAlchemyError) as error:
        logger.warning("CI administration unavailable (%s)", type(error).__name__)
        raise HTTPException(503, "Restricted CI setup is not available") from None


@router.get("/capabilities", response_model=CiAdminCapabilities)
def capabilities(
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminCapabilities:
    """Report rollout readiness and current human view/manage authority."""
    return _call(lambda: crud.crud_ci_administration.capabilities(db, actor=actor))


@router.post("/preview")
def preview(
    payload: CiAdminPreview,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Validate the actual dedicated hosted binding before creating secrets."""
    return _call(
        lambda: crud.crud_ci_administration.preview(
            db, actor=actor, grant=payload.grant
        )
    )


@router.get("", response_model=list[CiAdminIdentityRead])
def list_identities(
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[CiAdminIdentityRead]:
    """List only safe account-local metadata the human may currently view."""
    return _call(lambda: crud.crud_ci_administration.list(db, actor=actor))


@router.post("", response_model=CiAdminIssued, status_code=201)
def create_identity(
    payload: CiAdminCreate,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminIssued:
    """Create one stable principal and return its initial token exactly once."""

    return _call(
        lambda: crud.crud_ci_administration.create(db, actor=actor, payload=payload)
    )


@router.get("/{principal_id}", response_model=CiAdminIdentityRead)
def get_identity(
    principal_id: UUID,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminIdentityRead:
    """Read safe metadata including revoked/expired key recovery identifiers."""
    return _call(
        lambda: crud.crud_ci_administration.get(
            db, actor=actor, principal_id=principal_id
        )
    )


@router.patch("/{principal_id}", response_model=CiAdminIdentityRead)
def update_identity(
    principal_id: UUID,
    payload: CiAdminUpdate,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminIdentityRead:
    """Narrow/disable or explicitly change a grant without moving its resources."""

    def change() -> CiAdminIdentityRead:
        row = crud.crud_ci_administration.change(
            db, actor=actor, principal_id=principal_id, payload=payload
        )
        return crud.crud_ci_administration.project_metadata(db, principal=row)

    return _call(change)


@router.post("/{principal_id}/keys", response_model=CiAdminKeyIssued, status_code=201)
def issue_key(
    principal_id: UUID,
    payload: CiAdminIssue,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminKeyIssued:
    """Issue a replacement key after expiry/revocation without changing ownership."""
    return _call(
        lambda: crud.crud_ci_administration.issue(
            db, actor=actor, principal_id=principal_id, payload=payload
        )
    )


@router.post("/{principal_id}/keys/{key_id}/rotate", response_model=CiAdminKeyIssued)
def rotate_key(
    principal_id: UUID,
    key_id: UUID,
    payload: CiAdminRotate,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> CiAdminKeyIssued:
    """Atomically revoke one valid key and return its replacement token once."""
    return _call(
        lambda: crud.crud_ci_administration.rotate(
            db, actor=actor, principal_id=principal_id, key_id=key_id, payload=payload
        )
    )


@router.delete("/{principal_id}/keys/{key_id}", status_code=204)
def revoke_key(
    principal_id: UUID,
    key_id: UUID,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Response:
    """Revoke authentication without deleting principal-owned resources."""
    _call(
        lambda: crud.crud_ci_administration.revoke(
            db, actor=actor, principal_id=principal_id, key_id=key_id
        )
    )
    return Response(status_code=204)


@router.post(
    "/{principal_id}/subscriptions",
    response_model=WebhookEndpointCreated,
    status_code=201,
)
def create_subscription(
    principal_id: UUID,
    payload: CiAdminSubscription,
    actor: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> WebhookEndpointCreated:
    """Human-authorize a stable principal callback; no machine impersonation token."""
    from preloop.api.endpoints.event_webhooks import _to_read

    def create() -> WebhookEndpointCreated:
        endpoint, secret = crud.crud_ci_administration.create_subscription(
            db, actor=actor, principal_id=principal_id, payload=payload
        )
        return WebhookEndpointCreated(**_to_read(endpoint).model_dump(), secret=secret)

    return _call(create)
