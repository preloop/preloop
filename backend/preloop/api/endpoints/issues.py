"""Endpoints for managing issues across trackers."""

import logging
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from datetime import datetime
from preloop.api.common import get_tracker_client
from preloop.schemas.issue import (
    IssueCreate,
    IssueResponse,
    IssueUpdate,
)
from preloop.schemas.tracker_models import (
    IssueUpdate as TrackerIssueUpdate,
    IssueCreate as TrackerIssueCreate,
)
from preloop.models.models.user import User

from preloop.models.crud import (
    CRUDComment,
    CRUDIssue,
    CRUDOrganization,
    CRUDProject,
    CRUDTracker,
    crud_embedding_model,
    crud_issue_embedding,
)
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.comment import Comment
from preloop.models.models.issue import Issue
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.api.auth import get_current_active_user
from preloop.services.aux_model_retry import call_with_aux_retry_async
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.utils.permissions import require_permission
from preloop.config import settings

# Initialize CRUD operations
crud_comment = CRUDComment(Comment)
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)
crud_issue = CRUDIssue(Issue)
crud_tracker = CRUDTracker(Tracker)


def extract_label_strings(labels_data) -> List[str]:
    """
    Extract label strings from label data which might be objects or strings.

    Args:
        labels_data: Can be a list of label objects (dicts with 'title'/'name'),
                    strings, or a mix

    Returns:
        List of label strings
    """
    labels = []
    if isinstance(labels_data, list):
        for label in labels_data:
            if isinstance(label, dict):
                # Extract title or name from label object
                labels.append(label.get("title") or label.get("name") or str(label))
            elif isinstance(label, str):
                labels.append(label)
            else:
                labels.append(str(label))
    return labels


# Define the filter class for issue searching
class IssueFilter:
    def __init__(self, query: str, limit: int = 10):
        self.query = query
        self.limit = limit
        self.status = None
        self.labels = None
        self.assignee = None


logger = logging.getLogger(__name__)
router = APIRouter()


# API Endpoints


@router.get("/issues/search", response_model=List[IssueResponse])
@require_permission("view_issues")
async def search_issues(
    organization_id: Optional[str] = Query(None, description="Organization ID (UUID)"),
    organization: Optional[str] = Query(None, description="Organization name"),
    project_id: Optional[str] = Query(None, description="Project ID (UUID)"),
    project: Optional[str] = Query(None, description="Project name"),
    query: Optional[str] = Query("", description="Search query text"),
    limit: int = Query(
        10, ge=1, le=100, description="Maximum number of issues to return"
    ),
    search_type: str = Query(
        "fulltext",
        enum=["fulltext", "similarity"],
        description="Type of search to perform ('fulltext' or 'similarity')",
    ),
    status: Optional[str] = Query(None, description="Filter by issue status"),
    labels: Optional[str] = Query(
        None, description="Filter by comma-separated list of labels"
    ),
    assignee: Optional[str] = Query(None, description="Filter by assignee"),
    priority: Optional[str] = Query(
        None, description="Filter by issue priority"
    ),  # Added
    last_updated_before: Optional[datetime] = Query(
        None,
        description="Filter issues updated before this timestamp (ISO 8601 format)",
    ),  # Added
    last_updated_after: Optional[datetime] = Query(
        None, description="Filter issues updated after this timestamp (ISO 8601 format)"
    ),  # Added
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Search for issues within a project using text query and optional similarity search.
    Requires authentication and checks user access.
    """
    try:
        # Resolve organization and project using either ID, name, or identifier

        user_trackers = crud_tracker.get_for_account(
            db, account_id=current_user.account_id
        )
        tracker_ids = [t.id for t in user_trackers]

        if not tracker_ids:
            return []

        # Get Issues linked to the user's trackers using CRUD layer
        issues = crud_issue.get_for_trackers(
            db, tracker_ids=tracker_ids, account_id=current_user.account_id
        )
        # Process organization parameters
        org = None
        org_id = None
        if organization_id:
            org = crud_organization.get(
                db, id=organization_id, account_id=current_user.account_id
            )
            if org:
                org_id = org.id
        elif organization:
            org = crud_organization.get_by_name(
                db, name=organization, account_id=current_user.account_id
            )
            if org:
                org_id = org.id

        # Process project parameters
        proj = None
        if project_id:
            proj = crud_project.get(
                db, id=project_id, account_id=current_user.account_id
            )
        elif project:
            # If we have an organization, use it to narrow down the project search
            if org_id:
                # get_by_name returns Optional[Project] when org_id is specified
                proj = crud_project.get_by_name(
                    db,
                    name=project,
                    organization_id=org_id,
                    account_id=current_user.account_id,
                )
                # proj will be None if no project is found by that name in the org
            else:
                # --- No organization specified: Search globally by slug/id AND name ---
                logger.warning(
                    f"API: No organization specified. Searching globally for project '{project}'"
                )  # Use warning
                # Fetch single project by slug/id, returns Project or None
                project_by_slug_id = crud_project.get_by_slug_or_identifier(
                    db, slug_or_identifier=project, account_id=current_user.account_id
                )
                logger.warning(
                    f"API: Found project matching slug/identifier '{project}' globally? {'Yes' if project_by_slug_id else 'No'}"
                )  # Use warning

                # Fetch single project by name, returns Project or None
                project_by_name = crud_project.get_by_name(
                    db, name=project, account_id=current_user.account_id
                )
                logger.warning(
                    f"API: Found project matching name '{project}' globally? {'Yes' if project_by_name else 'No'}"
                )  # Use warning

                # Combine results and filter for active projects
                # Use a dictionary to handle potential duplicates from searching different fields
                combined_projects_dict: Dict[str, Project] = {}
                if project_by_slug_id:
                    combined_projects_dict[project_by_slug_id.id] = project_by_slug_id
                if project_by_name:  # Add/overwrite if found by name
                    combined_projects_dict[project_by_name.id] = project_by_name
                logger.warning(
                    f"API: Combined unique projects found: {len(combined_projects_dict)}"
                )  # Use warning

                active_projects = [
                    p for p in combined_projects_dict.values() if p.is_active
                ]
                logger.warning(
                    f"API: Active projects found matching '{project}' globally: {len(active_projects)}"
                )  # Use warning

                if not active_projects:
                    proj = None  # No active project found globally
                elif len(active_projects) == 1:
                    proj = active_projects[
                        0
                    ]  # Exactly one active project found globally
                else:
                    # Multiple active projects found globally
                    raise HTTPException(
                        status_code=400,
                        detail=f"Multiple active projects found matching '{project}'. Please specify an organization.",
                    )

        # --- Final Validation ---
        logger.warning(
            f"API: Before final validation: proj is {'set' if proj else 'None'}, project_id='{project_id}', project='{project}'"
        )  # Add log
        # Validate project (if project is specified but not found)
        # The check `if not proj` now correctly handles the case where the list was empty or ambiguity was detected earlier
        if (project_id or project) and not proj:
            # Raise 404 if proj is None after the checks above
            logger.error(
                f"API: Raising 404 because proj is None. project_id='{project_id}', project='{project}'"
            )  # Add log
            raise HTTPException(
                status_code=404, detail=f"Project '{project}' not found."
            )

        # Ensure project belongs to the specified organization, if applicable
        if org and proj and org.id != proj.organization_id:
            # This check remains valid as proj is now a single object or None
            raise HTTPException(
                status_code=400,  # Use 400 as it's a mismatch based on input
                detail=f"Project '{proj.name}' does not belong to organization '{org.identifier}'.",
            )

        # If organization wasn't specified initially, but we found a unique project, get its org
        if not org and proj:
            # Explicitly fetch the organization using CRUD
            logger.warning(
                f"API: Globally found project '{proj.name}' (ID: {proj.id}). Fetching its organization (ID: {proj.organization_id})."
            )
            org = crud_organization.get(
                db, id=proj.organization_id, account_id=current_user.account_id
            )
            if not org:
                # This indicates an orphaned project or data inconsistency
                logger.error(
                    f"API: Found project '{proj.name}' (ID: {proj.id}) but could not find its organization (ID: {proj.organization_id}) in the database."
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Project organization data inconsistent.",
                )
            elif not org.is_active:
                # Found the org, but it's inactive
                logger.warning(
                    f"API: Found project '{proj.name}' (ID: {proj.id}) but its organization '{org.identifier}' (ID: {org.id}) is inactive."
                )
                # Treat as project not found, as the org context is invalid
                raise HTTPException(
                    status_code=404,
                    detail=f"Project '{project}' not found (its organization is inactive).",
                )
            logger.warning(
                f"API: Successfully fetched organization '{org.identifier}' for project '{proj.name}'."
            )
        # Validate access and get tracker client (even if not used directly for DB search).
        # This enforces project selection rules *if a specific project context is resolved*.
        if (
            org and proj
        ):  # Only attempt tracker client validation if both org and project are resolved
            try:
                # Now we are sure org and proj are not None
                await get_tracker_client(org.id, proj.id, db, current_user)
            except HTTPException as e:
                # If get_tracker_client raises an error (e.g., 403 Forbidden due to project exclusion),
                # re-raise it to stop the search for this specific project context.
                raise e
            except Exception as e:
                # Catch potential errors during client creation/validation
                logger.error(
                    f"Error validating tracker access for search (org: {org.id}, proj: {proj.id}): {e}",
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Error validating tracker access for the specified project.",
                )
        # If only org is specified (proj is None), or neither org nor proj is specified,
        # the search proceeds based on project_ids_filter derived from org or user's trackers.
        # No specific single-project validation via get_tracker_client is performed in these cases.

        # Create filter object for traditional search
        filter_obj = IssueFilter(query=query, limit=limit)
        if status:
            filter_obj.status = status
        if labels:
            filter_obj.labels = labels.split(",")
        if assignee:
            filter_obj.assignee = assignee

        response_items = []

        # --- Determine Project IDs for Filtering ---
        project_ids_filter: Optional[List[str]] = None
        if proj:
            project_ids_filter = [proj.id]
        elif org:
            # Filter projects by those belonging to accessible trackers for the user
            project_ids_filter = [
                p.id for p in org.projects if p.tracker_id in tracker_ids
            ]
        # If neither proj nor org is set, project_ids_filter remains None,
        # and the CRUD layer will use tracker_ids as a fallback.

        if search_type == "similarity" and query:
            try:
                # Get the active embedding model
                active_models = crud_embedding_model.get_active(db)
                if not active_models:
                    logger.error(
                        "similarity search requested, but no active embedding model found."
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="similarity search cannot be performed: No active embedding model configured.",
                    )
                model = active_models[0]
                model_id = model.id

                async def _embed_query() -> list:
                    return crud_issue_embedding._generate_embedding_vector(query, model)

                query_vector = await call_with_aux_retry_async(
                    _embed_query,
                    operation_name="issue_similarity_query_embedding",
                    provider=getattr(model, "provider", None),
                )
                # Find similar issues using similarity search
                similar_issues = crud_issue_embedding.similarity_search(
                    db=db,
                    model_id=model_id,
                    query_vector=query_vector,
                    limit=limit,
                    project_ids=project_ids_filter,  # Pass resolved project IDs
                    tracker_ids=tracker_ids,  # Keep as fallback if project_ids_filter is None
                    status=status,  # Pass status filter
                    labels=filter_obj.labels,  # Pass labels filter (ensure filter_obj is populated)
                    priority=priority,  # Pass priority filter
                    assignee=assignee,  # Pass assignee filter
                    last_updated_before=last_updated_before,  # Pass date filter
                    last_updated_after=last_updated_after,  # Pass date filter
                    embedding_type="issue",
                )

                # Post-fetch filtering is removed as it's now handled by the CRUD layer.
                # The results from similarity_search are already filtered.

                # Convert directly to IssueResponse, limit is handled by CRUD
                for (
                    issue,
                    score,
                ) in similar_issues:  # similar_issues already respects limit
                    issue_project = crud_project.get(
                        db, id=issue.project_id, account_id=current_user.account_id
                    )  # Still need project for response model
                    project_name = issue_project.name if issue_project else None
                    organization_name = None
                    if issue_project:
                        issue_org = crud_organization.get(
                            db,
                            id=issue_project.organization_id,
                            account_id=current_user.account_id,
                        )
                        if issue_org:
                            organization_name = issue_org.name
                    metadata_dict = dict(issue.meta_data) if issue.meta_data else {}
                    external_url = metadata_dict.get("url") or issue.external_url
                    # Determine response ID based on project slug
                    response_id = issue.external_id or str(
                        issue.id
                    )  # Fallback to internal ID string
                    if issue_project and issue_project.slug and issue.external_id:
                        response_id = f"{issue_project.slug}#{issue.external_id}"
                    elif issue.external_id:
                        response_id = issue.external_id

                    # Ensure required fields are present (as per task constraints, assume they are)
                    if not issue.external_id:
                        logger.warning(
                            f"Issue {issue.id} missing external_id during similarity search response creation."
                        )
                        continue
                    if not issue.key:
                        logger.warning(
                            f"Issue {issue.id} missing key during similarity search response creation."
                        )
                        continue

                    response_items.append(
                        IssueResponse(
                            id=str(issue.id),  # Use internal DB UUID
                            external_id=issue.external_id,  # Use tracker's external ID
                            key=issue.key,  # Use human-readable key
                            title=issue.title,
                            description=issue.description,
                            status=issue.status,
                            priority=issue.priority,
                            organization=organization_name,
                            project=project_name,
                            project_id=str(issue.project_id),
                            url=external_url
                            or f"{settings.preloop_url}/issues/{issue.id}",  # Use external URL if available
                            created_at=issue.created_at,
                            updated_at=issue.updated_at,
                            meta_data=metadata_dict,
                            labels=extract_label_strings(
                                metadata_dict.get("labels", [])
                            ),
                            assignee=metadata_dict.get("assignee"),
                            score=score,  # Include similarity score
                        )
                    )

            except ModelGatewayAPIError as exc:
                logger.warning(
                    "Similarity search query embedding did not recover: %s", exc
                )
                raise HTTPException(
                    status_code=exc.status_code,
                    detail="Embedding provider is rate limited; retry later.",
                    headers=exc.response_headers(),
                )
            except Exception as e:
                logger.error(f"Error during similarity search: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500,
                    detail="An error occurred during similarity search.",
                )

        elif search_type == "fulltext":
            # Perform traditional full-text search
            try:
                from sqlalchemy import or_

                # Filter by project/org
                if proj:
                    issues = issues.filter(Issue.project_id == proj.id)
                elif org:
                    project_ids = [p.id for p in org.projects]
                    if project_ids:
                        issues = issues.filter(Issue.project_id.in_(project_ids))
                    else:
                        return []  # Org has no projects

                # Apply text query filter
                if query:
                    search_term = f"%{query}%"
                    issues = issues.filter(
                        or_(
                            Issue.title.ilike(search_term),
                            Issue.description.ilike(search_term),
                        )
                    )

                # Apply status filter
                if status:
                    issues = issues.filter(Issue.status == status)

                # Apply labels/assignee filters directly in the query if possible
                # Note: This example assumes simple JSON structure and might need adjustment
                # based on actual DB capabilities (e.g., using JSONB operators)
                if labels and isinstance(filter_obj.labels, list):
                    # Example for PostgreSQL JSONB containment:
                    # query_builder = query_builder.filter(Issue.meta_data['labels'].contains(filter_obj.labels))
                    # For now, we keep post-fetch filtering for broader compatibility
                    pass
                if assignee:
                    # Example for PostgreSQL JSONB:
                    # query_builder = query_builder.filter(Issue.meta_data['assignee'] == assignee)
                    pass

                # Fetch issues
                issues_db = issues.order_by(Issue.updated_at.desc()).limit(limit).all()

                # Post-fetch filtering (if DB filtering wasn't possible/implemented)
                if labels and isinstance(filter_obj.labels, list):
                    issues_db = [
                        issue
                        for issue in issues_db
                        if issue.meta_data
                        and "labels" in issue.meta_data
                        and all(
                            label in issue.meta_data["labels"]
                            for label in filter_obj.labels
                        )
                    ]
                if assignee:
                    issues_db = [
                        issue
                        for issue in issues_db
                        if issue.meta_data
                        and "assignee" in issue.meta_data
                        and issue.meta_data["assignee"] == assignee
                    ]

                # Convert to IssueResponse
                for issue in issues_db:
                    issue_project = crud_project.get(
                        db, id=issue.project_id, account_id=current_user.account_id
                    )
                    project_name = (
                        issue_project.name if issue_project else "Unknown Project"
                    )
                    organization_name = "Unknown Org"
                    if issue_project:
                        issue_org = crud_organization.get(
                            db,
                            id=issue_project.organization_id,
                            account_id=current_user.account_id,
                        )
                        if issue_org:
                            organization_name = issue_org.name
                    metadata_dict = dict(issue.meta_data) if issue.meta_data else {}
                    external_url = metadata_dict.get("url") or issue.external_url
                    # Determine response ID based on project slug
                    response_id = issue.external_id or str(
                        issue.id
                    )  # Fallback to internal ID string
                    if issue_project and issue_project.slug and issue.external_id:
                        response_id = f"{issue_project.slug}#{issue.external_id}"
                    elif issue.external_id:
                        response_id = issue.external_id

                    # Ensure required fields are present (as per task constraints, assume they are)
                    if not issue.external_id:
                        logger.warning(
                            f"Issue {issue.id} missing external_id during full-text search response creation."
                        )
                        # Decide handling: skip, error, or default? Skipping for now.
                        continue
                    if not issue.key:
                        logger.warning(
                            f"Issue {issue.id} missing key during full-text search response creation."
                        )
                        # Decide handling: skip, error, or default? Skipping for now.
                        continue

                    response_items.append(
                        IssueResponse(
                            id=str(issue.id),  # Use internal DB UUID
                            external_id=issue.external_id,  # Use tracker's external ID
                            key=issue.key,  # Use human-readable key
                            title=issue.title,
                            description=issue.description,
                            status=issue.status,
                            priority=issue.priority,
                            organization=organization_name,
                            project=project_name,
                            project_id=str(issue.project_id),
                            url=external_url
                            or f"https://{settings.preloop_url}/issues/{issue.id}",  # Use external URL if available
                            created_at=issue.created_at,
                            updated_at=issue.updated_at,
                            meta_data=metadata_dict,
                            labels=extract_label_strings(
                                metadata_dict.get("labels", [])
                            ),
                            assignee=metadata_dict.get("assignee"),
                            score=0.0,  # No score for full-text search
                        )
                    )

            except Exception as e:
                logger.error(f"Error during full-text search: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail="An error occurred during full-text search."
                )
        elif query:  # Handle case where type is invalid but query exists
            logger.warning(f"Invalid search type specified: {search_type}")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid search type: {search_type}. Must be 'fulltext' or 'similarity'.",
            )
        else:  # No query provided, return empty list or handle as needed
            pass  # Currently returns empty list by default

        return response_items
    except HTTPException:
        # Re-raise specific HTTP exceptions (like the 404 for project not found)
        raise
    except Exception as e:
        # Catch any other unexpected errors
        logger.error(f"Unexpected error searching issues: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error during issue search."
        )


@router.post("/issues", response_model=IssueResponse, status_code=201)
@require_permission("create_issues")
async def create_issue(
    issue: IssueCreate,  # Use the renamed API schema
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> IssueResponse:
    """Create a new issue in a specified project. Requires authentication and checks user access.

    Supports specifying organization/project by:
    - ID (organization_id/project_id)
    - Name (organization_name/project_name)
    - Slug (organization/project)
    """
    try:
        # Resolve organization and project using either ID, name, or identifier
        from preloop.models.crud import crud_organization, crud_project

        org: Optional[Organization] = None
        proj: Optional[Project] = None
        org_id: Optional[str] = None

        # --- Resolve Organization and Project ---
        project_input_identifier = (
            issue.project or issue.project_name
        )  # User provided project string
        org_input_identifier = (
            issue.organization or issue.organization_name
        )  # User provided org string

        if not project_input_identifier:
            raise HTTPException(
                status_code=400,
                detail="Project identifier (project or project_name) is required.",
            )

        # 1. Try with IDs first if provided
        if issue.organization_id:
            org = crud_organization.get(
                db, id=issue.organization_id, account_id=current_user.account_id
            )
            if not org:
                raise HTTPException(
                    status_code=404,
                    detail=f"Organization with ID '{issue.organization_id}' not found.",
                )
            if not org.is_active:
                raise HTTPException(
                    status_code=400, detail=f"Organization '{org.name}' is not active."
                )
            org_id = org.id

        if issue.project_id:
            proj = crud_project.get(
                db, id=issue.project_id, account_id=current_user.account_id
            )
            if not proj:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project with ID '{issue.project_id}' not found.",
                )
            if not proj.is_active:
                raise HTTPException(
                    status_code=400, detail=f"Project '{proj.name}' is not active."
                )
            # If org was found by ID, ensure project belongs to it
            if org_id and proj.organization_id != org_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Project '{proj.name}' (ID: {proj.id}) does not belong to specified organization (ID: {org_id}).",
                )
            # If org wasn't found by ID, infer it from project
            if not org:
                org = crud_organization.get(
                    db, id=proj.organization_id, account_id=current_user.account_id
                )
                if not org:  # Should not happen if DB is consistent
                    raise HTTPException(
                        status_code=500,
                        detail=f"Project '{proj.name}' (ID: {proj.id}) has an invalid organization ID '{proj.organization_id}'.",
                    )
                if not org.is_active:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Organization '{org.name}' for project '{proj.name}' is not active.",
                    )
                org_id = org.id

        # 2. If project or org still not resolved, try with names/slugs
        if not proj or not org:
            if (
                org_input_identifier and not org
            ):  # Org identifier provided, but org not resolved yet
                org = crud_organization.get_by_identifier(
                    db,
                    identifier=org_input_identifier,
                    account_id=current_user.account_id,
                ) or crud_organization.get_by_name(
                    db, name=org_input_identifier, account_id=current_user.account_id
                )
                if not org:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Organization '{org_input_identifier}' not found.",
                    )
                if not org.is_active:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Organization '{org.name}' is not active.",
                    )
                org_id = org.id

            if (
                org_id and not proj
            ):  # Org is resolved (either by ID or name/slug), try to find project within it
                # Try by slug/identifier within the organization
                proj_by_slug = crud_project.get_by_slug_or_identifier(
                    db,
                    organization_id=org_id,
                    slug_or_identifier=project_input_identifier,
                    account_id=current_user.account_id,
                )
                if proj_by_slug and proj_by_slug.is_active:
                    proj = proj_by_slug
                else:
                    # Try by name within the organization
                    proj_by_name = crud_project.get_by_name(
                        db,
                        organization_id=org_id,
                        name=project_input_identifier,
                        account_id=current_user.account_id,
                    )
                    if proj_by_name and proj_by_name.is_active:
                        proj = proj_by_name
                    else:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Active project '{project_input_identifier}' not found in organization '{org.name if org else org_input_identifier}'.",
                        )

            elif (
                not org_id and not proj
            ):  # No org identifier provided, try to find project globally
                # Use the new global search method
                candidate_projects = (
                    crud_project.get_all_active_by_identifier_or_name_globally(
                        db,
                        identifier_or_name=project_input_identifier,
                        account_id=current_user.account_id,
                    )
                )

                if not candidate_projects:
                    raise HTTPException(
                        status_code=404,
                        detail=f"No active project matching '{project_input_identifier}' found in any active organization.",
                    )
                if len(candidate_projects) > 1:
                    # Check if all candidates belong to the same org. If so, it's not ambiguous.
                    # This can happen if a project has a name that matches another project's identifier in the same org.
                    unique_org_ids = {p.organization_id for p in candidate_projects}
                    if len(unique_org_ids) > 1:
                        org_names = list(
                            set(
                                p.organization.name
                                for p in candidate_projects
                                if p.organization
                            )
                        )
                        raise HTTPException(
                            status_code=409,  # Conflict
                            detail=f"Project identifier '{project_input_identifier}' is ambiguous. Found in multiple organizations: {', '.join(org_names)}. Please specify an organization.",
                        )
                    # If all from same org, pick the first one (they should be consistently ordered)
                    proj = candidate_projects[0]
                    org = proj.organization  # Already eager loaded
                    if (
                        not org
                    ):  # Should not happen due to eager loading and DB consistency
                        raise HTTPException(
                            status_code=500,
                            detail="Internal error: Project's organization not loaded.",
                        )
                else:  # Exactly one project found
                    proj = candidate_projects[0]
                    org = proj.organization  # Already eager loaded
                    if not org:  # Should not happen
                        raise HTTPException(
                            status_code=500,
                            detail="Internal error: Project's organization not loaded.",
                        )

        # --- Final Sanity Checks ---
        if not org or not proj:
            # This should ideally be caught by earlier specific checks
            logger.error(
                f"Failed to resolve org or project. Org: {org}, Proj: {proj}, Input Org: {org_input_identifier}, Input Proj: {project_input_identifier}"
            )
            raise HTTPException(
                status_code=500,
                detail="Internal Server Error: Could not resolve project or organization.",
            )

        if not org.is_active:
            raise HTTPException(
                status_code=400, detail=f"Organization '{org.name}' is not active."
            )
        if not proj.is_active:
            raise HTTPException(
                status_code=400, detail=f"Project '{proj.name}' is not active."
            )
        if proj.organization_id != org.id:
            raise HTTPException(
                status_code=400,
                detail=f"Mismatch: Project '{proj.name}' (Org ID: {proj.organization_id}) does not belong to resolved Organization '{org.name}' (Org ID: {org.id}).",
            )

        # Get the tracker client using the resolved IDs, passing the current user for auth check
        tracker_client = await get_tracker_client(org.id, proj.id, db, current_user)

        # Prepare the issue create model using the correct tracker model
        tracker_issue = (
            TrackerIssueCreate(  # Use TrackerIssueCreate from tracker_models
                project=proj.slug or proj.identifier,
                organization_id=proj.organization_id,
                title=issue.title,
                description=issue.description,
                priority=issue.priority,
                assignee=issue.assignee,
                labels=issue.labels,
                # Map API metadata to custom_fields if needed by the tracker base model
                custom_fields=issue.meta_data or None,
            )
        )

        # Create the issue - Pass the project identifier expected by the tracker client
        # For most trackers (Jira, Linear, etc.), use identifier (the external project key)
        # For Git-based trackers (GitHub, GitLab), this will be the repo name
        project_key_for_tracker = proj.slug or proj.identifier
        if not project_key_for_tracker:
            # Fallback or specific logic might be needed if both are missing
            # For now, raise error
            raise HTTPException(
                status_code=500,
                detail="Project identifier/slug is missing for tracker interaction.",
            )

        logger.info(
            f"Creating issue in tracker. Project (DB): id={proj.id}, name='{proj.name}', "
            f"identifier='{proj.identifier}', slug='{proj.slug}'. "
            f"Using project_key_for_tracker='{project_key_for_tracker}'"
        )
        created_issue = await tracker_client.create_issue(
            project_key_for_tracker, tracker_issue
        )

        # --- Save to local database ---
        # Extract necessary IDs and data from the tracker response
        tracker_external_id = str(created_issue.id) if created_issue.id else None
        tracker_key = created_issue.key
        tracker_url = created_issue.url

        # Ensure tracker_id is available
        if not org.tracker_id:
            logger.error(f"Organization {org.id} has no associated tracker_id.")
            raise HTTPException(
                status_code=500,
                detail="Internal configuration error: Missing tracker ID.",
            )

        # Prepare data for database insertion
        issue_data_for_db = {
            "title": created_issue.title,
            "description": created_issue.description,
            "status": created_issue.status.name if created_issue.status else None,
            "priority": created_issue.priority.name if created_issue.priority else None,
            # "assignee": created_issue.assignee.name if created_issue.assignee else None,
            # "labels": created_issue.labels,
            "meta_data": created_issue.custom_fields,  # Map custom_fields to meta_data
            "external_id": tracker_external_id,
            "key": tracker_key,
            "external_url": tracker_url,  # Save the URL from tracker to external_url
            "tracker_id": org.tracker_id,  # Use tracker_id from the organization
            "project_id": proj.id,  # Use internal project UUID
            "created_at": created_issue.created_at,
            "updated_at": created_issue.updated_at,
            "last_updated_external": created_issue.updated_at,  # Also set this
            # Add other relevant fields if the Issue model requires them
        }

        # Create the issue in the database
        try:
            db_issue = crud_issue.create(db=db, obj_in=issue_data_for_db)
            db.commit()  # Commit the transaction
            db.refresh(db_issue)  # Refresh to get DB-generated values like ID
        except Exception as db_exc:
            db.rollback()  # Rollback on error
            logger.error(
                f"Error saving created issue to database: {db_exc}", exc_info=True
            )
            # Consider if we should delete the issue from the tracker here?
            # For now, return an error indicating partial success/failure.
            raise HTTPException(
                status_code=500,
                detail=f"Issue created in tracker ({tracker_key or tracker_external_id}) but failed to save locally: {str(db_exc)}",
            )

        # --- Construct the API Response using the database object ---
        # Ensure all fields required by IssueResponse are present and correctly typed
        response_url = db_issue.external_url or ""  # Use the saved external_url
        if not response_url and created_issue.url:  # Fallback if somehow not saved
            response_url = created_issue.url
        if not response_url:  # Final fallback if tracker didn't provide it
            logger.warning(
                f"No URL available for issue {db_issue.key} (ID: {db_issue.id}) from tracker or DB."
            )
            # Construct a placeholder or leave empty based on requirements. For now, empty.

        # Extract label strings from label objects
        labels = extract_label_strings(db_issue.meta_data.get("labels", []))

        return IssueResponse(
            id=str(db_issue.id),
            external_id=db_issue.external_id,
            key=db_issue.key,
            organization=org.name,
            project=proj.slug,
            project_id=proj.id,  # Add missing project_id
            project_identifier=proj.identifier or proj.slug,
            title=db_issue.title,
            description=db_issue.description,
            status=db_issue.status,
            priority=db_issue.priority,
            assignee=db_issue.meta_data.get("assignee"),
            labels=labels,
            url=response_url,  # Use the resolved URL
            created_at=db_issue.created_at,
            updated_at=db_issue.updated_at,
            meta_data=db_issue.meta_data or {},  # Ensure dict
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error creating issue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating issue: {str(e)}")


@router.get(
    "/issues/{issue_id:path}", response_model=IssueResponse
)  # Added response_model
@require_permission("view_issues")
def get_issue(
    issue_id: str,  # Issue key, Issue ID or external ID
    organization: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get details of a specific issue using its external ID."""
    try:
        # URL decode the issue_id in case it's still encoded
        from urllib.parse import unquote

        issue_id = unquote(issue_id)

        user_trackers = crud_tracker.get_for_account(
            db, account_id=current_user.account_id
        )
        tracker_ids = [t.id for t in user_trackers]

        if not tracker_ids:
            raise HTTPException(status_code=404, detail="No trackers found for user")

        project_slug = None
        project_obj = None
        if "#" in issue_id:
            project_slug, issue_external_id = issue_id.split("#")
        else:
            issue_external_id = issue_id
            project_slug = project

        # Get the project and organization if project_slug is provided
        if project_slug:
            project_obj = crud_project.get_by_slug_or_identifier(
                db, slug_or_identifier=project_slug, account_id=current_user.account_id
            )
            if not project_obj:
                raise HTTPException(status_code=404, detail="Project not found")
            organization = crud_organization.get(
                db, id=project_obj.organization_id, account_id=current_user.account_id
            )
            if not organization:
                raise HTTPException(status_code=404, detail="Organization not found")

        # Build alternative key formats to search for
        alternative_keys = [
            issue_external_id,
            issue_id,
        ]
        if organization and project:
            alternative_keys.append(f"{organization}/{project}#{issue_id}")

        # Find the issue using CRUD layer
        issue = crud_issue.find_by_flexible_identifier(
            db,
            identifier=issue_external_id,
            tracker_ids=tracker_ids,
            project_id=project_obj.id if project_obj else None,
            alternative_keys=alternative_keys,
            account_id=current_user.account_id,
        )

        if not issue:
            # Maybe it was the internal ID? Try that as a fallback.
            issue = crud_issue.get(db, id=issue_id, account_id=current_user.account_id)
            if not issue:
                raise HTTPException(
                    status_code=404, detail="Issue not found by external or internal ID"
                )

        project = crud_project.get(
            db, id=issue.project_id, account_id=current_user.account_id
        )
        if not project:
            # This indicates data inconsistency if the issue exists but project doesn't
            logger.error(
                f"Project with ID {issue.project_id} not found for issue {issue.id}"
            )
            raise HTTPException(status_code=404, detail="Associated project not found")

        organization = crud_organization.get(
            db, id=project.organization_id, account_id=current_user.account_id
        )
        if not organization:
            logger.error(
                f"Organization with ID {project.organization_id} not found for project {project.id}"
            )
            raise HTTPException(
                status_code=404, detail="Associated organization not found"
            )

        # Extract data from JSON fields if available
        meta_data = issue.meta_data or {}
        labels_list = (
            extract_label_strings(meta_data.get("labels", []))
            if isinstance(meta_data, dict)
            else []
        )
        assignee = meta_data.get("assignee") if isinstance(meta_data, dict) else None

        # Determine the URL
        external_url = meta_data.get("url") or issue.external_url
        if not external_url:
            # Basic fallback if external_id exists but no URL found
            external_url = f"https://{settings.preloop_url}/issues/{issue.id}"

        # Fetch comments for this issue
        comments = crud_comment.get_multi_by_issue(
            db, issue_id=issue.id, account_id=current_user.account_id
        )
        comments_list = [
            {
                "id": str(comment.id),
                "external_id": comment.external_id,
                "body": comment.body,
                "created_at": comment.created_at.isoformat()
                if comment.created_at
                else None,
                "updated_at": comment.updated_at.isoformat()
                if comment.updated_at
                else None,
            }
            for comment in comments
        ]

        # Convert to IssueResponse model
        if issue.key:
            final_response_key = issue.key
        elif (
            project and project.slug and issue.external_id
        ):  # Check external_id specifically for formatting
            final_response_key = f"{project.slug}#{issue.external_id}"

        return IssueResponse(
            id=issue.id,
            key=final_response_key,
            external_id=issue.external_id,
            organization=organization.name,
            project=project.name,
            project_id=issue.project_id,
            project_identifier=project.identifier or project.slug,
            title=issue.title,
            description=issue.description,
            status=issue.status,
            priority=issue.priority,
            url=external_url,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            meta_data=meta_data,
            labels=labels_list,
            assignee=assignee,
            comments=comments_list,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/issues-count", response_model=Dict[str, int])
@require_permission("view_issues")
def get_issue_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get the total number of issues for the current user's account."""
    count = crud_issue.get_issue_count(db=db, account_id=current_user.account_id)
    return {"total_issues": count}


@router.put("/issues/{issue_id:path}", response_model=IssueResponse)
@require_permission("edit_issues")
async def update_issue(
    issue_id: str,  # Issue key, Issue ID or external ID
    issue_update: IssueUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update an existing issue using its internal ID or external key."""
    # URL decode the issue_id in case it's still encoded
    from urllib.parse import unquote

    issue_id = unquote(issue_id)

    logger.info(f"Attempting to update issue: {issue_id}")
    try:
        user_trackers = crud_tracker.get_for_account(
            db, account_id=current_user.account_id
        )
        tracker_ids = [t.id for t in user_trackers]

        if not tracker_ids:
            logger.warning(f"User {current_user.username} has no associated trackers.")
            raise HTTPException(
                status_code=403, detail="User has no accessible trackers."
            )

        # --- Issue Retrieval Logic (Adapted from get_issue) ---
        issue: Optional[Issue] = None
        project_slug_from_key: Optional[str] = None
        external_id_from_key: Optional[str] = None

        # 1. Try internal UUID first
        if len(issue_id) == 36:  # Basic UUID check
            logger.debug(f"Attempting lookup by internal ID: {issue_id}")
            issue = crud_issue.get(db, id=issue_id, account_id=current_user.account_id)
            if issue and issue.tracker_id not in tracker_ids:
                logger.warning(
                    f"Issue {issue_id} found by ID, but tracker {issue.tracker_id} not accessible by user {current_user.account_id}"
                )
                issue = None  # Treat as not found if not accessible

        organization = None
        project = None
        if issue_update.organization:
            organization = crud_organization.get_by_name(
                db,
                name=issue_update.organization,
                account_id=current_user.account_id,
            )
        if issue_update.project:
            if organization:
                project = crud_project.get_by_slug_or_identifier(
                    db,
                    slug_or_identifier=issue_update.project,
                    organization_id=organization.id,
                    account_id=current_user.account_id,
                )
            else:
                project = crud_project.get_by_slug_or_identifier(
                    db,
                    slug_or_identifier=issue_update.project,
                    account_id=current_user.account_id,
                )

        # 2. Try combined key (project_slug#external_id)
        if not issue and "#" in issue_id:
            logger.debug(f"Attempting lookup by combined key: {issue_id}")
            try:
                project_slug_from_key, external_id_from_key = issue_id.split("#", 1)
                # Use get_by_slug_or_identifier which returns a list
                project = crud_project.get_by_slug_or_identifier(
                    db,
                    slug_or_identifier=project_slug_from_key,
                    account_id=current_user.account_id,
                )
                if project:
                    # Ensure the project's tracker is accessible
                    if project.organization.tracker_id in tracker_ids:
                        # Build alternative key formats to search for
                        alternative_keys = [
                            issue_id,
                            f"{project.slug}#{issue_id}",
                        ]
                        # Find the issue using CRUD layer
                        issue = crud_issue.find_by_flexible_identifier(
                            db,
                            identifier=external_id_from_key,
                            tracker_ids=tracker_ids,
                            project_id=project.id,
                            alternative_keys=alternative_keys,
                            account_id=current_user.account_id,
                        )
                    else:
                        logger.warning(
                            f"Project {project_slug_from_key} found, but its tracker {project_for_lookup.tracker_id} not accessible by user {current_user.account_id}"
                        )
                elif len(project_list) > 1:
                    logger.warning(
                        f"Ambiguous project slug '{project_slug_from_key}' found."
                    )
                    # Don't raise error, just proceed to next lookup method
                else:
                    logger.debug(
                        f"Project with slug '{project_slug_from_key}' not found."
                    )

            except ValueError:
                logger.warning(f"Invalid combined key format: {issue_id}")
                # Proceed to next lookup method
        elif (
            not issue and project
        ):  # 3. Try constructing combined key (only if project is available)
            logger.debug(f"Attempting lookup by constructing combined key: {issue_id}")
            issue_key = f"{project.slug}#{issue_id}"
            issue = crud_issue.get_by_key(
                db, key=issue_key, account_id=current_user.account_id
            )

        # 4. Try direct external ID
        if not issue:
            logger.debug(f"Attempting lookup by direct external ID: {issue_id}")

            # Search across all accessible trackers using CRUD layer
            issue = crud_issue.find_by_flexible_identifier(
                db,
                identifier=issue_id,
                tracker_ids=tracker_ids,
                project_id=project.id if project else None,
                alternative_keys=[issue_id],
                account_id=current_user.account_id,
            )

        if not issue:
            logger.warning(
                f"Issue '{issue_id}' not found or not accessible by user {current_user.account_id}."
            )
            raise HTTPException(
                status_code=404,
                detail=f"Issue '{issue_id}' not found or access denied.",
            )

        logger.info(
            f"Found issue {issue.id} (External: {issue.external_id}) for update."
        )

        # --- Retrieve Project and Organization ---
        project = crud_project.get(
            db, id=issue.project_id, account_id=current_user.account_id
        )
        if not project:
            logger.error(
                f"Data inconsistency: Project {issue.project_id} not found for issue {issue.id}"
            )
            raise HTTPException(
                status_code=500,
                detail="Internal server error: Associated project data missing.",
            )

        organization = crud_organization.get(
            db, id=project.organization_id, account_id=current_user.account_id
        )
        if not organization:
            logger.error(
                f"Data inconsistency: Organization {project.organization_id} not found for project {project.id}"
            )
            raise HTTPException(
                status_code=500,
                detail="Internal server error: Associated organization data missing.",
            )

        # --- Validate Access & Get Tracker Client ---
        try:
            # Use internal IDs for get_tracker_client
            tracker_client = await get_tracker_client(
                organization_id=str(organization.id),  # Pass UUID
                project_id=str(project.id),  # Pass UUID
                db=db,
                current_user=current_user,
            )
        except HTTPException as e:
            # Re-raise authorization or configuration errors from get_tracker_client
            logger.warning(f"Access validation failed for issue {issue.id}: {e.detail}")
            raise e
        except Exception as e:
            logger.error(
                f"Error getting tracker client for issue {issue.id}: {e}", exc_info=True
            )
            raise HTTPException(
                status_code=500, detail="Error preparing tracker connection."
            )
        # --- Prepare Update Payload for Tracker ---
        # Use the base IssueUpdate schema expected by the tracker client
        tracker_update_payload = IssueUpdate(
            title=issue_update.title,
            description=issue_update.description,
            status=issue_update.status,
            priority=issue_update.priority,
            labels=issue_update.labels,
            assignee=issue_update.assignee,
            # Add other fields if the base IssueUpdate schema supports them
        )
        # Filter out None values, as tracker clients might interpret None as "clear this field"
        update_data_for_tracker = tracker_update_payload.model_dump(exclude_unset=True)

        if not update_data_for_tracker:
            logger.info(
                f"No fields provided to update for issue {issue.id}. Skipping tracker update."
            )
            # Optionally, you could raise a 400 Bad Request here if an empty update is invalid
            # raise HTTPException(status_code=400, detail="No update data provided.")
        else:
            # --- Call Tracker Client ---
            if not issue.external_id:
                logger.error(
                    f"Cannot update issue {issue.id} in tracker: Missing external_id."
                )
                raise HTTPException(
                    status_code=400,
                    detail="Cannot update issue in tracker: Missing external identifier.",
                )

            try:
                logger.info(
                    f"Calling tracker client to update issue {issue.key} with data: {update_data_for_tracker}"
                )
                # Use the issue's key for the tracker API call (e.g., "owner/repo#123")
                # The tracker client will extract the issue number from it
                await tracker_client.update_issue(
                    issue.key, TrackerIssueUpdate(**update_data_for_tracker)
                )
                logger.info(
                    f"Successfully updated issue {issue.external_id} via tracker client."
                )
            except NotImplementedError:
                logger.warning(
                    f"Tracker type {tracker_client.tracker_type} does not support updating issues."
                )
                # Decide if this should be an error or just a warning
                # raise HTTPException(status_code=501, detail="Issue updates not supported by this tracker type.")
            except Exception as e:
                logger.error(
                    f"Error updating issue {issue.external_id} via tracker client: {e}",
                    exc_info=True,
                )
                # Depending on requirements, you might still update the local DB or raise an error
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to update issue in the external tracker: {str(e)}",
                )

        # --- Update Local DB ---
        # Prepare data for local DB update using the IssueUpdate model
        update_data_for_db = issue_update.model_dump(exclude_unset=True)

        if not update_data_for_db:
            logger.info(
                f"No fields provided to update for issue {issue.id} in local DB."
            )
            # If we skipped tracker update due to no data, we might skip DB update too,
            # or just proceed to return the current state.
        else:
            try:
                logger.info(
                    f"Updating local DB for issue {issue.id} with data: {update_data_for_db}"
                )
                # Update the local database record
                # Note: crud_issue.update expects the db object, the existing db_obj, and the update obj (Pydantic model or dict)
                updated_issue_db = crud_issue.update(
                    db=db, db_obj=issue, obj_in=update_data_for_db
                )
                db.commit()
                db.refresh(
                    updated_issue_db
                )  # Ensure we have the latest data including timestamps
                issue = updated_issue_db  # Use the updated object going forward
                logger.info(f"Successfully updated issue {issue.id} in local DB.")
            except SQLAlchemyError as e:
                db.rollback()
                logger.error(
                    f"Database error updating issue {issue.id}: {e}", exc_info=True
                )
                raise HTTPException(
                    status_code=500, detail="Database error during issue update."
                )

        # --- Format Response ---
        # Fetch potentially updated metadata or related objects if necessary
        # Re-fetch project/org in case their names changed (unlikely but possible)
        # Use a joined load to potentially optimize if project/org were frequently changing,
        # but simple re-fetch is fine for now.
        db.refresh(
            issue
        )  # Refresh again after potential commit/refresh inside update block
        project = crud_project.get(
            db, id=issue.project_id, account_id=current_user.account_id
        )  # Re-fetch
        organization = (
            crud_organization.get(
                db, id=project.organization_id, account_id=current_user.account_id
            )
            if project
            else None
        )  # Re-fetch safely

        if not project or not organization:
            logger.error(
                f"Data inconsistency after update: Project or Organization missing for issue {issue.id}"
            )
            # Fallback response data
            project_name = "Error: Missing Project"
            org_name = "Error: Missing Organization"
            project_slug = "error"
        else:
            project_name = project.name
            org_name = organization.name
            project_slug = project.slug

        meta_data = issue.meta_data or {}
        labels_list = (
            extract_label_strings(meta_data.get("labels", []))
            if isinstance(meta_data, dict)
            else []
        )
        assignee = meta_data.get("assignee") if isinstance(meta_data, dict) else None
        external_url = (
            meta_data.get("url") or issue.external_url or f"/issues/{issue.id}"
        )  # Fallback URL

        # Construct the key using potentially updated slug/external_id
        final_response_key = (
            f"{project_slug}#{issue.external_id}"
            if project_slug and issue.external_id
            else str(issue.id)
        )

        logger.info(f"Returning updated issue details for {issue.id}")
        return IssueResponse(
            id=str(issue.id),  # Ensure ID is string
            key=final_response_key,
            external_id=issue.external_id,
            organization=org_name,
            project=project_name,
            project_id=issue.project_id,
            project_identifier=project.identifier or project.slug if project else None,
            title=issue.title,
            description=issue.description,
            status=issue.status,
            priority=issue.priority,
            url=external_url,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
            meta_data=meta_data,
            labels=labels_list,
            assignee=assignee,
        )

    except HTTPException:
        # Re-raise HTTPExceptions directly
        db.rollback()  # Rollback on known HTTP errors too, just in case
        raise
    except Exception as e:
        db.rollback()  # Rollback on any unexpected error
        logger.error(f"Unexpected error updating issue {issue_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error during issue update."
        )
