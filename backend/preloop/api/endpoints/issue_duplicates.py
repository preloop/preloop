from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Literal, Optional, Tuple
import gc
import logging
import openai
import os
import json
from datetime import datetime, UTC

from preloop.services.aux_model_retry import call_with_aux_retry
from preloop.services.model_credentials import (
    get_aux_openai_sdk_extra_kwargs,
    resolve_model_call_credentials,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.schemas.issue_duplicate import (
    IssueDuplicate as IssueDuplicateSchema,
    IssueDuplicateSuggestionRequest,
    IssueDuplicateSuggestionResponse,
    IssueDuplicateResolutionRequest,
    IssueDuplicateResolutionResponse,
)
from preloop.models.crud import crud_issue_duplicate

from preloop.schemas.issue import IssueResponse, IssueUpdate

from fastapi import Query
from preloop.models.crud import (
    crud_issue_embedding,
    crud_embedding_model,
    crud_issue,
    crud_ai_model,
)
from preloop.models.db.session import get_db_session as get_db

from preloop.schemas.duplicates import (
    ProjectDuplicatesResponse,
    DuplicateIssuePair,
)
from preloop.schemas.issue_duplicate import (
    IssueDuplicateProjectStats,
    IssueDuplicateStats,
    IssueDuplicateUpdate,
    IssueDuplicate,
)
from preloop.models.models.account import Account
from preloop.models.models.user import User  # Import Account model
from preloop.models.models.project import Project
from preloop.models.models.issue import Issue

from .issues import update_issue

from preloop.api.auth import get_current_active_user  # Import user dependency
from preloop.config import get_settings, Settings
from preloop.api.common import (
    load_duplicates_prompts_config,
    get_accessible_projects,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/issue-duplicates/confirmed",
    response_model=List[IssueDuplicateSchema],
    tags=["Issue Duplicates"],
)
def get_duplicate_issues(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Retrieve confirmed duplicate issues.
    """
    duplicates = crud_issue_duplicate.get_multi(
        db,
        skip=skip,
        limit=limit,
        decision="duplicate",
        account_id=current_user.account_id,
    )
    return duplicates


@router.get(
    "/issue-duplicates/check",
    response_model=IssueDuplicateSchema,
    tags=["Issue Duplicates"],
)
def check_or_create_issue_duplicate(
    *,
    db: Session = Depends(get_db),
    issue1_id: str,
    issue2_id: str,
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
) -> Any:
    if issue1_id == issue2_id:
        raise HTTPException(status_code=400, detail="Issue IDs cannot be the same.")

    existing_duplicate = crud_issue_duplicate.get_by_issue_ids(
        db, issue1_id=issue1_id, issue2_id=issue2_id, account_id=current_user.account_id
    )
    if existing_duplicate:
        logger.info(
            f"Found existing duplicate entry for issues {issue1_id} and {issue2_id}."
        )
        return IssueDuplicate.model_validate(existing_duplicate)

    logger.info(
        f"No existing duplicate entry for issues {issue1_id} and {issue2_id}. Proceeding with AI model analysis."
    )

    issue1 = crud_issue.get(db, id=issue1_id, account_id=current_user.account_id)
    issue2 = crud_issue.get(db, id=issue2_id, account_id=current_user.account_id)

    if not issue1 or not issue2:
        missing_ids_str = []
        if not issue1:
            missing_ids_str.append(str(issue1_id))
        if not issue2:
            missing_ids_str.append(str(issue2_id))
        detail = f"Issue(s) not found: {', '.join(missing_ids_str)}."
        logger.warning(detail)
        raise HTTPException(status_code=404, detail=detail)

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        logger.error("No default active AI model configured.")
        raise HTTPException(
            status_code=500, detail="No default active AI model configured."
        )

    logger.info(f"Using AI model '{default_model.model_identifier}'.")

    prompts_config = load_duplicates_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get("duplicate_classification_v1")
    if not prompt_data or "system" not in prompt_data or "user" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Duplicate classification prompt not configured."
        )

    system_prompt = prompt_data["system"]
    user_prompt_template = prompt_data["user"]

    prompt_text = user_prompt_template.format(
        issue1_id=issue1.id,
        issue1_title=issue1.title or "N/A",
        issue1_description=issue1.description or "No description provided.",
        issue2_id=issue2.id,
        issue2_title=issue2.title or "N/A",
        issue2_description=issue2.description or "No description provided.",
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]

    llm_response_text = ""
    try:
        creds_kwargs = resolve_model_call_credentials(default_model, db=db)
        api_key = creds_kwargs.get("api_key")
        if not api_key:
            logger.warning(
                f"API key not found in credentials for model {default_model.model_identifier}. Trying OPENAI_API_KEY env var."
            )
            api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            logger.error(
                f"OpenAI API key not found for model {default_model.model_identifier} or environment variable."
            )
            raise HTTPException(
                status_code=500, detail="OpenAI API key not configured."
            )

        client = openai.OpenAI(api_key=api_key)
        aux_extras = get_aux_openai_sdk_extra_kwargs(
            default_model,
            call_site_kwargs={
                "model": default_model.model_identifier,
                "messages": messages,
                "response_format": {"type": "json_object"},
            },
        )

        response = call_with_aux_retry(
            lambda: client.chat.completions.create(
                model=default_model.model_identifier,
                messages=messages,
                response_format={"type": "json_object"},
                **aux_extras,
            ),
            operation_name="issue_duplicate_check",
            provider=getattr(default_model, "provider_name", None),
        )
        llm_response_text = response.choices[0].message.content.strip()
        logger.info(
            f"AI model response for issues {issue1_id}, {issue2_id}: '{llm_response_text}'"
        )

        response_obj = json.loads(llm_response_text)

        decision_word = response_obj.get("classification", "").upper()
        reason = response_obj.get("reason")
        suggestion = response_obj.get("suggestion")

        if decision_word == "DUPLICATE":
            parsed_status = "duplicate"
        elif decision_word == "OVERLAPPING":
            parsed_status = "overlapping"
        elif decision_word == "UNRELATED":
            parsed_status = "unrelated"
        else:
            logger.warning(
                f"AI model returned unexpected status: '{decision_word}'. Defaulting to 'undecided'."
            )
            parsed_status = "undecided"
            reason = llm_response_text

        duplicate_create_data = IssueDuplicate(
            issue1_id=issue1.id,
            issue2_id=issue2.id,
            decision=parsed_status,
            ai_model_id=default_model.id,
            ai_model_name=default_model.model_identifier,
            reason=reason,
            suggestion=suggestion,
        )

        new_duplicate_entry = crud_issue_duplicate.create(
            db, obj_in=duplicate_create_data.model_dump()
        )
        logger.info(
            f"Created new IssueDuplicate entry ID {new_duplicate_entry.id} for issues {issue1_id}, {issue2_id} with status '{parsed_status}'."
        )
        return new_duplicate_entry

    except openai.APIError as e:
        logger.exception(
            f"OpenAI API call for model '{default_model.model_identifier}' failed: {e}"
        )
        raise HTTPException(status_code=500, detail="AI model API error")
    except ModelGatewayAPIError as exc:
        logger.warning(
            "AI model call for issue duplicate check did not recover: %s", exc
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail="AI model is rate limited; retry later.",
            headers=exc.response_headers(),
        )
    except Exception as e:
        logger.exception(
            f"An unexpected error occurred during AI model invocation for model '{default_model.model_identifier}': {e}"
        )
        raise HTTPException(
            status_code=500, detail=f"AI model processing error: {str(e)}"
        )


@router.patch(
    "/issue-duplicates/propose-resolution",
    response_model=IssueDuplicateSchema,
    tags=["Issue Duplicates"],
)
def propose_issue_duplicate_resolution(
    resolution: IssueDuplicateSuggestionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Propose a resolution for an issue duplicate."""
    existing_duplicate = crud_issue_duplicate.get_by_issue_ids(
        db,
        issue1_id=resolution.issue1_id,
        issue2_id=resolution.issue2_id,
        account_id=current_user.account_id,
    )
    if not existing_duplicate:
        raise HTTPException(status_code=404, detail="Duplicate entry not found.")

    # Check if the user has access to the project containing the issues
    # Raises an exception if not
    project_id = existing_duplicate.issue1.project_id
    accessible_projects = get_accessible_projects(db, current_user, [project_id])
    if not accessible_projects:
        raise HTTPException(
            status_code=403,
            detail="User does not have access to the project containing the issues.",
        )

    if resolution.resolution == "merge":
        if not all(
            [
                resolution.resulting_issue1_id,
                resolution.merged_title,
                resolution.merged_description,
            ]
        ):
            raise HTTPException(
                status_code=400,
                detail="Merge resolution requires resulting_issue1_id, merged_title, and merged_description.",
            )
        issue_to_update = crud_issue.get(
            db=db, id=resolution.resulting_issue1_id, account_id=current_user.account_id
        )
        if not issue_to_update:
            raise HTTPException(status_code=404, detail="Resulting issue not found.")
        update_data = {
            "title": resolution.merged_title,
            "description": resolution.merged_description,
        }
        crud_issue.update(db=db, db_obj=issue_to_update, obj_in=update_data)

    elif resolution.resolution == "deconflict":
        if not all(
            [
                resolution.deconflicted_title1,
                resolution.deconflicted_description1,
                resolution.deconflicted_title2,
                resolution.deconflicted_description2,
            ]
        ):
            raise HTTPException(
                status_code=400,
                detail="Deconflict resolution requires titles and descriptions for both issues.",
            )

        issue1_to_update = crud_issue.get(
            db=db, id=resolution.issue1_id, account_id=current_user.account_id
        )
        issue2_to_update = crud_issue.get(
            db=db, id=resolution.issue2_id, account_id=current_user.account_id
        )
        if not issue1_to_update or not issue2_to_update:
            raise HTTPException(status_code=404, detail="One or both issues not found.")

        update_data1 = {
            "title": resolution.deconflicted_title1,
            "description": resolution.deconflicted_description1,
        }
        crud_issue.update(db=db, db_obj=issue1_to_update, obj_in=update_data1)

        update_data2 = {
            "title": resolution.deconflicted_title2,
            "description": resolution.deconflicted_description2,
        }
        crud_issue.update(db=db, db_obj=issue2_to_update, obj_in=update_data2)

    db_duplicate = crud_issue_duplicate.update_resolution(
        db=db,
        issue1_id=resolution.issue1_id,
        issue2_id=resolution.issue2_id,
        resolution_in=resolution,
    )

    if not db_duplicate:
        raise HTTPException(status_code=404, detail="Issue duplicate not found.")

    return db_duplicate


@router.patch(
    "/issue-duplicates/execute-resolution",
    response_model=IssueDuplicateResolutionResponse,
    tags=["Issue Duplicates"],
)
async def execute_issue_duplicate_resolution(
    resolution: IssueDuplicateResolutionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Execute the resolution for a pair of duplicate issues."""
    issue_a = crud_issue.get(
        db=db, id=resolution.issue1_id, account_id=current_user.account_id
    )
    issue_b = crud_issue.get(
        db=db, id=resolution.issue2_id, account_id=current_user.account_id
    )

    if not issue_a or not issue_b:
        raise HTTPException(status_code=404, detail="One or both issues not found")

    resolution_type = resolution.resolution

    # Map the detailed resolution to a canonical string value.
    resolution_map = {
        "close_a": "closed",
        "close_b": "closed",
        "merge_a_to_b": "merged",
        "merge_b_to_a": "merged",
        "deconflict": "deconflicted",
        "not_a_duplicate": "not_a_duplicate",
    }
    db_resolution_value = resolution_map.get(resolution_type)

    if db_resolution_value is None:
        raise HTTPException(
            status_code=400, detail=f"Invalid resolution type: {resolution_type}"
        )

    if resolution_type == "close_a":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Closed as duplicate of issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                comment=f"Issue {issue_a.key} was closed as a duplicate of this issue."
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "close_b":
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Closed as duplicate of issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                comment=f"Issue {issue_b.key} was closed as a duplicate of this issue."
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "merge_a_to_b":
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_2_title,
                description=resolution.resulting_issue_2_description,
                comment=f"Merged content from issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Merged into and closed as duplicate of issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "merge_b_to_a":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_1_title,
                description=resolution.resulting_issue_1_description,
                comment=f"Merged content from issue {issue_b.key}.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                status="closed",
                comment=f"Merged into and closed as duplicate of issue {issue_a.key}.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "deconflict":
        await update_issue(
            issue_id=issue_a.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_1_title,
                description=resolution.resulting_issue_1_description,
                comment=f"Deconflicted with issue {issue_b.key}. Title and description updated.",
            ),
            db=db,
            current_user=current_user,
        )
        await update_issue(
            issue_id=issue_b.id,
            issue_update=IssueUpdate(
                title=resolution.resulting_issue_2_title,
                description=resolution.resulting_issue_2_description,
                comment=f"Deconflicted with issue {issue_a.key}. Title and description updated.",
            ),
            db=db,
            current_user=current_user,
        )

    elif resolution_type == "not_a_duplicate":
        # No action needed for the issues themselves, only for the duplicate record.
        pass

    else:
        raise HTTPException(status_code=400, detail="Invalid resolution type")

    # Update the issue_duplicate record
    duplicate_record = crud_issue_duplicate.get_by_issue_ids(
        db=db,
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        account_id=current_user.account_id,
    )
    if duplicate_record:
        update_data = IssueDuplicateUpdate(
            resolution=db_resolution_value,
            resolution_at=datetime.now(UTC),
            resolution_reason=resolution.resolution_reason,
        )
        crud_issue_duplicate.update(
            db=db, db_obj=duplicate_record, obj_in=update_data.dict(exclude_unset=True)
        )
    else:
        # This case should ideally not be reached if the frontend is behaving correctly.
        raise HTTPException(status_code=404, detail="Issue duplicate record not found")

    return IssueDuplicateResolutionResponse(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution=resolution.resolution,
    )


def _find_issue_duplicates_logic(
    db: Session,
    current_user: Account,
    accessible_projects: List[Project],
    similarity_threshold: float,
    limit: int,
    skip: int,
    limit_per_issue: int,
    status: Optional[str],
    resolution: Optional[str] = None,
    max_issues_per_project: int = 5000,  # New parameter with reasonable default
    max_total_pairs: int = 50000,  # New parameter to prevent memory overflow
) -> Tuple[List[DuplicateIssuePair], str]:
    """Shared logic to find potential duplicate issues within specified projects."""
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

    all_duplicates_pairs: List[DuplicateIssuePair] = []
    reported_pairs = set()
    processed_issues = 0  # Track total processed issues

    for project in accessible_projects:
        # Limit issues per project to prevent memory overload using CRUD layer
        project_issues = crud_issue.get_for_project_with_embeddings(
            db,
            project_id=project.id,
            status=status if status and status != "all" else None,
            limit=max_issues_per_project,
            account_id=current_user.account_id,
        )

        for issue in project_issues:
            processed_issues += 1

            # Early exit if we have enough pairs
            if len(all_duplicates_pairs) >= max_total_pairs:
                logger.warning(
                    f"Reached maximum pairs limit ({max_total_pairs}), stopping early"
                )
                break

            query_embedding_vector = next(
                (
                    emb.embedding
                    for emb in issue.embeddings
                    if emb.embedding_model_id == model.id
                ),
                None,
            )

            # Skip if no embedding found
            if query_embedding_vector is None:
                continue

            try:
                similar_issue_score_tuples: List[Tuple[Issue, float]] = (
                    crud_issue_embedding.similarity_search(
                        db=db,
                        model_id=model.id,
                        query_vector=query_embedding_vector,
                        limit=min(limit_per_issue + 1, 20),  # Cap similarity results
                        project_ids=[project.id],
                        embedding_type="issue",
                        similarity=similarity_threshold,
                        status=status if status and status != "all" else None,
                        account_id=current_user.account_id,
                    )
                )
            except Exception as e:
                logger.error(f"Similarity search failed for issue {issue.id}: {e}")
                continue

            for similar_issue_obj, score in similar_issue_score_tuples:
                if similar_issue_obj.id == issue.id:
                    continue

                # Early exit if we have enough pairs
                if len(all_duplicates_pairs) >= max_total_pairs:
                    break

                id1_str = str(issue.id)
                id2_str = str(similar_issue_obj.id)
                pair_key = frozenset([id1_str, id2_str])

                if pair_key not in reported_pairs:
                    try:
                        duplicate_record = crud_issue_duplicate.get_by_issue_ids(
                            db,
                            issue1_id=id1_str,
                            issue2_id=id2_str,
                            account_id=current_user.account_id,
                        )
                    except Exception as e:
                        logger.error(f"Failed to get duplicate record: {e}")
                        continue

                    record_resolution = (
                        duplicate_record.resolution if duplicate_record else None
                    )

                    # Apply resolution filter
                    if resolution and resolution != "all":
                        is_resolved = bool(record_resolution)
                        if resolution == "resolved" and not is_resolved:
                            continue
                        if resolution == "unresolved" and is_resolved:
                            continue

                    try:
                        duplicate_pair = DuplicateIssuePair(
                            issue1=IssueResponse(
                                id=str(issue.id),
                                external_id=issue.external_id or "",  # Handle None
                                key=issue.key or "",  # Handle None
                                organization="",
                                project="",
                                url=issue.external_url or "",
                                created_at=issue.created_at,
                                updated_at=issue.updated_at,
                                title=issue.title or "",  # Handle None
                                description=issue.description or "",
                                status=issue.status or "",
                                priority=issue.priority or "",
                                author="",
                                assignees=[],
                                labels=[],
                                comments=[],
                                project_id=project.id,
                            ),
                            issue2=IssueResponse(
                                id=str(similar_issue_obj.id),
                                external_id=similar_issue_obj.external_id
                                or "",  # Handle None
                                key=similar_issue_obj.key or "",  # Handle None
                                organization="",
                                project="",
                                url=similar_issue_obj.external_url or "",
                                created_at=similar_issue_obj.created_at,
                                updated_at=similar_issue_obj.updated_at,
                                title=similar_issue_obj.title or "",  # Handle None
                                description=similar_issue_obj.description or "",
                                status=similar_issue_obj.status or "",
                                priority=similar_issue_obj.priority or "",
                                author="",
                                assignees=[],
                                labels=[],
                                comments=[],
                                project_id=project.id,
                            ),
                            similarity=score,
                            resolution=record_resolution,
                        )
                        all_duplicates_pairs.append(duplicate_pair)
                        reported_pairs.add(pair_key)
                    except Exception as e:
                        logger.error(
                            f"Error creating issue response for duplicate pair: {e}"
                        )
                        continue

            del similar_issue_score_tuples

            # Sparse garbage collection - only every 1000 processed issues
            if processed_issues % 1000 == 0:
                gc.collect()

        # Early exit if we have enough pairs
        if len(all_duplicates_pairs) >= max_total_pairs:
            break

        del project_issues

    all_duplicates_pairs.sort(key=lambda x: x.similarity, reverse=True)
    paginated_duplicates = all_duplicates_pairs[skip : skip + limit]
    return paginated_duplicates, str(model.id)


@router.get("/issue-duplicates", response_model=ProjectDuplicatesResponse)
def find_issue_duplicates(
    project_ids: Optional[List[str]] = Query(
        None,
        description=(
            "Optional list of project IDs to search within. "
            "If not provided, searches across all accessible projects."
        ),
    ),
    limit: int = Query(
        5,
        ge=1,
        description="Maximum number of duplicates to return.",
    ),
    skip: int = Query(
        0, ge=0, description="Number of duplicates to skip for pagination."
    ),
    similarity_threshold: float = Query(
        0.7,
        ge=0.0,
        le=1.0,
        description="Similarity threshold for considering issues as duplicates.",
    ),
    limit_per_issue: int = Query(
        5,
        ge=1,
        le=20,
        description="Maximum number of duplicates to find for each issue.",
    ),
    status: Literal["opened", "closed", "all"] = Query(
        "opened", description="Filter issues by status."
    ),
    resolution: Literal["resolved", "unresolved", "all"] = Query(
        "all", description="Filter by resolution status."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Finds potential duplicate issues within specified projects.
    """
    accessible_projects = get_accessible_projects(
        db=db, current_user=current_user, project_ids=project_ids
    )

    paginated_duplicates, model_id_used = _find_issue_duplicates_logic(
        db=db,
        current_user=current_user,
        accessible_projects=accessible_projects,
        similarity_threshold=similarity_threshold,
        limit=limit,
        skip=skip,
        limit_per_issue=limit_per_issue,
        status=status,
        resolution=resolution,
    )

    return ProjectDuplicatesResponse(
        project_ids=[str(p.id) for p in accessible_projects],
        model_id_used=model_id_used,
        threshold_used=similarity_threshold,
        duplicates=paginated_duplicates,
    )


@router.get("/project-duplicate-stats", response_model=IssueDuplicateStats)
def get_projects_duplicate_stats(
    project_ids: Optional[List[str]] = Query(
        None,
        description="A list of project IDs to filter the statistics by. If not provided, stats for all accessible projects will be returned.",
    ),
    similarity_threshold: float = Query(
        0.95,
        ge=0.0,
        le=1.0,
        description="Similarity threshold for considering issues as duplicates.",
    ),
    status: Optional[str] = Query(None, description="Filter issues by status."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get statistics about duplicate issues for specified projects.
    """
    accessible_projects = get_accessible_projects(
        db=db, current_user=current_user, project_ids=project_ids
    )

    issue_counts = crud_issue.get_issue_counts_per_project(
        db,
        project_ids=[str(p.id) for p in accessible_projects],
        account_id=current_user.account_id,
    )

    duplicate_issue_list, _ = _find_issue_duplicates_logic(
        db=db,
        current_user=current_user,
        accessible_projects=accessible_projects,
        similarity_threshold=similarity_threshold,
        limit=1000,  # A large enough number to get all duplicates for stats
        skip=0,
        limit_per_issue=100,  # A large enough number
        status=status,
    )

    stats: Dict[str, IssueDuplicateProjectStats] = {
        str(project.id): IssueDuplicateProjectStats(
            project_id=project.id, project_name=project.name, total=0, duplicates=0
        )
        for project in accessible_projects
    }

    for pid, data in issue_counts.items():
        pid_str = str(pid)
        if pid_str in stats:
            stats[pid_str].total = data.get("total", 0)

    # Since a duplicate pair contains two issues, we need to count unique issues involved
    duplicate_issues_per_project = {p.id: set() for p in accessible_projects}
    for pair in duplicate_issue_list:
        duplicate_issues_per_project[pair.issue1.project_id].add(pair.issue1.id)
        duplicate_issues_per_project[pair.issue2.project_id].add(pair.issue2.id)

    for pid, issues in duplicate_issues_per_project.items():
        pid_str = str(pid)
        stats[pid_str].duplicates = len(issues)

    return IssueDuplicateStats(projects=stats)


@router.post("/ai-suggestion", response_model=IssueDuplicateSuggestionResponse)
def get_resolution_suggestion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    issue1_id: str = Body(...),
    issue2_id: str = Body(...),
    resolution: str = Body(...),
    settings: Settings = Depends(get_settings),
):
    """Generate a suggestion for resolving a duplicate issue pair."""
    issue1 = crud_issue.get(db, id=issue1_id)
    issue2 = crud_issue.get(db, id=issue2_id)

    if not issue1 or not issue2:
        raise HTTPException(status_code=404, detail="One or both issues not found")

    # Authorization check
    project_id = issue1.project_id
    accessible_projects = get_accessible_projects(db, current_user, [project_id])
    if not accessible_projects:
        raise HTTPException(
            status_code=403,
            detail="User does not have access to the project containing the issues.",
        )

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        raise HTTPException(
            status_code=500, detail="No default active AI model configured."
        )

    logger.info(f"Using AI model '{default_model.model_identifier}'.")

    prompts_config = load_duplicates_prompts_config(settings.PROMPTS_FILE)
    if resolution == "merged":
        prompt_data = prompts_config.get("merge_issues_v1")
    elif resolution == "deconflicted":
        prompt_data = prompts_config.get("deconflict_issues_v1")
    else:
        raise HTTPException(
            status_code=400,
            detail="Suggestions are only available for 'merged' or 'deconflicted' resolutions.",
        )

    if not prompt_data or "system" not in prompt_data or "user" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Duplicate resolution prompt not configured."
        )

    system_prompt = prompt_data["system"]
    user_prompt_template = prompt_data["user"]

    prompt_text = user_prompt_template.format(
        title1=issue1.title,
        description1=issue1.description,
        title2=issue2.title,
        description2=issue2.description,
    )

    creds_kwargs = resolve_model_call_credentials(default_model, db=db)
    api_key = creds_kwargs.get("api_key")
    if not api_key:
        logger.warning(
            f"API key not found in credentials for model {default_model.model_identifier}. Trying OPENAI_API_KEY env var."
        )
        api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        logger.error(
            f"OpenAI API key not found for model {default_model.model_identifier} or environment variable."
        )
        raise HTTPException(status_code=500, detail="OpenAI API key not configured.")

    client = openai.OpenAI(api_key=api_key, base_url=creds_kwargs.get("api_base"))
    dup_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]
    aux_extras = get_aux_openai_sdk_extra_kwargs(
        default_model,
        call_site_kwargs={
            "model": default_model.model_identifier,
            "messages": dup_messages,
            "response_format": {"type": "json_object"},
        },
    )

    try:
        llm_response = call_with_aux_retry(
            lambda: client.chat.completions.create(
                model=default_model.model_identifier,
                messages=dup_messages,
                response_format={"type": "json_object"},
                **aux_extras,
            ),
            operation_name="issue_duplicate_resolution_suggestion",
            provider=getattr(default_model, "provider_name", None),
        )
        suggestion_data = json.loads(llm_response.choices[0].message.content)

        # Ensure 'explanation' is present, providing a default if it's missing.
        explanation = suggestion_data.pop("explanation", "")
        if not explanation:
            logger.warning("AI suggestion response missing 'explanation' field.")

        return IssueDuplicateSuggestionResponse(
            explanation=explanation, **suggestion_data
        )
    except openai.APIError as e:
        logger.error(f"OpenAI API call failed: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to get suggestion from AI model."
        )
    except ModelGatewayAPIError as exc:
        logger.warning("AI suggestion call did not recover: %s", exc)
        raise HTTPException(
            status_code=exc.status_code,
            detail="AI model is rate limited; retry later.",
            headers=exc.response_headers(),
        )
