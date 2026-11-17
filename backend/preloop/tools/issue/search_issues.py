"""Search issues across issue trackers."""

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from preloop.tools.base import Context
from preloop.schemas.tracker_models import IssueFilter
from preloop.sync.trackers.factory import create_tracker_client
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import CRUDProject
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project

logger = logging.getLogger(__name__)


class ProjectInfo(BaseModel):
    """Project information in search response."""

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


class TrackerError(BaseModel):
    """Error information for a tracker."""

    error: str
    message: str


class TrackerResult(BaseModel):
    """Search results from a tracker."""

    issues: List[IssueInfo]
    total_count: int


class SearchResponse(BaseModel):
    """Response model for search_issues tool."""

    project: ProjectInfo
    query: str
    results_by_tracker: Dict[str, Any]  # Can be TrackerResult or TrackerError
    combined_results: List[Dict[str, Any]]
    total_results: int


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str
    message: str


async def search_issues(
    organization: str,
    project: str,
    query: str,
    limit: int = 10,
    trackers: Optional[List[str]] = None,
    status: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    updated_after: Optional[str] = None,
    updated_before: Optional[str] = None,
    assigned_to: Optional[str] = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Search for issues across trackers.

    Args:
        organization: Organization identifier
        project: Project identifier
        query: Search query
        limit: Maximum number of results (default: 10)
        trackers: Optional specific trackers to search
        status: Optional filter by issue status
        labels: Optional filter by issue labels
        created_after: Optional filter by creation date (ISO format)
        created_before: Optional filter by creation date (ISO format)
        updated_after: Optional filter by update date (ISO format)
        updated_before: Optional filter by update date (ISO format)
        assigned_to: Optional filter by assignee
        ctx: Optional MCP context

    Returns:
        Search results from each tracker
    """
    # Get database session
    db = next(get_db())

    try:
        # Log operation if context is available
        if ctx:
            await ctx.info(
                f"Searching for '{query}' in project {project}, organization {organization}"
            )

        # Initialize CRUD objects
        crud_organization = CRUDOrganization(Organization)
        crud_project = CRUDProject(Project)

        org: Optional[Organization] = None
        proj: Optional[Project] = None
        possible_projects: List[Project] = []

        if organization:
            # --- Organization is specified ---
            org = crud_organization.get_by_identifier(db, identifier=organization)
            if not org or not org.is_active:
                return ErrorResponse(
                    error="not_found",
                    message=f"Organization '{organization}' not found or inactive.",
                ).model_dump()

            # Search within the specified organization
            projects_by_slug_id = crud_project.get_by_slug_or_identifier(
                db, organization_id=org.id, slug_or_identifier=project
            )
            projects_by_name = crud_project.get_by_name(
                db, organization_id=org.id, name=project
            )

            # Combine results (should ideally be 0 or 1 unique project)
            combined_results = {
                p.id: p for p in projects_by_slug_id + projects_by_name
            }.values()
            active_projects = [p for p in combined_results if p.is_active]

            if len(active_projects) == 1:
                proj = active_projects[0]
            elif len(active_projects) == 0:
                return ErrorResponse(
                    error="not_found",
                    message=f"Project '{project}' not found or inactive within organization '{organization}'.",
                ).model_dump()
            else:  # Should not happen with org_id specified due to constraints, but handle defensively
                return ErrorResponse(
                    error="multiple_matches",
                    message=f"Multiple active projects found for '{project}' within organization '{organization}'. This should not happen.",
                ).model_dump()

        else:
            # --- No organization specified ---
            projects_by_slug_id = crud_project.get_by_slug_or_identifier(
                db, slug_or_identifier=project
            )
            projects_by_name = crud_project.get_by_name(db, name=project)

            # Combine results and filter for active projects
            combined_results = {
                p.id: p for p in projects_by_slug_id + projects_by_name
            }.values()
            active_projects = [p for p in combined_results if p.is_active]

            if not active_projects:
                return ErrorResponse(
                    error="not_found",
                    message=f"Project '{project}' not found or inactive across all organizations.",
                ).model_dump()

            # Sort by updated_at (descending) to find the most recently updated
            active_projects.sort(key=lambda p: p.updated_at, reverse=True)
            proj = active_projects[0]  # Select the most recently updated

            # Get the organization for the selected project
            org = crud_organization.get(db, id=proj.organization_id)
            if not org or not org.is_active:
                # This case is unlikely if the project was found, but handle defensively
                return ErrorResponse(
                    error="internal_error",
                    message=f"Organization for the selected project '{proj.name}' not found or inactive.",
                ).model_dump()

        # --- Proceed with the found project and organization ---
        if not proj or not org:
            # Should be caught above, but final safety check
            return ErrorResponse(
                error="internal_error",
                message="Failed to determine project or organization.",
            ).model_dump()

        # In SpaceModels, we use tracker_settings instead of tracker_configurations
        tracker_settings = proj.tracker_settings or {}

        # If no tracker settings, return an error
        if not tracker_settings:
            return ErrorResponse(
                error="no_trackers",
                message=f"Project '{project}' has no configured trackers",
            ).model_dump()

        # Determine which trackers to search
        trackers_to_search = trackers if trackers else list(tracker_settings.keys())

        if ctx:
            await ctx.info(f"Searching in trackers: {', '.join(trackers_to_search)}")

        # Create an issue filter from the parameters
        filter_params = IssueFilter(
            query=query,
            status=status,
            labels=labels,
            created_after=created_after,
            created_before=created_before,
            updated_after=updated_after,
            updated_before=updated_before,
            assigned_to=assigned_to,
        )

        # Search issues in each tracker
        results = {}
        all_issues = []

        for tracker in trackers_to_search:
            # If the tracker is not configured, skip it
            if tracker not in tracker_settings:
                results[tracker] = TrackerError(
                    error="not_configured",
                    message=f"Tracker '{tracker}' is not configured for this project",
                ).model_dump()
                continue

            try:
                if ctx:
                    await ctx.info(f"Searching in {tracker}")

                # Get the tracker configuration
                tracker_config = tracker_settings[tracker]

                # Create a tracker client
                tracker_client = await create_tracker_client(
                    tracker_type=tracker,
                    tracker_id=proj.tracker_id,
                    api_key=tracker_config["api_key"],
                    connection_details=tracker_config,
                )

                if not tracker_client:
                    results[tracker] = TrackerError(
                        error="client_creation_failed",
                        message=f"Failed to create client for tracker '{tracker}'",
                    ).model_dump()
                    continue

                # Search for issues
                issues, total_count = await tracker_client.search_issues(
                    project_key=proj.identifier,
                    filter_params=filter_params,
                    limit=limit,
                    offset=0,
                )

                # Convert issues to dictionaries
                issue_infos = []
                for issue in issues:
                    issue_dict = issue.model_dump()
                    # Add a source field to indicate which tracker this came from
                    issue_dict["source"] = tracker

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

                    issue_infos.append(IssueInfo(**issue_dict))
                    all_issues.append(issue_dict)

                # Store the results
                results[tracker] = TrackerResult(
                    issues=issue_infos, total_count=total_count
                ).model_dump()

                if ctx:
                    await ctx.info(f"Found {len(issues)} issues in {tracker}")

            except Exception as e:
                logger.error(f"Error searching {tracker}: {type(e).__name__}")
                results[tracker] = TrackerError(
                    error="search_failed", message=f"Error searching issues: {str(e)}"
                ).model_dump()

        # Sort all issues by relevance or date
        # For now, we just sort by updated_at
        all_issues.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        # Limit the number of results
        all_issues = all_issues[:limit]

        if ctx:
            await ctx.info(f"Search completed with {len(all_issues)} total results")

        # Return using the response model
        return SearchResponse(
            project=ProjectInfo(id=proj.id, name=proj.name, identifier=proj.identifier),
            query=query,
            results_by_tracker=results,
            combined_results=all_issues,
            total_results=len(all_issues),
        ).model_dump()

    finally:
        db.close()
