"""Common utility functions for the Preloop API."""

import logging
from typing import Any, Dict, Optional, List, Union
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.api.auth import get_current_active_user
from preloop.schemas.issue_compliance import CompliancePromptMetadata
from preloop.sync.trackers import create_tracker_client
from preloop.models.crud import (
    CRUDOrganization,
    CRUDProject,
    crud_account,
    crud_tracker_scope_rule,
)
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
import yaml
import os

logger = logging.getLogger(__name__)

# Initialize CRUD operations
crud_organization = CRUDOrganization(Organization)
crud_project = CRUDProject(Project)


async def get_tracker_client(
    organization_id: Union[str, UUID],
    project_id: Union[str, UUID],
    db: Session,
    current_user: User,
):
    """Get the appropriate tracker client for the given organization and project,
    ensuring the current user has access.

    Args:
        organization_id: The organization ID or identifier.
        project_id: The project ID or identifier.
        db: Database session.
        current_user: The authenticated user.

    Returns:
        A tracker client instance.

    Raises:
        HTTPException: If the organization or project is not found, if the user
            does not have access, or if a tracker client cannot be created.
    """
    # Check if organization_id is a UUID or an identifier
    if isinstance(organization_id, UUID) or (
        isinstance(organization_id, str) and len(organization_id) == 36
    ):
        organization = crud_organization.get(
            db, id=organization_id, account_id=current_user.account_id
        )
    else:
        organization = crud_organization.get_by_identifier(
            db, identifier=str(organization_id), account_id=current_user.account_id
        )

    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Resolve project
    project: Optional[Project] = None
    if isinstance(project_id, UUID) or (
        isinstance(project_id, str) and len(project_id) == 36
    ):
        project = crud_project.get(
            db, id=project_id, account_id=current_user.account_id
        )
        # Verify it belongs to the correct organization
        if project and project.organization_id != organization.id:
            logger.warning(
                f"Project ID {project_id} found but belongs to wrong org ({project.organization_id} != {organization.id})"
            )
            project = None  # Treat as not found in this context
    else:
        # Assume project_id is a slug or identifier if not a UUID
        project_list = crud_project.get_by_slug_or_identifier(
            db,
            organization_id=organization.id,
            slug_or_identifier=project_id,
            account_id=current_user.account_id,
        )
        if len(project_list) == 1:
            project = project_list[0]
        elif len(project_list) > 1:
            # This shouldn't happen if org is specified, but handle defensively
            logger.error(
                f"Ambiguous project identifier '{project_id}' within organization '{organization.identifier}'."
            )
            raise HTTPException(
                status_code=400,
                detail=f"Ambiguous project identifier '{project_id}' within organization.",
            )

    if not project:
        # If project is still None after trying ID and slug/identifier
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_id}' not found within organization '{organization.identifier}'.",
        )

    # Get tracker details from the organization
    tracker = organization.tracker
    if not tracker:
        raise HTTPException(
            status_code=500, detail="Organization has no associated tracker."
        )

    # --- Authorization Check ---
    if tracker.account_id != current_user.account_id:
        logger.warning(
            f"Access denied: User {current_user.username} (Account ID: {current_user.account_id}) "
            f"attempted to access tracker {tracker.id} (Account ID: {tracker.account_id})."
        )
        raise HTTPException(
            status_code=403, detail="Forbidden: Access denied to this resource."
        )
    # --- End Authorization Check ---

    # --- New Scoping Logic ---
    scope_rules = crud_tracker_scope_rule.get_by_tracker(
        db, tracker_id=tracker.id, account_id=current_user.account_id
    )

    org_identifier = organization.identifier
    project_identifier = project.identifier

    org_rules = [rule for rule in scope_rules if rule.scope_type == "ORGANIZATION"]
    project_rules = [rule for rule in scope_rules if rule.scope_type == "PROJECT"]

    # Rule 1: Organization must be included
    org_included = any(
        rule.rule_type == "INCLUDE" and rule.identifier == org_identifier
        for rule in org_rules
    )
    org_excluded = any(
        rule.rule_type == "EXCLUDE" and rule.identifier == org_identifier
        for rule in org_rules
    )

    if org_excluded:
        raise HTTPException(
            status_code=403,
            detail="Access denied: Organization is explicitly excluded.",
        )
    if not org_included:
        raise HTTPException(
            status_code=403,
            detail="Access denied: Organization is not included in tracker scope.",
        )

    # Rule 2: Project must not be excluded
    project_excluded = any(
        rule.rule_type == "EXCLUDE" and rule.identifier == project_identifier
        for rule in project_rules
    )
    if project_excluded:
        raise HTTPException(
            status_code=403, detail="Access denied: Project is explicitly excluded."
        )

    # Rule 3: If project-level includes exist, project must be in the list
    project_level_includes = [
        rule for rule in project_rules if rule.rule_type == "INCLUDE"
    ]
    if project_level_includes:
        project_included = any(
            rule.identifier == project_identifier for rule in project_level_includes
        )
        if not project_included:
            raise HTTPException(
                status_code=403,
                detail="Access denied: Project is not in the tracker's include list.",
            )
    # --- End New Scoping Logic ---

    tracker_type = tracker.tracker_type

    # --- Assemble the full configuration ---
    # This will be passed as connection_details to the tracker client
    # Start with project-specific tracker settings from the database
    full_config: Dict[str, Any] = project.tracker_settings or {}

    # Add URL and other connection details from the main tracker object
    # The token/api_key is passed separately to the factory
    if tracker.url:
        full_config["url"] = tracker.url

    # Merge any other connection details from the tracker model's JSON field
    if tracker.connection_details:
        for key, value in tracker.connection_details.items():
            # Avoid overwriting settings that might have been set at the project level
            if key not in full_config:
                full_config[key] = value

    # Ensure project-specific identifiers are included, checking settings/metadata/identifier
    if tracker_type == "gitlab":
        if "project_id" not in full_config:
            # Check tracker_settings, then meta_data, then use project.identifier
            full_config["project_id"] = (
                (project.tracker_settings or {}).get("project_id")
                or (project.meta_data or {}).get("project_id")
                or project.identifier
            )
    elif tracker_type == "github":
        full_config["owner"] = organization.name
        full_config["repo"] = project.name
    elif tracker_type == "jira":
        # Jira might need project_key in config for some operations, add if available
        if "project_key" not in full_config:
            full_config["project_key"] = (
                (project.tracker_settings or {}).get("project_key")
                or (project.meta_data or {}).get("project_key")
                or project.identifier
            )

    # Add auth type to config for factory
    full_config["auth_type"] = tracker.auth_type

    # For GitHub App OAuth, add the installation ID
    if tracker.auth_type in ("github_app", "oauth_app") and tracker.oauth_installation:
        # Use the external_id which is the actual GitHub installation ID
        full_config["github_installation_id"] = tracker.oauth_installation.external_id
        logger.info(
            f"Using GitHub App OAuth with installation ID: {tracker.oauth_installation.external_id}"
        )

    logger.debug(
        f"Creating tracker client of type '{tracker_type}' with config: {full_config}"
    )

    try:
        # Create the tracker client using the combined config
        tracker_client = await create_tracker_client(
            tracker_type=tracker_type,
            tracker_id=str(tracker.id),
            api_key=tracker.resolved_api_key,  # May be empty for OAuth
            connection_details=full_config,
        )
        if not tracker_client:
            # Raise specific error if factory returns None (e.g., unsupported type or config error)
            raise ValueError(
                f"Failed to create tracker client for type '{tracker_type}'. Check configuration: {full_config}"
            )

        return tracker_client
    except ValueError as ve:  # Catch config errors from factory
        logger.error(f"Configuration error creating tracker client: {ve}")
        raise HTTPException(
            status_code=500, detail=f"Configuration error for tracker: {str(ve)}"
        )
    except Exception as e:
        logger.error(f"Error creating tracker client: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error creating tracker client: {str(e)}"
        )


def get_compliance_prompts_from_config(
    config_path: str,
) -> List[CompliancePromptMetadata]:
    """Load and parse compliance prompts from a YAML file."""
    if not os.path.exists(config_path):
        logger.error(f"Compliance config file not found at: {config_path}")
        return []

    try:
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {config_path}: {e}")
        return []

    compliance_section = config_data.get("compliance", {})
    if not compliance_section:
        logger.warning(f"'compliance' section not found or empty in {config_path}")
        return []

    prompts_metadata = [
        CompliancePromptMetadata(
            id=prompt_id,
            name=prompt_data.get("name", "Unnamed Prompt"),
            short_name=prompt_data.get("short_name", "N/A"),
        )
        for prompt_id, prompt_data in compliance_section.items()
    ]

    return prompts_metadata


def load_compliance_prompts_config(config_path: str) -> Dict[str, Any]:
    """Load the full compliance prompts configuration from a YAML file."""
    if not os.path.exists(config_path):
        logger.error(f"Compliance config file not found at: {config_path}")
        return {}

    try:
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {config_path}: {e}")
        return {}

    return config_data.get("compliance", {})


def load_dependencies_prompts_config(config_path: str) -> Dict[str, Any]:
    """Load the full dependency detection prompts configuration from a YAML file."""
    if not os.path.exists(config_path):
        logger.error(f"Prompts config file not found at: {config_path}")
        return {}

    try:
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {config_path}: {e}")
        return {}

    return config_data.get("dependencies", {})


def load_duplicates_prompts_config(config_path: str) -> Dict[str, Any]:
    """Load the full duplicates prompts configuration from a YAML file."""
    if not os.path.exists(config_path):
        logger.error(f"Prompts config file not found at: {config_path}")
        return {}

    try:
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {config_path}: {e}")
        return {}

    return config_data.get("duplicates", {})


def get_accessible_projects(
    db: Session,
    current_user: User,
    project_ids: Optional[List[str]] = None,
) -> List[Project]:
    """Get the list of accessible projects for the given user and project IDs.

    Applies TrackerScopeRule filtering to ensure only projects that match the
    tracker's scope configuration are returned. This follows the same logic as
    preloop sync scanner:
    1. Organization must be in INCLUDE rules
    2. Project must not be in EXCLUDE rules
    3. If PROJECT INCLUDE rules exist, project must be in them

    Args:
        db: Database session
        current_user: Current authenticated user
        project_ids: Optional list of project IDs to filter by. If None, returns
            all accessible projects for the user.

    Returns:
        List of Project objects that the user has access to and that match
        the tracker scope rules.
    """
    # Use CRUD layer to get projects accessible to user
    all_projects = crud_project.get_accessible_for_user(
        db, account_id=str(current_user.account_id), project_ids=project_ids
    )

    # Apply TrackerScopeRule filtering
    filtered_projects = []
    for project in all_projects:
        tracker = project.organization.tracker

        # Use CRUD layer to get scope rules for this tracker
        rules = crud_tracker_scope_rule.get_by_tracker(
            db, tracker_id=tracker.id, account_id=current_user.account_id
        )

        # Build rule sets (same logic as scanner/core.py)
        org_inclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "ORGANIZATION" and r.rule_type == "INCLUDE"
        }
        project_inclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "PROJECT" and r.rule_type == "INCLUDE"
        }
        project_exclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "PROJECT" and r.rule_type == "EXCLUDE"
        }

        # Check organization inclusion (required)
        if project.organization.identifier not in org_inclusions:
            logger.debug(
                f"Skipping project {project.name} - organization {project.organization.identifier} "
                f"not in inclusion list for tracker {tracker.id}"
            )
            continue

        # Check project exclusion
        if project.identifier in project_exclusions:
            logger.debug(
                f"Skipping project {project.name} ({project.identifier}) - in exclusion list"
            )
            continue

        # Check project inclusion (if any inclusion rules exist)
        if project_inclusions and project.identifier not in project_inclusions:
            logger.debug(
                f"Skipping project {project.name} ({project.identifier}) - not in inclusion list"
            )
            continue

        filtered_projects.append(project)

    return filtered_projects


def get_account_for_user(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Account:
    """Get Account model for authenticated user.

    This consolidated function replaces duplicate get_account() helpers
    found across multiple endpoint files. It uses the CRUD layer instead
    of direct database queries.

    Note: This is a convenience dependency. Prefer using the User.account
    relationship when possible to avoid extra database queries.

    Args:
        current_user: Authenticated user from JWT token (User model)
        db: Database session

    Returns:
        Account instance for the authenticated user

    Raises:
        HTTPException: 401 if account not found for user
    """
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found",
        )
    return account
