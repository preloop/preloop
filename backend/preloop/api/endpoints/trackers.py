"""Trackers router for registering and managing issue trackers."""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import UUID4, BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.schemas.auth import AuthUserResponse
from preloop.schemas.tracker import (
    TrackerResponse,
    TrackerUpdate,
    TrackerTestResponse,
    TrackerTestRequest,
)
from preloop.schemas.tracker import (
    ProjectIdentifier,
)  # Corrected import location


from preloop.sync.trackers import create_tracker_client
from preloop.utils.email import send_tracker_registered_email
from preloop.sync.services.event_bus import event_bus_service
from preloop.models.db.session import get_db_session


from preloop.models.models.tracker import Tracker, TrackerType, TrackerScopeRule


from preloop.models.crud import (
    crud_account,
    crud_tracker,
    crud_tracker_scope_rule,
)

from preloop.utils.audit import log_config_change
from preloop.utils.permissions import require_permission


logger = logging.getLogger(__name__)
router = APIRouter()


def _unique_tracker_name(db: Session, *, base_name: str, account_id: str) -> str:
    """Return a tracker name that is unique within the account."""
    candidate = base_name
    suffix = 2
    while crud_tracker.get_by_name(
        db, name=candidate, account_id=account_id, include_deleted=False
    ):
        candidate = f"{base_name} ({suffix})"
        suffix += 1
    return candidate


class TrackerCreateResponse(BaseModel):
    """Response model for tracker creation."""

    id: str
    warnings: Optional[List[str]] = None

    model_config = ConfigDict(from_attributes=True)


@router.post("/trackers/debug")
async def debug_tracker_request(request: Request):
    """Debug endpoint to see raw request data"""
    try:
        body = await request.json()
        print("DEBUG REQUEST BODY:", body)
        return {"received": body}
    except Exception as e:
        print("DEBUG ERROR:", str(e))
        return {"error": str(e)}


@router.post(
    "/trackers",
    status_code=status.HTTP_201_CREATED,
    response_model=TrackerCreateResponse,
)
@require_permission("create_trackers")
async def register_tracker(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> TrackerCreateResponse:
    """Register a new issue tracker.

    Args:
        tracker_data: Tracker registration data.
        background_tasks: Background tasks for sending emails.
        current_user: The current authenticated user.

    Returns:
        The registered tracker ID.

    Raises:
        HTTPException: If registration fails.
    """
    # Parse request body manually
    try:
        data = await request.json()
        print("Raw request data:", data)

        # Extract fields from the raw data
        name = data.get("name")
        tracker_type_str = data.get("type")
        url_str = data.get("url")
        api_key = data.get("api_key")
        config = data.get("config")
        scope_rules_data = data.get("scope_rules") or []
        auth_type = data.get("auth_type", "api_token")
        github_installation_id = data.get("github_installation_id")

        # Validate required fields based on auth type
        if not name or not tracker_type_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required fields: name, type",
            )

        # For github_app auth, api_key is not required
        if auth_type == "api_token" and not api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required field: api_key (required for API token authentication)",
            )

        # For OAuth auth types, github_installation_id is required
        if auth_type in ("github_app", "oauth_app") and not github_installation_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required field: github_installation_id (required for OAuth authentication)",
            )
    except Exception as e:
        logger.error(f"Error parsing request data: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request format: {str(e)}",
        )

    # Convert tracker_type string to enum
    try:
        tracker_type = TrackerType(tracker_type_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tracker type: {tracker_type_str}",
        )

    # For Jira, ensure username is present in config
    if tracker_type == TrackerType.JIRA and (not config or "username" not in config):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira tracker requires 'username' in connection_details",
        )

    # Create a tracker client to test the connection
    # For github_app auth, we need to resolve the installation_id to the actual GitHub installation ID
    resolved_github_installation_id = None
    permission_warnings = []
    installation = None  # Will be set for OAuth auth types

    try:
        if auth_type in ("github_app", "oauth_app"):
            # OAuth auth types are only supported for GitHub trackers
            if tracker_type != TrackerType.GITHUB:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"OAuth authentication is only supported for GitHub trackers, not {tracker_type.value}",
                )

            # Look up the OAuth App installation to get the actual installation ID
            from preloop.models.models.github_app_installation import (
                OAuthAppInstallation,
            )

            installation = (
                db.query(OAuthAppInstallation)
                .filter(
                    OAuthAppInstallation.external_id == github_installation_id,
                    OAuthAppInstallation.account_id == current_user.account_id,
                )
                .first()
            )

            if not installation:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="OAuth App installation not found",
                )

            resolved_github_installation_id = installation.external_id

            # For github_app auth, create client with installation token support
            from preloop.sync.trackers.github import GitHubTracker

            client = GitHubTracker(
                tracker_id="test-connection",
                api_key="",  # Not needed for github_app auth
                connection_details={
                    "url": str(url_str) if url_str else None,
                    **(config or {}),
                },
                auth_type="github_app",
                github_installation_id=resolved_github_installation_id,
            )
        else:
            # Create the client for api_token auth
            client = await create_tracker_client(
                tracker_type=tracker_type.value,
                tracker_id="test-connection",
                api_key=api_key,
                connection_details={
                    "url": str(url_str) if url_str else None,
                    **(config or {}),
                },
            )

        # Test the connection
        connection_result = await client.test_connection()

        if not connection_result.connected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to connect to tracker: {connection_result.message}",
            )

        # For GitHub trackers with api_token auth, validate token permissions and warn about missing scopes
        if (
            tracker_type == TrackerType.GITHUB
            and auth_type == "api_token"
            and hasattr(client, "validate_token_permissions")
        ):
            # Extract organization identifiers from scope rules to check admin access
            org_identifiers = []
            for rule in scope_rules_data:
                if (
                    rule.get("scope_type") == "ORGANIZATION"
                    and rule.get("rule_type") == "INCLUDE"
                ):
                    identifier = rule.get("identifier")
                    if identifier:  # Only append valid identifiers
                        org_identifiers.append(identifier)

            # First, validate basic token permissions (scopes) without org-specific checks
            permission_result = await client.validate_token_permissions(None)

            if not permission_result["valid"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"GitHub token validation failed: {', '.join(permission_result['errors'])}",
                )

            # Collect basic scope warnings
            permission_warnings = permission_result.get("warnings", [])

            # Now validate admin access for each organization in scope rules
            for org_id in org_identifiers:
                org_result = await client.validate_token_permissions(org_id)
                # Add org-specific warnings (skip duplicate scope warnings)
                for warning in org_result.get("warnings", []):
                    if warning not in permission_warnings:
                        permission_warnings.append(warning)
                # Add any org-specific errors as warnings (don't fail registration)
                for error in org_result.get("errors", []):
                    if error not in permission_warnings:
                        permission_warnings.append(error)

            if permission_warnings:
                logger.warning(
                    f"GitHub token permission warnings for tracker '{name}': {permission_warnings}"
                )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error testing tracker connection: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to connect to tracker: {str(e)}",
        )

    # Create a new tracker in the database
    try:
        # Find current user's account using CRUD layer
        account = crud_account.get(db, id=current_user.account_id)
        if not account:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account not found",
            )

        # OAuth tracker registrations may reuse a default display name; pick a
        # unique variant instead of failing when names collide.
        if auth_type in ("github_app", "oauth_app"):
            unique_name = _unique_tracker_name(
                db, base_name=name, account_id=str(account.id)
            )
            if unique_name != name:
                logger.info(
                    "Renamed tracker from '%s' to '%s' for account %s",
                    name,
                    unique_name,
                    account.id,
                )
                name = unique_name
        else:
            existing_tracker = crud_tracker.get_by_name(
                db, name=name, account_id=account.id, include_deleted=False
            )
            if existing_tracker:
                logger.warning(
                    f"Tracker with name '{name}' already exists for account {account.id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"A tracker with name '{name}' already exists for your account"
                    ),
                )

        # Log information before attempting to create the tracker
        logger.info(
            f"Creating new tracker: name='{name}', "
            f"type={tracker_type.value}, account_id={account.id}"
        )

        # Validate scope rules before creating the tracker
        if scope_rules_data:
            is_valid, error_message = crud_tracker_scope_rule.validate_scope_rules(
                scope_rules_data
            )
            if not is_valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid scope rules: {error_message}",
                )

        # Create the tracker with the account reference and project selection fields
        scope_rules = []
        for rule in scope_rules_data:
            if hasattr(rule.get("scope_type"), "value"):
                rule["scope_type"] = rule["scope_type"].value
            if hasattr(rule.get("rule_type"), "value"):
                rule["rule_type"] = rule["rule_type"].value
            scope_rules.append(TrackerScopeRule(**rule))

        # For OAuth auth types, use the internal installation UUID
        # (installation is looked up earlier in the flow)
        oauth_installation_uuid = None
        if auth_type in ("github_app", "oauth_app") and installation:
            oauth_installation_uuid = installation.id

        # Persist via CRUD so credentials are encrypted at rest (never write
        # plaintext api_key columns from the endpoint).
        new_tracker = crud_tracker.create(
            db,
            obj_in={
                "name": name,
                "tracker_type": tracker_type.value,
                "url": str(url_str) if url_str else None,
                "api_key": api_key or None,
                "connection_details": config or {},
                "account_id": account.id,
                "is_active": True,
                "meta_data": {},
                "auth_type": auth_type,
                "oauth_installation_id": oauth_installation_uuid,
            },
        )
        if scope_rules:
            new_tracker.scope_rules = scope_rules
            db.commit()
            db.refresh(new_tracker)

        log_config_change(
            db,
            user=current_user,
            config_type="tracker",
            action="created",
            new_value={
                "id": str(new_tracker.id),
                "name": new_tracker.name,
                "type": new_tracker.tracker_type,
            },
        )

        # Send email notification
        if current_user.email and current_user.email_verified:
            background_tasks.add_task(
                send_tracker_registered_email,
                user_email=current_user.email,
                tracker_name=new_tracker.name,
                tracker_type=new_tracker.tracker_type,
            )

        # Send NATS event
        await event_bus_service.publish_task("poll_tracker", str(new_tracker.id))

        # Build response with warnings if any
        response = {"id": str(new_tracker.id)}
        if permission_warnings:
            response["warnings"] = permission_warnings
        return response

    except IntegrityError as e:
        db.rollback()
        error_msg = str(e)
        constraint_info = ""
        if "unique constraint" in error_msg.lower():
            if "name" in error_msg.lower() and "account_id" in error_msg.lower():
                constraint_info = (
                    "A tracker with this name already exists for your account."
                )
            elif (
                "url" in error_msg.lower()
            ):  # Assuming URL might be unique per account too
                constraint_info = (
                    "A tracker with this URL already exists for your account."
                )
            else:
                constraint_info = (
                    "A duplicate entry exists (e.g., identifier conflict)."
                )
        logger.error(f"IntegrityError during tracker registration: {error_msg}")
        detail_msg = f"Database constraint violation: {constraint_info or error_msg}"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail_msg,
        )
    except Exception as e:
        db.rollback()
        logger.exception(
            f"Error registering tracker: {str(e)}"
        )  # Use logger.exception for stack trace
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error registering tracker: {str(e)}",
        )


@router.get("/trackers", response_model=List[TrackerResponse])
@require_permission("view_trackers")
def list_trackers(
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[Tracker]:
    """List all non-deleted trackers for the current user."""
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    # Use CRUD layer to get trackers
    trackers = crud_tracker.get_for_account(db, account_id=account.id)
    return trackers  # FastAPI handles conversion via response_model


@router.get("/trackers/{tracker_id}", response_model=TrackerResponse)
@require_permission("view_trackers")
def get_tracker(
    tracker_id: UUID4,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Tracker:
    """Get a non-deleted tracker by ID, ensuring it belongs to the current user."""
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    # Use CRUD layer to get tracker
    tracker = crud_tracker.get_by_id_and_account(
        db, id=str(tracker_id), account_id=account.id, include_deleted=False
    )

    if not tracker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracker not found or access denied",
        )

    # Note: Projects are not included in TrackerResponse by default.
    # If needed, fetch projects separately or adjust the response model.
    return tracker  # FastAPI handles conversion via response_model


@router.put("/trackers/{tracker_id}", response_model=TrackerResponse)
@require_permission("edit_trackers")
async def update_tracker(
    tracker_id: UUID4,
    tracker_update: TrackerUpdate,  # Use new update schema
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Tracker:
    """Update an existing tracker."""
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    # Use CRUD layer to get tracker
    tracker = crud_tracker.get_by_id_and_account(
        db, id=str(tracker_id), account_id=account.id, include_deleted=False
    )

    if not tracker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracker not found or access denied",
        )
    update_data = tracker_update.model_dump(exclude_unset=True)

    # Handle scope_rules separately
    if "scope_rules" in update_data:
        # Validate new scope rules before updating
        new_scope_rules_data = update_data.pop("scope_rules")
        if new_scope_rules_data is not None:
            is_valid, error_message = crud_tracker_scope_rule.validate_scope_rules(
                new_scope_rules_data
            )
            if not is_valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid scope rules: {error_message}",
                )

        # Delete existing scope rules using CRUD layer
        crud_tracker.delete_scope_rules(db, tracker_id=tracker.id)

        # Create new scope rules from the payload
        if new_scope_rules_data is not None:
            new_scope_rules = []
            for rule_data in new_scope_rules_data:
                if hasattr(rule_data.get("scope_type"), "value"):
                    rule_data["scope_type"] = rule_data["scope_type"].value
                if hasattr(rule_data.get("rule_type"), "value"):
                    rule_data["rule_type"] = rule_data["rule_type"].value
                new_scope_rules.append(TrackerScopeRule(**rule_data))
            tracker.scope_rules = new_scope_rules

    if update_data.get("api_key") == "unchanged":
        del update_data["api_key"]

    # Special handling if api_key is updated - revalidate connection.
    api_key_updated = "api_key" in update_data
    if api_key_updated:
        update_data["is_valid"] = False
        update_data["last_validation"] = None
        update_data["validation_message"] = "API key updated, revalidation needed."
        logger.info(
            "API key updated for tracker %s, marked for revalidation.", tracker.id
        )

    try:
        logger.info("Updating tracker %s with data: %s", tracker_id, update_data)
        # Route through CRUD so credential fields encrypt via Secret Service.
        tracker = crud_tracker.update(db, db_obj=tracker, obj_in=update_data)

        log_config_change(
            db,
            user=current_user,
            config_type="tracker",
            action="updated",
            new_value={
                "id": str(tracker.id),
                "name": tracker.name,
                "type": tracker.tracker_type,
            },
        )

        # Send NATS event (convert UUID to string for JSON serialization)
        await event_bus_service.publish_task("poll_tracker", str(tracker.id))

        return tracker
    except IntegrityError as e:
        db.rollback()
        logger.error(f"IntegrityError updating tracker {tracker_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Update failed due to database constraint.",
        )
    except Exception as e:
        db.rollback()
        logger.exception(f"Error updating tracker {tracker_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error updating tracker.",
        )


@router.post("/trackers/{tracker_id}/sync", status_code=status.HTTP_202_ACCEPTED)
@require_permission("edit_trackers")
async def sync_tracker(
    tracker_id: UUID4,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Queue a background sync for an existing tracker."""
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    tracker = crud_tracker.get_by_id_and_account(
        db, id=str(tracker_id), account_id=account.id, include_deleted=False
    )
    if not tracker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracker not found or access denied",
        )

    await event_bus_service.publish_task("poll_tracker", str(tracker.id))
    return {"status": "queued", "tracker_id": str(tracker.id)}


@router.delete("/trackers/{tracker_id}", status_code=status.HTTP_200_OK)
@require_permission("delete_trackers")
async def delete_tracker(
    tracker_id: UUID4,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    hard_delete: bool = False,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Delete a tracker by ID (soft delete by default, hard delete if specified)."""
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    # Use CRUD layer to get tracker, including potentially soft-deleted ones if hard_delete is true
    tracker = crud_tracker.get_by_id_and_account(
        db, id=str(tracker_id), account_id=account.id, include_deleted=hard_delete
    )

    if not tracker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tracker not found or access denied",
        )

    if hard_delete:
        logger.warning(
            f"Performing hard delete for tracker ID: {tracker.id} for user: {current_user.username}"
        )
        # TODO: Consider implications - delete related orgs/projects/issues?
        # Cascade might handle this, but needs verification.
        db.delete(tracker)
        message = "Tracker hard deleted successfully"
    else:
        if tracker.is_deleted:
            # Already soft-deleted, maybe return 200 OK or 404? Let's return OK.
            message = "Tracker already soft deleted"
        else:
            logger.info(
                f"Performing soft delete for tracker ID: {tracker.id} for user: {current_user.username}"
            )
            tracker.is_deleted = True
            tracker.is_active = False  # Also mark as inactive
            db.add(tracker)
            message = "Tracker soft deleted successfully"

    try:
        tracker_name = tracker.name  # capture before potential flush
        db.commit()

        log_config_change(
            db,
            user=current_user,
            config_type="tracker",
            action="deleted",
            old_value={"id": str(tracker_id), "name": tracker_name},
        )

        # Trigger webhook cleanup task
        logger.info(f"Scheduling webhook cleanup for deleted tracker {tracker_id}")
        await event_bus_service.publish_task(
            "cleanup_tracker_webhooks", str(tracker_id)
        )

        return {"message": message}
    except Exception as e:
        db.rollback()
        logger.exception(
            f"Error during tracker deletion (ID: {tracker_id}): {e}"
        )  # Use logger.exception for stack trace
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting tracker.",
        )


@router.post("/trackers/test-and-list-orgs", response_model=TrackerTestResponse)
@require_permission("manage_trackers")
async def test_connection_and_list_orgs(
    test_data: TrackerTestRequest,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> TrackerTestResponse:
    """
    Tests connection to a tracker and lists accessible organizations/groups.
    This endpoint does not fetch the projects within each organization.
    """
    logger.info(
        f"User {current_user.username} testing tracker connection for type {test_data.tracker_type.value}"
    )
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    if test_data.tracker_id:
        # Use CRUD layer to get tracker
        tracker = crud_tracker.get_by_id_and_account(
            db, id=test_data.tracker_id, account_id=account.id, include_deleted=False
        )
        if not tracker:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tracker not found or access denied",
            )
        if test_data.api_key == "unchanged":
            test_data.api_key = tracker.resolved_api_key
    try:
        client = await create_tracker_client(
            tracker_type=test_data.tracker_type.value,
            tracker_id="test-connection",
            api_key=test_data.api_key,
            connection_details={
                "url": str(test_data.url) if test_data.url else None,
                **(test_data.connection_details or {}),
            },
        )
        if not client:
            raise ValueError(
                f"Could not create client for type {test_data.tracker_type.value}"
            )

        connection_result = await client.test_connection()
        if not connection_result.connected:
            logger.warning(
                f"Connection test failed for user {current_user.username}: {connection_result.message}"
            )
            return TrackerTestResponse(
                success=False, message=connection_result.message, orgs=[]
            )

        logger.info(f"Connection test successful for user {current_user.username}")

        orgs = await client.get_organizations()
        if len(orgs) == 1:
            projects = await client.get_projects(orgs[0]["id"])
            orgs[0]["children"] = [
                ProjectIdentifier(
                    id=p["id"], name=p["name"], identifier=p["id"], type="project"
                )
                for p in projects
            ]
        return TrackerTestResponse(
            success=True,
            message="Connection successful!",
            orgs=orgs,
        )

    except Exception as e:
        logger.exception(
            f"Error during tracker org list for user {current_user.username}: {e}"
        )
        return TrackerTestResponse(
            success=False, message=f"An unexpected error occurred: {e}", orgs=[]
        )


@router.post("/trackers/list-projects-for-org", response_model=List[ProjectIdentifier])
@require_permission("manage_trackers")
async def list_projects_for_org(
    project_data: TrackerTestRequest,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ProjectIdentifier]:
    """
    Lists projects for a specific organization/group within a tracker.
    """
    account = crud_account.get(db, id=current_user.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User account not found"
        )

    logger.info(
        f"User {current_user.username} listing projects for org {project_data.organization_identifier} "
        f"in tracker type {project_data.tracker_type.value}"
    )
    if project_data.tracker_id:
        # Use CRUD layer to get tracker
        tracker = crud_tracker.get_by_id_and_account(
            db, id=project_data.tracker_id, account_id=account.id, include_deleted=False
        )
        if not tracker:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tracker not found or access denied",
            )
        if project_data.api_key == "unchanged":
            project_data.api_key = tracker.resolved_api_key
    try:
        if project_data.url and not project_data.url.endswith("/"):
            project_data.url = project_data.url + "/"
        client = await create_tracker_client(
            tracker_type=project_data.tracker_type.value,
            tracker_id="list-projects",
            api_key=project_data.api_key,
            connection_details={
                "url": str(project_data.url) if project_data.url else None,
                **(project_data.connection_details or {}),
            },
        )
        if not client:
            raise HTTPException(
                status_code=400, detail="Could not create tracker client"
            )
        return await client.get_projects(project_data.organization_identifier)

    except Exception as e:
        logger.exception(
            f"Error listing projects for org for user {current_user.username}: {e}"
        )
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {e}"
        )
