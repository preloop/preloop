"""Endpoints for managing issue compliance."""

import logging
import json
import os
from typing import List, Dict

import openai
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.config import get_settings, Settings
from preloop.schemas.issue import IssueResponse, IssueUpdate
from preloop.services.model_credentials import resolve_model_call_credentials
from preloop.schemas.issue_compliance import (
    ComplianceSuggestionResponse,
    CompliancePromptMetadata,
    ComplianceWorkflow,
)
from preloop.schemas.issue_compliance import (
    IssueComplianceResultCreate,
    IssueComplianceResultResponse,
)
from .issues import update_issue
from preloop.models.crud import (
    CRUDIssue,
    CRUDIssueComplianceResult,
    CRUDAIModel,
    CRUDOrganization,
    CRUDProject,
)
from preloop.models.crud.issue_duplicate import crud_issue_duplicate
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.models.issue import Issue
from preloop.models.models.issue_compliance_result import IssueComplianceResult
from preloop.models.models.ai_model import AIModel
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.api.common import (
    get_compliance_prompts_from_config,
    load_compliance_prompts_config,
)

logger = logging.getLogger(__name__)

router = APIRouter()

crud_issue = CRUDIssue(Issue)
crud_issue_compliance_result = CRUDIssueComplianceResult(IssueComplianceResult)
crud_ai_model = CRUDAIModel(AIModel)
crud_project = CRUDProject(Project)
crud_organization = CRUDOrganization(Organization)


@router.get(
    "/issue_compliance_prompts",
    response_model=List[CompliancePromptMetadata],
    tags=["Issues"],
)
async def get_compliance_prompts(
    settings: Settings = Depends(get_settings),
) -> List[CompliancePromptMetadata]:
    """Get a list of available compliance prompts."""
    return get_compliance_prompts_from_config(settings.PROMPTS_FILE)


def _calculate_issue_compliance(
    issue_id: str,
    prompt_name: str,
    db: Session,
    current_user: Account,
    settings: Settings,
) -> IssueComplianceResultResponse:
    """Get or calculate the compliance result for a given issue."""

    prompts_config = load_compliance_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get(prompt_name)

    if not prompt_data:
        raise HTTPException(
            status_code=404, detail=f"Prompt '{prompt_name}' not found."
        )

    prompt_template = ComplianceWorkflow(**prompt_data)

    existing_result = crud_issue_compliance_result.get_by_issue_id_and_prompt_id(
        db, issue_id=issue_id, prompt_id=prompt_name, account_id=current_user.account_id
    )
    if existing_result:
        existing_result.short_name = prompt_template.short_name
        return existing_result

    issue = crud_issue.get(db, id=issue_id, account_id=current_user.account_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    project = crud_project.get(db, id=issue.project_id)

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        raise HTTPException(
            status_code=500, detail="No default active AI model configured."
        )

    evaluate_prompt = prompt_template.evaluate

    user_prompt = evaluate_prompt.user.format(
        issue_title=issue.title or "N/A",
        issue_description=issue.description or "No description provided.",
        project_name=project.name,
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": evaluate_prompt.system},
        {"role": "user", "content": user_prompt},
    ]

    try:
        creds_kwargs = resolve_model_call_credentials(default_model, db=db)
        api_key = creds_kwargs.get("api_key")
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise HTTPException(
                status_code=500, detail="OpenAI API key not configured."
            )

        client = openai.OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=default_model.model_identifier,
            messages=messages,
            response_format={"type": "json_object"},
        )
        llm_response_text = response.choices[0].message.content.strip()

        response_obj = json.loads(llm_response_text)

        compliance_factor = response_obj.get("compliance_factor")
        reason = response_obj.get("reason")
        suggestion = response_obj.get("suggestion")
        annotated_description = response_obj.get("annotated_description")

    except openai.APIError as e:
        raise HTTPException(status_code=500, detail=f"AI model API error: {e}")
    except (ValueError, IndexError) as e:
        raise HTTPException(
            status_code=500, detail=f"Error parsing AI model response: {e}"
        )

    compliance_result_in = IssueComplianceResultCreate(
        issue_id=issue_id,
        prompt_id=prompt_name,
        name=prompt_template.name,
        compliance_factor=compliance_factor,
        reason=reason,
        suggestion=suggestion,
        annotated_description=annotated_description,
    )

    new_result = crud_issue_compliance_result.create(
        db, obj_in=compliance_result_in.model_dump()
    )

    new_result.short_name = prompt_template.short_name
    return new_result


@router.get(
    "/issue_compliance/{issue_id}",
    response_model=IssueComplianceResultResponse,
    tags=["Issues"],
)
def get_issue_compliance(
    issue_id: str,
    prompt_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
):
    """Get or calculate the compliance result for a given issue."""
    return _calculate_issue_compliance(
        issue_id=issue_id,
        prompt_name=prompt_name,
        db=db,
        current_user=current_user,
        settings=settings,
    )


@router.get(
    "/issue_compliance_suggestion/{issue_id}",
    response_model=ComplianceSuggestionResponse,
)
def get_compliance_improvement_suggestion(
    issue_id: str,
    prompt_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    settings: Settings = Depends(get_settings),
):
    """Generate a compliance improvement suggestion for a given issue."""
    compliance_result = _calculate_issue_compliance(
        issue_id=issue_id,
        prompt_name=prompt_name,
        db=db,
        current_user=current_user,
        settings=settings,
    )

    issue = crud_issue.get(db, id=issue_id)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Authorization check
    project = crud_project.get(db, id=issue.project_id)
    organization = crud_organization.get(db, id=project.organization_id)
    if (
        not organization
        or not organization.tracker
        or organization.tracker.account_id != current_user.account_id
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    default_model = crud_ai_model.get_default_active_model(
        db, account_id=current_user.account_id
    )
    if not default_model:
        logger.error("No default active AI model configured.")
        raise HTTPException(
            status_code=500, detail="No default active AI model configured."
        )

    prompts_config = load_compliance_prompts_config(settings.PROMPTS_FILE)
    prompt_data = prompts_config.get(prompt_name)
    if not prompt_data:
        raise HTTPException(
            status_code=404, detail=f"Prompt '{prompt_name}' not found."
        )

    try:
        prompt_template = ComplianceWorkflow(**prompt_data)
    except Exception as e:
        logger.error(f"Error parsing prompt template '{prompt_name}': {e}")
        raise HTTPException(
            status_code=500, detail=f"Invalid prompt configuration for '{prompt_name}'."
        )

    improvement_prompt = prompt_template.propose_improvement
    if not improvement_prompt:
        raise HTTPException(
            status_code=500,
            detail=f"'propose_improvement' section not found for prompt '{prompt_name}'.",
        )

    system_prompt = improvement_prompt.system
    user_prompt = improvement_prompt.user.format(
        issue_title=issue.title,
        issue_description=issue.description or "",
        project_name=project.name,
        compliance_score=compliance_result.compliance_factor,
        compliance_reason=compliance_result.reason,
        compliance_suggestion=compliance_result.suggestion,
    )

    client = openai.OpenAI()
    try:
        llm_response = client.chat.completions.create(
            model=default_model.model_identifier,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        suggestion_data = json.loads(llm_response.choices[0].message.content)
        return ComplianceSuggestionResponse(**suggestion_data)

    except openai.APIError as e:
        logger.error(f"OpenAI API call failed: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to get compliance suggestion from AI model."
        )


@router.patch(
    "/issue_compliance_update/{issue_id}", response_model=IssueResponse, tags=["Issues"]
)
async def update_issue_content(
    issue_id: str,
    issue_update: IssueUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update the title and description of an issue and sync to tracker."""
    # Delete any existing compliance results for this issue
    crud_issue_compliance_result.delete_by_issue_id(db, issue_id=issue_id)

    # Remove any duplicate pairs associated with this issue
    crud_issue_duplicate.remove_by_issue_id(db, issue_id=issue_id)

    # Convert the compliance-specific update schema to the general API update schema
    api_issue_update = IssueUpdate(
        title=issue_update.title,
        description=issue_update.description,
        comment="Issue content updated for compliance.",
    )

    # Call the centralized update_issue function which handles DB and tracker updates
    return await update_issue(
        issue_id=issue_id,
        issue_update=api_issue_update,
        db=db,
        current_user=current_user,
    )
