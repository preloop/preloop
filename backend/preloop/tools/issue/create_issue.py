"""Create an issue in an issue tracker."""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from preloop.tools.base import Context
from preloop.schemas.tracker_models import IssueCreate
from preloop.sync.trackers.factory import create_tracker_client
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import CRUDProject
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project

logger = logging.getLogger(__name__)


class OrganizationInfo(BaseModel):
    """Organization information."""

    id: int
    name: str
    identifier: str


class ProjectInfo(BaseModel):
    """Project information."""

    id: int
    name: str
    identifier: str


class IssueInfo(BaseModel):
    """Issue information."""

    id: str
    key: Optional[str] = None
    title: str
    description: Optional[str] = None
    url: Optional[str] = None
    source: str
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    labels: Optional[List[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    custom_fields: Optional[Dict[str, Any]] = None


class CreateIssueResponse(BaseModel):
    """Response model for create_issue tool."""

    issue: IssueInfo
    tracker: str
    project: ProjectInfo
    organization: OrganizationInfo


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str
    message: str


async def create_issue(
    organization: str,
    project: str,
    title: str,
    description: str,
    tracker: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
    assignee: Optional[str] = None,
    custom_fields: Optional[Dict[str, Any]] = None,
    check_duplicates: bool = True,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Create a new issue in the specified tracker.

    Args:
        organization: Organization identifier
        project: Project identifier
        title: Issue title
        description: Issue description
        tracker: Optional specific tracker to create the issue in
        status: Optional initial issue status
        priority: Optional issue priority
        labels: Optional issue labels
        assignee: Optional initial assignee
        custom_fields: Optional tracker-specific custom fields
        check_duplicates: Whether to check for potential duplicates
        ctx: Optional MCP context

    Returns:
        Information about the created issue
    """
    # Get database session
    db = next(get_db())

    try:
        # Log operation if context is available
        if ctx:
            await ctx.info(
                f"Creating issue '{title}' in project {project}, organization {organization}"
            )

        # Initialize CRUD objects
        crud_organization = CRUDOrganization(Organization)
        crud_project = CRUDProject(Project)

        # Get organization using CRUD operations
        org = crud_organization.get_by_identifier(db, identifier=organization)
        if not org or not org.is_active:
            return ErrorResponse(
                error="not_found", message=f"Organization '{organization}' not found"
            ).model_dump()

        # Get project using CRUD operations
        proj = crud_project.get_by_identifier(
            db, organization_id=org.id, identifier=project
        )
        if not proj or not proj.is_active:
            return ErrorResponse(
                error="not_found",
                message=f"Project '{project}' not found in organization '{organization}'",
            ).model_dump()

        # In SpaceModels, we use tracker_settings instead of tracker_configurations
        tracker_settings = proj.tracker_settings or {}

        # If no tracker settings, return an error
        if not tracker_settings:
            return ErrorResponse(
                error="no_trackers",
                message=f"Project '{project}' has no configured trackers",
            ).model_dump()

        # Determine which tracker to use
        if tracker:
            # Use the specified tracker
            if tracker not in tracker_settings:
                return ErrorResponse(
                    error="tracker_not_found",
                    message=f"Tracker '{tracker}' is not configured for this project",
                ).model_dump()
            tracker_to_use = tracker
        else:
            # Use the first available tracker
            tracker_to_use = list(tracker_settings.keys())[0]

        # Log the tracker being used
        if ctx:
            await ctx.info(f"Using tracker: {tracker_to_use}")

        # Create issue data
        issue_data = IssueCreate(
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=assignee,
            labels=labels,
            custom_fields=custom_fields,
        )

        try:
            # Get the tracker configuration from tracker_settings
            tracker_config = tracker_settings[tracker_to_use]

            # Create a tracker client
            tracker_client = await create_tracker_client(
                tracker_type=tracker_to_use,
                tracker_id=proj.tracker_id,
                api_key=tracker_config["api_key"],
                connection_details=tracker_config,
            )

            if not tracker_client:
                return ErrorResponse(
                    error="client_creation_failed",
                    message=f"Failed to create client for tracker '{tracker_to_use}'",
                ).model_dump()

            # Create the issue
            issue = await tracker_client.create_issue(
                project_key=proj.identifier,
                issue_data=issue_data,
            )

            # Convert issue to dictionary and add source
            issue_dict = issue.model_dump()
            issue_dict["source"] = tracker_to_use

            # Flatten nested objects for IssueInfo
            if issue.status:
                issue_dict["status"] = issue.status.name
            if issue.priority:
                issue_dict["priority"] = issue.priority.name
            if issue.assignee:
                issue_dict["assignee"] = issue.assignee.name
            if issue.created_at:
                issue_dict["created_at"] = issue.created_at.isoformat()
            if issue.updated_at:
                issue_dict["updated_at"] = issue.updated_at.isoformat()

            # Return using the response model
            return CreateIssueResponse(
                issue=IssueInfo(**issue_dict),
                tracker=tracker_to_use,
                project=ProjectInfo(
                    id=proj.id, name=proj.name, identifier=proj.identifier
                ),
                organization=OrganizationInfo(
                    id=org.id, name=org.name, identifier=org.identifier
                ),
            ).model_dump()

        except Exception as e:
            logger.error(
                f"Error creating issue in {tracker_to_use}: {type(e).__name__}"
            )
            return ErrorResponse(
                error="creation_failed", message=f"Error creating issue: {str(e)}"
            ).model_dump()

    finally:
        db.close()
