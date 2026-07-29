"""
Endpoints for handling MCP (Model Context Protocol) tool calls via HTTP.

This module provides a secure, scalable, and integrated way for MCP clients
to interact with the Preloop platform using FastMCP.
"""

import asyncio
import logging
import re
from typing import Any, Optional, Dict, List, Literal
from urllib.parse import urlparse, urlunparse
from sqlalchemy.exc import SQLAlchemyError

from fastapi import HTTPException

from preloop.api.common import (
    get_tracker_client,
    load_compliance_prompts_config,
)

from preloop.api.endpoints.issues import (
    create_issue as api_create_issue,
    extract_label_strings,
)
from preloop.api.endpoints.search import (
    perform_search,
    SearchResponse as ApiSearchResponse,
)
from preloop.api.endpoints.issue_compliance import (
    _calculate_issue_compliance,
    get_compliance_improvement_suggestion as api_get_compliance_suggestion,
)
from preloop.schemas.issue import IssueCreate
from preloop.schemas.tracker_models import IssueUpdate
from preloop.schemas.mcp import (
    GetIssueResponse,
    CreateIssueResponse,
    UpdateIssueResponse,
    EstimateComplianceResponse,
    ImproveComplianceResponse,
    ProcessingMetadata,
    SuggestedUpdate,
    UpdateIssueRequest,
)

from preloop.services.duplicate_detection import DuplicateDetector
from preloop.config import get_settings
from preloop.models.crud import (
    CRUDIssue,
    CRUDProject,
    CRUDOrganization,
    CRUDIssueComplianceResult,
)
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.issue import Issue
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import TrackerType
from preloop.models.models.issue_compliance_result import IssueComplianceResult
from preloop.api.auth.jwt import get_user_from_token_if_valid
from fastmcp.server.dependencies import get_http_request


logger = logging.getLogger(__name__)


def _extract_assignee_name(assignee_data: Any) -> Optional[str]:
    """Normalize tracker assignee metadata to a display name string."""
    if assignee_data is None:
        return None
    if isinstance(assignee_data, str):
        return assignee_data
    if isinstance(assignee_data, dict):
        return (
            assignee_data.get("name")
            or assignee_data.get("username")
            or assignee_data.get("title")
            or str(assignee_data)
        )
    return str(assignee_data)


def _detect_platform_from_url(url: str) -> Literal["github", "gitlab"]:
    """
    Detect if URL is GitHub or GitLab.

    Args:
        url: The URL to analyze.

    Returns:
        "github" or "gitlab" based on URL analysis.

    Raises:
        ValueError: If platform cannot be determined.
    """
    from preloop.utils.repo_urls import tracker_host_kind

    host_kind = tracker_host_kind(url)
    if host_kind in ("github", "gitlab"):
        return host_kind

    # Path-based fallback for non-standard hosts (parsed path segments only).
    path = (urlparse(url).path or "").lower()
    path_parts = [part for part in path.split("/") if part]
    if "pull" in path_parts:
        return "github"
    if "merge_requests" in path_parts or "-" in path_parts:
        return "gitlab"

    raise ValueError(f"Cannot determine platform from URL: {url}")


crud_issue = CRUDIssue(Issue)
crud_project = CRUDProject(Project)
crud_organization = CRUDOrganization(Organization)
crud_issue_compliance_result = CRUDIssueComplianceResult(IssueComplianceResult)


class IssueProcessingError(Exception):
    """Exception for issue processing errors."""

    pass


class IssueNotFoundError(IssueProcessingError):
    """Exception for when an issue cannot be found."""

    pass


class ProcessingResult:
    """Container for processing results."""

    def __init__(
        self, success: bool, data=None, error: str = None, issue_identifier: str = None
    ):
        self.success = success
        self.data = data
        self.error = error
        self.issue_identifier = issue_identifier


async def _get_authenticated_user(request_headers):
    """Extract and authenticate user from request headers."""
    db = next(get_db())
    authorization = request_headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = authorization.split("Bearer ")[1]
    current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return db, current_user


def _parse_issue_key_from_url(url: str) -> Optional[str]:
    """Extract an issue key from a tracker URL.

    Supports:
      - GitLab:  https://gitlab.example.com/group/project/-/issues/28  → group/project#28
      - GitHub:  https://github.com/org/repo/issues/123               → org/repo#123
      - Jira:    https://jira.example.com/browse/PROJ-123              → PROJ-123

    Returns None if the URL doesn't match a known pattern.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # GitLab: /group/project/-/issues/28  (group may have subgroups)
    m = re.match(r"^/(.+?)/-/issues/(\d+)$", path)
    if m:
        return f"{m.group(1)}#{m.group(2)}"

    # GitHub: /org/repo/issues/123
    m = re.match(r"^/([^/]+/[^/]+)/issues/(\d+)$", path)
    if m:
        return f"{m.group(1)}#{m.group(2)}"

    # Jira: /browse/PROJ-123
    m = re.match(r"^/browse/([A-Z][A-Z0-9]+-\d+)$", path)
    if m:
        return m.group(1)

    return None


def _parse_pr_key_from_url(url: str) -> Optional[Dict[str, Any]]:
    """Extract PR/MR components from a tracker URL.

    Supports:
      - GitHub:  https://github.com/org/repo/pull/123
      - GitLab:  https://gitlab.example.com/group/project/-/merge_requests/28

    Returns dict with platform, project_path, owner, repo, pr_number, or None.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # GitLab: /group/subgroup/project/-/merge_requests/28
    m = re.match(r"^/(.+?)/-/merge_requests/(\d+)$", path)
    if m:
        project_path = m.group(1)
        parts = project_path.rsplit("/", 1)
        return {
            "platform": "gitlab",
            "project_path": project_path,
            "owner": parts[0] if len(parts) == 2 else None,
            "repo": parts[-1],
            "pr_number": m.group(2),
        }

    # GitHub: /org/repo/pull/123
    m = re.match(r"^/([^/]+)/([^/]+)/pull/(\d+)$", path)
    if m:
        return {
            "platform": "github",
            "project_path": f"{m.group(1)}/{m.group(2)}",
            "owner": m.group(1),
            "repo": m.group(2),
            "pr_number": m.group(3),
        }

    return None


def _parse_pr_identifier(pr_identifier: str) -> Dict[str, Any]:
    """Parse a PR/MR identifier (URL, slug, or number) into structured components.

    Handles:
      - Full URLs (GitHub pull requests, GitLab merge requests)
      - Slug format: owner/repo#123
      - Plain number: 123

    Returns dict with: platform, project_path, owner, repo, pr_number.
    """
    pr_identifier = pr_identifier.strip()
    result: Dict[str, Any] = {
        "platform": None,
        "project_path": None,
        "owner": None,
        "repo": None,
        "pr_number": pr_identifier,
    }

    if pr_identifier.startswith("http"):
        # Normalize URL first (strip query/fragment/trailing slash)
        normalized = _normalize_url(pr_identifier)

        # Try structured URL parsing
        parsed = _parse_pr_key_from_url(normalized)
        if parsed:
            return parsed

        # Fallback: try platform detection + basic extraction
        try:
            result["platform"] = _detect_platform_from_url(pr_identifier)
        except ValueError:
            # URL may not match a known tracker host; fall back to regex extraction.
            pass
        m = re.search(r"/(\d+)/?$", urlparse(normalized).path)
        if m:
            result["pr_number"] = m.group(1)

    elif "/" in pr_identifier and "#" in pr_identifier:
        # Slug format: owner/repo#123 or group/subgroup/project#123
        slug_parts = pr_identifier.split("#", 1)
        result["pr_number"] = slug_parts[1]
        result["project_path"] = slug_parts[0]
        repo_parts = slug_parts[0].rsplit("/", 1)
        if len(repo_parts) == 2:
            result["owner"] = repo_parts[0]
            result["repo"] = repo_parts[1]
        else:
            result["repo"] = repo_parts[0]

    return result


def _find_pr_project(
    db,
    project_path: Optional[str],
    owner: Optional[str],
    repo: Optional[str],
    platform: Optional[str],
    account_id: str,
) -> Project:
    """Find the project for a PR/MR using parsed identifier components."""
    project_obj = None

    # Try full project path first (handles GitLab nested groups)
    if project_path:
        project_obj = crud_project.get_by_slug_or_identifier(
            db,
            slug_or_identifier=project_path,
            account_id=account_id,
        )
    if not project_obj and owner and repo:
        project_obj = crud_project.get_by_slug_or_identifier(
            db,
            slug_or_identifier=f"{owner}/{repo}",
            account_id=account_id,
        )

    if not project_obj:
        from preloop.models.crud import crud_tracker

        tracker_type = (
            TrackerType.GITHUB if platform != "gitlab" else TrackerType.GITLAB
        )
        trackers = crud_tracker.get_by_type(
            db,
            tracker_type=tracker_type,
            account_id=account_id,
        )

        if not trackers and platform is None:
            tracker_type = TrackerType.GITLAB
            trackers = crud_tracker.get_by_type(
                db,
                tracker_type=tracker_type,
                account_id=account_id,
            )

        if not trackers:
            raise HTTPException(
                status_code=404,
                detail="No tracker found. Please provide full PR/MR identifier.",
            )

        tracker = trackers[0]
        from preloop.models.crud import crud_organization

        organizations = crud_organization.get_for_tracker(
            db,
            tracker_id=tracker.id,
            account_id=account_id,
        )
        if not organizations:
            raise HTTPException(
                status_code=404,
                detail="No organizations found for tracker.",
            )

        projects = crud_project.get_for_organization(
            db,
            organization_id=organizations[0].id,
            account_id=account_id,
        )
        if not projects:
            raise HTTPException(
                status_code=404,
                detail="No projects found. Please provide full PR/MR identifier.",
            )

        project_obj = projects[0]

    return project_obj


def _normalize_url(url: str) -> str:
    """Normalize a URL by stripping query params, fragments, and trailing slashes."""
    parsed = urlparse(url)
    normalized = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )
    return normalized


def _find_issue_by_identifier(db, identifier: str, account_id: str) -> Issue:
    """Find an issue by identifier (URL, key, or ID) with comprehensive lookup logic."""
    if not identifier or not identifier.strip():
        raise IssueNotFoundError(f"Empty or invalid issue identifier: '{identifier}'")

    identifier = identifier.strip()

    # Check if issue is a URL
    if identifier.startswith("http"):
        # 1. Exact URL match
        issue_obj = crud_issue.get_by_external_url(
            db, external_url=identifier, account_id=account_id
        )
        if issue_obj:
            return issue_obj

        # 2. Normalized URL match (strip query params, fragments, trailing slash)
        normalized = _normalize_url(identifier)
        if normalized != identifier:
            issue_obj = crud_issue.get_by_external_url(
                db, external_url=normalized, account_id=account_id
            )
            if issue_obj:
                return issue_obj

        # 3. Parse issue key from URL and fall through to key-based lookups
        parsed_key = _parse_issue_key_from_url(normalized)
        if parsed_key:
            identifier = parsed_key
        else:
            raise IssueNotFoundError(f"Issue not found by URL: {identifier}")

    # Try exact key match first
    issue_obj = crud_issue.get_by_key(db, key=identifier, account_id=account_id)
    if issue_obj:
        return issue_obj

    # Try key postfix match
    issue_obj = crud_issue.get_by_key_postfix(
        db, key_postfix=identifier, account_id=account_id
    )
    if issue_obj:
        return issue_obj

    # Try direct ID lookup if identifier looks like a UUID
    try:
        issue_obj = crud_issue.get(db, id=identifier, account_id=account_id)
        if issue_obj:
            return issue_obj
    except Exception:  # Catch potential UUID conversion errors
        pass

    raise IssueNotFoundError(f"Issue not found: {identifier}")


def _validate_issues_input(issues: List[str]) -> List[str]:
    """Validate and sanitize issues input."""
    if not issues:
        raise HTTPException(status_code=400, detail="No issues provided")

    if len(issues) > 100:  # Reasonable batch limit
        raise HTTPException(status_code=400, detail="Too many issues (max 100)")

    # Filter out empty strings and strip whitespace
    validated_issues = []
    for issue in issues:
        if isinstance(issue, str) and issue.strip():
            validated_issues.append(issue.strip())

    if not validated_issues:
        raise HTTPException(status_code=400, detail="No valid issues provided")

    return validated_issues


def _parse_issue_slug(slug: str) -> Dict[str, Optional[str]]:
    """
    Parses a full issue slug into its components.
    Handles formats: org/project#key, project#key, or a standalone key/UUID.
    """
    match = re.match(r"^(?:([^/]+)/)?([^#]+)#(.+)$", slug)
    if match:
        org, proj, key = match.groups()
        return {"organization": org, "project": proj, "key": key}
    elif re.match(r"^(?:([^/]+)/)?([^#]+)$", slug):
        proj, key = match.groups()
        return {"organization": None, "project": proj, "key": key}
    return {"organization": None, "project": None, "key": slug}


def _enrich_compliance_results(db_results):
    """Enrich compliance results with short_name from config."""
    settings = get_settings()
    prompts_config = load_compliance_prompts_config(settings.PROMPTS_FILE)
    enriched_results = []
    for result in db_results:
        prompt_data = prompts_config.get(result.prompt_id)
        if prompt_data:
            result_dict = result.to_dict()
            result_dict["short_name"] = prompt_data.get("short_name")
            enriched_results.append(result_dict)
    return enriched_results


async def get_issue(
    issue: str,
) -> GetIssueResponse:
    """
    Handles the 'get_issue' tool call.
    """
    db = next(get_db())
    current_user = None
    authorization = get_http_request().headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1]
        current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        issue_obj = _find_issue_by_identifier(db, issue, current_user.account_id)
    except IssueNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    project_name = issue_obj.project.name
    organization_name = issue_obj.project.organization.name
    project_identifier = issue_obj.project.identifier or issue_obj.project.slug

    # Get compliance results using CRUD layer
    compliance_results = crud_issue_compliance_result.get_for_issue(
        db, issue_id=issue_obj.id, account_id=str(current_user.account_id)
    )
    settings = get_settings()
    meta_data = issue_obj.meta_data or {}
    labels_list = extract_label_strings(meta_data.get("labels", []))
    assignee = _extract_assignee_name(meta_data.get("assignee"))

    return GetIssueResponse(
        id=str(issue_obj.id),
        external_id=issue_obj.external_id,
        key=issue_obj.key,
        title=issue_obj.title,
        description=issue_obj.description,
        status=issue_obj.status,
        priority=issue_obj.priority,
        organization=organization_name,
        project=project_name,
        project_id=str(issue_obj.project_id),
        project_identifier=project_identifier,
        url=issue_obj.external_url
        or f"https://{settings.preloop_url}/issues/{issue_obj.id}",
        created_at=issue_obj.created_at,
        updated_at=issue_obj.updated_at,
        meta_data=meta_data,
        labels=labels_list,
        assignee=assignee,
        compliance_results=_enrich_compliance_results(compliance_results),
    )


async def create_issue(
    project: str,
    title: str,
    description: str,
    labels: Optional[List[str]] = None,
    assignee: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    prevent_duplicates: bool = True,
) -> CreateIssueResponse:
    """
    Handles the 'create_issue' tool call.
    """
    db = next(get_db())
    current_user = None
    authorization = get_http_request().headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1]
        current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    project_obj = crud_project.get_by_slug_or_identifier(
        db, slug_or_identifier=project, account_id=str(current_user.account_id)
    )
    if not project_obj:
        raise HTTPException(status_code=404, detail=f"Project '{project}' not found.")

    if prevent_duplicates:
        combined_text = f"{title}\n\n{description}"
        try:
            search_response = await perform_search(
                query=combined_text,
                embedding_type="issue",
                project=project_obj.slug or project_obj.identifier,
                search_type="similarity",
                limit=5,
                db=db,
                current_user=current_user,
            )
            search_results = search_response.results
        except Exception as e:
            logger.error(f"Similarity search failed during duplicate check: {e}")
            search_results = []

        if search_results:
            detector = DuplicateDetector()
            potential_duplicates = [r.model_dump() for r in search_results]
            decision = await detector.check_duplicates(
                new_title=title,
                new_description=description,
                potential_duplicates=potential_duplicates,
            )

            if decision.get("status") == "duplicate":
                dup_issue = decision.get("duplicate_issue", {})
                return CreateIssueResponse(
                    issue_id=dup_issue.get("id", "Unknown"),
                    status="existing_duplicate_found",
                    message=f"Duplicate detection found a likely match: {dup_issue.get('key')}",
                    url=dup_issue.get("url"),
                )

    issue_create_schema = IssueCreate(
        project=project_obj.slug or project_obj.identifier,
        title=title,
        description=description,
        project_id=str(project_obj.id),
        labels=labels,
        assignee=assignee,
        priority=priority,
        status=status,
    )

    try:
        created_issue = await api_create_issue(
            issue=issue_create_schema, db=db, current_user=current_user
        )
        return CreateIssueResponse(
            issue_id=created_issue.id,
            status="created",
            message="Successfully created new issue.",
            url=created_issue.url,
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Failed to create issue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create issue.")


async def update_issue(
    issue: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> UpdateIssueResponse:
    """
    Handles the 'update_issue' tool call.
    """
    db = next(get_db())
    current_user = None
    authorization = get_http_request().headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1]
        current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        issue_obj = _find_issue_by_identifier(db, issue, current_user.account_id)
    except IssueNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # --- Prepare Update Payload for Tracker ---
    # Use the base IssueUpdate schema expected by the tracker client
    issue_update = IssueUpdate()
    if title:
        issue_update.title = title
    if description:
        issue_update.description = description
    if status:
        issue_update.status = status
    if priority:
        issue_update.priority = priority
    if labels:
        issue_update.labels = labels
    if assignee:
        issue_update.assignee = assignee
    # Filter out None values, as tracker clients might interpret None as "clear this field"
    update_data_for_tracker = issue_update.model_dump(exclude_unset=True)

    if not update_data_for_tracker:
        logger.info(
            f"No fields provided to update for issue {issue_obj.id}. Skipping tracker update."
        )
    else:
        # --- Call Tracker Client ---
        tracker_client = await get_tracker_client(
            issue_obj.project.organization_id, issue_obj.project_id, db, current_user
        )

        if not issue_obj.external_id:
            logger.error(
                f"Cannot update issue {issue_obj.id} in tracker: Missing external_id."
            )
            raise HTTPException(
                status_code=400,
                detail="Cannot update issue in tracker: Missing external identifier.",
            )

        try:
            # Determine the correct identifier for the tracker API call
            # Prefer using the key (e.g., "owner/repo#1" for GitHub) over external_id
            # since external_id might be the internal tracker ID (e.g., GitHub's numeric ID)
            issue_repo_id = issue_obj.key if issue_obj.key else issue_obj.external_id

            logger.info(
                f"Calling tracker client to update issue {issue_repo_id} with data: {update_data_for_tracker}"
            )
            await tracker_client.update_issue(
                issue_repo_id, IssueUpdate(**update_data_for_tracker)
            )
            logger.info(
                f"Successfully updated issue {issue_obj.external_id} via tracker client."
            )
        except NotImplementedError:
            logger.warning(
                f"Tracker type {tracker_client.tracker_type} does not support updating issues."
            )
            # Decide if this should be an error or just a warning
            # raise HTTPException(status_code=501, detail="Issue updates not supported by this tracker type.")
        except Exception as e:
            logger.error(
                f"Error updating issue {issue_obj.external_id} via tracker client: {e}",
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
                f"No fields provided to update for issue {issue_obj.id} in local DB."
            )
            # If we skipped tracker update due to no data, we might skip DB update too,
            # or just proceed to return the current state.
        else:
            try:
                logger.info(
                    f"Updating local DB for issue {issue_obj.id} with data: {update_data_for_db}"
                )
                # Update the local database record
                # Note: crud_issue.update expects the db object, the existing db_obj, and the update obj (Pydantic model or dict)
                updated_issue_db = crud_issue.update(
                    db=db, db_obj=issue_obj, obj_in=update_data_for_db
                )
                db.commit()
                db.refresh(
                    updated_issue_db
                )  # Ensure we have the latest data including timestamps
                issue_obj = updated_issue_db  # Use the updated object going forward
                logger.info(f"Successfully updated issue {issue_obj.id} in local DB.")
            except SQLAlchemyError as e:
                db.rollback()
                logger.error(
                    f"Database error updating issue {issue_obj.id}: {e}", exc_info=True
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
            issue_obj
        )  # Refresh again after potential commit/refresh inside update block
        project = crud_project.get(
            db, id=issue_obj.project_id, account_id=str(current_user.account_id)
        )  # Re-fetch
        organization = (
            crud_organization.get(
                db, id=project.organization_id, account_id=str(current_user.account_id)
            )
            if project
            else None
        )  # Re-fetch safely

        if not project or not organization:
            logger.error(
                f"Data inconsistency after update: Project or Organization missing for issue {issue.id}"
            )
            # Fallback response data
            project_name, org_name, project_slug = (
                "Error: Missing Project",
                "Error: Missing Organization",
                "error",
            )
        else:
            project_name, org_name, project_slug = (
                project.name,
                organization.name,
                project.slug,
            )

        meta_data = issue_obj.meta_data or {}
        labels_list = (
            extract_label_strings(meta_data.get("labels", []))
            if isinstance(meta_data, dict)
            else []
        )
        assignee = (
            _extract_assignee_name(meta_data.get("assignee"))
            if isinstance(meta_data, dict)
            else None
        )

    # Get compliance results using CRUD layer
    compliance_results = crud_issue_compliance_result.get_for_issue(
        db, issue_id=issue_obj.id, account_id=str(current_user.account_id)
    )
    project_name = issue_obj.project.name
    organization_name = issue_obj.project.organization.name
    project_identifier = issue_obj.project.identifier or issue_obj.project.slug
    settings = get_settings()
    return GetIssueResponse(
        id=str(issue_obj.id),
        external_id=issue_obj.external_id,
        key=issue_obj.key,
        title=issue_obj.title,
        description=issue_obj.description,
        status=issue_obj.status,
        priority=issue_obj.priority,
        organization=organization_name,
        project=project_name,
        project_id=str(issue_obj.project_id),
        project_identifier=project_identifier,
        url=issue_obj.external_url
        or f"https://{settings.preloop_url}/issues/{issue_obj.id}",
        created_at=issue_obj.created_at,
        updated_at=issue_obj.updated_at,
        meta_data=meta_data,
        labels=labels_list,
        assignee=assignee,
        compliance_results=_enrich_compliance_results(compliance_results),
    )


async def search(
    query: str,
    project: Optional[str] = None,
    target_type: Literal["issue", "comment", "all"] = "all",
    search_type: Literal["similarity", "fulltext"] = "similarity",
    limit: int = 10,
) -> ApiSearchResponse:
    """
    Handles the 'search' tool call.
    """
    db = next(get_db())
    current_user = None
    authorization = get_http_request().headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1]
        current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    project_obj = crud_project.get_by_slug_or_identifier(
        db, slug_or_identifier=project, account_id=str(current_user.account_id)
    )
    if project_obj:
        project = project_obj.slug or project_obj.identifier
    else:
        project = None
    if target_type == "all":
        target_type = None
    try:
        return await perform_search(
            query=query,
            project=project,
            embedding_type=target_type,
            limit=limit,
            search_type=search_type,
            db=db,
            current_user=current_user,
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Failed to perform search for query '{query}': {e}")
        raise HTTPException(status_code=500, detail="Failed to perform search.")


async def estimate_compliance(
    issues: List[str],
    compliance_metric: str = "DoR",
) -> EstimateComplianceResponse:
    """
    Handles the 'estimate_compliance' tool call with enhanced parallel processing and error reporting.

    Args:
        issues: List of issue slugs/IDs/URLs to process
        compliance_metric: Name of the compliance metric to use (default: "DoR")

    Returns:
        Enhanced response with compliance results and processing metadata
    """
    # Validate input
    validated_issues = _validate_issues_input(issues)

    # Authenticate user
    db, current_user = await _get_authenticated_user(get_http_request().headers)
    settings = get_settings()

    # Process issues with controlled parallelism (max 10 concurrent)
    semaphore = asyncio.Semaphore(10)

    async def process_with_semaphore(issue_identifier: str) -> ProcessingResult:
        async with semaphore:
            return await _process_single_issue_estimate(
                issue_identifier,
                db,
                current_user,
                compliance_metric,
                settings=settings,
            )

    # Execute all tasks in parallel
    logger.info(f"Processing {len(validated_issues)} issues for compliance estimation")
    results = await asyncio.gather(
        *[
            process_with_semaphore(issue_identifier)
            for issue_identifier in validated_issues
        ],
        return_exceptions=True,
    )

    # Separate successful and failed results
    compliance_results = []
    failed_issues = []
    errors = []

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # Handle unexpected exceptions from gather
            issue_identifier = validated_issues[i]
            error_msg = f"Processing exception: {str(result)}"
            failed_issues.append(issue_identifier)
            errors.append(f"{issue_identifier}: {error_msg}")
            logger.error(
                f"Exception processing issue '{issue_identifier}': {result}",
                exc_info=True,
            )
        elif result.success:
            compliance_results.append(result.data)
        else:
            failed_issues.append(result.issue_identifier)
            errors.append(f"{result.issue_identifier}: {result.error}")

    # Create processing metadata
    metadata = ProcessingMetadata(
        total_requested=len(validated_issues),
        successfully_processed=len(compliance_results),
        failed_count=len(failed_issues),
        failed_issues=failed_issues,
        errors=errors,
    )

    logger.info(
        f"Compliance estimation processing completed: "
        f"{metadata.successfully_processed}/{metadata.total_requested} successful, "
        f"{metadata.failed_count} failed"
    )

    return EstimateComplianceResponse(results=compliance_results, metadata=metadata)


async def _process_single_issue_estimate(
    issue_identifier: str,
    db,
    current_user,
    compliance_metric: str,
    settings=None,
) -> ProcessingResult:
    """Process compliance estimation for a single issue."""
    try:
        # Find the issue using our enhanced lookup
        issue_obj = _find_issue_by_identifier(
            db, issue_identifier, current_user.account_id
        )

        # Get compliance estimate
        prompt_name = (
            "dor_compliance_v1" if compliance_metric == "DoR" else compliance_metric
        )
        compliance_result = _calculate_issue_compliance(
            issue_id=issue_obj.id,
            prompt_name=prompt_name,
            db=db,
            current_user=current_user,
            settings=settings,
        )

        return ProcessingResult(
            success=True, data=compliance_result, issue_identifier=issue_identifier
        )

    except IssueNotFoundError as e:
        logger.warning(f"Issue not found: '{issue_identifier}': {str(e)}")
        return ProcessingResult(
            success=False,
            error=f"Issue not found: {str(e)}",
            issue_identifier=issue_identifier,
        )
    except HTTPException as e:
        logger.warning(
            f"Could not get compliance estimate for issue '{issue_identifier}': {e.detail}"
        )
        return ProcessingResult(
            success=False,
            error=f"API error: {e.detail}",
            issue_identifier=issue_identifier,
        )
    except Exception as e:
        logger.error(
            f"Unexpected error processing issue '{issue_identifier}': {e}",
            exc_info=True,
        )
        return ProcessingResult(
            success=False,
            error=f"Unexpected error: {str(e)}",
            issue_identifier=issue_identifier,
        )


async def _process_single_issue_compliance(
    issue_identifier: str,
    db,
    current_user,
    prompt_name: str = "default",
    settings=None,
) -> ProcessingResult:
    """Process compliance improvement for a single issue."""
    try:
        # Find the issue using our enhanced lookup
        issue = _find_issue_by_identifier(db, issue_identifier, current_user.account_id)

        # Get compliance suggestion
        suggestion = api_get_compliance_suggestion(
            issue_id=issue.id,
            prompt_name=prompt_name,
            db=db,
            current_user=current_user,
            settings=settings,
        )

        # Create suggested update
        update_args = UpdateIssueRequest(
            issue=issue_identifier,
            title=suggestion.title,
            description=suggestion.description,
        )
        suggested_update = SuggestedUpdate(arguments=update_args)

        return ProcessingResult(
            success=True, data=suggested_update, issue_identifier=issue_identifier
        )

    except IssueNotFoundError as e:
        logger.warning(f"Issue not found: '{issue_identifier}': {str(e)}")
        return ProcessingResult(
            success=False,
            error=f"Issue not found: {str(e)}",
            issue_identifier=issue_identifier,
        )
    except HTTPException as e:
        logger.warning(
            f"Could not get compliance suggestion for issue '{issue_identifier}': {e.detail}"
        )
        return ProcessingResult(
            success=False,
            error=f"API error: {e.detail}",
            issue_identifier=issue_identifier,
        )
    except Exception as e:
        logger.error(
            f"Unexpected error processing issue '{issue_identifier}': {e}",
            exc_info=True,
        )
        return ProcessingResult(
            success=False,
            error=f"Unexpected error: {str(e)}",
            issue_identifier=issue_identifier,
        )


async def improve_compliance(
    issues: List[str],
    compliance_metric: str = "DoR",
) -> ImproveComplianceResponse:
    """
    Handles the 'improve_compliance' tool call with enhanced error handling and parallel processing.

    Args:
        issues: List of issue slugs/IDs/URLs to process
        compliance_metric: Name of the compliance metric to use (default: "DoR")

    Returns:
        Enhanced response with suggested updates and processing metadata
    """
    # Validate input
    validated_issues = _validate_issues_input(issues)

    # Authenticate user
    db, current_user = await _get_authenticated_user(get_http_request().headers)
    settings = get_settings()

    # Process issues with controlled parallelism (max 10 concurrent)
    semaphore = asyncio.Semaphore(10)
    prompt_name = (
        "dor_compliance_v1" if compliance_metric == "DoR" else compliance_metric
    )

    async def process_with_semaphore(issue_identifier: str) -> ProcessingResult:
        async with semaphore:
            return await _process_single_issue_compliance(
                issue_identifier,
                db,
                current_user,
                prompt_name,
                settings=settings,
            )

    # Execute all tasks in parallel
    logger.info(
        f"Processing {len(validated_issues)} issues for compliance improvements"
    )
    results = await asyncio.gather(
        *[
            process_with_semaphore(issue_identifier)
            for issue_identifier in validated_issues
        ],
        return_exceptions=True,
    )

    # Separate successful and failed results
    suggested_updates = []
    failed_issues = []
    errors = []

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # Handle unexpected exceptions from gather
            issue_identifier = validated_issues[i]
            error_msg = f"Processing exception: {str(result)}"
            failed_issues.append(issue_identifier)
            errors.append(f"{issue_identifier}: {error_msg}")
            logger.error(
                f"Exception processing issue '{issue_identifier}': {result}",
                exc_info=True,
            )
        elif result.success:
            suggested_updates.append(result.data)
        else:
            failed_issues.append(result.issue_identifier)
            errors.append(f"{result.issue_identifier}: {result.error}")

    # Create processing metadata
    metadata = ProcessingMetadata(
        total_requested=len(validated_issues),
        successfully_processed=len(suggested_updates),
        failed_count=len(failed_issues),
        failed_issues=failed_issues,
        errors=errors,
    )

    logger.info(
        f"Compliance improvement processing completed: "
        f"{metadata.successfully_processed}/{metadata.total_requested} successful, "
        f"{metadata.failed_count} failed"
    )

    return ImproveComplianceResponse(
        suggested_updates=suggested_updates, metadata=metadata
    )


async def add_comment(
    target: str,
    comment: str,
    # New parameters for inline comments
    path: Optional[str] = None,
    line: Optional[int] = None,
    side: Optional[str] = None,  # "LEFT" or "RIGHT", defaults to "RIGHT"
    in_reply_to: Optional[str] = None,
) -> "AddCommentResponse":
    """
    Handles the 'add_comment' tool call.

    Adds a comment to an issue, pull request, or merge request.
    Supports inline diff comments when path and line are provided.

    Args:
        target: Issue/PR/MR identifier (URL, key, or ID).
        comment: Comment text to add.
        path: File path for inline diff comments.
        line: Line number for inline diff comments.
        side: Side of diff for inline comments - "LEFT" (old) or "RIGHT" (new).
        in_reply_to: Comment ID to reply to (for threaded replies).

    Returns:
        AddCommentResponse with comment details.
    """
    from preloop.schemas.mcp import AddCommentResponse

    db = next(get_db())
    current_user = None
    authorization = get_http_request().headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ")[1]
        current_user = await get_user_from_token_if_valid(token, db)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    target = target.strip()

    # Validate that path and line must be provided together for inline comments
    if (path is not None and line is None) or (path is None and line is not None):
        raise HTTPException(
            status_code=400,
            detail="For inline comments, both 'path' and 'line' must be provided together. "
            "Received only one of them.",
        )

    # Validate side parameter only for inline comments (when path and line provided)
    # For non-inline comments, the side parameter is ignored
    if path is not None and line is not None:
        # This is an inline comment - validate/default the side parameter
        if side is None:
            side = "RIGHT"
        elif side not in ("LEFT", "RIGHT"):
            raise HTTPException(
                status_code=400,
                detail="Invalid side parameter. Must be 'LEFT' or 'RIGHT'.",
            )

    # Detect if target is a PR/MR URL and handle separately
    is_pull_request = False
    is_merge_request = False
    project_path = None
    pr_mr_number = None
    platform: Optional[Literal["github", "gitlab"]] = None

    if target.startswith("http"):
        # Detect platform from URL
        try:
            platform = _detect_platform_from_url(target)
        except ValueError:
            # Unrecognized tracker URL; continue with path-based heuristics below.
            pass

        # GitHub PR URL: https://github.com/owner/repo/pull/123
        # Use hostname-derived platform (not substring match) to avoid host confusion.
        if platform == "github" and "/pull/" in target:
            is_pull_request = True
            parts = target.split("/")
            if len(parts) >= 7:
                owner = parts[3]
                repo = parts[4]
                pr_mr_number = parts[6].rstrip("/").split("?")[0].split("#")[0]
                project_path = f"{owner}/{repo}"
                logger.info(f"Detected GitHub PR: {project_path}#{pr_mr_number}")
        # GitLab MR URL: https://gitlab.com/owner/repo/-/merge_requests/1
        # Also handles self-hosted GitLab where platform was detected via URL patterns
        # (e.g., https://git.example.com/owner/repo/-/merge_requests/123)
        # platform is set from _detect_platform_from_url which checks for /merge_requests/ pattern
        elif platform == "gitlab" and "/merge_requests/" in target:
            is_merge_request = True
            mr_parts = target.split("/merge_requests/")
            pr_mr_number = mr_parts[-1].rstrip("/").split("?")[0].split("#")[0]
            url_path = mr_parts[0].split("://")[1].split("/")
            if len(url_path) >= 3:
                project_path = "/".join(url_path[1:]).rstrip("/-")
                logger.info(f"Detected GitLab MR: {project_path}#{pr_mr_number}")
    # Parse slug format for PRs/MRs: owner/repo#123 or repo#123
    elif "#" in target:
        slug_parts = target.split("#")
        pr_mr_number = slug_parts[1]
        project_path = slug_parts[0]
        logger.info(f"Detected PR/MR slug format: {project_path}#{pr_mr_number}")

    # Handle PR/MR comments separately
    if is_pull_request or is_merge_request or (project_path and pr_mr_number):
        # Find the project
        if project_path:
            project_obj = crud_project.get_by_slug_or_identifier(
                db,
                slug_or_identifier=project_path,
                account_id=str(current_user.account_id),
            )
            if not project_obj:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project not found for {project_path}",
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="Could not parse project path from PR/MR identifier",
            )

        # Get tracker client
        tracker_client = await get_tracker_client(
            project_obj.organization_id, project_obj.id, db, current_user
        )

        # Determine platform from tracker if not yet known
        if platform is None:
            if tracker_client.tracker_type.lower() == "github":
                platform = "github"
            elif tracker_client.tracker_type.lower() == "gitlab":
                platform = "gitlab"

        target_id = pr_mr_number

        try:
            # Handle inline comments (path and line provided)
            if path and line is not None:
                # Inline comments are only supported on GitHub and GitLab PRs/MRs
                if platform not in ("github", "gitlab"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Inline comments (path/line) are only supported for "
                        f"GitHub and GitLab pull/merge requests. "
                        f"Tracker type '{tracker_client.tracker_type}' does not support this feature.",
                    )

                if platform == "github":
                    # If replying to an existing review comment, use threaded reply
                    if in_reply_to:
                        logger.info(
                            f"Replying to review comment {in_reply_to} on GitHub PR {target_id}"
                        )
                        reply_result = await tracker_client.reply_to_review_comment(
                            pr_number=target_id,
                            comment_id=in_reply_to,
                            body=comment,
                        )
                        return AddCommentResponse(
                            comment_id=str(reply_result.get("id", "")),
                            status="created",
                            message=f"Successfully replied to comment {in_reply_to} on PR {target_id}",
                            url=reply_result.get("html_url"),
                        )

                    # Otherwise, create a new inline comment
                    logger.info(
                        f"Adding inline comment to GitHub PR {target_id} "
                        f"at {path}:{line}"
                    )
                    inline_comment = {
                        "path": path,
                        "line": line,
                        "body": comment,
                        "side": side,
                    }

                    review_result = await tracker_client.submit_pull_request_review(
                        pr_number=target_id,
                        body="Inline comment",  # GitHub requires non-empty body for COMMENT reviews
                        event="COMMENT",
                        comments=[inline_comment],
                    )

                    # Fetch the actual comment ID from the review
                    # The review response contains the review ID, not the comment ID
                    # We need to fetch the review's comments to get the actual comment ID
                    review_id = str(review_result.get("id", ""))
                    comment_id = (
                        review_id  # Fallback to review ID if we can't get comment
                    )
                    comment_url = review_result.get("html_url")

                    try:
                        review_comments = await tracker_client.get_review_comments(
                            pr_number=target_id,
                            review_id=review_id,
                        )
                        if review_comments:
                            # Get the first (and should be only) comment from this review
                            comment_id = review_comments[0].get("id", review_id)
                            comment_url = review_comments[0].get(
                                "html_url", comment_url
                            )
                    except Exception as e:
                        logger.warning(
                            f"Could not fetch review comment ID, using review ID: {e}"
                        )

                    return AddCommentResponse(
                        comment_id=str(comment_id),
                        status="created",
                        message=f"Successfully added inline comment to PR {target_id} at {path}:{line}",
                        url=comment_url,
                    )

                else:  # GitLab (explicitly checked above)
                    # If replying to an existing discussion, add a note to it
                    if in_reply_to:
                        logger.info(
                            f"Replying to discussion {in_reply_to} on GitLab MR {target_id}"
                        )
                        reply_result = await tracker_client.reply_to_mr_discussion(
                            mr_iid=target_id,
                            discussion_id=in_reply_to,
                            body=comment,
                        )
                        return AddCommentResponse(
                            comment_id=str(reply_result.get("id", "")),
                            status="created",
                            message=f"Successfully replied to discussion {in_reply_to} on MR {target_id}",
                            url=None,
                        )

                    # GitLab inline diff comments require position data (base_sha,
                    # start_sha, head_sha, new_path, new_line) which is not available
                    # in this context. Degrade gracefully: create a general discussion
                    # that references the file/line in the body text (same fallback
                    # update_pull_request uses for review_comments).
                    logger.info(
                        f"GitLab inline comment on MR {target_id} at {path}:{line} "
                        "falling back to general discussion (no diff position data)"
                    )
                    discussion_result = await tracker_client.create_mr_discussion(
                        mr_iid=target_id,
                        body=f"**{path}:{line}**\n\n{comment}",
                    )
                    return AddCommentResponse(
                        comment_id=str(discussion_result.get("id", "")),
                        status="created",
                        message=(
                            f"Added comment to MR {target_id} as a general discussion "
                            f"referencing {path}:{line} (GitLab inline diff positioning "
                            "requires diff position data which is not available)"
                        ),
                        url=None,
                    )

            # Regular PR/MR comment (not inline)
            else:
                # Handle threaded replies
                if in_reply_to:
                    # Threaded replies are only supported on GitHub and GitLab
                    if platform not in ("github", "gitlab"):
                        raise HTTPException(
                            status_code=400,
                            detail=f"Threaded replies (in_reply_to) are only supported for "
                            f"GitHub and GitLab pull/merge requests. "
                            f"Tracker type '{tracker_client.tracker_type}' does not support this feature.",
                        )

                    if platform == "github":
                        # GitHub: reply to a review comment
                        logger.info(
                            f"Replying to comment {in_reply_to} on GitHub PR {target_id}"
                        )
                        reply_result = await tracker_client.reply_to_review_comment(
                            pr_number=target_id,
                            comment_id=in_reply_to,
                            body=comment,
                        )
                        return AddCommentResponse(
                            comment_id=str(reply_result.get("id", "")),
                            status="created",
                            message=f"Successfully replied to comment {in_reply_to} on PR {target_id}",
                            url=reply_result.get("html_url"),
                        )
                    else:  # GitLab (explicitly checked above)
                        # GitLab: reply to a discussion
                        logger.info(
                            f"Replying to discussion {in_reply_to} on GitLab MR {target_id}"
                        )
                        reply_result = await tracker_client.reply_to_mr_discussion(
                            mr_iid=target_id,
                            discussion_id=in_reply_to,
                            body=comment,
                        )
                        return AddCommentResponse(
                            comment_id=str(reply_result.get("id", "")),
                            status="created",
                            message=f"Successfully replied to discussion {in_reply_to} on MR {target_id}",
                            url=None,
                        )

                logger.info(f"Adding comment to {target_id} via tracker client")
                created_comment = await tracker_client.add_comment(target_id, comment)
                logger.info(f"Successfully added comment to {target_id}")

                return AddCommentResponse(
                    comment_id=str(created_comment.id),
                    status="created",
                    message=f"Successfully added comment to {target_id}",
                    url=created_comment.meta_data.get("url")
                    if hasattr(created_comment, "meta_data")
                    else None,
                )

        except NotImplementedError:
            logger.warning(
                f"Tracker type {tracker_client.tracker_type} does not support "
                "adding comments."
            )
            raise HTTPException(
                status_code=501,
                detail="Adding comments not supported by this tracker type.",
            )
        except HTTPException:
            # Re-raise HTTPException without wrapping
            raise
        except Exception as e:
            logger.error(
                f"Error adding comment to {target_id} via tracker client: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to add comment to the external tracker: {str(e)}",
            )

    else:
        # This is a regular issue - use the existing logic
        parsed_key = None
        if target.startswith("http"):
            # Detect platform from URL for self-hosted instance support
            issue_platform = None
            try:
                issue_platform = _detect_platform_from_url(target)
            except ValueError:
                # Self-hosted or nonstandard issue URL; rely on later slug parsing.
                pass

            # GitHub issue URL: https://github.com/owner/repo/issues/123
            if issue_platform == "github" and "/issues/" in target:
                parts = target.split("/")
                if len(parts) >= 7:
                    owner = parts[3]
                    repo = parts[4]
                    issue_number = parts[6].rstrip("/").split("?")[0].split("#")[0]
                    parsed_key = f"{owner}/{repo}#{issue_number}"
                    logger.info(f"Parsed GitHub issue URL to key: {parsed_key}")
            # GitLab issue URL: https://gitlab.com/owner/repo/-/issues/1
            # Also handles self-hosted GitLab (e.g., https://git.example.com/owner/repo/-/issues/1)
            elif issue_platform == "gitlab" and "/issues/" in target:
                issue_parts = target.split("/issues/")
                issue_number = issue_parts[-1].rstrip("/").split("?")[0].split("#")[0]
                url_path = issue_parts[0].split("://")[1].split("/")
                if len(url_path) >= 3:
                    project_path_tmp = "/".join(url_path[1:]).rstrip("/-")
                    parsed_key = f"{project_path_tmp}#{issue_number}"
                    logger.info(f"Parsed GitLab issue URL to key: {parsed_key}")

        # Try to find the issue
        issue_obj = None
        try:
            if parsed_key:
                try:
                    issue_obj = _find_issue_by_identifier(
                        db, parsed_key, current_user.account_id
                    )
                    logger.info(f"Found issue using parsed key: {parsed_key}")
                except IssueNotFoundError:
                    logger.info(
                        f"Could not find issue with parsed key {parsed_key}, "
                        "trying original target"
                    )

            if not issue_obj:
                issue_obj = _find_issue_by_identifier(
                    db, target, current_user.account_id
                )
        except IssueNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        # Get tracker client
        tracker_client = await get_tracker_client(
            issue_obj.project.organization_id, issue_obj.project_id, db, current_user
        )

        if not issue_obj.external_id and not issue_obj.key:
            logger.error(
                f"Cannot add comment to {target}: Missing external_id and key."
            )
            raise HTTPException(
                status_code=400,
                detail="Cannot add comment: Missing external identifier.",
            )

        target_id = issue_obj.key if issue_obj.key else issue_obj.external_id

        try:
            logger.info(f"Adding comment to issue {target_id} via tracker client")
            created_comment = await tracker_client.add_comment(target_id, comment)
            logger.info(f"Successfully added comment to issue {target_id}")

            return AddCommentResponse(
                comment_id=str(created_comment.id),
                status="created",
                message=f"Successfully added comment to {target_id}",
                url=created_comment.meta_data.get("url")
                if hasattr(created_comment, "meta_data")
                else None,
            )
        except NotImplementedError:
            logger.warning(
                f"Tracker type {tracker_client.tracker_type} does not support "
                "adding comments."
            )
            raise HTTPException(
                status_code=501,
                detail="Adding comments not supported by this tracker type.",
            )
        except HTTPException:
            # Re-raise HTTPException without wrapping
            raise
        except Exception as e:
            logger.error(
                f"Error adding comment to {target_id} via tracker client: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Failed to add comment to the external tracker: {str(e)}",
            )


async def get_pull_request(
    pull_request: str,
    include_comments: bool = True,
    include_diff: bool = True,
) -> "PullRequestResponse":
    """
    Handles the 'get_pull_request' tool call.

    Gets details of a GitHub pull request or GitLab merge request.
    Auto-detects the platform from the URL.

    Args:
        pull_request: PR/MR identifier (URL, slug, or number).
        include_comments: Whether to include comments in the response.
        include_diff: Whether to include diff/changes in the response.

    Returns:
        PullRequestResponse with PR/MR details.
    """
    from preloop.schemas.mcp import PullRequestResponse

    db, current_user = await _get_authenticated_user(get_http_request().headers)

    pr_info = _parse_pr_identifier(pull_request)
    pr_number = pr_info["pr_number"]
    project_path = pr_info["project_path"]
    owner = pr_info["owner"]
    repo = pr_info["repo"]
    platform: Optional[Literal["github", "gitlab"]] = pr_info["platform"]

    # Find the project
    project_obj = _find_pr_project(
        db,
        project_path=project_path,
        owner=owner,
        repo=repo,
        platform=platform,
        account_id=str(current_user.account_id),
    )

    # Get tracker client
    tracker_client = await get_tracker_client(
        project_obj.organization_id, project_obj.id, db, current_user
    )

    # Determine platform from tracker if not yet known
    if platform is None:
        platform = (
            "github" if tracker_client.tracker_type.lower() == "github" else "gitlab"
        )

    try:
        if platform == "github":
            logger.info(f"Getting GitHub pull request {pr_number}")
            pr_data = await tracker_client.get_pull_request(pr_number)

            # The tracker method already fetches comments by default.
            # Strip them if not requested to avoid unnecessary data transfer.
            if not include_comments:
                pr_data["comments"] = []
            else:
                # Optionally fetch additional comments via dedicated method
                # (tracker.get_pull_request already includes comments, but this
                # could be used for more detailed filtering in the future)
                pass

            # Include diff if already in pr_data or requested
            if not include_diff and "changes" in pr_data:
                pr_data["changes"] = None

            logger.info(f"Successfully retrieved pull request {pr_number}")
            return PullRequestResponse(**pr_data)

        else:  # GitLab
            logger.info(f"Getting GitLab merge request {pr_number}")
            mr_data = await tracker_client.get_merge_request(pr_number)

            # The tracker method already fetches comments/discussions by default.
            # Strip them if not requested to avoid unnecessary data transfer.
            if not include_comments:
                mr_data["comments"] = []
            else:
                # Optionally fetch additional discussions via dedicated method
                pass

            # Include diff if already in mr_data or requested
            if not include_diff and "changes" in mr_data:
                mr_data["changes"] = None

            logger.info(f"Successfully retrieved merge request {pr_number}")

            # Map GitLab MR fields to PullRequestResponse format for consistency
            return PullRequestResponse(
                id=mr_data.get("id", ""),
                number=mr_data.get("iid", 0),
                title=mr_data.get("title", ""),
                description=mr_data.get("description"),
                state=mr_data.get("state", ""),
                author=mr_data.get("author"),
                assignees=mr_data.get("assignees", []),
                reviewers=mr_data.get("reviewers", []),
                labels=mr_data.get("labels", []),
                url=mr_data.get("url", ""),
                source_branch=mr_data.get("source_branch"),
                target_branch=mr_data.get("target_branch"),
                created_at=mr_data.get("created_at"),
                updated_at=mr_data.get("updated_at"),
                merged_at=mr_data.get("merged_at"),
                is_draft=mr_data.get("work_in_progress", False),
                comments=mr_data.get("comments", []),
                changes=mr_data.get("changes"),
            )

    except Exception as e:
        logger.error(
            f"Error getting pull request/merge request {pr_number}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to get pull request/merge request: {str(e)}",
        )


async def update_pull_request(
    pull_request: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    state: Optional[str] = None,
    assignees: Optional[List[str]] = None,
    reviewers: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    draft: Optional[bool] = None,
    # Review parameters
    review_action: Optional[str] = None,  # "approve", "request_changes", "comment"
    review_body: Optional[str] = None,
    review_comments: Optional[List[Dict]] = None,  # [{path, line, body, side}]
    # Reaction parameters
    add_reaction: Optional[
        str
    ] = None,  # Emoji name to add (e.g., "eyes", "+1", "rocket")
    remove_reaction: Optional[str] = None,  # Emoji name to remove
) -> "UpdatePullRequestResponse":
    """
    Handles the 'update_pull_request' tool call.

    Updates a GitHub pull request or GitLab merge request.
    Auto-detects the platform from the URL.

    Args:
        pull_request: PR/MR identifier (URL, slug, or number).
        title: New title for the PR/MR.
        description: New description for the PR/MR.
        state: New state ("open"/"closed" for GitHub, "close"/"reopen" for GitLab).
        assignees: List of assignee usernames.
        reviewers: List of reviewer usernames.
        labels: List of label names.
        draft: Whether to mark as draft.
        review_action: Review action - "approve", "request_changes", or "comment".
        review_body: Review summary/body text.
        review_comments: List of inline review comments, each with:
            - path: file path
            - line: line number
            - body: comment text
            - side: "LEFT" (old) or "RIGHT" (new), default "RIGHT"
        add_reaction: Emoji name to add as a reaction (e.g., "eyes", "+1", "rocket", "heart").
            GitHub uses: +1, -1, laugh, confused, heart, hooray, rocket, eyes
            GitLab uses: thumbsup, thumbsdown, smile, grinning, eyes, rocket, etc.
        remove_reaction: Emoji name to remove from reactions.

    Returns:
        UpdatePullRequestResponse with update status.
    """
    from preloop.schemas.mcp import UpdatePullRequestResponse

    db, current_user = await _get_authenticated_user(get_http_request().headers)

    pr_info = _parse_pr_identifier(pull_request)
    pr_number = pr_info["pr_number"]
    project_path = pr_info["project_path"]
    owner = pr_info["owner"]
    repo = pr_info["repo"]
    platform: Optional[Literal["github", "gitlab"]] = pr_info["platform"]

    # Find the project
    project_obj = _find_pr_project(
        db,
        project_path=project_path,
        owner=owner,
        repo=repo,
        platform=platform,
        account_id=str(current_user.account_id),
    )

    # Get tracker client
    tracker_client = await get_tracker_client(
        project_obj.organization_id, project_obj.id, db, current_user
    )

    # Determine platform from tracker if not yet known
    if platform is None:
        platform = (
            "github" if tracker_client.tracker_type.lower() == "github" else "gitlab"
        )

    try:
        result_url = None
        result_id = None

        # Validate that review_comments requires review_action
        if review_comments and not review_action:
            raise HTTPException(
                status_code=400,
                detail="review_comments requires review_action to be set. "
                "Provide review_action='comment' (or 'approve'/'request_changes') "
                "along with review_comments.",
            )

        # Track GitLab-specific limitations encountered during the request
        gitlab_warnings: list[str] = []

        # Handle review action if provided
        if review_action:
            review_action_lower = review_action.lower()

            if platform == "github":
                # Map review_action to GitHub event
                event_map = {
                    "approve": "APPROVE",
                    "request_changes": "REQUEST_CHANGES",
                    "comment": "COMMENT",
                }
                event = event_map.get(review_action_lower)
                if not event:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid review_action: {review_action}. "
                        "Must be 'approve', 'request_changes', or 'comment'.",
                    )

                # Validate that comment reviews have content
                if event == "COMMENT" and not review_body and not review_comments:
                    raise HTTPException(
                        status_code=400,
                        detail="review_action='comment' requires either review_body "
                        "or review_comments to be provided.",
                    )

                # Validate review_comments structure if provided
                if review_comments:
                    for idx, rc in enumerate(review_comments):
                        if not isinstance(rc, dict):
                            raise HTTPException(
                                status_code=400,
                                detail=f"review_comments[{idx}] must be an object, "
                                f"got {type(rc).__name__}",
                            )
                        missing_fields = []
                        if "path" not in rc:
                            missing_fields.append("path")
                        if "body" not in rc:
                            missing_fields.append("body")
                        # line is required for inline comments
                        if "line" not in rc:
                            missing_fields.append("line")
                        if missing_fields:
                            raise HTTPException(
                                status_code=400,
                                detail=f"review_comments[{idx}] is missing required "
                                f"field(s): {', '.join(missing_fields)}. "
                                "Each comment must have 'path', 'line', and 'body'.",
                            )

                # GitHub COMMENT reviews require a non-empty body
                # If review_comments are provided without a body, use a default
                effective_body = review_body or ""
                if event == "COMMENT" and not effective_body and review_comments:
                    effective_body = "Inline comments"

                logger.info(
                    f"Submitting GitHub review for PR {pr_number} with action {event}"
                )
                review_result = await tracker_client.submit_pull_request_review(
                    pr_number=pr_number,
                    body=effective_body,
                    event=event,
                    comments=review_comments,
                )
                result_id = review_result.get("id")
                result_url = review_result.get("html_url")
                logger.info(f"Successfully submitted review for PR {pr_number}")

            else:  # GitLab
                if review_action_lower == "approve":
                    logger.info(f"Approving GitLab MR {pr_number}")
                    approval_result = await tracker_client.approve_merge_request(
                        pr_number
                    )
                    result_id = approval_result.get("id")
                    logger.info(f"Successfully approved MR {pr_number}")

                elif review_action_lower == "request_changes":
                    # GitLab doesn't have request_changes, so unapprove and add note
                    logger.info(f"Unapproving GitLab MR {pr_number} (request changes)")
                    await tracker_client.unapprove_merge_request(pr_number)

                    # Add a comment explaining the change request
                    if review_body:
                        await tracker_client.create_mr_discussion(
                            mr_iid=pr_number,
                            body=f"**Changes Requested**\n\n{review_body}",
                        )
                    logger.info(f"Successfully requested changes on MR {pr_number}")

                elif review_action_lower == "comment":
                    # Validate that comment reviews have content
                    if not review_body and not review_comments:
                        raise HTTPException(
                            status_code=400,
                            detail="review_action='comment' requires either review_body "
                            "or review_comments to be provided.",
                        )
                    # Add a comment
                    if review_body:
                        logger.info(f"Adding comment to GitLab MR {pr_number}")
                        discussion_result = await tracker_client.create_mr_discussion(
                            mr_iid=pr_number,
                            body=review_body,
                        )
                        result_id = discussion_result.get("id")
                        logger.info(f"Successfully added comment to MR {pr_number}")
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid review_action: {review_action}. "
                        "Must be 'approve', 'request_changes', or 'comment'.",
                    )

                # Handle inline review comments for GitLab
                if review_comments:
                    # Validate review_comments structure
                    for idx, comment in enumerate(review_comments):
                        if not isinstance(comment, dict):
                            raise HTTPException(
                                status_code=400,
                                detail=f"review_comments[{idx}] must be an object, "
                                f"got {type(comment).__name__}",
                            )
                        missing_fields = []
                        if "path" not in comment:
                            missing_fields.append("path")
                        if "body" not in comment:
                            missing_fields.append("body")
                        if "line" not in comment:
                            missing_fields.append("line")
                        if missing_fields:
                            raise HTTPException(
                                status_code=400,
                                detail=f"review_comments[{idx}] is missing required "
                                f"field(s): {', '.join(missing_fields)}. "
                                "Each comment must have 'path', 'line', and 'body'.",
                            )

                    # GitLab inline diff comments require position data (base_sha, start_sha,
                    # head_sha) which we don't have access to. Instead, create general
                    # discussions that reference the file/line in the body text.
                    gitlab_warnings.append(
                        "GitLab review_comments created as general discussions "
                        "(inline diff positioning not available)"
                    )
                    for comment in review_comments:
                        await tracker_client.create_mr_discussion(
                            mr_iid=pr_number,
                            body=f"**{comment['path']}:{comment['line']}**\n\n"
                            f"{comment['body']}",
                        )

        # Handle PR/MR metadata updates
        # Use 'is not None' checks to allow clearing fields with empty values
        # (e.g., labels=[] to remove all labels, description="" to clear description)
        has_updates = any(
            x is not None
            for x in [title, description, state, assignees, reviewers, labels, draft]
        )

        if has_updates:
            if platform == "github":
                logger.info(f"Updating GitHub pull request {pr_number}")
                pr_data = await tracker_client.update_pull_request(
                    pr_identifier=pr_number,
                    title=title,
                    description=description,
                    state=state,
                    assignees=assignees,
                    reviewers=reviewers,
                    labels=labels,
                    draft=draft,
                )
                result_id = result_id or pr_data.get("id")
                result_url = result_url or pr_data.get("url")
                logger.info(f"Successfully updated pull request {pr_number}")

            else:  # GitLab
                logger.info(f"Updating GitLab merge request {pr_number}")
                # Convert state to state_event for GitLab
                state_event = None
                if state:
                    state_lower = state.lower()
                    if state_lower in ("closed", "close"):
                        state_event = "close"
                    elif state_lower in ("open", "reopen"):
                        state_event = "reopen"

                # Look up user IDs for assignees/reviewers
                # Note: assignees/reviewers being None means "don't change"
                # Empty list [] means "clear all assignees/reviewers"
                assignee_ids = None
                reviewer_ids = None

                if assignees is not None:
                    if len(assignees) == 0:
                        # Explicitly clear assignees by passing empty list
                        assignee_ids = []
                        logger.info("Clearing all assignees from MR")
                    else:
                        try:
                            assignee_ids = (
                                await tracker_client.get_user_ids_by_usernames(
                                    assignees
                                )
                            )
                            if len(assignee_ids) < len(assignees):
                                not_found = len(assignees) - len(assignee_ids)
                                gitlab_warnings.append(
                                    f"{not_found} assignee(s) not found in GitLab"
                                )
                            logger.info(
                                f"Resolved {len(assignee_ids)}/{len(assignees)} assignee IDs"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to resolve assignee IDs: {e}")
                            gitlab_warnings.append(
                                f"assignees not applied (lookup failed: {e})"
                            )

                if reviewers is not None:
                    if len(reviewers) == 0:
                        # Explicitly clear reviewers by passing empty list
                        reviewer_ids = []
                        logger.info("Clearing all reviewers from MR")
                    else:
                        try:
                            reviewer_ids = (
                                await tracker_client.get_user_ids_by_usernames(
                                    reviewers
                                )
                            )
                            if len(reviewer_ids) < len(reviewers):
                                not_found = len(reviewers) - len(reviewer_ids)
                                gitlab_warnings.append(
                                    f"{not_found} reviewer(s) not found in GitLab"
                                )
                            logger.info(
                                f"Resolved {len(reviewer_ids)}/{len(reviewers)} reviewer IDs"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to resolve reviewer IDs: {e}")
                            gitlab_warnings.append(
                                f"reviewers not applied (lookup failed: {e})"
                            )

                mr_data = await tracker_client.update_merge_request(
                    mr_identifier=pr_number,
                    title=title,
                    description=description,
                    state_event=state_event,
                    assignee_ids=assignee_ids,
                    reviewer_ids=reviewer_ids,
                    labels=labels,
                    draft=draft,
                )
                result_id = result_id or mr_data.get("id")
                result_url = result_url or mr_data.get("url")
                logger.info(f"Successfully updated merge request {pr_number}")

        # Handle reactions
        reaction_added = False
        reaction_removed = False
        reaction_errors = []

        if add_reaction or remove_reaction:
            if platform == "github":
                # GitHub uses issue reactions API (PRs are issues)
                # Reaction names: +1, -1, laugh, confused, heart, hooray, rocket, eyes
                if add_reaction:
                    logger.info(
                        f"Adding reaction '{add_reaction}' to GitHub PR {pr_number} "
                        f"(owner={tracker_client.connection_details.get('owner')}, "
                        f"repo={tracker_client.connection_details.get('repo')})"
                    )
                    try:
                        await tracker_client.add_issue_reaction(
                            issue_number=pr_number,
                            reaction=add_reaction,
                        )
                        logger.info(f"Successfully added reaction to PR {pr_number}")
                        reaction_added = True
                    except Exception as e:
                        error_msg = f"Failed to add reaction '{add_reaction}': {e}"
                        logger.error(error_msg)
                        reaction_errors.append(error_msg)

                if remove_reaction:
                    logger.info(
                        f"Removing reaction '{remove_reaction}' from GitHub PR {pr_number}"
                    )
                    try:
                        removed = await tracker_client.remove_issue_reaction(
                            issue_number=pr_number,
                            reaction=remove_reaction,
                        )
                        if removed:
                            logger.info(
                                f"Successfully removed reaction from PR {pr_number}"
                            )
                            reaction_removed = True
                        else:
                            # Reaction not found - not an error, but note it
                            logger.info(
                                f"Reaction '{remove_reaction}' not found on PR {pr_number} "
                                "(may have been already removed or never added)"
                            )
                    except Exception as e:
                        error_msg = (
                            f"Failed to remove reaction '{remove_reaction}': {e}"
                        )
                        logger.error(error_msg)
                        reaction_errors.append(error_msg)

            else:  # GitLab
                # GitLab uses award emoji API
                # Emoji names: thumbsup, thumbsdown, smile, grinning, eyes, rocket, etc.
                if add_reaction:
                    logger.info(
                        f"Adding emoji '{add_reaction}' to GitLab MR {pr_number}"
                    )
                    try:
                        await tracker_client.add_mr_award_emoji(
                            mr_iid=pr_number,
                            emoji_name=add_reaction,
                        )
                        logger.info(f"Successfully added emoji to MR {pr_number}")
                        reaction_added = True
                    except Exception as e:
                        error_msg = f"Failed to add emoji '{add_reaction}': {e}"
                        logger.error(error_msg)
                        reaction_errors.append(error_msg)

                if remove_reaction:
                    logger.info(
                        f"Removing emoji '{remove_reaction}' from GitLab MR {pr_number}"
                    )
                    try:
                        await tracker_client.remove_mr_award_emoji(
                            mr_iid=pr_number,
                            emoji_name=remove_reaction,
                        )
                        logger.info(f"Successfully removed emoji from MR {pr_number}")
                        reaction_removed = True
                    except Exception as e:
                        error_msg = f"Failed to remove emoji '{remove_reaction}': {e}"
                        logger.error(error_msg)
                        reaction_errors.append(error_msg)

        # Build response message
        actions_taken = []
        actions_failed = []

        if review_action:
            actions_taken.append(f"review ({review_action})")
        if has_updates:
            actions_taken.append("metadata update")
        if add_reaction:
            if reaction_added:
                actions_taken.append(f"added reaction ({add_reaction})")
            else:
                actions_failed.append(f"add reaction ({add_reaction})")
        if remove_reaction:
            if reaction_removed:
                actions_taken.append(f"removed reaction ({remove_reaction})")
            else:
                actions_failed.append(f"remove reaction ({remove_reaction})")

        # Build message based on what succeeded/failed
        if actions_taken:
            message = (
                f"Successfully performed {', '.join(actions_taken)} on "
                f"{'PR' if platform == 'github' else 'MR'} {pr_number}"
            )
        else:
            message = f"No actions completed on {'PR' if platform == 'github' else 'MR'} {pr_number}"

        if actions_failed:
            message += f". FAILED: {', '.join(actions_failed)}"

        if reaction_errors:
            message += f". Errors: {'; '.join(reaction_errors)}"

        # Add warnings for GitLab limitations
        if gitlab_warnings:
            message += f". Note: {'; '.join(gitlab_warnings)}"

        return UpdatePullRequestResponse(
            pull_request_id=str(result_id) if result_id else pr_number,
            status="updated" if actions_taken else "failed",
            message=message,
            url=result_url,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error updating pull request/merge request {pr_number}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to update pull request/merge request: {str(e)}",
        )


async def create_pull_request(
    project: str,
    title: str,
    source_branch: str,
    target_branch: str,
    description: Optional[str] = None,
    draft: bool = False,
    assignees: Optional[List[str]] = None,
    reviewers: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    milestone: Optional[str] = None,
    extra_options: Optional[Dict] = None,
) -> "CreatePullRequestResponse":
    """
    Handles the 'create_pull_request' tool call.

    Creates a GitHub pull request or GitLab merge request.
    Auto-detects the platform from the project configuration.

    Args:
        project: Project identifier (slug like "owner/repo", full path, or URL).
        title: PR/MR title.
        source_branch: Branch containing the changes (head branch).
        target_branch: Branch to merge into (base branch).
        description: PR/MR description/body.
        draft: Whether to create as draft.
        assignees: List of assignee usernames.
        reviewers: List of reviewer usernames.
        labels: List of label names.
        milestone: Milestone number or title.
        extra_options: Additional options (e.g., squash, remove_source_branch for GitLab).

    Returns:
        CreatePullRequestResponse with created PR/MR details.
    """
    from preloop.schemas.mcp import CreatePullRequestResponse

    db, current_user = await _get_authenticated_user(get_http_request().headers)

    # Parse project identifier
    project_path = project
    platform = None

    # Detect platform from URL hostname (not substring match).
    if project.startswith("http"):
        try:
            platform = _detect_platform_from_url(project)
        except ValueError:
            platform = None
        parsed_project = urlparse(project)
        path_parts = [p for p in parsed_project.path.split("/") if p]
        if platform == "github" and len(path_parts) >= 2:
            project_path = f"{path_parts[0]}/{path_parts[1]}"
        elif platform == "gitlab" and path_parts:
            project_path = "/".join(path_parts).rstrip("/")

    # Find the project in database
    from preloop.models.crud import crud_project

    # Try full project path first (handles GitLab nested groups)
    project_obj = crud_project.get_by_slug_or_identifier(
        db,
        slug_or_identifier=project_path,
        account_id=str(current_user.account_id),
    )

    # Fall back to simpler matching if not found
    if not project_obj and "/" in project_path:
        parts = project_path.split("/")
        if len(parts) >= 2:
            simple_path = f"{parts[-2]}/{parts[-1]}"
            project_obj = crud_project.get_by_slug_or_identifier(
                db,
                slug_or_identifier=simple_path,
                account_id=str(current_user.account_id),
            )

    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project not found: {project}. Make sure it's synced to your account.",
        )

    # Get tracker client
    tracker_client = await get_tracker_client(
        project_obj.organization_id, project_obj.id, db, current_user
    )

    # Determine platform from tracker if not already known
    if platform is None:
        platform = tracker_client.tracker_type.lower()

    try:
        if platform == "github":
            logger.info(
                f"Creating GitHub PR: {title} ({source_branch} -> {target_branch})"
            )
            result = await tracker_client.create_pull_request(
                title=title,
                source_branch=source_branch,
                target_branch=target_branch,
                description=description,
                draft=draft,
                assignees=assignees,
                reviewers=reviewers,
                labels=labels,
                milestone=milestone,
            )

            return CreatePullRequestResponse(
                pull_request_id=str(result["id"]),
                number=result["number"],
                status="created",
                message=f"Successfully created pull request #{result['number']}",
                url=result["url"],
                source_branch=source_branch,
                target_branch=target_branch,
                is_draft=result.get("is_draft", False),
            )

        else:  # GitLab
            logger.info(
                f"Creating GitLab MR: {title} ({source_branch} -> {target_branch})"
            )

            # Parse extra options for GitLab
            squash = None
            remove_source_branch = None
            allow_collaboration = None

            if extra_options:
                squash = extra_options.get("squash")
                remove_source_branch = extra_options.get("remove_source_branch")
                allow_collaboration = extra_options.get("allow_collaboration")

            # GitLab requires user IDs, not usernames, for assignees/reviewers
            # Look up IDs from usernames, with fallback to extra_options
            assignee_ids = None
            reviewer_ids = None
            warnings = []

            if extra_options and "assignee_ids" in extra_options:
                assignee_ids = extra_options["assignee_ids"]
            elif assignees:
                try:
                    assignee_ids = await tracker_client.get_user_ids_by_usernames(
                        assignees
                    )
                    if len(assignee_ids) < len(assignees):
                        not_found = len(assignees) - len(assignee_ids)
                        warnings.append(f"{not_found} assignee(s) not found in GitLab")
                    logger.info(
                        f"Resolved {len(assignee_ids)}/{len(assignees)} assignee IDs for MR creation"
                    )
                except Exception as e:
                    logger.warning(f"Failed to resolve assignee IDs: {e}")
                    warnings.append("assignees not applied (lookup failed)")

            if extra_options and "reviewer_ids" in extra_options:
                reviewer_ids = extra_options["reviewer_ids"]
            elif reviewers:
                try:
                    reviewer_ids = await tracker_client.get_user_ids_by_usernames(
                        reviewers
                    )
                    if len(reviewer_ids) < len(reviewers):
                        not_found = len(reviewers) - len(reviewer_ids)
                        warnings.append(f"{not_found} reviewer(s) not found in GitLab")
                    logger.info(
                        f"Resolved {len(reviewer_ids)}/{len(reviewers)} reviewer IDs for MR creation"
                    )
                except Exception as e:
                    logger.warning(f"Failed to resolve reviewer IDs: {e}")
                    warnings.append("reviewers not applied (lookup failed)")

            milestone_id = None
            if milestone:
                if milestone.isdigit():
                    milestone_id = int(milestone)
                elif extra_options and "milestone_id" in extra_options:
                    milestone_id = extra_options["milestone_id"]
                else:
                    # Non-numeric milestone title provided but can't be resolved to ID
                    # GitLab API requires milestone_id, not title
                    warnings.append(
                        f"milestone '{milestone}' ignored (use numeric ID or set "
                        "extra_options.milestone_id for non-numeric milestones)"
                    )
                    logger.warning(
                        f"Non-numeric milestone '{milestone}' provided for GitLab MR but "
                        "milestone_id not in extra_options. Milestone will not be set."
                    )
            elif extra_options and "milestone_id" in extra_options:
                milestone_id = extra_options["milestone_id"]

            result = await tracker_client.create_merge_request(
                title=title,
                source_branch=source_branch,
                target_branch=target_branch,
                description=description,
                draft=draft,
                assignee_ids=assignee_ids,
                reviewer_ids=reviewer_ids,
                labels=labels,
                milestone_id=milestone_id,
                squash=squash,
                remove_source_branch=remove_source_branch,
                allow_collaboration=allow_collaboration,
            )

            message = f"Successfully created merge request !{result['iid']}"
            if warnings:
                message += f". Note: {' '.join(warnings)}"

            return CreatePullRequestResponse(
                pull_request_id=str(result["id"]),
                number=result["iid"],
                status="created",
                message=message,
                url=result["url"],
                source_branch=source_branch,
                target_branch=target_branch,
                is_draft=result.get("work_in_progress", False),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error creating pull request/merge request: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to create pull request/merge request: {str(e)}",
        )


async def update_comment(
    target: str,
    comment_id: str,
    body: Optional[str] = None,
    resolved: Optional[bool] = None,
    thread_id: Optional[str] = None,
    comment_type: Optional[Literal["review_comment", "issue_comment"]] = None,
) -> "UpdateCommentResponse":
    """
    Handles the 'update_comment' tool call.

    Updates or resolves a comment on a pull request or merge request.
    Works with both GitHub review comments and GitLab MR notes.

    Supports two types of GitHub comments:
    - review_comment: Inline code review comments (on specific lines of code)
    - issue_comment: PR conversation comments (general discussion)

    For GitLab, this updates MR discussion notes (both inline and general).

    Args:
        target: PR/MR identifier (URL, slug, or number). Issue URLs are not supported.
        comment_id: The comment ID to update.
        body: New body text for the comment (optional).
        resolved: Whether to resolve/unresolve the comment thread (optional).
            Note: Thread resolution only works for review_comment types on GitHub.
        thread_id: The thread/discussion ID for resolution (optional).
            For GitHub: The review thread node_id (e.g., "PRRT_...").
            For GitLab: The discussion ID.
            If not provided, comment_id is used for resolution (may fail if wrong ID type).
        comment_type: Type of comment being updated (optional).
            For GitHub: "review_comment" for inline code comments,
                       "issue_comment" for PR conversation comments.
            If not provided, attempts review_comment first, then issue_comment.
            Tip: The get_pull_request response includes a "type" field for each comment.

    Returns:
        UpdateCommentResponse with update status.
    """
    from preloop.schemas.mcp import UpdateCommentResponse

    db, current_user = await _get_authenticated_user(get_http_request().headers)

    # Validate that at least one update is requested
    if body is None and resolved is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'body' or 'resolved' must be provided.",
        )

    target = target.strip()
    comment_id = comment_id.strip()

    # Detect platform and parse target
    project_path = None
    pr_mr_number = None
    platform: Optional[Literal["github", "gitlab"]] = None

    if target.startswith("http"):
        try:
            platform = _detect_platform_from_url(target)
        except ValueError:
            # Unrecognized tracker URL; continue with path-based heuristics below.
            pass

        # GitHub PR URL: https://github.com/owner/repo/pull/123
        if platform == "github" and "/pull/" in target:
            parts = target.split("/")
            if len(parts) >= 7:
                owner = parts[3]
                repo = parts[4]
                pr_mr_number = parts[6].rstrip("/").split("?")[0].split("#")[0]
                project_path = f"{owner}/{repo}"
                logger.info(f"Detected GitHub PR: {project_path}#{pr_mr_number}")
        # GitLab MR URL: https://gitlab.com/owner/repo/-/merge_requests/1
        # Also handles self-hosted GitLab where platform was detected via URL patterns
        # (e.g., https://git.example.com/owner/repo/-/merge_requests/123)
        # platform is set from _detect_platform_from_url which checks for /merge_requests/ pattern
        elif platform == "gitlab" and "/merge_requests/" in target:
            mr_parts = target.split("/merge_requests/")
            pr_mr_number = mr_parts[-1].rstrip("/").split("?")[0].split("#")[0]
            url_path = mr_parts[0].split("://")[1].split("/")
            if len(url_path) >= 3:
                project_path = "/".join(url_path[1:]).rstrip("/-")
                logger.info(f"Detected GitLab MR: {project_path}#{pr_mr_number}")
    # Parse slug format: owner/repo#123
    elif "/" in target and "#" in target:
        slug_parts = target.split("#")
        pr_mr_number = slug_parts[1]
        project_path = slug_parts[0]
        logger.info(f"Detected PR/MR slug format: {project_path}#{pr_mr_number}")
    # Handle numeric-only target (just the PR/MR number)
    elif target.isdigit():
        raise HTTPException(
            status_code=400,
            detail=f"Numeric target '{target}' requires project context. "
            "Use URL format (e.g., https://github.com/owner/repo/pull/123) or "
            "slug format (e.g., owner/repo#123) to specify the project.",
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid target format. Use URL (e.g., https://github.com/owner/repo/pull/123) "
            "or slug format (e.g., owner/repo#123).",
        )

    if not project_path:
        raise HTTPException(
            status_code=400,
            detail="Could not parse project path from target identifier.",
        )

    # Find the project
    project_obj = crud_project.get_by_slug_or_identifier(
        db,
        slug_or_identifier=project_path,
        account_id=str(current_user.account_id),
    )
    if not project_obj:
        raise HTTPException(
            status_code=404,
            detail=f"Project not found for {project_path}",
        )

    # Get tracker client
    tracker_client = await get_tracker_client(
        project_obj.organization_id, project_obj.id, db, current_user
    )

    # Determine platform from tracker if not yet known
    if platform is None:
        platform = (
            "github" if tracker_client.tracker_type.lower() == "github" else "gitlab"
        )

    # Upfront validation: GitHub issue comments cannot be resolved
    # Check this before any updates to avoid partial success states
    # Note: GitLab discussions CAN be resolved regardless of type, so only block GitHub
    if (
        platform == "github"
        and comment_type == "issue_comment"
        and resolved is not None
    ):
        raise HTTPException(
            status_code=400,
            detail="Thread resolution is not supported for GitHub issue comments "
            "(PR conversation comments). Only inline review comments can be resolved. "
            "Remove the 'resolved' parameter or use comment_type='review_comment'.",
        )

    try:
        result_url = None
        actions_taken = []

        if platform == "github":
            # Track which comment type was actually used for logging
            updated_comment_type = None

            # Pre-check: If resolved is requested with body but no comment_type,
            # we need to determine the type upfront to avoid partial success
            # (updating body then failing on resolution).
            if resolved is not None and body is not None and comment_type is None:
                logger.info(
                    f"Pre-checking comment type for {comment_id} since both body "
                    "and resolved are requested without explicit comment_type"
                )
                # Try to get the review comment - if it 404s, it's an issue comment
                try:
                    # Use a lightweight check - try to get the comment
                    owner = tracker_client.connection_details.get("owner")
                    repo = tracker_client.connection_details.get("repo")
                    if owner and repo:
                        import httpx

                        headers = await tracker_client._get_auth_headers()
                        async with httpx.AsyncClient() as client:
                            check_url = (
                                f"https://api.github.com/repos/{owner}/{repo}"
                                f"/pulls/comments/{comment_id}"
                            )
                            response = await client.get(
                                check_url, headers=headers, timeout=10.0
                            )
                            if response.status_code == 404:
                                # It's an issue comment - fail upfront
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        "Thread resolution is not supported for GitHub "
                                        "issue comments. The comment appears to be a PR "
                                        "conversation comment, not an inline review comment. "
                                        "Remove the 'resolved' parameter or set "
                                        "comment_type='review_comment' if this is actually "
                                        "an inline comment."
                                    ),
                                )
                            elif response.status_code == 200:
                                # It's a review comment - proceed
                                logger.info(
                                    f"Pre-check: comment {comment_id} is a review_comment"
                                )
                except HTTPException:
                    raise
                except Exception as e:
                    logger.warning(
                        f"Pre-check failed, proceeding with auto-detect: {e}"
                    )

            # Update comment body if provided
            if body is not None:
                # If comment_type is specified, use only that API
                # If not specified, try review_comment first, then issue_comment
                if comment_type == "issue_comment":
                    # Directly use issue comment API
                    logger.info(f"Updating GitHub issue comment {comment_id}")
                    try:
                        update_result = await tracker_client.update_issue_comment(
                            comment_id=comment_id,
                            body=body,
                        )
                        result_url = update_result.get("html_url")
                        actions_taken.append("body updated")
                        updated_comment_type = "issue_comment"
                        logger.info(f"Successfully updated issue comment {comment_id}")
                    except Exception as e:
                        error_msg = str(e)
                        if "404" in error_msg or "Not Found" in error_msg:
                            raise HTTPException(
                                status_code=404,
                                detail=f"Issue comment {comment_id} not found.",
                            )
                        raise
                else:
                    # Try review_comment first
                    logger.info(f"Updating GitHub review comment {comment_id}")
                    detected_comment_type = "review_comment"
                    try:
                        update_result = await tracker_client.update_review_comment(
                            comment_id=comment_id,
                            body=body,
                        )
                        result_url = update_result.get("html_url")
                        actions_taken.append("body updated")
                        logger.info(f"Successfully updated review comment {comment_id}")
                    except Exception as e:
                        error_msg = str(e)
                        # Check if this is a 404 - might be an issue comment
                        if "404" in error_msg or "Not Found" in error_msg:
                            if comment_type == "review_comment":
                                # User explicitly specified review_comment, don't try fallback
                                raise HTTPException(
                                    status_code=404,
                                    detail=f"Review comment {comment_id} not found.",
                                )
                            # Try issue_comment as fallback
                            logger.info(
                                f"Review comment {comment_id} not found, "
                                "trying as issue comment"
                            )
                            try:
                                update_result = (
                                    await tracker_client.update_issue_comment(
                                        comment_id=comment_id,
                                        body=body,
                                    )
                                )
                                result_url = update_result.get("html_url")
                                actions_taken.append("body updated")
                                detected_comment_type = "issue_comment"
                                logger.info(
                                    f"Successfully updated issue comment {comment_id}"
                                )
                            except Exception as e2:
                                error_msg2 = str(e2)
                                if "404" in error_msg2 or "Not Found" in error_msg2:
                                    raise HTTPException(
                                        status_code=404,
                                        detail=f"Comment {comment_id} not found as either "
                                        "a review comment or issue comment.",
                                    )
                                raise
                        else:
                            raise
                    updated_comment_type = detected_comment_type

            # Resolve/unresolve thread if requested
            if resolved is not None:
                # Thread resolution only works for review comments, not issue comments.
                # Note: explicit comment_type="issue_comment" + resolved is validated upfront.
                # This check handles the auto-detect fallback case where we updated an
                # issue comment but the caller also requested resolution.
                if body is not None and updated_comment_type == "issue_comment":
                    raise HTTPException(
                        status_code=400,
                        detail="Thread resolution is not supported for issue comments "
                        "(PR conversation comments). The comment was updated but cannot "
                        "be resolved. Only inline review comments can be resolved.",
                    )

                # GitHub's resolve_review_thread requires the thread's GraphQL node_id
                # (format: "PRRT_..."), not a comment ID (format: "PRRC_...").
                if not thread_id:
                    # Check if comment_id looks like a thread ID
                    if comment_id.startswith("PRRT_"):
                        # User passed thread_id as comment_id, use it
                        thread_id = comment_id
                        logger.info(
                            "Using comment_id as thread_id since it has PRRT_ prefix"
                        )
                    else:
                        # Try to look up the thread_id from the comment_id
                        logger.info(
                            f"Attempting to look up thread_id for comment {comment_id}"
                        )
                        try:
                            looked_up_thread_id = (
                                await tracker_client.get_thread_id_for_comment(
                                    pr_number=pr_mr_number,
                                    comment_id=comment_id,
                                )
                            )
                            if looked_up_thread_id:
                                thread_id = looked_up_thread_id
                                logger.info(
                                    f"Found thread_id {thread_id} for comment {comment_id}"
                                )
                            else:
                                raise HTTPException(
                                    status_code=400,
                                    detail=(
                                        "Could not find thread_id for the given comment_id. "
                                        f"The comment_id '{comment_id}' may not be a review comment. "
                                        "Thread resolution only works for review comments (inline diff comments). "
                                        "For issue comments, resolution is not supported."
                                    ),
                                )
                        except HTTPException:
                            raise
                        except Exception as e:
                            logger.warning(f"Thread lookup failed: {e}")
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    "GitHub thread resolution requires a thread_id parameter "
                                    "(format: 'PRRT_...'). Automatic lookup failed. "
                                    "To resolve a thread, provide the thread's GraphQL "
                                    "node_id in the thread_id parameter."
                                ),
                            )
                logger.info(
                    f"{'Resolving' if resolved else 'Unresolving'} GitHub review "
                    f"thread {thread_id}"
                )
                await tracker_client.resolve_review_thread(
                    thread_id=thread_id,
                    resolved=resolved,
                )
                actions_taken.append("resolved" if resolved else "unresolved")
                logger.info(
                    f"Successfully {'resolved' if resolved else 'unresolved'} "
                    f"review thread {thread_id}"
                )

        else:  # GitLab
            # Update note body if provided
            if body is not None:
                logger.info(
                    f"Updating GitLab MR note {comment_id} for MR {pr_mr_number}"
                )
                await tracker_client.update_mr_note(
                    mr_iid=pr_mr_number,
                    note_id=comment_id,
                    body=body,
                )
                actions_taken.append("body updated")
                logger.info(f"Successfully updated MR note {comment_id}")

            # Resolve/unresolve discussion if requested
            if resolved is not None:
                # GitLab's resolve_mr_discussion requires the discussion ID, not a note ID.
                # If thread_id is not provided, auto-lookup the discussion containing
                # this note ID.
                if not thread_id:
                    logger.info(
                        f"Auto-looking up discussion ID for GitLab note {comment_id} "
                        f"in MR {pr_mr_number}"
                    )
                    try:
                        discussions = await tracker_client.get_mr_discussions(
                            mr_iid=pr_mr_number
                        )
                        for discussion in discussions:
                            for note in discussion.get("notes", []):
                                if str(note.get("id")) == str(comment_id):
                                    thread_id = discussion["id"]
                                    logger.info(
                                        f"Found discussion ID {thread_id} for "
                                        f"note {comment_id}"
                                    )
                                    break
                            if thread_id:
                                break
                    except Exception as e:
                        logger.warning(f"Failed to auto-lookup discussion ID: {e}")

                    if not thread_id:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Could not find the discussion containing "
                                f"note '{comment_id}'. Please provide the "
                                "discussion ID via the thread_id parameter."
                            ),
                        )
                logger.info(
                    f"{'Resolving' if resolved else 'Unresolving'} GitLab MR "
                    f"discussion {thread_id}"
                )
                await tracker_client.resolve_mr_discussion(
                    mr_iid=pr_mr_number,
                    discussion_id=thread_id,
                    resolved=resolved,
                )
                actions_taken.append("resolved" if resolved else "unresolved")
                logger.info(
                    f"Successfully {'resolved' if resolved else 'unresolved'} "
                    f"MR discussion {thread_id}"
                )

        message = (
            f"Successfully performed {', '.join(actions_taken)} on comment {comment_id}"
        )

        return UpdateCommentResponse(
            comment_id=comment_id,
            status="updated",
            message=message,
            url=result_url,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error updating comment {comment_id} for {target}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to update comment: {str(e)}",
        )
