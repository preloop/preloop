"""Endpoints for managing projects."""

import datetime
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_accessible_projects
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

from preloop.schemas.project import (
    ProjectCreate,
    ProjectResponse,
    ProjectTransferReceipt,
    ProjectTransferRequest,
    RepositoryLocation,
    ProjectUpdate,
    TestConnectionRequest,
    TestConnectionResponse,
)
from preloop.services.managed_credentials import tracker_credential_source
from preloop.sync.trackers import create_tracker_client
from preloop.models.crud.organization import CRUDOrganization
from preloop.models.crud.project import (
    REPOSITORY_ID_TRACKER_TYPES,
    CRUDProject,
    find_same_repository_projects,
    lock_repository_identity,
    repository_host,
)
from preloop.models.crud.tracker import CRUDTracker
from preloop.models.crud.issue import CRUDIssue
from preloop.models.crud.embedding import CRUDEmbeddingModel, CRUDIssueEmbedding
from preloop.models.db.session import get_db_session as get_db
from preloop.models.db.session import release_transaction
from preloop.models.models.user import User
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.models.models.issue import Issue
from preloop.models.models.issue import IssueEmbedding, EmbeddingModel
from preloop.models.models.tracker_scope_rule import TrackerScopeRule
from preloop.models.models.flow import Flow
from preloop.models.models.webhook import Webhook
from preloop.models.crud import crud_audit_log

logger = logging.getLogger(__name__)
router = APIRouter()
crud_project = CRUDProject(Project)
crud_organization = CRUDOrganization(Organization)
crud_tracker = CRUDTracker(Tracker)
crud_issue = CRUDIssue(Issue)
crud_issue_embedding = CRUDIssueEmbedding(IssueEmbedding)
crud_embedding_model = CRUDEmbeddingModel(EmbeddingModel)


def _get_organization_in_account(
    db: Session, organization_id: object, account_id: object
) -> Optional[Organization]:
    """Return the organization only if its tracker belongs to ``account_id``.

    Organizations carry no account column; ownership is their tracker's
    account. Unlike ``crud_organization.get``, a malformed id returns None
    instead of reaching the database.
    """
    try:
        org_uuid = uuid.UUID(str(organization_id))
    except ValueError:
        return None
    return (
        db.query(Organization)
        .join(Organization.tracker)
        .filter(Organization.id == org_uuid, Tracker.account_id == account_id)
        .first()
    )


def _get_project_in_account(
    db: Session, project_id: object, account_id: object
) -> Optional[Project]:
    """Return the project only if its organization's tracker is in the account."""
    try:
        project_uuid = uuid.UUID(str(project_id))
    except ValueError:
        return None
    return (
        db.query(Project)
        .join(Project.organization)
        .join(Organization.tracker)
        .filter(Project.id == project_uuid, Tracker.account_id == account_id)
        .first()
    )


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=201,
    responses={
        400: {"description": "The identifier already exists in this organization"},
        404: {"description": "Organization not found in the caller's account"},
        409: {
            "description": (
                "repository_already_registered: the repository is already a "
                "project in another organization; move it with "
                "POST /api/v1/projects/{project_id}/transfer"
            )
        },
    },
)
@require_permission("create_projects")
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Create a new project, ensuring user has access to the organization."""
    # Check the organization exists and belongs to the caller's account
    organization = _get_organization_in_account(
        db, project.organization_id, current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Both duplicate checks below and the insert run under this lock, held
    # until crud_project.create commits, so concurrent creates or transfers
    # of one repository cannot both pass the checks.
    if organization.tracker is not None:
        lock_repository_identity(
            db,
            account_id=current_user.account_id,
            host=repository_host(organization.tracker),
            identifier=str(project.identifier),
        )

    # Check if project with this identifier already exists in the organization
    existing_project = crud_project.get_by_identifier(
        db, organization_id=project.organization_id, identifier=project.identifier
    )
    if existing_project:
        raise HTTPException(
            status_code=400,
            detail=f"Project with identifier '{project.identifier}' already exists in this organization",
        )

    # Create new project using CRUD operation
    # Note: Adapter code to handle the tracker_configurations from the old model
    # In SpaceModels, we have tracker_settings instead
    project_data = {
        "id": str(uuid.uuid4()),
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings or {},
        "tracker_settings": project.tracker_configurations or {},
        "meta_data": {},
    }

    # A repository keeps its ID when it moves between organisations. A second
    # registration under the new owner would split its issues and reviews
    # across two records and make webhook routing ambiguous (#1159).
    registered = find_same_repository_projects(
        db,
        organization=organization,
        identifier=project.identifier,
        account_id=current_user.account_id,
    )
    if registered:
        existing = registered[0]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "repository_already_registered",
                "message": (
                    f"Repository '{project.identifier}' is already registered as "
                    f"project {existing.id} in another organization. If the "
                    "repository moved, preview and apply the move with "
                    f"POST /api/v1/projects/{existing.id}/transfer instead of "
                    "creating a second project."
                ),
                "existing_project_id": str(existing.id),
                "existing_organization_id": str(existing.organization_id),
            },
        )

    db_project = crud_project.create(db, obj_in=project_data)

    # ProjectResponse declares the timestamps as strings. Returning datetime
    # objects failed response validation after the row was committed, which
    # surfaced as an HTTP 500 for every create (#1159).
    return {
        "id": db_project.id,
        "name": db_project.name,
        "identifier": db_project.identifier,
        "description": db_project.description,
        "organization_id": db_project.organization_id,
        "settings": db_project.settings,
        "tracker_configurations": db_project.tracker_settings,
        "created_at": db_project.created_at.isoformat(),
        "updated_at": db_project.updated_at.isoformat(),
    }


@router.get("/projects", response_model=List[ProjectResponse])
@require_permission("view_projects")
def list_projects(
    organization_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> List[dict]:
    """List projects accessible to the current user, applying scope rules and optionally filtered by organization."""
    # Use get_accessible_projects which applies TrackerScopeRule filtering
    accessible_projects = get_accessible_projects(
        db=db,
        current_user=current_user,
        project_ids=None,  # Get all accessible projects
    )

    # Apply organization filter if provided
    if organization_id:
        # Check organization access first
        organization = _get_organization_in_account(
            db, organization_id, current_user.account_id
        )
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Filter to only projects in this organization
        accessible_projects = [
            p for p in accessible_projects if p.organization_id == organization_id
        ]

    # Apply pagination
    paginated_projects = accessible_projects[offset : offset + limit]

    # Convert datetime objects to ISO format strings
    result = []
    for project in paginated_projects:
        result.append(
            {
                "id": project.id,
                "name": project.name,
                "identifier": project.identifier,
                "description": project.description,
                "organization_id": project.organization_id,
                "settings": project.settings,
                "tracker_configurations": project.tracker_settings,
                "created_at": project.created_at.isoformat(),
                "updated_at": project.updated_at.isoformat(),
                "group": (
                    project.meta_data.get("project_name")
                    if isinstance(project.meta_data, dict)
                    else None
                ),
            }
        )

    return result


@router.get("/organizations/{organization_id}/projects")
@require_permission("view_projects")
def list_organization_projects(
    organization_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all projects for an organization, applying scope rules and ensuring user has access."""
    # Check if organization exists
    organization = _get_organization_in_account(
        db, organization_id, current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Use get_accessible_projects which applies TrackerScopeRule filtering
    accessible_projects = get_accessible_projects(
        db=db,
        current_user=current_user,
        project_ids=None,
    )

    # Filter to only projects in this organization
    org_projects = [
        p for p in accessible_projects if p.organization_id == organization_id
    ]

    # Apply pagination
    total = len(org_projects)
    paginated_projects = org_projects[offset : offset + limit]

    # Convert SQLAlchemy model objects to dictionaries
    project_dicts = []
    for project in paginated_projects:
        project_dict = {
            "id": project.id,
            "name": project.name,
            "identifier": project.identifier,
            "description": project.description,
            "is_active": project.is_active,
            "organization_id": project.organization_id,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "settings": project.settings or {},
            "tracker_settings": project.tracker_settings or {},
            "meta_data": project.meta_data or {},
        }
        project_dicts.append(project_dict)

    # Format the response to match the expected structure
    return {"items": project_dicts, "total": total, "limit": limit, "offset": offset}


@router.get("/projects/{project_id}", response_model=ProjectResponse)
@require_permission("view_projects")
def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Get a project by ID, ensuring user has access."""
    project = _get_project_in_account(db, project_id, current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert datetime objects to ISO format strings
    return {
        "id": project.id,
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings,
        "tracker_configurations": project.tracker_settings,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


@router.get(
    "/organizations/{organization_id}/projects/{identifier}",
    response_model=ProjectResponse,
)
@require_permission("view_projects")
def get_project_by_identifier(
    organization_id: str,
    identifier: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Get a project by organization ID and project identifier, ensuring user has access."""
    # Check organization access first
    organization = _get_organization_in_account(
        db, organization_id, current_user.account_id
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Now get the project within the authorized organization using slug or identifier
    project = crud_project.get_by_slug_or_identifier(
        db, organization_id=organization_id, slug_or_identifier=identifier
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Convert datetime objects to ISO format strings
    return {
        "id": project.id,
        "name": project.name,
        "identifier": project.identifier,
        "description": project.description,
        "organization_id": project.organization_id,
        "settings": project.settings,
        "tracker_configurations": project.tracker_settings,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


@router.put("/projects/{project_id}", response_model=ProjectResponse)
@require_permission("edit_projects")
def update_project(
    project_id: str,
    project_update: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Update a project, ensuring user has access."""
    project = _get_project_in_account(db, project_id, current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update project using CRUD operation
    # Note: Handle potential field name differences (e.g., tracker_configurations)
    update_data = project_update.dict(exclude_unset=True)

    # If tracker_configurations is present, map it to tracker_settings
    if "tracker_configurations" in update_data:
        update_data["tracker_settings"] = update_data.pop("tracker_configurations")

    updated_project = crud_project.update(db, db_obj=project, obj_in=update_data)

    # Convert datetime objects to ISO format strings
    return {
        "id": updated_project.id,
        "name": updated_project.name,
        "identifier": updated_project.identifier,
        "description": updated_project.description,
        "organization_id": updated_project.organization_id,
        "settings": updated_project.settings,
        "tracker_configurations": updated_project.tracker_settings,
        "created_at": updated_project.created_at.isoformat(),
        "updated_at": updated_project.updated_at.isoformat(),
    }


@router.delete("/projects/{project_id}", status_code=204)
@require_permission("delete_projects")
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """Delete a project, ensuring user has access."""
    project = _get_project_in_account(db, project_id, current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete the project
    crud_project.delete(db, id=project_id)


@router.post("/projects/test-connection", response_model=TestConnectionResponse)
@require_permission("view_projects")
async def test_project_connection(
    request: TestConnectionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TestConnectionResponse:
    """Test the connection to an issue tracker for a project, ensuring user has access."""
    try:
        # Resolve organization
        organization_id = request.organization
        if len(organization_id) == 36:  # Simple UUID check
            organization = _get_organization_in_account(
                db, organization_id, current_user.account_id
            )
        else:
            organization = crud_organization.get_by_identifier(
                db, identifier=organization_id, account_id=current_user.account_id
            )

        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Resolve project
        project_id = request.project
        if len(project_id) == 36:  # Simple UUID check
            project = _get_project_in_account(db, project_id, current_user.account_id)
        else:
            project = crud_project.get_by_identifier(
                db, organization_id=organization.id, identifier=project_id
            )

        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        # Get tracker from organization
        tracker = organization.tracker
        if not tracker:
            return TestConnectionResponse(
                success=False,
                message="Organization has no associated tracker.",
                details={},
            )

        # Determine the tracker type and config
        tracker_type = tracker.tracker_type
        connection_details = project.tracker_settings or {}
        if tracker.url:
            connection_details["url"] = tracker.url
        if tracker.connection_details:
            for key, value in tracker.connection_details.items():
                if key not in connection_details:
                    connection_details[key] = value

        # Create the tracker client
        try:
            if tracker_type == "bitbucket":
                # The row's auth mode decides between a pasted token and a
                # managed grant resolved per request.
                connection_details = {
                    **connection_details,
                    "auth_type": tracker.auth_type,
                }
            tracker_client = await create_tracker_client(
                tracker_type=tracker_type,
                tracker_id=str(tracker.id),
                api_key=tracker.resolved_api_key,
                connection_details=connection_details,
                credential_source=tracker_credential_source(tracker),
            )
            if not tracker_client:
                raise ValueError("Unsupported tracker type or configuration error")
        except Exception as e:
            logger.error(f"Error creating tracker client: {e}")
            return TestConnectionResponse(
                success=False,
                message="Error creating tracker client",
                details={"error": str(e)},
            )

        # Test the connection
        try:
            connection_result = await tracker_client.test_connection()

            # Return the connection result
            return TestConnectionResponse(
                success=connection_result.success,
                message=connection_result.message,
                details=connection_result.details,
            )
        except Exception as e:
            logger.error(f"Error testing connection: {e}")
            return TestConnectionResponse(
                success=False,
                message="Error testing connection",
                details={"error": str(e)},
            )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error in test_project_connection: {e}")
        raise HTTPException(
            status_code=500, detail=f"Error testing connection: {str(e)}"
        )


def _transfer_error(
    status_code: int, code: str, message: str, **extra
) -> HTTPException:
    """An actionable transfer refusal with a stable machine-readable code."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
    )


def _list_destination_repositories(
    tracker: Tracker, organization: Organization
) -> List[dict]:
    """List the repositories the destination integration can see in the org.

    Uses the destination tracker's own credentials, so a repository appears
    only if that installation or token has been granted access to it.

    Runs on the request's worker thread. The client is built here because a
    GitHub App client may read its installation from the database; only the
    HTTP listing is handed to the event loop.
    """
    from anyio import from_thread

    from preloop.sync.scanner.core import TrackerClient

    client = TrackerClient(tracker).client
    return from_thread.run(client.get_projects, str(organization.identifier))


def _scope_allows(
    rules: List[TrackerScopeRule], org_identifier: str, repo_id: str
) -> bool:
    """Mirror the scanner and project listing scope rules for one repository."""
    org_includes = {
        r.identifier
        for r in rules
        if r.scope_type == "ORGANIZATION" and r.rule_type == "INCLUDE"
    }
    project_includes = {
        r.identifier
        for r in rules
        if r.scope_type == "PROJECT" and r.rule_type == "INCLUDE"
    }
    project_excludes = {
        r.identifier
        for r in rules
        if r.scope_type == "PROJECT" and r.rule_type == "EXCLUDE"
    }
    if org_identifier not in org_includes:
        return False
    if repo_id in project_excludes:
        return False
    return not project_includes or repo_id in project_includes


def _refuse_destination_duplicate(
    db: Session, project: Project, destination: Organization
) -> None:
    """Raise 409 if the destination already has another project for the repo."""
    repo_id = str(project.identifier)
    duplicate = (
        db.query(Project)
        .filter(
            Project.organization_id == destination.id,
            Project.identifier == repo_id,
            Project.id != project.id,
        )
        .first()
    )
    if duplicate:
        issue_count = db.query(Issue).filter(Issue.project_id == duplicate.id).count()
        raise _transfer_error(
            409,
            "destination_has_duplicate",
            f"Organization '{destination.name}' already has project "
            f"{duplicate.id} for repository {repo_id} ({issue_count} issues). "
            "Two projects for one repository make webhook routing ambiguous. "
            "Review it and delete it if it holds nothing you need, then retry. "
            "It is not merged automatically.",
            duplicate_project_id=str(duplicate.id),
            duplicate_issue_count=issue_count,
        )


@router.post(
    "/projects/{project_id}/transfer",
    response_model=ProjectTransferReceipt,
    responses={
        403: {"description": "Caller lacks manage_trackers"},
        404: {"description": "Project or destination organization not found"},
        409: {"description": "Transfer refused; detail.code says why"},
        422: {"description": "Repository type or host cannot be transferred"},
        502: {"description": "Destination integration could not be queried"},
    },
)
@require_permission("manage_trackers")
def transfer_project(
    project_id: str,
    request: ProjectTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ProjectTransferReceipt:
    """Rebind a repository project to the organization that now owns it.

    A GitHub repository keeps its repository ID when it is transferred to
    another owner or renamed. This keeps the same project row (and so its ID,
    issues, flow triggers, audit and review history) and moves it to the
    destination organization, updating the owner/name that clone and review
    publication use.

    Preview by default; send ``dry_run: false`` to apply. Applying again is a
    no-op that returns ``status: unchanged``.

    The destination integration must already be able to see the repository
    (checked live with its own credentials) and must include it in scope.
    Repository-level grants on the source integration are not copied; the
    receipt lists them under ``not_carried_over``.
    """
    # require_permission is a no-op without the RBAC plugin. Moving a repo
    # changes which integration's credentials publish to it, so enforce it.
    ensure_permission_in_oss(db, current_user, "manage_trackers")

    project = _get_project_in_account(db, project_id, current_user.account_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    destination = _get_organization_in_account(
        db, request.organization_id, current_user.account_id
    )
    if not destination:
        raise HTTPException(status_code=404, detail="Organization not found")

    source = project.organization
    source_tracker = source.tracker
    dest_tracker = destination.tracker
    repo_id = str(project.identifier)

    if source_tracker.tracker_type not in REPOSITORY_ID_TRACKER_TYPES:
        raise _transfer_error(
            422,
            "unsupported_tracker",
            f"Transfers are supported for {sorted(REPOSITORY_ID_TRACKER_TYPES)} "
            f"repositories; this project's tracker is '{source_tracker.tracker_type}'.",
        )
    if dest_tracker.tracker_type != source_tracker.tracker_type or repository_host(
        dest_tracker
    ) != repository_host(source_tracker):
        raise _transfer_error(
            422,
            "different_repository_host",
            "The destination organization is on a different host or tracker "
            "type. Repository IDs are only stable within one GitHub host.",
        )
    if not dest_tracker.is_active or dest_tracker.is_deleted:
        raise _transfer_error(
            409,
            "destination_integration_inactive",
            "The destination organization's integration is inactive. "
            "Reactivate it and retry.",
        )

    dest_rules = (
        db.query(TrackerScopeRule)
        .filter(TrackerScopeRule.tracker_id == dest_tracker.id)
        .all()
    )
    if not _scope_allows(dest_rules, str(destination.identifier), repo_id):
        raise _transfer_error(
            409,
            "destination_out_of_scope",
            f"Organization '{destination.name}' or repository {repo_id} is not "
            "included in the destination integration's scope. Include them in "
            "the destination integration's settings, then retry. Scope is not "
            "copied from the source integration.",
        )

    _refuse_destination_duplicate(db, project, destination)

    # Do not sit idle in a transaction while GitHub answers.
    release_transaction(db)
    try:
        visible = _list_destination_repositories(dest_tracker, destination)
    except Exception as exc:  # tracker/network failure, never a bare 500
        logger.warning(
            "Destination access check failed for project %s: %s", project.id, exc
        )
        raise _transfer_error(
            502,
            "destination_check_failed",
            "Could not list repositories through the destination integration. "
            "Check its credentials and retry.",
        ) from exc
    live = next(
        (
            r
            for r in visible
            if str(r.get("identifier") or r.get("id") or "") == repo_id
        ),
        None,
    )
    if live is None:
        raise _transfer_error(
            409,
            "destination_cannot_see_repository",
            f"The integration for '{destination.name}' cannot see repository "
            f"{repo_id}. Grant it access (for a GitHub App with selected "
            "repositories, add this repository to the installation), then retry.",
        )

    live_meta = live.get("meta_data") or {}
    new_full_name = live_meta.get("full_name") or project.slug
    new_name = live.get("name") or project.name
    old_meta = dict(project.meta_data or {})
    old_full_name = old_meta.get("full_name") or project.slug

    source_location = RepositoryLocation(
        organization_id=str(source.id),
        organization_name=source.name,
        tracker_id=str(source_tracker.id),
        full_name=old_full_name,
    )
    destination_location = RepositoryLocation(
        organization_id=str(destination.id),
        organization_name=destination.name,
        tracker_id=str(dest_tracker.id),
        full_name=new_full_name,
    )

    moving = source.id != destination.id
    changes: List[str] = []
    if moving:
        changes.append(f"organization: {source.name} -> {destination.name}")
    if project.slug != new_full_name:
        changes.append(
            f"repository: {project.slug} -> {new_full_name} "
            "(clone and review publication target)"
        )
    if project.name != new_name:
        changes.append(f"name: {project.name} -> {new_name}")

    not_carried_over: List[str] = []
    if moving:
        source_repo_rules = (
            db.query(TrackerScopeRule)
            .filter(
                TrackerScopeRule.tracker_id == source_tracker.id,
                TrackerScopeRule.scope_type == "PROJECT",
                TrackerScopeRule.identifier == repo_id,
            )
            .all()
        )
        for rule in source_repo_rules:
            not_carried_over.append(
                f"{rule.rule_type} scope rule for repository {repo_id} on the "
                f"source integration {source_tracker.id}"
            )

    manual_actions: List[str] = []
    stale_hooks = (
        db.query(Webhook)
        .filter(
            Webhook.project_id == project.id,
            Webhook.organization_id == source.id,
        )
        .count()
        if moving
        else 0
    )
    if stale_hooks:
        manual_actions.append(
            f"{stale_hooks} repository webhook record(s) were registered by the "
            "source integration; re-register webhooks from the destination "
            "integration if it uses repository webhooks."
        )
    if old_full_name and new_full_name and old_full_name != new_full_name:
        flows = db.query(Flow).filter(Flow.account_id == current_user.account_id).all()
        pinned = [
            str(f.id)
            for f in flows
            if f.git_clone_config and old_full_name in str(f.git_clone_config)
        ]
        if pinned:
            manual_actions.append(
                f"Flow(s) {', '.join(pinned)} pin the old repository "
                f"'{old_full_name}' in their clone configuration; update them "
                f"to '{new_full_name}'."
            )

    receipt = ProjectTransferReceipt(
        project_id=str(project.id),
        repository_id=repo_id,
        status="preview",
        source=source_location,
        destination=destination_location,
        changes=changes,
        not_carried_over=not_carried_over,
        manual_actions=manual_actions,
    )
    if not changes:
        receipt.status = "unchanged"
        return receipt
    if request.dry_run:
        return receipt

    # The live check above may take a while and must not hold a lock. Take
    # the repository lock now and check again before writing, so a create or
    # transfer that ran meanwhile cannot leave two projects in the destination.
    lock_repository_identity(
        db,
        account_id=current_user.account_id,
        host=repository_host(dest_tracker),
        identifier=repo_id,
    )
    _refuse_destination_duplicate(db, project, destination)

    new_meta = {**old_meta, **live_meta}
    if moving:
        history = list(old_meta.get("repository_transfers") or [])
        history.append(
            {
                "from_organization_id": str(source.id),
                "to_organization_id": str(destination.id),
                "from_full_name": old_full_name,
                "to_full_name": new_full_name,
                "by_user_id": str(current_user.id),
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        new_meta["repository_transfers"] = history
    project.organization_id = destination.id
    project.slug = new_full_name
    project.name = new_name
    project.meta_data = new_meta  # reassign: JSON column mutations are untracked

    crud_audit_log.log_action(
        db,
        account_id=current_user.account_id,
        user_id=current_user.id,
        action="project_transferred" if moving else "project_repository_renamed",
        resource_type="project",
        resource_id=str(project.id),
        status="success",
        details={
            "repository_id": repo_id,
            "from_organization_id": str(source.id),
            "to_organization_id": str(destination.id),
            "from_full_name": old_full_name,
            "to_full_name": new_full_name,
            "not_carried_over": not_carried_over,
        },
        commit=False,
    )
    db.commit()
    # Events for this repo on the destination may have been backed off as an
    # unknown project; it resolves now.
    crud_tracker.clear_unknown_project(
        db, id=str(dest_tracker.id), project_identifier=repo_id
    )
    logger.info(
        "Project %s (repository %s) rebound from org %s to org %s",
        project.id,
        repo_id,
        source.id,
        destination.id,
    )
    receipt.status = "transferred" if moving else "updated"
    return receipt
