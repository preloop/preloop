"""Authenticated account-scoped managed bundle registration and counts."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_bundle_revision, crud_control_expectation
from preloop.models.db.session import get_db_session
from preloop.schemas.managed_bundle import (
    BundleManifest,
    BundleRegistryResponse,
    BundleRevisionResponse,
)
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/agents/managed-config/bundles", tags=["Managed bundles"])


def _response(
    db: Session, row: models.RegisteredBundleRevision
) -> BundleRevisionResponse:
    """Build a safe response using a count scoped to the row's owning account."""
    return BundleRevisionResponse(
        id=row.id,
        manifest=BundleManifest.model_validate(row.manifest),
        registered_at=row.registered_at,
        generated_count=crud_control_expectation.generated_count(
            db, account_id=row.account_id, bundle_revision_id=row.id
        ),
    )


@router.post(
    "",
    response_model=BundleRevisionResponse,
    responses={
        409: {"description": "Retired revision or conflicting manifest metadata"}
    },
)
@require_permission("manage_agents")
def register_bundle(
    manifest: BundleManifest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> BundleRevisionResponse:
    """Register manifest metadata, superseding only the caller's app/platform.

    Registration neither enrolls devices nor generates expectations. The
    optional fleet generator must join registered bundles to matching evidence.
    """
    try:
        row = crud_bundle_revision.register(
            db, account_id=current_user.account_id, manifest=manifest
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _response(db, row)


@router.get("", response_model=BundleRegistryResponse)
@require_permission("view_agents")
def list_bundles(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> BundleRegistryResponse:
    """Read active bundles and the real active generated count for this account."""
    return BundleRegistryResponse(
        bundles=[
            _response(db, row)
            for row in crud_bundle_revision.list_active(
                db, account_id=current_user.account_id
            )
        ],
        generated_count=crud_control_expectation.generated_count(
            db, account_id=current_user.account_id
        ),
    )
