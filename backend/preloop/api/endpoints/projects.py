"""Endpoints for managing projects."""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_accessible_projects
from preloop.utils.permissions import require_permission

from preloop.schemas.project import (
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TestConnectionRequest,
    TestConnectionResponse,
)
from preloop.sync.trackers import create_tracker_client
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import CRUDProject
from preloop.models.crud.tracker import CRUDTracker
from preloop.models.crud.issue import CRUDIssue
from preloop.models.crud.embedding import CRUDEmbeddingModel, CRUDIssueEmbedding
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.models.models.issue import Issue
from preloop.models.models.issue import IssueEmbedding, EmbeddingModel

logger = logging.getLogger(__name__)
router = APIRouter()
crud_project = CRUDProject(Project)
crud_organization = CRUDOrganization(Organization)
crud_tracker = CRUDTracker(Tracker)
crud_issue = CRUDIssue(Issue)
crud_issue_embedding = CRUDIssueEmbedding(IssueEmbedding)
crud_embedding_model = CRUDEmbeddingModel(EmbeddingModel)


@router.post("/projects", response_model=ProjectResponse, status_code=201)
@require_permission("create_projects")
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Create a new project, ensuring user has access to the organization."""
    # Check if organization exists
    organization = crud_organization.get(
        db, id=project.organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Check if project with this identifier already exists in the organization
    existing_project = crud_project.get_by_identifier(
        db, organization_id=project.organization_id, identifier=project.identifier
    )
    if existing_project:
        raise HTTPException(
            status_code=400,
            detail=f"Project with identifier '{project.identifier}' already exists in this organization",
        )

    # Create new project using CRUD operation
    # Note: Adapter code to handle the tracker_configurations from the old model
    # In SpaceModels, we have tracker_settings instead
    project_data = {
        "id": str(uuid.uuid4()),
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings or {},
        "tracker_settings": project.tracker_configurations or {},
        "meta_data": {},
    }

    db_project = crud_project.create(db, obj_in=project_data)

    # Convert datetime objects to ISO format strings
    return {
        "id": db_project.id,
        "name": db_project.name,
        "identifier": db_project.identifier,
        "description": db_project.description,
        "organization_id": db_project.organization_id,
        "settings": db_project.settings,
        "tracker_configurations": db_project.tracker_settings,
        "created_at": db_project.created_at,
        "updated_at": db_project.updated_at,
    }


@router.get("/projects", response_model=List[ProjectResponse])
@require_permission("view_projects")
def list_projects(
    organization_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> List[dict]:
    """List projects accessible to the current user, applying scope rules and optionally filtered by organization."""
    # Use get_accessible_projects which applies TrackerScopeRule filtering
    accessible_projects = get_accessible_projects(
        db=db,
        current_user=current_user,
        project_ids=None,  # Get all accessible projects
    )

    # Apply organization filter if provided
    if organization_id:
        # Check organization access first
        organization = crud_organization.get(
            db, id=organization_id, account_id=current_user.account_id
        )
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Filter to only projects in this organization
        accessible_projects = [
            p for p in accessible_projects if p.organization_id == organization_id
        ]

    # Apply pagination
    total = len(accessible_projects)
    paginated_projects = accessible_projects[offset : offset + limit]

    # Convert datetime objects to ISO format strings
    result = []
    for project in paginated_projects:
        result.append(
            {
                "id": project.id,
                "name": project.name,
                "identifier": project.identifier,
                "description": project.description,
                "organization_id": project.organization_id,
                "settings": project.settings,
                "tracker_configurations": project.tracker_settings,
                "created_at": project.created_at.isoformat(),
                "updated_at": project.updated_at.isoformat(),
            }
        )

    return result


@router.get("/organizations/{organization_id}/projects")
@require_permission("view_projects")
def list_organization_projects(
    organization_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all projects for an organization, applying scope rules and ensuring user has access."""
    # Check if organization exists
    organization = crud_organization.get(
        db, id=organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Use get_accessible_projects which applies TrackerScopeRule filtering
    accessible_projects = get_accessible_projects(
        db=db,
        current_user=current_user,
        project_ids=None,
    )

    # Filter to only projects in this organization
    org_projects = [
        p for p in accessible_projects if p.organization_id == organization_id
    ]

    # Apply pagination
    total = len(org_projects)
    paginated_projects = org_projects[offset : offset + limit]

    # Convert SQLAlchemy model objects to dictionaries
    project_dicts = []
    for project in paginated_projects:
        project_dict = {
            "id": project.id,
            "name": project.name,
            "identifier": project.identifier,
            "description": project.description,
            "is_active": project.is_active,
            "organization_id": project.organization_id,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "settings": project.settings or {},
            "tracker_settings": project.tracker_settings or {},
            "meta_data": project.meta_data or {},
        }
        project_dicts.append(project_dict)

    # Format the response to match the expected structure
    return {"items": project_dicts, "total": total, "limit": limit, "offset": offset}


@router.get("/projects/{project_id}", response_model=ProjectResponse)
@require_permission("view_projects")
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Get a project by ID, ensuring user has access."""
    project = crud_project.get(db, id=project_id, account_id=current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert datetime objects to ISO format strings
    return {
        "id": project.id,
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings,
        "tracker_configurations": project.tracker_settings,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


@router.get(
    "/organizations/{organization_id}/projects/{identifier}",
    response_model=ProjectResponse,
)
@require_permission("view_projects")
def get_project_by_identifier(
    organization_id: str,
    identifier: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Get a project by organization ID and project identifier, ensuring user has access."""
    # Check organization access first
    organization = crud_organization.get(
        db, id=organization_id, account_id=current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Now get the project within the authorized organization using slug or identifier
    project = crud_project.get_by_slug_or_identifier(
        db, organization_id=organization_id, slug_or_identifier=identifier
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert datetime objects to ISO format strings
    return {
        "id": project.id,
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings,
        "tracker_configurations": project.tracker_settings,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


@router.put("/projects/{project_id}", response_model=ProjectResponse)
@require_permission("edit_projects")
def update_project(
    project_id: str,
    project_update: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Update a project, ensuring user has access."""
    project = crud_project.get(db, id=project_id, account_id=current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update project using CRUD operation
    # Note: Handle potential field name differences (e.g., tracker_configurations)
    update_data = project_update.dict(exclude_unset=True)

    # If tracker_configurations is present, map it to tracker_settings
    if "tracker_configurations" in update_data:
        update_data["tracker_settings"] = update_data.pop("tracker_configurations")

    updated_project = crud_project.update(db, db_obj=project, obj_in=update_data)

    # Convert datetime objects to ISO format strings
    return {
        "id": updated_project.id,
        "name": updated_project.name,
        "identifier": updated_project.identifier,
        "description": updated_project.description,
        "organization_id": updated_project.organization_id,
        "settings": updated_project.settings,
        "tracker_configurations": updated_project.tracker_settings,
        "created_at": updated_project.created_at.isoformat(),
        "updated_at": updated_project.updated_at.isoformat(),
    }


@router.delete("/projects/{project_id}", status_code=204)
@require_permission("delete_projects")
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """Delete a project, ensuring user has access."""
    project = crud_project.get(db, id=project_id, account_id=current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete the project
    crud_project.delete(db, id=project_id)


@router.post("/projects/test-connection", response_model=TestConnectionResponse)
@require_permission("view_projects")
async def test_project_connection(
    request: TestConnectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TestConnectionResponse:
    """Test the connection to an issue tracker for a project, ensuring user has access."""
    try:
        # Resolve organization
        organization_id = request.organization
        if len(organization_id) == 36:  # Simple UUID check
            organization = crud_organization.get(
                db, id=organization_id, account_id=current_user.account_id
            )
        else:
            organization = crud_organization.get_by_identifier(
                db, identifier=organization_id, account_id=current_user.account_id
            )

        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Resolve project
        project_id = request.project
        if len(project_id) == 36:  # Simple UUID check
            project = crud_project.get(
                db, id=project_id, account_id=current_user.account_id
            )
        else:
            project = crud_project.get_by_identifier(
                db, organization_id=organization.id, identifier=project_id
            )

        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        # Get tracker from organization
        tracker = organization.tracker
        if not tracker:
            return TestConnectionResponse(
                success=False,
                message="Organization has no associated tracker.",
                details={},
            )

        # Determine the tracker type and config
        tracker_type = tracker.tracker_type
        connection_details = project.tracker_settings or {}
        if tracker.url:
            connection_details["url"] = tracker.url
        if tracker.connection_details:
            for key, value in tracker.connection_details.items():
                if key not in connection_details:
                    connection_details[key] = value

        # Create the tracker client
        try:
            tracker_client = await create_tracker_client(
                tracker_type=tracker_type,
                tracker_id=str(tracker.id),
                api_key=tracker.resolved_api_key,
                connection_details=connection_details,
            )
            if not tracker_client:
                raise ValueError("Unsupported tracker type or configuration error")
        except Exception as e:
            logger.error(f"Error creating tracker client: {e}")
            return TestConnectionResponse(
                success=False,
                message="Error creating tracker client",
                details={"error": str(e)},
            )

        # Test the connection
        try:
            connection_result = await tracker_client.test_connection()

            # Return the connection result
            return TestConnectionResponse(
                success=connection_result.success,
                message=connection_result.message,
                details=connection_result.details,
            )
        except Exception as e:
            logger.error(f"Error testing connection: {e}")
            return TestConnectionResponse(
                success=False,
                message="Error testing connection",
                details={"error": str(e)},
            )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error in test_project_connection: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error testing connection: {str(e)}"
        )
