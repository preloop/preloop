"""Endpoints for detecting dependencies between issues."""

import logging
from datetime import datetime
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models.models.user import User
from preloop.models.crud import (
    CRUDIssue,
    crud_ai_model,
    crud_issue_set,
    crud_issue_relationship,
)
from preloop.services.model_credentials import resolve_model_call_credentials
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.issue import Issue
from preloop.api.auth import get_current_active_user
import json
import openai

from preloop.config import get_settings, Settings
from preloop.api.common import (
    get_tracker_client,
    load_dependencies_prompts_config,
)
from preloop.schemas.issue_dependency import (
    CommitDependenciesRequest,
    DependencyRequest,
    DependencyResponse,
    ExtendScanRequest,
    DependencyPair,
)

logger = logging.getLogger(__name__)
router = APIRouter()
crud_issue = CRUDIssue(Issue)


@router.post(
    "/issue-dependencies/detect",
    response_model=DependencyResponse,
    tags=["Issues", "AI"],
    summary="Detect dependencies between a list of issues using an AI model.",
)
def detect_issue_dependencies(
    request: DependencyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
):
    """
    Analyzes a list of issues to find potential dependencies between them.

    - **issue_ids**: A list of UUIDs for the issues to be analyzed.
    - **model_id**: Optional ID of a specific AI model to use. If not provided, the user's default model will be used.
    """
    if len(request.issue_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least two issue IDs are required for dependency analysis.",
        )

    # 1. Fetch issues from the database
    issues = []
    issue_map = {}
    for issue_id in request.issue_ids:
        issue = crud_issue.get(db, id=issue_id, account_id=current_user.account_id)
        if not issue:
            raise HTTPException(
                status_code=404, detail=f"Issue with ID '{issue_id}' not found."
            )
        issues.append(issue)
        issue_map[str(issue.id)] = issue

    # 2. Select AI Model
    if request.model_id:
        ai_model = crud_ai_model.get(db, id=request.model_id)
        if not ai_model or ai_model.account_id != current_user.account_id:
            raise HTTPException(
                status_code=404,
                detail=f"AI Model with ID '{request.model_id}' not found or access denied.",
            )
    else:
        ai_model = crud_ai_model.get_default_active_model(
            db, account_id=current_user.account_id
        )
        if not ai_model:
            raise HTTPException(
                status_code=404,
                detail="No default AI model configured for your account.",
            )

    # 3. Check for a cached IssueSet
    sorted_issue_ids = sorted(request.issue_ids)
    existing_sets = crud_issue_set.get_supersets_by_issues(
        db,
        issue_ids=sorted_issue_ids,
        ai_model_ids=[ai_model.id, None],
        account_id=current_user.account_id,
    )

    if existing_sets:
        logger.info(f"Cache hit for issue set with AI model {ai_model.id}.")
        # If a superset exists, we can return the cached relationships
        cached_relationships = crud_issue_relationship.get_relationships_for_issues(
            db, issue_ids=request.issue_ids, any_in_list=True
        )

        dependencies = []
        for rel in cached_relationships:
            source_issue = None
            dependent_issue = None
            if rel.type == "depends_on":
                source_issue = issue_map.get(str(rel.source_issue_id))
                dependent_issue = issue_map.get(str(rel.target_issue_id))
            elif rel.type == "blocks":
                source_issue = issue_map.get(str(rel.source_issue_id))
                dependent_issue = issue_map.get(str(rel.target_issue_id))

            if source_issue and dependent_issue:
                dependencies.append(
                    DependencyPair(
                        source_issue_id=str(source_issue.id),
                        dependent_issue_id=str(dependent_issue.id),
                        reason=rel.reason or "No reason provided",
                        confidence_score=rel.confidence_score or 0.0,
                        issue_key=source_issue.key,
                        dependency_key=dependent_issue.key,
                        is_committed=rel.is_committed,
                        comes_from_tracker=rel.comes_from_tracker,
                    )
                )
        return DependencyResponse(dependencies=dependencies)

    logger.info(f"Cache miss for issue set. Calling AI model {ai_model.id}.")

    # 4. Construct the user prompt
    issue_details = []
    for issue in issues:
        detail = (
            f"ID: {issue.id}\n"
            f"Project: {issue.project.name}\n"
            f"Title: {issue.title}\n"
            f"Description: {issue.description or 'No description provided.'}"
        )
        issue_details.append(detail)

    user_prompt = (
        "Please analyze the following issues for dependencies:\n\n---\n"
        + "\n\n---\n".join(issue_details)
    )

    prompts_config = load_dependencies_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get("dependency_detection_v1")
    if not prompt_data or "system" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Dependency detection prompt not configured."
        )
    system_prompt = prompt_data["system"]

    # 5. Call the AI model
    try:
        creds_kwargs = resolve_model_call_credentials(ai_model, db=db)
        api_key = creds_kwargs.get("api_key") or openai.api_key
        client = openai.OpenAI(api_key=api_key, base_url=creds_kwargs.get("api_base"))

        response = client.chat.completions.create(
            model=ai_model.model_identifier,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )

        response_content = response.choices[0].message.content
        parsed_json = json.loads(response_content)

        dependencies_from_ai = parsed_json.get("dependencies", [])

        # 6. Store new relationships and add issue keys to the response
        for dep in dependencies_from_ai:
            source_id = dep.get("source_issue_id")
            target_id = dep.get("dependent_issue_id")

            # Validate that the AI is not hallucinating dependencies for issues not in the request
            if source_id not in request.issue_ids or target_id not in request.issue_ids:
                logger.warning(
                    f"AI returned dependency for an issue not in the request: {source_id} -> {target_id}"
                )
                continue

            try:
                crud_issue_relationship.create(
                    db,
                    source_issue_id=source_id,
                    target_issue_id=target_id,
                    type="depends_on",
                    reason=dep.get("reason"),
                    confidence_score=dep.get("confidence_score"),
                )
            except IntegrityError:
                db.rollback()
                logger.warning(
                    f"Duplicate dependency relationship: {source_id} -> {target_id}"
                )
            source_issue = issue_map.get(source_id)
            dependent_issue = issue_map.get(target_id)
            if source_issue:
                dep["issue_key"] = source_issue.key
            if dependent_issue:
                dep["dependency_key"] = dependent_issue.key
            dep["is_committed"] = False
            dep["comes_from_tracker"] = False

        # 7. Create a new IssueSet to mark this analysis as complete
        set_name = f"Analysis for {len(sorted_issue_ids)} issues at {datetime.utcnow().isoformat()}"
        crud_issue_set.create_and_remove_subsets(
            db,
            name=set_name,
            issue_ids=sorted_issue_ids,
            ai_model_id=ai_model.id,
            account_id=current_user.account_id,
        )

        return DependencyResponse(dependencies=dependencies_from_ai)

    except openai.APIError as e:
        logger.error(f"OpenAI API call failed: {e}")
        raise HTTPException(
            status_code=502, detail="Failed to get dependency analysis from AI model."
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Error parsing AI model response: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error processing AI model response: {e}"
        )


@router.post(
    "/issue-dependencies/commit",
    response_model=DependencyResponse,
    tags=["Issues"],
    summary="Commit a list of issue dependencies.",
)
async def commit_issue_dependencies(
    request: CommitDependenciesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Commits a list of issue dependency pairs. This will create the dependency in the external tracker and mark it as 'committed' in Preloop.

    - **dependencies**: A list of dependency pairs to commit.
    """
    tasks = []
    dependencies_to_commit = []

    for dep in request.dependencies:
        if dep.is_committed or dep.comes_from_tracker:
            continue

        # Eagerly load the required relationships to get to the tracker using CRUD layer
        source_issue = crud_issue.get_with_full_hierarchy(
            db, id=dep.source_issue_id, account_id=current_user.account_id
        )

        if (
            not source_issue
            or not source_issue.project
            or not source_issue.project.organization
            or not source_issue.project.organization.tracker
        ):
            logger.warning(
                f"Source issue {dep.source_issue_id} or its associations not found, skipping."
            )
            continue

        tracker = source_issue.project.organization.tracker
        if not tracker.is_active:
            logger.warning(
                f"Tracker for project {source_issue.project.name} is not active, skipping dependency."
            )
            continue

        try:
            client = await get_tracker_client(
                organization_id=str(source_issue.project.organization.id),
                project_id=str(source_issue.project.id),
                db=db,
                current_user=current_user,
            )

            if client:
                relation_type = "blocks"  # Assuming 'blocks' as a default relation type
                tasks.append(
                    client.add_relation(
                        issue_id=dep.issue_key,
                        target_issue_id=dep.dependency_key,
                        relation_type=relation_type,
                    )
                )
                dependencies_to_commit.append(dep)
            else:
                logger.error(
                    f"Failed to create tracker client for tracker {tracker.id}"
                )

        except Exception as e:
            logger.error(
                f"Failed to create client or relation for dependency {dep.issue_key} -> {dep.dependency_key}: {e}"
            )

    # Run all API calls concurrently
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                dep = dependencies_to_commit[i]
                logger.error(
                    f"Failed to create dependency {dep.issue_key} -> {dep.dependency_key} in external tracker: {result}"
                )

    # Commit all dependencies that were attempted, regardless of API call success
    updated_relationships = crud_issue_relationship.commit_relationships(
        db, relationships=[dep.dict() for dep in request.dependencies]
    )

    # We need to map the SQLAlchemy models back to the Pydantic model.
    response_dependencies = []
    for rel in updated_relationships:
        response_dependencies.append(
            DependencyPair(
                source_issue_id=str(rel.source_issue_id),
                dependent_issue_id=str(rel.target_issue_id),
                reason=rel.reason or "",
                confidence_score=rel.confidence_score or 0.0,
                issue_key=rel.source_issue.key,  # Assuming source_issue is loaded
                dependency_key=rel.target_issue.key,  # Assuming target_issue is loaded
                is_committed=rel.is_committed,
                comes_from_tracker=rel.comes_from_tracker,
            )
        )

    return DependencyResponse(dependencies=response_dependencies)


@router.post(
    "/issue-dependencies/extend",
    response_model=DependencyResponse,  # Assuming it returns a similar response
    tags=["Issues", "AI"],
    summary="Extend the dependency scan for a specific issue.",
)
def extend_dependency_scan(
    request: ExtendScanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
):
    """
    Extends the dependency scan for a specific issue to find more related issues.

    - **issue_ids**: The IDs of the issues to extend the scan from.
    - **extend_by**: The number of additional issues to fetch and analyze.
    """
    if not request.issue_ids:
        raise HTTPException(
            status_code=400, detail="At least one issue ID is required."
        )

    # Fetch the first issue to determine the project context
    initial_issue = crud_issue.get(
        db, id=request.issue_ids[0], account_id=current_user.account_id
    )
    if not initial_issue:
        raise HTTPException(
            status_code=404, detail=f"Issue with ID '{request.issue_ids[0]}' not found."
        )
    project_id = initial_issue.project_id

    # 1. Get all issues for the project
    # Note: get_for_project is paginated, we might need a way to get ALL issues.
    # Assuming for now it returns enough issues or we get all of them.
    project_issues = crud_issue.get_for_project(
        db, project_id=project_id, limit=1000, account_id=current_user.account_id
    )  # Arbitrary high limit
    project_issue_ids = {str(issue.id) for issue in project_issues}

    # Select AI Model (same logic as detect_issue_dependencies)
    ai_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not ai_model:
        raise HTTPException(status_code=404, detail="No default AI model configured.")

    # 2. Get all supersets containing the initial issues
    existing_sets = crud_issue_set.get_supersets_by_issues(
        db,
        issue_ids=request.issue_ids,
        ai_model_ids=[ai_model.id, None],
        account_id=current_user.account_id,
    )

    # 3. Union all retrieved sets to get the final set of issues
    known_issue_ids = set(request.issue_ids)
    for issue_set in existing_sets:
        known_issue_ids.update(issue_set.issue_ids)

    # 4. Subtract the final set of issues from project_set
    unknown_issue_ids = project_issue_ids - known_issue_ids

    if not unknown_issue_ids:
        return DependencyResponse(dependencies=[])  # No remaining issues to scan

    # 5. Get the n most recent issues from unknown_set
    unknown_issues = [
        issue for issue in project_issues if str(issue.id) in unknown_issue_ids
    ]
    # Already sorted by created_at desc in get_for_project
    query_issues = unknown_issues[: request.extend_by]
    query_issue_ids = {str(issue.id) for issue in query_issues}

    # Combine requested and unscanned issues for analysis
    analysis_issue_ids = list(set(request.issue_ids).union(query_issue_ids))
    analysis_issues = [
        issue for issue in project_issues if str(issue.id) in analysis_issue_ids
    ]
    issue_map = {str(issue.id): issue for issue in analysis_issues}

    # 6. Call AI model to analyze the query_set
    issue_details = []
    for issue in analysis_issues:
        detail = (
            f"ID: {issue.id}\n"
            f"Project: {issue.project.name}\n"
            f"Title: {issue.title}\n"
            f"Description: {issue.description or 'No description provided.'}"
        )
        issue_details.append(detail)

    user_prompt = (
        "Please analyze the following issues for dependencies:\n\n---\n"
        + "\n\n---".join(issue_details)
    )

    prompts_config = load_dependencies_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get("dependency_detection_v1")
    if not prompt_data or "system" not in prompt_data:
        raise HTTPException(
            status_code=500, detail="Dependency detection prompt not configured."
        )
    system_prompt = prompt_data["system"]

    try:
        creds_kwargs = resolve_model_call_credentials(ai_model, db=db)
        api_key = creds_kwargs.get("api_key") or openai.api_key
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=ai_model.model_identifier,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        response_content = response.choices[0].message.content
        dependencies_from_ai = json.loads(response_content).get("dependencies", [])

        new_dependencies = []
        # 7. Store the new relationships
        for dep in dependencies_from_ai:
            source_id = dep.get("source_issue_id")
            target_id = dep.get("dependent_issue_id")

            if (
                source_id not in analysis_issue_ids
                or target_id not in analysis_issue_ids
            ):
                continue  # Skip hallucinated issues

            # Only consider new dependencies involving the query set
            if source_id not in query_issue_ids and target_id not in query_issue_ids:
                continue

            try:
                crud_issue_relationship.create(
                    db,
                    source_issue_id=source_id,
                    target_issue_id=target_id,
                    type="depends_on",
                    reason=dep.get("reason"),
                    confidence_score=dep.get("confidence_score"),
                )

            except IntegrityError:
                db.rollback()  # Rollback the session to a clean state
                logger.warning(
                    f"Duplicate relationship skipped: {source_id}-{target_id}"
                )
            source_issue = issue_map.get(source_id)
            dependent_issue = issue_map.get(target_id)
            if source_issue:
                dep["issue_key"] = source_issue.key
            if dependent_issue:
                dep["dependency_key"] = dependent_issue.key
            dep["is_committed"] = False
            dep["comes_from_tracker"] = False
            new_dependencies.append(dep)

        # 7b. Store the new combined set in the IssueSet table
        set_name = f"Extended analysis for {len(analysis_issue_ids)} issues at {datetime.utcnow().isoformat()}"
        crud_issue_set.create_and_remove_subsets(
            db,
            name=set_name,
            issue_ids=sorted(analysis_issue_ids),
            ai_model_id=ai_model.id,
            account_id=current_user.account_id,
        )

        # 8. Return the dependencies found
        return DependencyResponse(dependencies=new_dependencies)

    except openai.APIError as e:
        logger.error(f"OpenAI API call failed: {e}")
        raise HTTPException(
            status_code=502, detail="Failed to get dependency analysis from AI model."
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error(f"Error parsing AI model response: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error processing AI model response: {e}"
        )
