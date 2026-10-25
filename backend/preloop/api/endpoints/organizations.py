"""Endpoints for managing organizations."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user  # Import user dependency
from preloop.schemas.organization import (
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
)
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.tracker import CRUDTracker  # Import tracker CRUD
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User  # Import Account model
from preloop.models.models.organization import Organization
from preloop.models.models.tracker import Tracker
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

# Initialize CRUD operations for Organization
crud_organization = CRUDOrganization(Organization)

router = APIRouter()
crud_tracker = CRUDTracker(Tracker)  # Instantiate tracker CRUD


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
@require_permission("create_organizations")
def create_organization(
    organization: OrganizationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Organization:
    """Create a new organization, ensuring it's linked to the current user's tracker."""
    ensure_permission_in_oss(db, current_user, "create_organizations")
    # Check if organization with this identifier already exists
    existing_org = crud_organization.get_by_identifier(
        db, identifier=organization.identifier
    )
    if existing_org:
        raise HTTPException(
            status_code=400,
            detail=f"Organization with identifier '{organization.identifier}' already exists",
        )

    # Note: In a real implementation, you would need to get the tracker_id from somewhere.
    # For now, let's use a placeholder that would need to be properly integrated
    # with your authentication and tracker selection flow.

    # Get the first tracker associated with the current user
    user_trackers = crud_tracker.get_for_account(db, account_id=current_user.account_id)
    if not user_trackers:
        raise HTTPException(
            status_code=400,
            detail="No trackers found for the current user. Please register a tracker first.",
        )
    # Use the first tracker found for this user
    tracker_id_to_use = user_trackers[0].id

    # Create new organization with CRUD operation
    org_data = {
        "id": str(uuid.uuid4()),
        "name": organization.name,
        "identifier": organization.identifier,
        "description": organization.description,
        "settings": organization.settings or {},
        "tracker_id": tracker_id_to_use,
        "meta_data": {},
    }

    db_organization = crud_organization.create(db, obj_in=org_data)
    return db_organization


@router.get("/organizations")
@require_permission("view_organizations")
def list_organizations(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List organizations accessible to the current user."""
    # Get trackers for the current user
    user_trackers = crud_tracker.get_for_account(db, account_id=current_user.account_id)
    tracker_ids = [t.id for t in user_trackers]

    if not tracker_ids:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    # Get organizations linked to the user's trackers using CRUD layer
    organizations, total = crud_organization.get_for_trackers(
        db,
        tracker_ids=tracker_ids,
        skip=offset,
        limit=limit,
        account_id=current_user.account_id,
    )

    # Convert SQLAlchemy model objects to dictionaries
    org_dicts = []
    for org in organizations:
        org_dict = {
            "id": org.id,
            "name": org.name,
            "identifier": org.identifier,
            "description": org.description,
            "is_active": org.is_active,
            "tracker_id": org.tracker_id,
            "created_at": org.created_at,
            "updated_at": org.updated_at,
            "settings": org.settings or {},
            "meta_data": org.meta_data or {},
        }
        org_dicts.append(org_dict)

    # Format the response to match the expected structure
    return {"items": org_dicts, "total": total, "limit": limit, "offset": offset}


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
@require_permission("view_organizations")
def get_organization(
    organization_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Organization:
    """Get an organization by ID, ensuring user has access."""
    organization = crud_organization.get(
        db, id=organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    return organization


@router.get(
    "/organizations/by-identifier/{identifier}", response_model=OrganizationResponse
)
@require_permission("view_organizations")
def get_organization_by_identifier(
    identifier: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Organization:
    """Get an organization by identifier, ensuring user has access."""
    organization = crud_organization.get_by_identifier(
        db, identifier=identifier, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    return organization


@router.put("/organizations/{organization_id}", response_model=OrganizationResponse)
@require_permission("edit_organizations")
def update_organization(
    organization_id: str,
    organization_update: OrganizationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Organization:
    """Update an organization, ensuring user has access."""
    ensure_permission_in_oss(db, current_user, "edit_organizations")
    organization = crud_organization.get(
        db, id=organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Update organization using CRUD operation
    update_data = organization_update.dict(exclude_unset=True)
    updated_organization = crud_organization.update(
        db, db_obj=organization, obj_in=update_data
    )

    return updated_organization


@router.delete("/organizations/{organization_id}", status_code=204)
@require_permission("delete_organizations")
def delete_organization(
    organization_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """Delete an organization, ensuring user has access."""
    ensure_permission_in_oss(db, current_user, "delete_organizations")
    organization = crud_organization.get(
        db, id=organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Delete the organization
    crud_organization.delete(db, id=organization_id)
