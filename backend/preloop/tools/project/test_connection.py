"""Test connection to an issue tracker."""

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from preloop.tools.base import Context
from preloop.sync.trackers.factory import create_tracker_client
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import CRUDProject
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project

logger = logging.getLogger(__name__)


class ProjectInfo(BaseModel):
    """Project information."""

    id: int
    name: str
    identifier: str


class ConnectionResult(BaseModel):
    """Tracker connection test result."""

    connected: bool
    message: str
    rate_limit: Optional[Dict[str, Any]] = None
    server_info: Optional[Dict[str, Any]] = None
    rate_limit: Optional[Dict[str, Any]] = None
    server_info: Optional[Dict[str, Any]] = None


class TestConnectionResponse(BaseModel):
    """Response model for test_connection tool."""

    project: ProjectInfo
    connection_results: Dict[str, ConnectionResult]


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str
    message: str


async def verify_connection(
    organization: str, project: str, tracker: Optional[str] = None, ctx: Context = None
) -> Dict[str, Any]:
    """Test connectivity to configured issue trackers.

    Args:
        organization: Organization identifier
        project: Project identifier
        tracker: Optional specific tracker to test (tests all if not specified)
        ctx: Optional MCP context

    Returns:
        Connection status for each tracker
    """
    # Get database session
    db = next(get_db())

    try:
        # Log operation if context is available
        if ctx:
            await ctx.info(
                f"Testing connection for project {project} in organization {organization}"
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

        # Test connection to each tracker
        results = {}
        trackers_to_test = [tracker] if tracker else list(tracker_settings.keys())

        # For each tracker configuration
        for tracker_name in trackers_to_test:
            # If the tracker is not configured, skip it
            if tracker_name not in tracker_settings:
                results[tracker_name] = ConnectionResult(
                    connected=False,
                    message=f"Tracker '{tracker_name}' is not configured for this project",
                )
                continue

            try:
                if ctx:
                    await ctx.info(f"Testing connection to {tracker_name}")

                # Get the tracker configuration from tracker_settings
                tracker_config = tracker_settings[tracker_name]

                # Create a tracker client based on tracker type
                # Handles all supported trackers: github, gitlab, jira
                tracker_client = await create_tracker_client(
                    tracker_type=tracker_name,
                    tracker_id=proj.tracker_id,
                    api_key=tracker_config["api_key"],
                    connection_details=tracker_config,
                )

                if not tracker_client:
                    results[tracker_name] = ConnectionResult(
                        connected=False,
                        message=f"Failed to create client for tracker '{tracker_name}'",
                    )
                    continue

                # Test the connection
                connection_result = await tracker_client.test_connection()

                # Store the result
                results[tracker_name] = ConnectionResult(
                    connected=connection_result.connected,
                    message=connection_result.message,
                    rate_limit=connection_result.rate_limit,
                    server_info=connection_result.server_info,
                )

            except Exception as e:
                logger.error(
                    f"Error testing connection to {tracker_name}: {type(e).__name__}"
                )
                results[tracker_name] = ConnectionResult(
                    connected=False, message=f"Error testing connection: {str(e)}"
                )

        # Return the result
        return TestConnectionResponse(
            project=ProjectInfo(id=proj.id, name=proj.name, identifier=proj.identifier),
            connection_results=results,
        ).model_dump()

    finally:
        db.close()
