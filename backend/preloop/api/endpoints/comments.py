"""Endpoints for managing issue comments."""

import logging

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_tracker_client
from preloop.schemas.comment import CommentCreate, CommentList, CommentResponse
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User
from preloop.schemas.comment import CommentSearchResults

from preloop.models.crud import (
    CRUDIssue,
    CRUDOrganization,
    CRUDProject,
    CRUDTracker,
    crud_comment,
    crud_embedding_model,
    crud_issue_embedding,
)
from preloop.models.models.issue import Issue
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.services.aux_model_retry import call_with_aux_retry
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

# Initialize CRUD operations
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)
crud_issue = CRUDIssue(Issue)
crud_tracker = CRUDTracker(Tracker)

# API Endpoints


@router.get("/issues/{issue_id}/comments", response_model=CommentList)
@require_permission("view_issues")
async def list_issue_comments(
    issue_id: str,
    organization: str = Query(..., description="Organization identifier"),
    project: str = Query(..., description="Project identifier"),
    limit: int = Query(
        20, ge=1, le=100, description="Maximum number of comments to return"
    ),
    offset: int = Query(0, ge=0, description="Number of comments to skip"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CommentList:
    """Get a list of comments for a specific issue. Requires authentication."""
    try:
        # Get the tracker client, passing current user for auth check
        tracker_client = await get_tracker_client(
            organization, project, db, current_user
        )

        # Get the issue to verify it exists
        issue = await tracker_client.get_issue(issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Get the comments
        comments = await tracker_client.get_comments(issue_id)

        # Apply pagination
        paginated_comments = comments[offset : offset + limit]

        # Convert tracker comments to API response model
        comment_responses = []
        for comment in paginated_comments:
            comment_responses.append(
                CommentResponse(
                    id=comment.id,
                    issue_id=issue_id,
                    author=comment.author,
                    body=comment.body,
                    created_at=comment.created_at,
                    updated_at=comment.updated_at,
                    meta_data=comment.metadata,
                )
            )

        # Return the comment list
        return CommentList(
            items=comment_responses,
            total=len(comments),
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error getting comments: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting comments: {str(e)}")


@router.post(
    "/issues/{issue_id}/comments", response_model=CommentResponse, status_code=201
)
@require_permission("edit_issues")
async def add_issue_comment(
    issue_id: str,
    comment: CommentCreate,
    organization: str = Query(..., description="Organization identifier"),
    project: str = Query(..., description="Project identifier"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CommentResponse:
    """Add a new comment to a specific issue. Requires authentication."""
    try:
        # Get the tracker client, passing current user for auth check
        tracker_client = await get_tracker_client(
            organization, project, db, current_user
        )

        # Get the issue to verify it exists
        issue = await tracker_client.get_issue(issue_id)
        if not issue:
            raise HTTPException(status_code=404, detail="Issue not found")

        # Create the comment
        created_comment = await tracker_client.add_comment(issue_id, comment.body)

        # Convert tracker comment to API response model
        return CommentResponse(
            id=created_comment.id,
            issue_id=issue_id,
            author=created_comment.author,
            body=created_comment.body,
            created_at=created_comment.created_at,
            updated_at=created_comment.updated_at,
            meta_data=created_comment.meta_data,
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error adding comment: {e}")
        raise HTTPException(status_code=500, detail=f"Error adding comment: {str(e)}")


@router.get("/comments/search", response_model=CommentSearchResults)
@require_permission("view_issues")
async def search_comments(
    query: Optional[str] = Query(
        None, description="Search query text for comment body or vector search"
    ),
    search_type: str = Query(
        "fulltext",
        enum=["fulltext", "similarity"],
        description="Type of search to perform ('fulltext' or 'similarity')",
    ),
    limit: int = Query(
        10, ge=1, le=100, description="Maximum number of comments to return"
    ),
    issue_id: Optional[str] = Query(
        None, description="Filter comments by a specific issue ID (UUID)"
    ),
    project_id: Optional[str] = Query(
        None, description="Filter comments by parent issue's project ID (UUID)"
    ),
    organization_id: Optional[str] = Query(
        None, description="Filter comments by parent issue's organization ID (UUID)"
    ),
    author: Optional[str] = Query(
        None, description="Filter comments by author (username)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Search for comments using full-text or similarity search.
    Requires authentication and checks user access to related issues/projects.
    """
    user_trackers = crud_tracker.get_for_account(db, account_id=current_user.account_id)
    accessible_tracker_ids = [t.id for t in user_trackers]

    if not accessible_tracker_ids:
        logger.warning(f"User {current_user.id} has no accessible trackers.")
        return CommentSearchResults(items=[], total=0, query=query or "")

    comments_data: List[CommentResponse] = []
    total_comments = 0

    # Prepare project_ids and organization_ids for CRUD functions
    # similarity_search_comments expects List[str] or None
    resolved_project_ids: Optional[List[str]] = [project_id] if project_id else None
    resolved_organization_ids: Optional[List[str]] = (
        [organization_id] if organization_id else None
    )

    try:
        if search_type == "similarity" and query:
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
            try:
                query_vector = call_with_aux_retry(
                    lambda: crud_issue_embedding._generate_embedding_vector(
                        query, model
                    ),
                    operation_name="comment_similarity_query_embedding",
                    provider=getattr(model, "provider", None),
                )
            except ModelGatewayAPIError as exc:
                logger.warning(
                    "Comment similarity query embedding did not recover: %s", exc
                )
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

            similar_comments = crud_issue_embedding.similarity_search(
                db,
                model_id=model.id,
                query_vector=query_vector,
                limit=limit,
                project_ids=resolved_project_ids,
                embedding_type="comment",
                account_id=current_user.account_id,
            )
            total_comments = len(similar_comments)

            for comment_obj, score in similar_comments:
                parent_issue = db.get(Issue, comment_obj.issue_id)
                if (
                    not parent_issue
                    or parent_issue.tracker_id not in accessible_tracker_ids
                ):
                    if not parent_issue:
                        logger.warning(
                            f"Comment {comment_obj.id} links to non-existent issue {comment_obj.issue_id}."
                        )
                    else:
                        logger.warning(
                            f"User {current_user.id} lacks access to tracker for comment {comment_obj.id}."
                        )
                    continue

                parent_project = db.get(Project, parent_issue.project_id)
                if not parent_project:
                    logger.warning(
                        f"Issue {parent_issue.id} links to non-existent project {parent_issue.project_id}."
                    )
                    continue

                comments_data.append(
                    CommentResponse(
                        id=comment_obj.id,
                        body=comment_obj.body,
                        author=comment_obj.author or "",
                        created_at=comment_obj.created_at,
                        updated_at=comment_obj.updated_at,
                        issue_id=comment_obj.issue_id,
                        project_id=parent_issue.project_id,
                        organization_id=parent_project.organization_id,
                        score=score,
                    )
                )

        elif search_type == "fulltext":
            # TODO: implement full-text search
            # For full-text, crud_comment.search_full_text expects single ID strings or None
            if author:
                raw_results = crud_comment.get_multi_by_author(
                    db, author=author, limit=limit, account_id=current_user.account_id
                )
            elif issue_id:
                raw_results = crud_comment.get_multi_by_issue(
                    db,
                    issue_id=issue_id,
                    limit=limit,
                    account_id=current_user.account_id,
                )
            else:
                raw_results = []
            total_comments = len(raw_results)

            for comment_obj in raw_results:
                parent_issue = db.get(Issue, comment_obj.issue_id)
                if (
                    not parent_issue
                    or parent_issue.tracker_id not in accessible_tracker_ids
                ):
                    if not parent_issue:
                        logger.warning(
                            f"Comment {comment_obj.id} links to non-existent issue {comment_obj.issue_id}."
                        )
                    else:
                        logger.warning(
                            f"User {current_user.id} lacks access to tracker for comment {comment_obj.id}."
                        )
                    continue

                parent_project = db.get(Project, parent_issue.project_id)
                if not parent_project:
                    logger.warning(
                        f"Issue {parent_issue.id} links to non-existent project {parent_issue.project_id}."
                    )
                    continue

                comments_data.append(
                    CommentResponse(
                        id=comment_obj.id,
                        body=comment_obj.body,
                        author=comment_obj.author,
                        created_at=comment_obj.created_at,
                        updated_at=comment_obj.updated_at,
                        issue_id=comment_obj.issue_id,
                        project_id=parent_issue.project_id,
                        organization_id=parent_project.organization_id,
                        score=None,
                    )
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid search_type specified. Must be 'fulltext' or 'similarity'.",
            )

        return CommentSearchResults(
            items=comments_data, total=total_comments, query=query or ""
        )

    except HTTPException:  # Re-raise HTTPExceptions directly
        raise
    except Exception as e:
        logger.error(
            f"Error searching comments (type: {search_type}, query: '{query}'): {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error during comment search: {str(e)}",
        )
