from typing import List, Optional, Union
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_accessible_projects
from preloop.models.db.session import get_db_session as get_db
from preloop.models import models as sm_models
from preloop.models.models.user import User

from preloop.models.crud import (
    CRUDIssue,
    CRUDOrganization,
    CRUDProject,
    CRUDTracker,
    crud_embedding_model,
    crud_issue_embedding,
)

from preloop.schemas.issue import IssueResponse
from preloop.schemas.comment import CommentResponse
from preloop.models.models.issue import Issue
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.config import get_settings
from preloop.services.aux_model_retry import call_with_aux_retry_async
from preloop.services.model_gateway_errors import ModelGatewayAPIError

settings = get_settings()
# Initialize CRUD operations
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)
crud_issue = CRUDIssue(Issue)
crud_tracker = CRUDTracker(Tracker)

logger = logging.getLogger(__name__)

# Pydantic Schemas for Search


class SearchResultItem(BaseModel):
    item_type: str = Field(
        ...,
        examples=["issue", "comment"],
        description="Type of the search result item: 'issue' or 'comment'.",
    )
    item: Union[IssueResponse, CommentResponse] = Field(
        ..., description="The actual issue or comment object."
    )
    similarity: float = Field(
        ..., description="Similarity score of the item to the query."
    )


class SearchResponse(BaseModel):
    results: List[SearchResultItem]


async def perform_search(
    query: str,
    db: Session,
    current_user: User,
    embedding_type: Optional[str] = None,
    search_type: str = "fulltext",
    limit: int = 10,
    skip: int = 0,
    sort: Optional[str] = None,
    issue_id: Optional[str] = None,
    project_id: Optional[str] = None,
    project: Optional[str] = None,
    organization_id: Optional[str] = None,
    organization: Optional[str] = None,
    author: Optional[str] = None,
    status: Optional[str] = None,
) -> SearchResponse:
    """
    Perform a similarity search based on query text.
    - **query**: The natural language query.
    - **embedding_type**: 'issue', 'comment', or null (for both).
    - **search_type**: 'similarity' or 'fulltext'.
    - Filters: project_id, limit, etc. Note: issue_id, organization_id, author are not used for similarity search.
    """
    # --- Project and Organization Resolution Logic ---
    # ALWAYS get accessible projects to apply scope rules
    accessible_projects = get_accessible_projects(
        db=db,
        current_user=current_user,
        project_ids=[project_id] if project_id else None,
    )

    logger.info(
        f"Found {len(accessible_projects)} accessible projects for user {current_user.username} "
        f"(account_id={current_user.account_id})"
    )

    # Apply additional filtering based on organization/project name filters
    if organization_id or organization:
        # Filter by organization
        if organization_id:
            accessible_projects = [
                p for p in accessible_projects if p.organization_id == organization_id
            ]
        elif organization:
            accessible_projects = [
                p for p in accessible_projects if p.organization.name == organization
            ]
        logger.info(f"After organization filter: {len(accessible_projects)} projects")

    if project:
        # Filter by project name
        accessible_projects = [p for p in accessible_projects if p.name == project]
        logger.info(f"After project name filter: {len(accessible_projects)} projects")

    # Extract project IDs - this now always contains scope-filtered projects
    resolved_project_ids_param = [p.id for p in accessible_projects]

    if not resolved_project_ids_param:
        logger.warning(
            f"No accessible projects found for search. User: {current_user.username}, "
            f"Account: {current_user.account_id}, Filters: project_id={project_id}, "
            f"organization_id={organization_id}, project={project}, organization={organization}"
        )

    # --- End of Project and Organization Resolution Logic ---

    # If no query provided, just list issues from database
    if not query or not query.strip():
        logger.info(
            f"No query provided, listing issues from database for user {current_user.username}"
        )
        # Query issues directly from database with filters
        # Need to join through Project -> Organization -> Tracker to access account_id
        from preloop.models.models.tracker import Tracker
        from preloop.models.models.organization import Organization

        issues_query = (
            db.query(Issue)
            .join(Project, Issue.project_id == Project.id)
            .join(Organization, Project.organization_id == Organization.id)
            .join(Tracker, Organization.tracker_id == Tracker.id)
            .filter(Tracker.account_id == current_user.account_id)
        )

        # Apply project filter if provided
        if resolved_project_ids_param:
            issues_query = issues_query.filter(
                Issue.project_id.in_(resolved_project_ids_param)
            )

        # Apply status filter if provided
        if status:
            if status == "opened":
                issues_query = issues_query.filter(Issue.status.in_(["opened", "open"]))
            elif status == "closed":
                issues_query = issues_query.filter(Issue.status.in_(["closed", "done"]))
            # 'all' means no filtering

        # Apply pagination
        issues_query = issues_query.offset(skip).limit(limit).all()

        db_results_with_scores = [(issue, 1.0) for issue in issues_query]

    elif search_type == "similarity":
        # Validate query is not empty for similarity search
        if not query or not query.strip():
            logger.warning(
                f"Empty query provided for similarity search by user {current_user.username}"
            )
            raise HTTPException(
                status_code=400,
                detail="Query cannot be empty for similarity search. Please provide a search query.",
            )

        # TODO: enforce AI call billing limits before similarity search proceeds.

        # 1. Get Active Embedding Model (since model_id is not in signature)
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
        try:

            async def _embed_query() -> list:
                return crud_issue_embedding._generate_embedding_vector(query, model)

            query_vector = await call_with_aux_retry_async(
                _embed_query,
                operation_name="search_query_embedding",
                provider=getattr(model, "provider", None),
            )
        except ModelGatewayAPIError as exc:
            logger.warning("Search query embedding did not recover: %s", exc)
            raise HTTPException(
                status_code=exc.status_code,
                detail="Embedding provider is rate limited; retry later.",
                headers=exc.response_headers(),
            )
        except Exception as e:
            logger.error(
                f"Error generating query vector for '{query}': {e}", exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail="Error generating query vector for similarity search.",
            )

        # 3. Call similarity_search from CRUD
        # Only pass parameters supported by CRUD and available in the current signature

        db_results_with_scores = crud_issue_embedding.similarity_search(
            db,
            model_id=model.id,
            query_vector=query_vector,
            limit=limit,
            skip=skip,
            project_ids=resolved_project_ids_param,  # Use the new resolved list
            embedding_type=embedding_type,
            sort=sort,  # Pass sort parameter to the CRUD method
            status=status,
            account_id=str(current_user.account_id),
        )

        # Record usage after successful search
        # Usage is now recorded in specific services where AI models are directly used.
        # billing_service.record_usage(current_user.id, "ai_calls")

        # print(resolved_project_ids_param) # Optional: for debugging

    elif search_type == "fulltext":
        raise HTTPException(
            status_code=501,  # Not Implemented
            detail="Full-text search is not implemented on this generic endpoint. Please use specific issue or comment search endpoints.",
        )
    else:
        # This case should ideally be caught by FastAPI's enum validation
        raise HTTPException(
            status_code=400,
            detail=f"Invalid search_type: '{search_type}'. Must be 'similarity' or 'fulltext'.",
        )

    # 4. Transform Results to Pydantic Schemas
    response_items: List[SearchResultItem] = []
    for db_obj, score in db_results_with_scores:
        item_schema: Union[IssueResponse, CommentResponse]
        item_type_str: str

        if isinstance(db_obj, sm_models.Issue):
            issue_project = crud_project.get(
                db, id=db_obj.project_id, account_id=str(current_user.account_id)
            )  # Still need project for response model
            project_name = issue_project.name if issue_project else None
            project_identifier = (
                issue_project.identifier or issue_project.slug
                if issue_project
                else None
            )
            organization_name = None
            if issue_project:
                issue_org = crud_organization.get(
                    db,
                    id=issue_project.organization_id,
                    account_id=str(current_user.account_id),
                )
                if issue_org:
                    organization_name = issue_org.name
            external_url = db_obj.external_url
            metadata_dict = db_obj.meta_data or {}

            # Extract label strings from label objects (helper function defined in issues.py)
            from preloop.api.endpoints.issues import extract_label_strings

            labels = extract_label_strings(metadata_dict.get("labels", []))

            item_schema = IssueResponse(
                id=str(db_obj.id),
                project_id=str(db_obj.project_id),
                external_id=db_obj.external_id,
                key=db_obj.key,
                title=db_obj.title,
                description=db_obj.description,
                status=db_obj.status,
                priority=db_obj.priority,
                organization=organization_name,
                project=project_name,
                project_identifier=project_identifier,
                url=external_url or f"{settings.preloop_url}/issues/{db_obj.id}",
                created_at=db_obj.created_at,
                updated_at=db_obj.updated_at,
                meta_data=metadata_dict,
                labels=labels,
                assignee=metadata_dict.get("assignee"),
                score=score,
            )
            item_type_str = "issue"
        elif isinstance(db_obj, sm_models.Comment):
            item_schema = CommentResponse(
                id=str(db_obj.id),
                body=db_obj.body,
                author=db_obj.author,
                created_at=db_obj.created_at,
                updated_at=db_obj.updated_at,
                issue_id=str(db_obj.issue_id),
                meta_data=db_obj.meta_data or {},
                score=score,
            )
            item_type_str = "comment"
        else:
            continue

        response_items.append(
            SearchResultItem(
                item_type=item_type_str, item=item_schema, similarity=score
            )
        )

    return SearchResponse(results=response_items)


router = APIRouter()


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Perform Similarity Search",
    description="Performs a similarity search across issues and/or comments based on a query text and an embedding model.",
)
async def search_all(
    query: Optional[str] = Query(
        None,
        description="The text query to search for. If not provided, lists all issues matching filters.",
    ),
    embedding_type: Optional[str] = Query(
        None,
        examples=["issue", "comment"],
        description="Type of items to search: 'issue', 'comment', or null for both.",
    ),
    search_type: str = Query(
        "fulltext",
        enum=["fulltext", "similarity"],
        description="Type of search to perform ('fulltext' or 'similarity')",
    ),
    limit: int = Query(
        10, ge=1, le=100, description="Maximum number of comments to return"
    ),
    skip: int = Query(0, ge=0, description="Number of results to skip for pagination"),
    sort: Optional[str] = Query(
        None,
        enum=["newest"],
        description="Sort order. 'newest' sorts by creation date descending.",
    ),
    issue_id: Optional[str | None] = Query(
        None, description="Filter comments by a specific issue ID (UUID)"
    ),
    project_id: Optional[str | None] = Query(
        None, description="Filter search results by project ID (UUID)."
    ),
    project: Optional[str | None] = Query(
        None, description="Filter search results by project name."
    ),
    organization_id: Optional[str | None] = Query(
        None, description="Filter search results by organization ID (UUID)."
    ),
    organization: Optional[str | None] = Query(
        None, description="Filter search results by organization name."
    ),
    author: Optional[str | None] = Query(
        None, description="Filter comments by author (username)"
    ),
    status: Optional[str | None] = Query(
        None, description="Filter issues by status ('opened', 'closed', 'all')."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        logger.info(
            f"Search request: query='{query}', search_type={search_type}, embedding_type={embedding_type}, limit={limit}"
        )
        return await perform_search(
            query=query,
            db=db,
            current_user=current_user,
            embedding_type=embedding_type,
            search_type=search_type,
            limit=limit,
            skip=skip,
            sort=sort,
            issue_id=issue_id,
            project_id=project_id,
            project=project,
            organization_id=organization_id,
            organization=organization,
            author=author,
            status=status,
        )
    except Exception as e:
        logger.error(f"Error in search endpoint: {e}", exc_info=True)
        raise
