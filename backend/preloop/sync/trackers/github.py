"""
GitHub tracker implementation for preloop.sync.

Supports two authentication types:
- api_token: Traditional Personal Access Token (PAT) authentication
- github_app: GitHub App installation-based authentication (for SaaS)
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from preloop.models.models.organization import Organization
from preloop.models.models.webhook import Webhook
from preloop.schemas.tracker_models import (
    Issue,
    IssueComment,
    IssueCreate,
    IssueFilter,
    IssuePriority,
    IssueStatus,
    IssueUpdate,
    ProjectMetadata,
    TrackerConnection,
    IssueUser,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerResponseError,
    TrackerPermissionError,
)
from .base import BaseTracker
from .utils import (
    async_retry,
    GITHUB_DEFAULT_PAGE_SIZE,
    HTTP_STATUS_OK,
    HTTP_STATUS_CREATED,
    HTTP_STATUS_NO_CONTENT,
    HTTP_STATUS_UNAUTHORIZED,
    HTTP_STATUS_NOT_FOUND,
    HTTP_STATUS_UNPROCESSABLE_ENTITY,
)
from ..config import logger
from preloop.models.models.project import Project
from preloop.models.crud import crud_organization, crud_project, crud_webhook


def _github_link_has_next(link_header: Optional[str]) -> bool:
    """Return True when a GitHub Link header includes rel=next."""
    if not link_header:
        return False
    for part in link_header.split(","):
        if 'rel="next"' in part or "rel=next" in part or "rel='next'" in part:
            return True
    return False


class GitHubTracker(BaseTracker):
    """GitHub tracker implementation.

    Supports two authentication types:
    - api_token: Traditional PAT authentication (self-hosted or personal use)
    - github_app: GitHub App OAuth authentication (Preloop SaaS)
    """

    tracker_type: str = "github"
    API_BASE_URL = "https://api.github.com"

    def __init__(
        self,
        tracker_id: str,
        api_key: str,
        connection_details: Dict[str, Any],
        auth_type: str = "api_token",
        github_installation_id: Optional[int] = None,
    ):
        """
        Initialize the GitHub tracker.

        Args:
            tracker_id: The tracker ID
            api_key: The API key (PAT token for api_token auth, or None for github_app)
            connection_details: Connection configuration
            auth_type: Authentication type - "api_token" or "github_app"
            github_installation_id: GitHub App installation ID (required for github_app auth)
        """
        super().__init__(tracker_id, api_key, connection_details)
        self.auth_type = auth_type
        self.github_installation_id = github_installation_id
        self._installation_token: Optional[str] = None
        self._installation_token_expires_at: Optional[datetime] = None

        # Set up headers based on auth type
        if auth_type == "api_token":
            self.headers = {
                "Authorization": f"token {api_key}",
                "Accept": "application/vnd.github.v3+json",
            }
        else:
            # For github_app, headers will be set dynamically with installation token
            self.headers = {
                "Accept": "application/vnd.github.v3+json",
            }

    async def _get_installation_token(self) -> str:
        """Get a valid installation access token for GitHub App auth.

        Returns:
            Installation access token

        Raises:
            TrackerAuthenticationError: If unable to obtain token
        """
        if self.auth_type not in ("github_app", "oauth_app"):
            raise TrackerAuthenticationError(
                "Installation token only available for github_app or oauth_app auth types"
            )

        if not self.github_installation_id:
            raise TrackerAuthenticationError(
                "GitHub installation ID not configured for this tracker"
            )

        # Check if we have a valid cached token
        from datetime import timedelta, timezone

        now = datetime.now(timezone.utc)
        if (
            self._installation_token
            and self._installation_token_expires_at
            and self._installation_token_expires_at > now
        ):
            return self._installation_token

        # Get a new installation access token
        try:
            # Import from the plugin (EE only)
            from preloop.plugins.proprietary.github_app.service import (
                get_github_app_service,
            )

            service = get_github_app_service()
            token = await service.get_installation_access_token(
                self.github_installation_id
            )

            # Cache the token (GitHub installation tokens are valid for 1 hour)
            # We'll expire it slightly early (55 minutes) to be safe
            self._installation_token = token
            self._installation_token_expires_at = now + timedelta(minutes=55)

            return token
        except Exception as e:
            logger.error(f"Failed to get installation access token: {e}")
            raise TrackerAuthenticationError(
                f"Failed to get GitHub App installation token: {e}"
            )

    async def _get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests.

        Returns:
            Headers dict with appropriate authorization
        """
        if self.auth_type == "api_token":
            return self.headers
        else:
            # Get fresh installation token
            token = await self._get_installation_token()
            return {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }

    @async_retry()
    async def _make_request(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        Make a request to the GitHub API, handling pagination.
        """
        if params is None:
            params = {}
        params.setdefault("per_page", GITHUB_DEFAULT_PAGE_SIZE)
        results = []
        url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"
        # Get auth headers (may refresh installation token for github_app auth)
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient() as client:
            while url:
                try:
                    response = await client.get(url, headers=headers, params=params)
                    params = None

                    if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                        raise TrackerAuthenticationError("GitHub authentication failed")
                    elif response.status_code >= 400:
                        raise TrackerResponseError(
                            f"GitHub API error: {response.status_code} - {response.text}"
                        )

                    data = response.json()
                    if isinstance(data, list):
                        results.extend(data)
                    else:
                        return data

                    if "next" in response.links:
                        url = response.links["next"]["url"]
                    else:
                        url = None
                except httpx.RequestError as e:
                    raise TrackerConnectionError(f"GitHub connection error: {str(e)}")
        return results

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a request to the GitHub API and return the JSON body."""
        body, _headers = await self._request_with_headers(
            method, endpoint, data, params
        )
        return body

    async def _request_with_headers(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        """GET/POST helper that also returns response headers (for Link pagination)."""
        url = (
            f"{self.API_BASE_URL}{endpoint}"
            if endpoint.startswith("/")
            else f"{self.API_BASE_URL}/{endpoint}"
        )
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=data,
                    params=params,
                )
                if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                    raise TrackerAuthenticationError("GitHub authentication failed")
                elif response.status_code >= 400:
                    denied = False
                    if (
                        response.status_code == 403
                        and response.headers.get("x-ratelimit-remaining") != "0"
                        and not response.headers.get("retry-after")
                    ):
                        try:
                            message = (
                                response.json().get("message", "").rstrip(".").lower()
                            )
                            denied = message in {
                                "resource not accessible by integration",
                                "resource not accessible by personal access token",
                                "must have admin rights to repository",
                            }
                        except (ValueError, AttributeError):
                            # Malformed responses do not establish permission
                            # denial; retain the generic tracker response error.
                            pass
                    error_type = (
                        TrackerPermissionError if denied else TrackerResponseError
                    )
                    raise error_type(
                        f"GitHub API error: {response.status_code} - {response.text}"
                    )
                return response.json(), dict(response.headers)
            except httpx.RequestError as e:
                raise TrackerConnectionError(f"GitHub connection error: {str(e)}")

    def _parse_github_issue(self, issue_data: Dict[str, Any]) -> Issue:
        """Parse a GitHub issue into our standard format.

        Args:
            issue_data: Raw GitHub issue data.

        Returns:
            Standardized issue.
        """
        owner = self.connection_details.get("owner", "")
        repo = self.connection_details.get("repo", "")

        # Parse assignee
        assignee = None
        if issue_data.get("assignee"):
            assignee = IssueUser(
                id=str(issue_data["assignee"]["id"]),
                name=issue_data["assignee"]["login"],
                email=None,
                avatar_url=issue_data["assignee"]["avatar_url"],
            )

        # Parse reporter
        reporter = None
        if issue_data.get("user"):
            reporter = IssueUser(
                id=str(issue_data["user"]["id"]),
                name=issue_data["user"]["login"],
                email=None,
                avatar_url=issue_data["user"]["avatar_url"],
            )

        # Parse status
        status_id = "closed" if issue_data["state"] == "closed" else "open"
        status_name = "Closed" if issue_data["state"] == "closed" else "Open"
        status_category = "done" if issue_data["state"] == "closed" else "todo"

        status = IssueStatus(
            id=status_id,
            name=status_name,
            category=status_category,
        )

        # Parse labels
        labels = [label["name"] for label in issue_data.get("labels", [])]

        # Parse priority from labels
        priority = None
        priority_map = {
            "priority:high": IssuePriority(id="high", name="High", level=3),
            "priority:medium": IssuePriority(id="medium", name="Medium", level=2),
            "priority:low": IssuePriority(id="low", name="Low", level=1),
        }

        for label in labels:
            if label in priority_map:
                priority = priority_map[label]
                break

        # Parse dates
        created_at = datetime.fromisoformat(
            issue_data["created_at"].replace("Z", "+00:00")
        )
        updated_at = datetime.fromisoformat(
            issue_data["updated_at"].replace("Z", "+00:00")
        )
        resolved_at = None
        if issue_data.get("closed_at"):
            resolved_at = datetime.fromisoformat(
                issue_data["closed_at"].replace("Z", "+00:00")
            )

        # Create issue key
        issue_key = (
            f"{owner}/{repo}#{issue_data['number']}"
            if repo
            else f"{owner}#{issue_data['number']}"
        )

        return Issue(
            id=str(issue_data["id"]),
            key=issue_key,
            title=issue_data["title"],
            description=issue_data.get("body") or "",
            status=status,
            priority=priority,
            created_at=created_at,
            updated_at=updated_at,
            resolved_at=resolved_at,
            reporter=reporter,
            assignee=assignee,
            labels=labels,
            components=[],
            parent=None,
            relations=[],
            comments=[],
            url=issue_data["html_url"],
            api_url=issue_data["url"],
            tracker_type="github",
            project_key=f"{owner}/{repo}" if repo else owner,
            custom_fields={},
        )

    @async_retry()
    async def _make_request_delete(self, endpoint: str) -> bool:
        """
        Make a DELETE request to the GitHub API.
        """
        # Get auth headers (may refresh installation token for github_app auth)
        headers = await self._get_auth_headers()
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"
                response = await client.delete(url, headers=headers)

                if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                    raise TrackerAuthenticationError("GitHub authentication failed")
                elif response.status_code == HTTP_STATUS_NOT_FOUND:
                    logger.warning(
                        f"Resource not found during DELETE request to {endpoint}"
                    )
                    return True
                elif response.status_code >= 400:
                    raise TrackerResponseError(
                        f"GitHub API error: {response.status_code} - {response.text}"
                    )

                return response.status_code == HTTP_STATUS_NO_CONTENT
            except httpx.RequestError as e:
                raise TrackerConnectionError(f"GitHub connection error: {str(e)}")

    async def _parse_dependencies(
        self, content: str, current_repo: str
    ) -> List[Dict[str, str]]:
        """Parse dependencies from text content (issue body, comments)."""
        dependencies = []
        import re

        # Regex to find keywords like 'closes', 'fixes', 'relates to', etc.,
        # followed by an issue reference.
        # It supports cross-repo references like 'owner/repo#123'.
        pattern = re.compile(
            r"(closes|fixes|resolves|relates to|blocked by|blocks)\s+((?:[a-zA-Z0-9-]+\/[a-zA-Z0-9_.-]+)?#\d+)",
            re.IGNORECASE,
        )

        for match in pattern.finditer(content):
            relationship_type = match.group(1).lower()
            target_issue_ref = match.group(2)

            # Normalize relationship type for consistency
            if relationship_type in ["closes", "fixes", "resolves"]:
                relationship_type = "closes"
            elif relationship_type == "relates to":
                relationship_type = "related"
            elif relationship_type == "blocked by":
                relationship_type = "is blocked by"

            # Construct the full key for the target issue
            if "#" in target_issue_ref and "/" not in target_issue_ref:
                # It's a short reference like '#123', so it's in the same repo.
                target_key = f"{current_repo}{target_issue_ref}"
            else:
                # It's a full reference like 'owner/repo#123'.
                target_key = target_issue_ref

            dependencies.append(
                {
                    "target_key": target_key,
                    "type": relationship_type,
                }
            )

        return dependencies

    async def test_connection(self) -> TrackerConnection:
        """Test the connection to the tracker.

        For api_token auth, tests using the /user endpoint.
        For github_app auth, tests by getting an installation token and
        checking the installation's accessible repositories.
        """
        try:
            if self.auth_type in ("github_app", "oauth_app"):
                # For GitHub App auth, test by getting an installation token
                # This validates both the app configuration and the installation
                token = await self._get_installation_token()
                if not token:
                    return TrackerConnection(
                        connected=False,
                        message="Failed to obtain GitHub App installation token",
                    )
                # Optionally verify by listing repos (installation tokens can access this)
                headers = await self._get_auth_headers()
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"{self.API_BASE_URL}/installation/repositories",
                        headers=headers,
                        params={"per_page": 1},
                    )
                    if response.status_code == HTTP_STATUS_OK:
                        return TrackerConnection(
                            connected=True, message="GitHub App connection successful"
                        )
                    else:
                        return TrackerConnection(
                            connected=False,
                            message=f"GitHub App connection failed: {response.status_code}",
                        )
            else:
                # For api_token auth, use the /user endpoint
                await self._make_request("user")
                return TrackerConnection(
                    connected=True, message="Connection successful"
                )
        except (
            TrackerAuthenticationError,
            TrackerConnectionError,
            TrackerResponseError,
            TrackerPermissionError,
        ) as e:
            return TrackerConnection(connected=False, message=str(e))

    async def validate_token_permissions(
        self, org_identifier: str | None = None
    ) -> dict:
        """
        Validate that the GitHub token has the required permissions.

        This checks:
        1. Token scopes from the X-OAuth-Scopes header
        2. If org_identifier is provided, checks if user is an admin of that org

        Args:
            org_identifier: Optional organization name to check admin access for.

        Returns:
            Dict with 'valid' (bool), 'scopes' (list), 'warnings' (list), and 'errors' (list)
        """
        result = {
            "valid": True,
            "scopes": [],
            "warnings": [],
            "errors": [],
            "is_org_admin": None,
        }

        try:
            # Get auth headers (may refresh installation token for github_app auth)
            headers = await self._get_auth_headers()
            # Make a request to /user to get the token scopes from response headers
            async with httpx.AsyncClient() as client:
                url = f"{self.API_BASE_URL}/user"
                response = await client.get(url, headers=headers)

                if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                    result["valid"] = False
                    result["errors"].append("Invalid or expired GitHub token")
                    return result

                # Handle other error responses (403, 404, 500, etc.)
                if response.status_code >= 400:
                    result["valid"] = False
                    result["errors"].append(
                        f"GitHub API error: {response.status_code} - {response.text[:200]}"
                    )
                    return result

                # Extract scopes from X-OAuth-Scopes header
                oauth_scopes_header = response.headers.get("X-OAuth-Scopes", "")
                scopes = [
                    s.strip() for s in oauth_scopes_header.split(",") if s.strip()
                ]
                result["scopes"] = scopes

                # Check for required scopes for webhook registration
                # admin:org_hook or admin:org includes org hook permissions
                has_org_hook_scope = any(
                    scope in scopes
                    for scope in ["admin:org_hook", "admin:org", "write:org_hook"]
                )

                if not has_org_hook_scope:
                    result["warnings"].append(
                        "Token is missing 'admin:org_hook' scope. "
                        "Webhook registration for organizations will fail. "
                        "Please regenerate the token with 'admin:org_hook' or 'admin:org' scope."
                    )

                # Check for repo scope (needed for reading repositories)
                has_repo_scope = any(
                    scope in scopes for scope in ["repo", "public_repo"]
                )
                if not has_repo_scope:
                    result["warnings"].append(
                        "Token is missing 'repo' scope. "
                        "Access to private repositories will be limited."
                    )

                # If org_identifier provided, check if user is admin of that org
                if org_identifier and org_identifier != "personal":
                    try:
                        # Get user's membership in the organization
                        user_data = response.json()
                        username = user_data.get("login")

                        # The org_identifier might be a numeric ID (from transform_organization)
                        # or a login/slug. The membership API requires the login, so we need
                        # to resolve numeric IDs to logins first.
                        org_login = org_identifier
                        org_lookup_failed = False
                        if org_identifier.isdigit():
                            # Numeric ID - need to look up the org login
                            org_lookup_url = (
                                f"{self.API_BASE_URL}/organizations/{org_identifier}"
                            )
                            org_lookup_response = await client.get(
                                org_lookup_url, headers=headers
                            )
                            if org_lookup_response.status_code == 200:
                                org_data = org_lookup_response.json()
                                org_login = org_data.get("login", org_identifier)
                            else:
                                # Can't resolve org ID - skip membership check to avoid
                                # misleading "not a member" errors when we just can't
                                # resolve the numeric ID to a slug
                                org_lookup_failed = True
                                logger.warning(
                                    f"Could not resolve org ID {org_identifier} to login: "
                                    f"{org_lookup_response.status_code}"
                                )
                                result["is_org_admin"] = None
                                result["warnings"].append(
                                    f"Could not verify admin status for organization ID '{org_identifier}'. "
                                    "The token may be missing 'read:org' scope or the organization "
                                    "may not be accessible. Webhook registration may fail if you "
                                    "are not an organization admin."
                                )

                        # Only proceed with membership check if we resolved the org
                        if not org_lookup_failed:
                            membership_url = f"{self.API_BASE_URL}/orgs/{org_login}/memberships/{username}"
                            membership_response = await client.get(
                                membership_url, headers=headers
                            )

                            if membership_response.status_code == 200:
                                membership_data = membership_response.json()
                                role = membership_data.get("role")
                                state = membership_data.get("state")

                                if state != "active":
                                    result["is_org_admin"] = False
                                    result["warnings"].append(
                                        f"Your membership in organization '{org_login}' is pending. "
                                        "Webhook registration may fail until membership is active."
                                    )
                                elif role == "admin":
                                    result["is_org_admin"] = True
                                else:
                                    result["is_org_admin"] = False
                                    result["warnings"].append(
                                        f"You are not an admin of organization '{org_login}'. "
                                        "Webhook registration requires organization admin access. "
                                        "Contact an organization owner to register webhooks, or use a Fine-Grained Token with webhook permissions."
                                    )
                            elif membership_response.status_code == 404:
                                result["is_org_admin"] = False
                                result["warnings"].append(
                                    f"You are not a member of organization '{org_login}'. "
                                    "Webhook registration will fail."
                                )
                            elif membership_response.status_code == 403:
                                # User doesn't have permission to view membership
                                # This can happen with Fine-Grained tokens
                                result["is_org_admin"] = None
                                result["warnings"].append(
                                    f"Cannot verify admin status for organization '{org_login}'. "
                                    "If using a Fine-Grained Personal Access Token, ensure it has "
                                    "'Organization webhooks' write permission."
                                )
                    except Exception as e:
                        logger.warning(
                            f"Error checking org membership for {org_identifier}: {e}"
                        )
                        result["is_org_admin"] = None

        except Exception as e:
            result["valid"] = False
            result["errors"].append(f"Failed to validate token: {str(e)}")

        return result

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Get metadata about a GitHub project.

        Args:
            project_key: Project key (owner/repo format).

        Returns:
            Project metadata.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        repo_full_name = f"{owner}/{repo}"

        # Get repository details
        repo_data = await self._make_request(f"repos/{repo_full_name}")

        # GitHub has simple status model: open/closed
        statuses = [
            IssueStatus(id="open", name="Open", category="todo"),
            IssueStatus(id="closed", name="Closed", category="done"),
        ]

        # GitHub doesn't have built-in priorities, but commonly uses labels
        priorities = [
            IssuePriority(id="high", name="priority:high", level=3),
            IssuePriority(id="medium", name="priority:medium", level=2),
            IssuePriority(id="low", name="priority:low", level=1),
        ]

        return ProjectMetadata(
            key=repo_full_name,
            name=repo_data.get("name", repo),
            description=repo_data.get("description"),
            statuses=statuses,
            priorities=priorities,
            url=repo_data.get("html_url"),
        )

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Search for issues in a GitHub repository.

        Args:
            project_key: Project key (ignored for GitHub).
            filter_params: Filter parameters.
            limit: Maximum number of issues to return.
            offset: Pagination offset.

        Returns:
            Tuple of (list of issues, total count).
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        # Build the search query
        query_parts = []

        if repo:
            query_parts.append(f"repo:{owner}/{repo}")
        else:
            query_parts.append(f"user:{owner}")

        if filter_params.query:
            query_parts.append(filter_params.query)

        if filter_params.status:
            for status in filter_params.status:
                if status.lower() == "open" or status.lower() == "closed":
                    query_parts.append(f"is:{status.lower()}")

        if filter_params.labels:
            for label in filter_params.labels:
                query_parts.append(f'label:"{label}"')

        if filter_params.created_after:
            date_str = filter_params.created_after.strftime("%Y-%m-%d")
            query_parts.append(f"created:>={date_str}")

        if filter_params.created_before:
            date_str = filter_params.created_before.strftime("%Y-%m-%d")
            query_parts.append(f"created:<={date_str}")

        if filter_params.updated_after:
            date_str = filter_params.updated_after.strftime("%Y-%m-%d")
            query_parts.append(f"updated:>={date_str}")

        if filter_params.updated_before:
            date_str = filter_params.updated_before.strftime("%Y-%m-%d")
            query_parts.append(f"updated:<={date_str}")

        if filter_params.assigned_to:
            query_parts.append(f"assignee:{filter_params.assigned_to}")

        if filter_params.reported_by:
            query_parts.append(f"author:{filter_params.reported_by}")

        # Build the final query
        query = " ".join(query_parts)

        # Determine sort options
        sort_field = "updated"
        if filter_params.sort_by:
            if filter_params.sort_by in ["created", "updated", "comments"]:
                sort_field = filter_params.sort_by

        sort_direction = "desc"
        if filter_params.sort_direction and filter_params.sort_direction.lower() in [
            "asc",
            "desc",
        ]:
            sort_direction = filter_params.sort_direction.lower()

        # Calculate page number (GitHub uses 1-based pagination)
        page = (offset // limit) + 1

        # Make the search request
        search_path = "/search/issues"
        params = {
            "q": query,
            "sort": sort_field,
            "order": sort_direction,
            "per_page": limit,
            "page": page,
        }

        search_data = await self._request("GET", search_path, params=params)

        # Parse the issues
        issues = []
        for issue_data in search_data["items"]:
            issues.append(self._parse_github_issue(issue_data))

        return issues, search_data["total_count"]

    async def get_issue(self, issue_id: str) -> Issue:
        """Get a specific issue by ID.

        Args:
            issue_id: Issue number in the repository.

        Returns:
            Issue object.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        repo_full_name = f"{owner}/{repo}"
        issue_data = await self._make_request(
            f"repos/{repo_full_name}/issues/{issue_id}"
        )

        if "pull_request" in issue_data:
            raise TrackerResponseError(
                f"Issue {issue_id} is a pull request, not an issue"
            )

        # Use the mapper to convert to Issue object
        return self._parse_github_issue(issue_data)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Get comments for an issue."""
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        repo_full_name = f"{owner}/{repo}"
        comments_endpoint = f"repos/{repo_full_name}/issues/{issue_id}/comments"

        try:
            raw_comments_data = await self._make_request(
                comments_endpoint, params={"per_page": GITHUB_DEFAULT_PAGE_SIZE}
            )
            if isinstance(raw_comments_data, dict):
                raw_comments_data = [raw_comments_data]

            comments_data_transformed = []
            for comment_item in raw_comments_data:
                try:
                    comment_created_at = datetime.strptime(
                        comment_item["created_at"], "%Y-%m-%dT%H:%M:%SZ"
                    )
                    comment_updated_at = datetime.strptime(
                        comment_item["updated_at"], "%Y-%m-%dT%H:%M:%SZ"
                    )
                except (ValueError, TypeError):
                    comment_created_at = datetime.now()
                    comment_updated_at = datetime.now()

                comments_data_transformed.append(
                    IssueComment(
                        id=str(comment_item["id"]),
                        body=comment_item.get("body", "") or "",
                        author=IssueUser(
                            id=str(comment_item["user"]["id"]),
                            name=comment_item["user"]["login"],
                            avatar_url=comment_item["user"]["avatar_url"],
                        ),
                        created_at=comment_created_at,
                        updated_at=comment_updated_at,
                        url=comment_item.get("html_url"),
                    )
                )

            return comments_data_transformed
        except TrackerResponseError as e:
            logger.error(
                f"Failed to get comments for issue {repo_full_name}#{issue_id}: {e}"
            )
            return []

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Create a new GitHub issue.

        Args:
            project_key: Project key (ignored for GitHub).
            issue_data: Issue data.

        Returns:
            Created issue.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Build the request body
        body = {
            "title": issue_data.title,
            "body": issue_data.description or "",
        }

        # Set assignee if provided
        if issue_data.assignee:
            body["assignee"] = issue_data.assignee

        # Set labels if provided
        if issue_data.labels:
            body["labels"] = issue_data.labels

        # Create the issue
        issues_path = f"/repos/{owner}/{repo}/issues"
        created_issue_data = await self._request("POST", issues_path, data=body)

        # Parse and return the issue
        return self._parse_github_issue(created_issue_data)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Update an existing GitHub issue.

        Args:
            issue_id: Issue number in the repository.
            issue_data: Updated issue data.

        Returns:
            Updated issue.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Issue ID might be in various formats, so we extract just the number
        issue_number = issue_id
        if "/" in issue_id:
            parts = issue_id.split("/")
            issue_number = parts[-1]
        if "#" in issue_number:
            issue_number = issue_number.split("#")[-1]

        # Build the request body
        body = {}

        if issue_data.title is not None:
            body["title"] = issue_data.title

        if issue_data.description is not None:
            body["body"] = issue_data.description

        if issue_data.status is not None:
            body["state"] = issue_data.status.lower()

        if issue_data.assignee is not None:
            body["assignee"] = issue_data.assignee

        if issue_data.labels is not None:
            body["labels"] = issue_data.labels

        # Update the issue
        issue_path = f"/repos/{owner}/{repo}/issues/{issue_number}"
        updated_issue_data = await self._request("PATCH", issue_path, data=body)

        # Parse and return the issue
        return self._parse_github_issue(updated_issue_data)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Add a comment to a GitHub issue.

        Args:
            issue_id: Issue number in the repository.
            comment: Comment text.

        Returns:
            Created comment.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Issue ID might be in various formats, so we extract just the number
        issue_number = issue_id
        if "/" in issue_id:
            parts = issue_id.split("/")
            issue_number = parts[-1]
        if "#" in issue_number:
            issue_number = issue_number.split("#")[-1]

        # Build the request body
        body = {
            "body": comment,
        }

        # Add the comment
        comments_path = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        comment_data = await self._request("POST", comments_path, data=body)

        # Parse and return the comment
        return IssueComment(
            id=str(comment_data["id"]),
            body=comment_data["body"],
            created_at=datetime.fromisoformat(
                comment_data["created_at"].replace("Z", "+00:00")
            ),
            updated_at=datetime.fromisoformat(
                comment_data["updated_at"].replace("Z", "+00:00")
            ),
            author=IssueUser(
                id=str(comment_data["user"]["id"]),
                name=comment_data["user"]["login"],
                email=None,
                avatar_url=comment_data["user"]["avatar_url"],
            ),
            url=comment_data.get("html_url"),
        )

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Add a relation between GitHub issues.

        Since GitHub doesn't have a built-in way to relate issues beyond
        mentioning them in comments or body, this method adds a comment
        to the issue referencing the related issue.

        Args:
            issue_id: Source issue number.
            related_issue_id: Target issue number.
            relation_type: Relation type.

        Returns:
            Whether the operation was successful.
        """
        # Issue IDs might be in various formats, so we extract just the numbers
        issue_number = issue_id
        if "/" in issue_id:
            parts = issue_id.split("/")
            issue_number = parts[-1]
        if "#" in issue_number:
            issue_number = issue_number.split("#")[-1]

        related_issue_number = related_issue_id
        if "/" in related_issue_id:
            parts = related_issue_id.split("/")
            related_issue_number = parts[-1]
        if "#" in related_issue_number:
            related_issue_number = related_issue_number.split("#")[-1]

        # Format the relation as a comment
        comment = f"This issue {relation_type} #{related_issue_number}"

        # Add the comment
        try:
            await self.add_comment(issue_number, comment)
            return True
        except Exception as e:
            logger.exception(f"Failed to add relation: {e}")
            return False

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """
        Get organizations from GitHub.

        For api_token auth: Uses /user and /user/orgs endpoints.
        For github_app auth: Uses /installation/repositories to derive orgs from accessible repos.
        """
        organizations = []

        if self.auth_type in ("github_app", "oauth_app"):
            # For GitHub App auth, get organizations from installation repositories
            # The installation has access to specific repos, we derive orgs from those
            headers = await self._get_auth_headers()
            async with httpx.AsyncClient() as client:
                url = f"{self.API_BASE_URL}/installation/repositories"
                params = {"per_page": GITHUB_DEFAULT_PAGE_SIZE}
                seen_orgs = set()

                while url:
                    response = await client.get(url, headers=headers, params=params)
                    params = None  # Only use params on first request

                    if response.status_code != HTTP_STATUS_OK:
                        raise TrackerResponseError(
                            f"GitHub API error: {response.status_code} - {response.text}"
                        )

                    data = response.json()
                    repos = data.get("repositories", [])

                    for repo in repos:
                        owner = repo.get("owner", {})
                        owner_id = str(owner.get("id", ""))
                        owner_login = owner.get("login", "")
                        owner_type = owner.get("type", "User")

                        if owner_id and owner_id not in seen_orgs:
                            seen_orgs.add(owner_id)
                            organizations.append(
                                {
                                    "id": owner_id,
                                    "name": owner_login,
                                    "url": owner.get(
                                        "html_url", f"https://github.com/{owner_login}"
                                    ),
                                    "type": owner_type,
                                }
                            )

                    # Handle pagination
                    if "next" in response.links:
                        url = response.links["next"]["url"]
                    else:
                        url = None

            return organizations
        else:
            # For api_token auth, use user endpoints
            user_data = await self._make_request("user")
            organizations.append(
                {
                    "id": "personal",
                    "name": f"{user_data['login']}",
                    "url": user_data["html_url"],
                }
            )
            orgs_data = await self._make_request(
                "user/orgs", {"per_page": GITHUB_DEFAULT_PAGE_SIZE}
            )
            for org in orgs_data:
                organizations.append(
                    {
                        "id": str(org["id"]),
                        "name": org["login"],
                        "url": org["url"]
                        .replace("api.github.com", "github.com")
                        .replace("/orgs/", "/"),
                    }
                )
            return organizations

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """
        Get repositories (projects) for an organization from GitHub.

        For api_token auth: Uses /user/repos or /orgs/{org}/repos endpoints.
        For github_app auth: Uses /installation/repositories and filters by owner.
        """
        projects = []

        if self.auth_type in ("github_app", "oauth_app"):
            # For GitHub App auth, get repos from installation and filter by org
            headers = await self._get_auth_headers()
            async with httpx.AsyncClient() as client:
                url = f"{self.API_BASE_URL}/installation/repositories"
                params = {"per_page": GITHUB_DEFAULT_PAGE_SIZE}

                while url:
                    response = await client.get(url, headers=headers, params=params)
                    params = None  # Only use params on first request

                    if response.status_code != HTTP_STATUS_OK:
                        raise TrackerResponseError(
                            f"GitHub API error: {response.status_code} - {response.text}"
                        )

                    data = response.json()
                    repos = data.get("repositories", [])

                    for repo in repos:
                        owner = repo.get("owner", {})
                        owner_id = str(owner.get("id", ""))

                        # Filter by organization_id
                        if owner_id == organization_id:
                            projects.append(
                                {
                                    "id": str(repo["id"]),
                                    "identifier": str(repo["id"]),
                                    "name": repo["name"],
                                    "description": repo.get("description") or "",
                                    "url": repo["html_url"],
                                    "meta_data": {
                                        "full_name": repo["full_name"],
                                        "default_branch": repo.get("default_branch"),
                                        "language": repo.get("language"),
                                        "created_at": repo.get("created_at"),
                                        "updated_at": repo.get("pushed_at"),
                                        "stars": repo.get("stargazers_count", 0),
                                    },
                                }
                            )

                    # Handle pagination
                    if "next" in response.links:
                        url = response.links["next"]["url"]
                    else:
                        url = None

            return projects
        else:
            # For api_token auth, use user/org endpoints
            params = {
                "per_page": GITHUB_DEFAULT_PAGE_SIZE,
                "sort": "updated",
                "direction": "desc",
            }
            if organization_id == "personal":
                repos_data = await self._make_request("user/repos", params)
            else:
                repos_data = await self._make_request(
                    f"orgs/{organization_id}/repos", params
                )
            for repo in repos_data:
                projects.append(
                    {
                        "id": str(repo["id"]),
                        "identifier": str(repo["id"]),
                        "name": repo["name"],
                        "description": repo["description"] or "",
                        "url": repo["html_url"],
                        "meta_data": {
                            "full_name": repo["full_name"],
                            "default_branch": repo["default_branch"],
                            "language": repo.get("language"),
                            "created_at": repo["created_at"],
                            "updated_at": repo["pushed_at"],
                            "stars": repo["stargazers_count"],
                        },
                    }
                )
            return projects

    async def get_issues(
        self,
        organization_id: str,
        project_id: str,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get issues for a repository from GitHub.
        """
        if "/" in project_id:
            repo_name = project_id
        else:
            try:
                repo_details = await self._make_request(f"repositories/{project_id}")
                repo_name = repo_details["full_name"]
            except TrackerResponseError as e:
                logger.error(
                    f"Failed to get repository details for project_id {project_id}: {e}"
                )
                return []

        params = {
            "state": "all",
            "per_page": GITHUB_DEFAULT_PAGE_SIZE,
            "sort": "updated",
            "direction": "desc",
        }
        if since:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        issues_endpoint = f"repos/{repo_name}/issues"
        try:
            raw_issues_data = await self._make_request(issues_endpoint, params)
        except TrackerResponseError as e:
            logger.error(f"Failed to get issues for repo {repo_name}: {e}")
            return []

        processed_issues = []
        for issue_data in raw_issues_data:
            if "pull_request" in issue_data:
                continue

            issue_number = issue_data["number"]
            comments_data_transformed = []
            comments_endpoint = f"repos/{repo_name}/issues/{issue_number}/comments"
            try:
                raw_comments_data = await self._make_request(
                    comments_endpoint, params={"per_page": GITHUB_DEFAULT_PAGE_SIZE}
                )
                if isinstance(raw_comments_data, dict):
                    raw_comments_data = [raw_comments_data]
                for comment_item in raw_comments_data:
                    # Store as dictionary for transform_comment compatibility
                    comments_data_transformed.append(
                        {
                            "id": comment_item["id"],
                            "body": comment_item.get("body", "") or "",
                            "user": comment_item["user"],
                            "created_at": comment_item["created_at"],
                            "updated_at": comment_item["updated_at"],
                            "html_url": comment_item.get("html_url"),
                        }
                    )
            except TrackerResponseError as e:
                logger.error(
                    f"Failed to get comments for issue {repo_name}#{issue_number}: {e}"
                )

            issue_data["comments"] = comments_data_transformed

            # Parse dependencies from issue body and comments
            dependencies = []
            if issue_data.get("body"):
                dependencies.extend(
                    await self._parse_dependencies(issue_data["body"], repo_name)
                )
            for comment in comments_data_transformed:
                if comment.get("body"):
                    dependencies.extend(
                        await self._parse_dependencies(comment["body"], repo_name)
                    )
            issue_data["dependencies"] = dependencies

            processed_issues.append(issue_data)
        return processed_issues

    async def register_webhook(
        self,
        db: Optional[Session] = None,
        organization: Optional[Organization] = None,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
    ) -> bool:
        """
        Register a webhook for the given GitHub organization.
        """
        if db is None or organization is None or not webhook_url or not secret:
            logger.error(
                "GitHub webhook registration requires db, organization, URL, and secret."
            )
            return False

        org_identifier = organization.identifier
        if org_identifier == "personal":
            logger.info(
                f"Skipping webhook registration for personal account '{self.connection_details.get('login', 'N/A')}'."
            )
            return True

        endpoint = f"orgs/{org_identifier}/hooks"
        events = [
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "discussion",
            "project",
            "repository",
            "push",
            "check_run",
            "check_suite",
            "workflow_run",
        ]
        payload = {
            "name": "web",
            "active": True,
            "events": events,
            "config": {
                "url": webhook_url,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        }

        try:
            # Get auth headers (may refresh installation token for github_app auth)
            headers = await self._get_auth_headers()
            async with httpx.AsyncClient() as client:
                url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"
                response = await client.post(url, headers=headers, json=payload)

            if response.status_code in [HTTP_STATUS_OK, HTTP_STATUS_CREATED]:
                response_data = response.json()
                webhook_id = response_data.get("id")
                if not webhook_id:
                    logger.error(
                        f"Successfully registered webhook for org '{org_identifier}' but could not get webhook ID from response."
                    )
                    return False

                crud_webhook.create(
                    db,
                    obj_in={
                        "organization_id": organization.id,
                        "external_id": str(webhook_id),
                        "url": webhook_url,
                        "secret": secret,
                        "events": events,
                    },
                )
                logger.info(
                    f"Successfully registered and stored webhook {webhook_id} for GitHub org '{org_identifier}'"
                )
                return True
            elif response.status_code == HTTP_STATUS_UNAUTHORIZED:
                logger.error(
                    f"GitHub authentication failed while trying to register webhook for org '{org_identifier}'."
                )
                return False
            elif response.status_code == 403:
                logger.error(
                    f"Permission denied: Unable to register webhook for GitHub org '{org_identifier}'. Check token permissions (needs admin:org_hook)."
                )
                return False
            elif response.status_code == HTTP_STATUS_NOT_FOUND:
                logger.error(
                    f"GitHub organization '{org_identifier}' not found while trying to register webhook."
                )
                return False
            elif response.status_code == HTTP_STATUS_UNPROCESSABLE_ENTITY:
                response_data = response.json()
                if "errors" in response_data and any(
                    "Hook already exists" in e.get("message", "")
                    for e in response_data["errors"]
                ):
                    logger.warning(
                        f"Webhook for GitHub org '{org_identifier}' pointing to {webhook_url} already exists."
                    )
                    return True
                else:
                    logger.error(
                        f"Failed to register webhook for GitHub org '{org_identifier}' (Unprocessable Entity - check config/permissions): {response.text}"
                    )
                    return False
            else:
                logger.error(
                    f"GitHub API error registering webhook for org '{org_identifier}': {response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(
                f"GitHub connection error while registering webhook for org '{org_identifier}': {e}",
                exc_info=True,
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error registering webhook for GitHub org '{org_identifier}': {e}",
                exc_info=True,
            )
            return False

    async def unregister_webhook(
        self, db: Optional[Session] = None, webhook: Optional[Webhook] = None
    ) -> bool:
        """
        Unregister a webhook for the given GitHub organization.

        Args:
            db: The database session.
            webhook: The webhook to unregister.

        Returns:
            True if unregistration was successful, False otherwise.
        """
        if db is None or webhook is None:
            logger.error("GitHub webhook unregistration requires db and webhook.")
            return False

        org_identifier = None
        if webhook.organization:
            org_identifier = webhook.organization.identifier
        elif webhook.project:
            org_identifier = webhook.project.organization.identifier
        webhook_id = webhook.external_id

        if not org_identifier or org_identifier == "personal":
            logger.info(f"Skipping webhook unregistration for org '{org_identifier}'.")
            return True

        if webhook.project:
            repo_full_name = webhook.project.slug
            endpoint = f"repos/{repo_full_name}/hooks/{webhook_id}"
        else:
            endpoint = f"orgs/{org_identifier}/hooks/{webhook_id}"
        try:
            # Get auth headers (may refresh installation token for github_app auth)
            headers = await self._get_auth_headers()
            async with httpx.AsyncClient() as client:
                url = f"{self.API_BASE_URL}/{endpoint.lstrip('/')}"
                response = await client.delete(url, headers=headers)

            if response.status_code == HTTP_STATUS_NO_CONTENT:
                logger.info(
                    f"Successfully unregistered webhook {webhook_id} for GitHub org '{org_identifier}'."
                )
                crud_webhook.remove(db, id=webhook.id)
                return True
            elif response.status_code == HTTP_STATUS_NOT_FOUND:
                logger.warning(
                    f"Webhook {webhook_id} for GitHub org '{org_identifier}' not found during delete attempt. Assuming already unregistered."
                )
                crud_webhook.remove(db, id=webhook.id)
                return True
            else:
                logger.error(
                    f"Failed to unregister webhook {webhook_id} for GitHub org '{org_identifier}': {response.status_code} - {response.text}"
                )
                return False
        except httpx.RequestError as e:
            logger.error(
                f"GitHub connection error while unregistering webhook for org '{org_identifier}': {e}",
                exc_info=True,
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error unregistering webhook for GitHub org '{org_identifier}': {e}",
                exc_info=True,
            )
            return False

    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Unregister all webhooks for all organizations managed by this tracker instance.
        Args:
            db: The database session.
        """
        results = {"unregistered": 0, "failed": 0, "not_found": 0}
        logger.info(
            f"Starting to unregister all webhooks for tracker {self.tracker_id}."
        )
        try:
            organizations = crud_organization.get_multi(db, tracker_id=self.tracker_id)
            organization_ids = [org.id for org in organizations]
            project_ids = []
            for org_id in organization_ids:
                projects = crud_project.get_for_organization(db, organization_id=org_id)
                project_ids.extend([proj.id for proj in projects])

            webhooks_to_delete = (
                db.query(Webhook)
                .filter(
                    (Webhook.organization_id.in_(organization_ids))
                    | (Webhook.project_id.in_(project_ids))
                )
                .all()
            )

            if not webhooks_to_delete:
                logger.info("No webhooks found in the database for this tracker.")
                return results

            for webhook in webhooks_to_delete:
                if await self.unregister_webhook(db=db, webhook=webhook):
                    results["unregistered"] += 1
                else:
                    results["failed"] += 1

        except Exception as e:
            logger.error(
                f"An unexpected error occurred during webhook unregistration for tracker {self.tracker_id}: {e}",
                exc_info=True,
            )
            results["failed"] += 1
        logger.info(
            f"Finished unregistering all webhooks for tracker {self.tracker_id}."
        )
        logger.info(f"GitHub unregister_all_webhooks summary: {results}")
        return results

    async def cleanup_stale_webhooks(
        self, preloop_url: str, cleanup_projects: bool = False
    ) -> dict:
        """
        Deletes stale webhooks pointing to the given Preloop URL.

        By default, this method cleans up organization-level webhooks.
        If `cleanup_projects` is True, it cleans up repository-level webhooks instead.

        Args:
            preloop_url: The base URL of the Preloop instance.
            cleanup_projects: If True, clean up repository-level webhooks. Defaults to False.

        Returns:
            A dictionary summarizing the actions taken, e.g., `{"unregistered": count, "failed": count}`.
        """
        results = {"unregistered": 0, "failed": 0}
        logger.info(
            f"Starting cleanup of stale webhooks for URL: {preloop_url} (cleanup_projects={cleanup_projects})"
        )

        if cleanup_projects:
            await self._cleanup_project_webhooks(preloop_url, results)
        else:
            await self._cleanup_organization_webhooks(preloop_url, results)

        logger.info(f"Webhook cleanup summary: {results}")
        return results

    async def _cleanup_organization_webhooks(
        self, preloop_url: str, results: dict
    ) -> None:
        """Helper to clean up organization-level webhooks."""
        try:
            organizations = await self.get_organizations()
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(f"Failed to retrieve organizations: {e}")
            return

        for org in organizations:
            org_login = org.get("name")
            if not org_login or org.get("id") == "personal":
                continue

            logger.info(f"Checking webhooks for organization: {org_login}")
            try:
                hooks = await self._make_request(f"orgs/{org_login}/hooks")
            except (TrackerConnectionError, TrackerResponseError) as e:
                logger.error(f"Failed to list webhooks for {org_login}: {e}")
                results["failed"] += 1
                continue

            for hook in hooks:
                await self._process_hook(
                    hook, preloop_url, results, f"orgs/{org_login}/hooks"
                )

    async def _cleanup_project_webhooks(self, preloop_url: str, results: dict) -> None:
        """Helper to clean up repository-level webhooks."""
        try:
            organizations = await self.get_organizations()
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(f"Failed to retrieve organizations: {e}")
            return

        for org in organizations:
            org_id = org.get("id")
            if not org_id:
                continue

            try:
                projects = await self.get_projects(org_id)
            except (TrackerConnectionError, TrackerResponseError) as e:
                logger.error(
                    f"Failed to retrieve projects for org {org.get('name')}: {e}"
                )
                continue

            for repo in projects:
                repo_full_name = repo.get("meta_data", {}).get("full_name")
                if not repo_full_name:
                    continue

                logger.info(f"Checking webhooks for repository: {repo_full_name}")
                try:
                    hooks = await self._make_request(f"repos/{repo_full_name}/hooks")
                except (TrackerConnectionError, TrackerResponseError) as e:
                    logger.error(f"Failed to list webhooks for {repo_full_name}: {e}")
                    results["failed"] += 1
                    continue

                for hook in hooks:
                    await self._process_hook(
                        hook, preloop_url, results, f"repos/{repo_full_name}/hooks"
                    )

    async def _process_hook(
        self, hook: dict, preloop_url: str, results: dict, base_endpoint: str
    ) -> None:
        """
        Processes a single webhook for cleanup.

        Stale webhooks are webhooks that:
        1. Have a URL starting with preloop_url (they point to our Preloop instance)
        2. Are NOT registered in our database (they were created but not tracked, or orphaned)

        This method checks if the webhook is stale and deletes it if so.
        """
        hook_id = hook.get("id")
        hook_config = hook.get("config", {})
        hook_url = hook_config.get("url")

        if not all([hook_id, hook_url]):
            return

        # Only consider webhooks pointing to our Preloop instance
        if not hook_url.startswith(preloop_url):
            # This webhook points to a different service, ignore it
            return

        # Check if this webhook exists in our database
        from preloop.models.crud import crud_webhook
        from preloop.models.db.session import get_db_session

        db = next(get_db_session())
        try:
            # Look up webhook by external_id (the GitHub webhook ID)
            existing_webhook = crud_webhook.get_by_external_id(
                db, external_id=str(hook_id), tracker_id=self.tracker_id
            )

            if existing_webhook:
                # Webhook is in our database, keep it
                logger.debug(
                    f"Webhook {hook_id} in {base_endpoint} is registered in database, keeping it."
                )
                return

            # Webhook points to our Preloop but is NOT in database - it's stale
            logger.info(
                f"Found stale webhook {hook_id} in {base_endpoint} pointing to {hook_url}. "
                f"This webhook is not in our database. Deleting..."
            )
            try:
                delete_endpoint = f"{base_endpoint}/{hook_id}"
                if await self._make_request_delete(delete_endpoint):
                    logger.info(
                        f"Successfully deleted stale webhook {hook_id} from {base_endpoint}."
                    )
                    results["unregistered"] += 1
                else:
                    logger.error(
                        f"Failed to delete webhook {hook_id} from {base_endpoint}."
                    )
                    results["failed"] += 1
            except (TrackerConnectionError, TrackerResponseError) as e:
                logger.error(
                    f"An error occurred while deleting webhook {hook_id} from {base_endpoint}: {e}"
                )
                results["failed"] += 1
        finally:
            db.close()

    async def is_webhook_registered(self, webhook: "Webhook") -> bool:
        """
        Check if a webhook is registered in the tracker.

        Args:
            webhook: The webhook to check.

        Returns:
            Whether the webhook is registered.
        """
        if not webhook.external_id:
            return False

        if not webhook.project:
            return False
        repo_full_name = webhook.project.slug
        endpoint = f"repos/{repo_full_name}/hooks/{webhook.external_id}"
        try:
            await self._make_request(endpoint)
            return True
        except TrackerResponseError as e:
            if "Not Found" in str(e):
                return False
            raise

    async def get_webhooks(
        self, organization_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all webhooks for a specific organization's repositories.
        """
        if not organization_id:
            logger.error("GitHub webhook listing requires an organization identifier.")
            return []

        all_webhooks = []
        repos = await self.get_projects(organization_id)
        for repo in repos:
            repo_full_name = repo["meta_data"]["full_name"]
            try:
                repo_webhooks = await self._make_request(
                    f"repos/{repo_full_name}/hooks",
                    params={"per_page": GITHUB_DEFAULT_PAGE_SIZE},
                )
                all_webhooks.extend(repo_webhooks)
            except TrackerResponseError as e:
                logger.error(f"Failed to get webhooks for repo {repo_full_name}: {e}")
        return all_webhooks

    async def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """
        Delete a webhook from the tracker.

        Args:
            webhook: The webhook to delete.

        Returns:
            Whether the webhook was deleted successfully.
        """
        webhook_id = webhook.get("id")
        if not webhook_id:
            return False

        # The webhook response doesn't contain the org identifier, so we have to parse it from the url
        url = webhook.get("url")
        if not url:
            return False

        org_identifier = url.split("/")[-2]
        endpoint = f"orgs/{org_identifier}/hooks/{webhook_id}"
        try:
            return await self._make_request_delete(endpoint)
        except TrackerResponseError as e:
            logger.error(f"Failed to delete webhook {webhook_id}: {e}")
            return False

    async def is_webhook_registered_for_project(
        self, project: "Project", webhook_url: str
    ) -> bool:
        """
        Check if a webhook is registered for a project.

        Args:
            project: The project to check.
            webhook_url: The URL of the webhook.

        Returns:
            Whether the webhook is registered.
        """
        endpoint = f"repos/{project.slug}/hooks"
        try:
            hooks = await self._make_request(endpoint)
            for hook in hooks:
                if hook.get("config", {}).get("url") == webhook_url:
                    return True
            return False
        except TrackerResponseError:
            return False

    def transform_organization(self, org_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transforms a GitHub organization into the common format."""
        return {
            "identifier": str(org_data["id"]),
            "name": org_data["name"],
            "meta_data": {"source": "github", "url": org_data.get("url")},
        }

    def transform_project(
        self, proj_data: Dict[str, Any], organization_id: str
    ) -> Dict[str, Any]:
        """Transforms a GitHub repository into the common format."""
        return {
            "identifier": str(proj_data["id"]),
            "name": proj_data["name"],
            "description": proj_data.get("description"),
            "organization_id": organization_id,
            "slug": proj_data.get("meta_data", {}).get("full_name", ""),
            "meta_data": proj_data.get("meta_data"),
        }

    def transform_issue(
        self, issue_data: Dict[str, Any], project: "Project"
    ) -> Dict[str, Any]:
        """Transforms a GitHub issue into the common format."""
        return {
            "external_id": str(issue_data["id"]),
            "key": f"{project.slug}#{issue_data['number']}",
            "title": issue_data["title"],
            "description": issue_data.get("body"),
            "status": issue_data["state"],
            "created_at": datetime.strptime(
                issue_data["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ),
            "updated_at": datetime.strptime(
                issue_data["updated_at"], "%Y-%m-%dT%H:%M:%SZ"
            ),
            "project_id": project.id,
            "tracker_id": self.tracker_id,
            "comments": issue_data.get("comments", []),
        }

    def transform_comment(
        self, comment_data: Dict[str, Any], issue_id: str
    ) -> Dict[str, Any]:
        """Transforms a GitHub comment into the common format."""
        return {
            "external_id": str(comment_data["id"]),
            "body": comment_data.get("body"),
            "created_at": datetime.strptime(
                comment_data["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ),
            "updated_at": datetime.strptime(
                comment_data["updated_at"], "%Y-%m-%dT%H:%M:%SZ"
            ),
            "issue_id": issue_id,
            "tracker_id": self.tracker_id,
        }

    async def is_webhook_registered_for_organization(
        self, organization: "Organization", webhook_url: str
    ) -> bool:
        """
        Check if a webhook is registered for an organization.

        Args:
            organization: The organization to check.
            webhook_url: The URL of the webhook.

        Returns:
            Whether the webhook is registered.
        """
        endpoint = f"orgs/{organization.identifier}/hooks"
        try:
            hooks = await self._make_request(endpoint)
            for hook in hooks:
                if hook.get("config", {}).get("url") == webhook_url:
                    return True
            return False
        except TrackerResponseError:
            return False

    async def get_pull_request(self, pr_identifier: str) -> Dict[str, Any]:
        """
        Get details of a GitHub pull request.

        Args:
            pr_identifier: PR identifier (number, slug, or URL)

        Returns:
            Dict with PR details including title, description, state, comments, and changes
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Extract PR number from various formats
        pr_number = pr_identifier
        if "/" in pr_identifier:
            # Handle formats like "owner/repo#123" or "owner/repo/pull/123"
            parts = pr_identifier.split("/")
            pr_number = parts[-1]
        if "#" in pr_number:
            pr_number = pr_number.split("#")[-1]

        try:
            # Get PR details
            pr_path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
            pr_data = await self._request("GET", pr_path)

            # Get PR comments (review comments + issue comments)
            comments_path = f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
            review_comments = await self._request("GET", comments_path)

            issue_comments_path = f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
            issue_comments = await self._request("GET", issue_comments_path)

            # Combine all comments
            all_comments = []
            for comment in review_comments:
                all_comments.append(
                    {
                        "id": str(comment["id"]),
                        "author": comment["user"]["login"],
                        "body": comment["body"],
                        "created_at": comment["created_at"],
                        "type": "review_comment",
                        "path": comment.get("path"),
                        "position": comment.get("position"),
                    }
                )

            for comment in issue_comments:
                all_comments.append(
                    {
                        "id": str(comment["id"]),
                        "author": comment["user"]["login"],
                        "body": comment["body"],
                        "created_at": comment["created_at"],
                        "type": "issue_comment",
                    }
                )

            # Get PR files/changes
            files_path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files"
            files = await self._request("GET", files_path)

            changes = {
                "files_changed": len(files),
                "additions": pr_data.get("additions", 0),
                "deletions": pr_data.get("deletions", 0),
                "changed_files": [
                    {
                        "filename": f["filename"],
                        "status": f["status"],
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                        "patch": f.get("patch", ""),
                    }
                    for f in files
                ],
            }

            return {
                "id": str(pr_data["id"]),
                "number": pr_data["number"],
                "title": pr_data["title"],
                "description": pr_data.get("body", ""),
                "state": pr_data["state"],
                "author": pr_data["user"]["login"],
                "assignees": [a["login"] for a in pr_data.get("assignees", [])],
                "reviewers": [
                    r["login"] for r in pr_data.get("requested_reviewers", [])
                ],
                "labels": [label["name"] for label in pr_data.get("labels", [])],
                "url": pr_data["html_url"],
                "source_branch": pr_data["head"]["ref"],
                "target_branch": pr_data["base"]["ref"],
                "created_at": pr_data["created_at"],
                "updated_at": pr_data["updated_at"],
                "merged_at": pr_data.get("merged_at"),
                "is_draft": pr_data.get("draft", False),
                "comments": all_comments,
                "changes": changes,
            }

        except Exception as e:
            logger.error(f"Error getting pull request {pr_number}: {e}")
            raise TrackerResponseError(f"Failed to get pull request: {e}")

    async def list_pull_requests(
        self,
        state: str = "open",
        limit: int = 20,
        page: int = 1,
    ) -> Dict[str, Any]:
        """List pull requests for the connected repository.

        Args:
            state: GitHub PR state (open, closed, all).
            limit: Page size (per_page).
            page: 1-based page number.

        Returns:
            Dict with normalized ``items`` and ``has_more`` from the Link header.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")
        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        path = f"/repos/{owner}/{repo}/pulls"
        params = {
            "state": state,
            "per_page": limit,
            "page": page,
            "sort": "updated",
            "direction": "desc",
        }
        raw, headers = await self._request_with_headers("GET", path, params=params)
        if not isinstance(raw, list):
            raise TrackerResponseError("GitHub pull request list was not an array")
        items = [self._normalize_listed_pull_request(pr) for pr in raw]
        return {
            "items": items,
            "has_more": _github_link_has_next(
                headers.get("Link") or headers.get("link")
            ),
        }

    def _normalize_listed_pull_request(self, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Map a GitHub pulls-list item to the shared PR list shape."""
        user = pr_data.get("user") if isinstance(pr_data.get("user"), dict) else {}
        head = pr_data.get("head") if isinstance(pr_data.get("head"), dict) else {}
        base = pr_data.get("base") if isinstance(pr_data.get("base"), dict) else {}
        number = int(pr_data.get("number") or 0)
        state = str(pr_data.get("state") or "open")
        if state == "opened":
            state = "open"
        return {
            "number": number,
            "iid": number,
            "title": pr_data.get("title") or "",
            "description": pr_data.get("body") or "",
            "url": pr_data.get("html_url") or "",
            "author": user.get("login") or "",
            "source_branch": head.get("ref") or "",
            "target_branch": base.get("ref") or "",
            "state": state,
            "draft": bool(pr_data.get("draft", False)),
            "created_at": pr_data.get("created_at"),
            "updated_at": pr_data.get("updated_at"),
        }

    async def update_pull_request(
        self,
        pr_identifier: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        state: Optional[str] = None,
        assignees: Optional[List[str]] = None,
        reviewers: Optional[List[str]] = None,
        labels: Optional[List[str]] = None,
        draft: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Update a GitHub pull request.

        Args:
            pr_identifier: PR identifier (number, slug, or URL)
            title: New PR title
            description: New PR description
            state: New state ("open" or "closed")
            assignees: List of assignee usernames
            reviewers: List of reviewer usernames
            labels: List of label names
            draft: Whether to mark as draft

        Returns:
            Dict with updated PR details
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Extract PR number from various formats
        pr_number = pr_identifier
        if "/" in pr_identifier:
            parts = pr_identifier.split("/")
            pr_number = parts[-1]
        if "#" in pr_number:
            pr_number = pr_number.split("#")[-1]

        try:
            # Build update payload
            update_data = {}
            if title is not None:
                update_data["title"] = title
            if description is not None:
                update_data["body"] = description
            if state is not None:
                update_data["state"] = state
            if draft is not None:
                update_data["draft"] = draft

            pr_path = f"/repos/{owner}/{repo}/pulls/{pr_number}"

            # Only PATCH the PR if there's data to update
            # An empty PATCH can result in 422 errors
            if update_data:
                pr_data = await self._request("PATCH", pr_path, data=update_data)
            else:
                # Just fetch current PR data for assignee/reviewer updates
                pr_data = await self._request("GET", pr_path)

            # Update assignees if provided
            # Note: GitHub's POST endpoint only adds assignees, it doesn't replace.
            # To clear or replace assignees, we need to remove existing ones first.
            if assignees is not None:
                assignees_path = f"/repos/{owner}/{repo}/issues/{pr_number}/assignees"
                if len(assignees) == 0:
                    # Clear all assignees: get current assignees and remove them
                    current_assignees = [
                        a["login"] for a in pr_data.get("assignees", [])
                    ]
                    if current_assignees:
                        await self._request(
                            "DELETE",
                            assignees_path,
                            data={"assignees": current_assignees},
                        )
                        logger.info(
                            f"Cleared {len(current_assignees)} assignees from PR {pr_number}"
                        )
                else:
                    # First remove existing assignees, then add new ones
                    # This ensures we replace rather than just add
                    current_assignees = [
                        a["login"] for a in pr_data.get("assignees", [])
                    ]
                    assignees_to_remove = [
                        a for a in current_assignees if a not in assignees
                    ]
                    if assignees_to_remove:
                        await self._request(
                            "DELETE",
                            assignees_path,
                            data={"assignees": assignees_to_remove},
                        )
                    # Add the new assignees
                    await self._request(
                        "POST", assignees_path, data={"assignees": assignees}
                    )

            # Update reviewers if provided
            # Note: GitHub's POST endpoint only adds reviewers, it doesn't replace.
            if reviewers is not None:
                reviewers_path = (
                    f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
                )
                if len(reviewers) == 0:
                    # Clear all reviewers: get current reviewers and remove them
                    current_reviewers = [
                        r["login"] for r in pr_data.get("requested_reviewers", [])
                    ]
                    if current_reviewers:
                        await self._request(
                            "DELETE",
                            reviewers_path,
                            data={"reviewers": current_reviewers},
                        )
                        logger.info(
                            f"Cleared {len(current_reviewers)} reviewers from PR {pr_number}"
                        )
                else:
                    # First remove existing reviewers not in the new list
                    current_reviewers = [
                        r["login"] for r in pr_data.get("requested_reviewers", [])
                    ]
                    reviewers_to_remove = [
                        r for r in current_reviewers if r not in reviewers
                    ]
                    if reviewers_to_remove:
                        await self._request(
                            "DELETE",
                            reviewers_path,
                            data={"reviewers": reviewers_to_remove},
                        )
                    # Add the new reviewers
                    await self._request(
                        "POST", reviewers_path, data={"reviewers": reviewers}
                    )

            # Update labels if provided
            if labels is not None:
                labels_path = f"/repos/{owner}/{repo}/issues/{pr_number}/labels"
                await self._request("PUT", labels_path, data={"labels": labels})

            # Return updated PR data
            return {
                "id": str(pr_data["id"]),
                "number": pr_data["number"],
                "title": pr_data["title"],
                "description": pr_data.get("body", ""),
                "state": pr_data["state"],
                "url": pr_data["html_url"],
                "is_draft": pr_data.get("draft", False),
            }

        except Exception as e:
            logger.error(f"Error updating pull request {pr_number}: {e}")
            raise TrackerResponseError(f"Failed to update pull request: {e}")

    async def create_pull_request(
        self,
        title: str,
        source_branch: str,
        target_branch: str,
        description: Optional[str] = None,
        draft: bool = False,
        assignees: Optional[List[str]] = None,
        reviewers: Optional[List[str]] = None,
        labels: Optional[List[str]] = None,
        milestone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a GitHub pull request.

        Args:
            title: PR title
            source_branch: Branch containing the changes (head branch)
            target_branch: Branch to merge into (base branch)
            description: PR description/body
            draft: Whether to create as draft PR
            assignees: List of assignee usernames
            reviewers: List of reviewer usernames
            labels: List of label names
            milestone: Milestone number or title

        Returns:
            Dict with created PR details including id, number, title, url
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        try:
            # Create the pull request
            create_data = {
                "title": title,
                "head": source_branch,
                "base": target_branch,
                "body": description or "",
                "draft": draft,
            }

            pr_path = f"/repos/{owner}/{repo}/pulls"
            pr_data = await self._request("POST", pr_path, data=create_data)
            pr_number = pr_data["number"]

            logger.info(f"Created pull request #{pr_number}: {title}")

            # Add assignees if provided
            if assignees:
                try:
                    assignees_path = (
                        f"/repos/{owner}/{repo}/issues/{pr_number}/assignees"
                    )
                    await self._request(
                        "POST", assignees_path, data={"assignees": assignees}
                    )
                except Exception as e:
                    logger.warning(f"Failed to add assignees to PR #{pr_number}: {e}")

            # Request reviewers if provided
            if reviewers:
                try:
                    reviewers_path = (
                        f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
                    )
                    await self._request(
                        "POST", reviewers_path, data={"reviewers": reviewers}
                    )
                except Exception as e:
                    logger.warning(f"Failed to add reviewers to PR #{pr_number}: {e}")

            # Add labels if provided
            if labels:
                try:
                    labels_path = f"/repos/{owner}/{repo}/issues/{pr_number}/labels"
                    await self._request("POST", labels_path, data={"labels": labels})
                except Exception as e:
                    logger.warning(f"Failed to add labels to PR #{pr_number}: {e}")

            # Set milestone if provided
            if milestone:
                try:
                    # Try to get milestone by number first, then by title
                    milestone_number = None
                    if milestone.isdigit():
                        milestone_number = int(milestone)
                    else:
                        # Search for milestone by title with pagination
                        milestones_path = f"/repos/{owner}/{repo}/milestones"
                        page = 1
                        per_page = 100
                        while milestone_number is None:
                            milestones = await self._request(
                                "GET",
                                milestones_path,
                                params={
                                    "state": "all",
                                    "page": page,
                                    "per_page": per_page,
                                },
                            )
                            if not milestones:
                                break  # No more pages
                            for m in milestones:
                                if m["title"].lower() == milestone.lower():
                                    milestone_number = m["number"]
                                    break
                            if len(milestones) < per_page:
                                break  # Last page
                            page += 1

                    if milestone_number:
                        issue_path = f"/repos/{owner}/{repo}/issues/{pr_number}"
                        await self._request(
                            "PATCH", issue_path, data={"milestone": milestone_number}
                        )
                except Exception as e:
                    logger.warning(f"Failed to set milestone on PR #{pr_number}: {e}")

            return {
                "id": str(pr_data["id"]),
                "number": pr_data["number"],
                "title": pr_data["title"],
                "description": pr_data.get("body", ""),
                "state": pr_data["state"],
                "url": pr_data["html_url"],
                "is_draft": pr_data.get("draft", False),
                "source_branch": source_branch,
                "target_branch": target_branch,
            }

        except Exception as e:
            logger.error(f"Error creating pull request: {e}")
            raise TrackerResponseError(f"Failed to create pull request: {e}")

    @async_retry()
    async def submit_pull_request_review(
        self,
        pr_number: str,
        body: str,
        event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        comments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Submit a review on a pull request.

        Args:
            pr_number: PR number.
            body: Review summary comment.
            event: Review action - APPROVE, REQUEST_CHANGES, or COMMENT.
            comments: Optional list of inline review comments, each with:
                - path: file path
                - line: line number in the diff
                - body: comment text
                - side: "LEFT" (old) or "RIGHT" (new), default "RIGHT"

        Returns:
            Dict with review details including id, state, body.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Extract PR number from various formats
        pr_num = pr_number
        if "/" in pr_number:
            parts = pr_number.split("/")
            pr_num = parts[-1]
        if "#" in pr_num:
            pr_num = pr_num.split("#")[-1]

        # Helper to clean string values that may have extra quotes from AI output
        # e.g., "'REQUEST_CHANGES'" -> "REQUEST_CHANGES"
        def clean_str(val: Any) -> str:
            s = str(val)
            if s.startswith("'") and s.endswith("'"):
                s = s[1:-1]
            return s

        # Build the request payload
        # Clean body and event in case they have extra quotes from AI output parsing
        clean_body = clean_str(body) if body else ""
        clean_event = clean_str(event)

        payload: Dict[str, Any] = {
            "body": clean_body,
            "event": clean_event,
        }

        # Add inline comments if provided
        if comments:
            formatted_comments = []
            for comment in comments:
                formatted_comment: Dict[str, Any] = {
                    "path": clean_str(comment["path"]),
                    "body": clean_str(comment["body"]),
                }
                # Use 'line' for single-line comments
                # Must be an integer for GitHub API
                if "line" in comment:
                    line_val = comment["line"]
                    # Handle string line numbers (may come from AI output)
                    if isinstance(line_val, str):
                        # Strip surrounding quotes if present
                        line_val = line_val.strip("'\"")
                        try:
                            line_val = int(line_val)
                        except ValueError:
                            logger.warning(
                                f"Invalid line number '{comment['line']}', skipping"
                            )
                            continue
                    formatted_comment["line"] = int(line_val)
                # Use side if provided, default to RIGHT
                if "side" in comment:
                    formatted_comment["side"] = clean_str(comment["side"])
                else:
                    formatted_comment["side"] = "RIGHT"
                formatted_comments.append(formatted_comment)
            payload["comments"] = formatted_comments

        try:
            review_path = f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews"
            review_data = await self._request("POST", review_path, data=payload)

            return {
                "id": str(review_data["id"]),
                "node_id": review_data.get("node_id"),
                "state": review_data["state"],
                "body": review_data.get("body", ""),
                "user": review_data["user"]["login"],
                "submitted_at": review_data.get("submitted_at"),
                "html_url": review_data.get("html_url"),
            }

        except TrackerResponseError as e:
            error_str = str(e).lower()
            # Handle 422 "Line could not be resolved" errors
            # This happens when inline comments reference lines not in the diff
            if "422" in error_str and (
                "line could not be resolved" in error_str
                or "unprocessable" in error_str
            ):
                if comments:
                    logger.warning(
                        f"PR review for {pr_num} failed due to invalid line references. "
                        f"Retrying without {len(formatted_comments)} inline comment(s). "
                        f"Original error: {e}"
                    )
                    # Retry without inline comments - just submit the review body
                    payload_without_comments = {
                        "body": body,
                        "event": event,
                    }
                    try:
                        review_data = await self._request(
                            "POST", review_path, data=payload_without_comments
                        )
                        return {
                            "id": str(review_data["id"]),
                            "node_id": review_data.get("node_id"),
                            "state": review_data["state"],
                            "body": review_data.get("body", ""),
                            "user": review_data["user"]["login"],
                            "submitted_at": review_data.get("submitted_at"),
                            "html_url": review_data.get("html_url"),
                            "warning": (
                                f"Submitted without {len(formatted_comments)} inline "
                                "comment(s) due to invalid line references"
                            ),
                        }
                    except Exception as retry_error:
                        logger.error(
                            f"Retry without comments also failed for {pr_num}: {retry_error}"
                        )
                        raise TrackerResponseError(
                            f"Failed to submit PR review (even without comments): {retry_error}"
                        )
            # Re-raise for other errors
            raise

        except Exception as e:
            logger.error(f"Error submitting PR review for {pr_num}: {e}")
            raise TrackerResponseError(f"Failed to submit pull request review: {e}")

    @async_retry()
    async def get_review_comments(
        self,
        pr_number: str,
        review_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Get comments for a specific pull request review.

        Args:
            pr_number: PR number.
            review_id: The review ID to get comments for.

        Returns:
            List of comment dicts with id, body, path, line, etc.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Extract PR number from various formats
        pr_num = pr_number
        if "/" in pr_number:
            parts = pr_number.split("/")
            pr_num = parts[-1]
        if "#" in pr_num:
            pr_num = pr_num.split("#")[-1]

        try:
            comments_path = (
                f"/repos/{owner}/{repo}/pulls/{pr_num}/reviews/{review_id}/comments"
            )
            comments_data = await self._request("GET", comments_path)

            comments = []
            for comment in comments_data:
                comments.append(
                    {
                        "id": str(comment["id"]),
                        "node_id": comment.get("node_id"),
                        "body": comment.get("body", ""),
                        "path": comment.get("path"),
                        "line": comment.get("line"),
                        "position": comment.get("position"),
                        "author": comment["user"]["login"]
                        if comment.get("user")
                        else None,
                        "created_at": comment.get("created_at"),
                        "updated_at": comment.get("updated_at"),
                        "html_url": comment.get("html_url"),
                    }
                )

            return comments

        except Exception as e:
            logger.error(
                f"Error getting review comments for PR {pr_num}, review {review_id}: {e}"
            )
            raise TrackerResponseError(f"Failed to get review comments: {e}")

    async def _get_comment_thread_id_map(
        self, pr_number: str
    ) -> Dict[str, Optional[str]]:
        """
        Fetch a mapping of comment database IDs to their thread node_ids.

        Uses GraphQL to get all review threads and their comments for a PR,
        then builds a mapping from comment ID to thread ID. This allows
        get_pull_request_comments to include thread_id for each review comment.

        Args:
            pr_number: The PR number.

        Returns:
            Dict mapping comment database ID (str) to thread node_id (PRRT_*).
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            logger.warning("Owner/repo not found for thread mapping")
            return {}

        query = """
            query GetPRReviewThreads($owner: String!, $name: String!, $prNumber: Int!) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: $prNumber) {
                        reviewThreads(first: 100) {
                            nodes {
                                id
                                isResolved
                                comments(first: 50) {
                                    nodes {
                                        databaseId
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "owner": owner,
            "name": repo,
            "prNumber": int(pr_number),
        }

        graphql_url = "https://api.github.com/graphql"
        headers = await self._get_auth_headers()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    graphql_url,
                    headers=headers,
                    json={"query": query, "variables": variables},
                    timeout=30.0,
                )

                if response.status_code >= 400:
                    logger.warning(
                        f"GraphQL query for thread mapping failed: {response.status_code}"
                    )
                    return {}

                data = response.json()

                if "errors" in data:
                    logger.warning(
                        f"GraphQL errors in thread mapping: {data['errors']}"
                    )
                    return {}

                # Build mapping from comment database ID to thread ID
                mapping: Dict[str, Optional[str]] = {}
                threads = (
                    data.get("data", {})
                    .get("repository", {})
                    .get("pullRequest", {})
                    .get("reviewThreads", {})
                    .get("nodes", [])
                )

                for thread in threads:
                    thread_id = thread.get("id")
                    comments = thread.get("comments", {}).get("nodes", [])

                    for comment in comments:
                        db_id = comment.get("databaseId")
                        if db_id is not None:
                            mapping[str(db_id)] = thread_id

                logger.debug(
                    f"Built thread_id mapping for PR {pr_number}: "
                    f"{len(mapping)} comments mapped"
                )
                return mapping

        except Exception as e:
            logger.warning(f"Error building thread_id mapping for PR {pr_number}: {e}")
            return {}

    @async_retry()
    async def get_pull_request_comments(
        self,
        pr_number: str,
        filter_author: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get all comments on a pull request, optionally filtered by author.

        Fetches both review comments (inline code comments) and issue comments
        (general PR discussion comments).

        Args:
            pr_number: PR number.
            filter_author: Optional username to filter comments by.

        Returns:
            List of comment dicts with id, author, body, type, path, line, etc.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        # Extract PR number from various formats
        pr_num = pr_number
        if "/" in pr_number:
            parts = pr_number.split("/")
            pr_num = parts[-1]
        if "#" in pr_num:
            pr_num = pr_num.split("#")[-1]

        all_comments: List[Dict[str, Any]] = []

        try:
            # Fetch review comments (inline code comments)
            review_comments_path = f"/repos/{owner}/{repo}/pulls/{pr_num}/comments"
            review_comments = await self._make_request(review_comments_path)

            # Fetch thread_id mappings for review comments via GraphQL
            # This allows agents to resolve threads without extra lookups
            thread_id_map = await self._get_comment_thread_id_map(pr_num)

            for comment in review_comments:
                author = comment["user"]["login"]
                if filter_author and author != filter_author:
                    continue

                comment_id = str(comment["id"])
                all_comments.append(
                    {
                        "id": comment_id,
                        "node_id": comment.get("node_id"),
                        "thread_id": thread_id_map.get(comment_id),
                        "author": author,
                        "body": comment.get("body", ""),
                        "type": "review_comment",
                        "path": comment.get("path"),
                        "line": comment.get("line"),
                        "original_line": comment.get("original_line"),
                        "side": comment.get("side"),
                        "diff_hunk": comment.get("diff_hunk"),
                        "commit_id": comment.get("commit_id"),
                        "in_reply_to_id": comment.get("in_reply_to_id"),
                        "created_at": comment["created_at"],
                        "updated_at": comment["updated_at"],
                        "html_url": comment.get("html_url"),
                    }
                )

            # Fetch issue comments (general PR discussion)
            issue_comments_path = f"/repos/{owner}/{repo}/issues/{pr_num}/comments"
            issue_comments = await self._make_request(issue_comments_path)

            for comment in issue_comments:
                author = comment["user"]["login"]
                if filter_author and author != filter_author:
                    continue

                all_comments.append(
                    {
                        "id": str(comment["id"]),
                        "node_id": comment.get("node_id"),
                        "author": author,
                        "body": comment.get("body", ""),
                        "type": "issue_comment",
                        "path": None,
                        "line": None,
                        "original_line": None,
                        "side": None,
                        "diff_hunk": None,
                        "commit_id": None,
                        "in_reply_to_id": None,
                        "created_at": comment["created_at"],
                        "updated_at": comment["updated_at"],
                        "html_url": comment.get("html_url"),
                    }
                )

            return all_comments

        except Exception as e:
            logger.error(f"Error getting PR comments for {pr_num}: {e}")
            raise TrackerResponseError(f"Failed to get pull request comments: {e}")

    @async_retry()
    async def update_review_comment(
        self,
        comment_id: str,
        body: str,
    ) -> Dict[str, Any]:
        """
        Update the body of an existing review comment.

        Args:
            comment_id: The comment ID to update.
            body: New comment body.

        Returns:
            Dict with updated comment details.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        try:
            comment_path = f"/repos/{owner}/{repo}/pulls/comments/{comment_id}"
            payload = {"body": body}
            comment_data = await self._request("PATCH", comment_path, data=payload)

            return {
                "id": str(comment_data["id"]),
                "node_id": comment_data.get("node_id"),
                "author": comment_data["user"]["login"],
                "body": comment_data["body"],
                "path": comment_data.get("path"),
                "line": comment_data.get("line"),
                "side": comment_data.get("side"),
                "created_at": comment_data["created_at"],
                "updated_at": comment_data["updated_at"],
                "html_url": comment_data.get("html_url"),
            }

        except Exception as e:
            error_msg = str(e)
            # Use debug level for 404s since they're expected when comment type is unknown
            # and caller will try issue_comment as fallback
            if "404" in error_msg or "Not Found" in error_msg:
                logger.debug(f"Review comment {comment_id} not found (404): {e}")
            else:
                logger.error(f"Error updating review comment {comment_id}: {e}")
            raise TrackerResponseError(f"Failed to update review comment: {e}")

    @async_retry()
    async def update_issue_comment(
        self,
        comment_id: str,
        body: str,
    ) -> Dict[str, Any]:
        """
        Update the body of an existing issue comment (PR conversation comment).

        This is for comments on the PR's "Conversation" tab, not inline code review
        comments. In GitHub's API, these are accessed via the issues endpoint even
        for pull requests.

        Args:
            comment_id: The comment ID to update.
            body: New comment body.

        Returns:
            Dict with updated comment details.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        try:
            comment_path = f"/repos/{owner}/{repo}/issues/comments/{comment_id}"
            payload = {"body": body}
            comment_data = await self._request("PATCH", comment_path, data=payload)

            return {
                "id": str(comment_data["id"]),
                "node_id": comment_data.get("node_id"),
                "author": comment_data["user"]["login"],
                "body": comment_data["body"],
                "created_at": comment_data["created_at"],
                "updated_at": comment_data["updated_at"],
                "html_url": comment_data.get("html_url"),
            }

        except Exception as e:
            error_msg = str(e)
            # 404s for issue comments are actual errors (not fallback cases)
            if "404" in error_msg or "Not Found" in error_msg:
                logger.warning(f"Issue comment {comment_id} not found (404): {e}")
            else:
                logger.error(f"Error updating issue comment {comment_id}: {e}")
            raise TrackerResponseError(f"Failed to update issue comment: {e}")

    @async_retry()
    async def reply_to_review_comment(
        self,
        pr_number: str,
        comment_id: str,
        body: str,
    ) -> Dict[str, Any]:
        """
        Reply to an existing pull request review comment (threaded reply).

        Args:
            pr_number: The pull request number.
            comment_id: The ID of the comment to reply to.
            body: The reply body text.

        Returns:
            Dict with the created reply comment details.

        Raises:
            TrackerResponseError: If owner/repo not found or API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError("Owner/repo not found in connection details")

        try:
            # GitHub REST API endpoint for replying to a review comment
            # POST /repos/{owner}/{repo}/pulls/{pull_number}/comments/{comment_id}/replies
            reply_path = (
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments/{comment_id}/replies"
            )
            payload = {"body": body}
            reply_data = await self._request("POST", reply_path, data=payload)

            return {
                "id": str(reply_data["id"]),
                "node_id": reply_data.get("node_id"),
                "author": reply_data["user"]["login"],
                "body": reply_data["body"],
                "path": reply_data.get("path"),
                "line": reply_data.get("line"),
                "side": reply_data.get("side"),
                "in_reply_to_id": reply_data.get("in_reply_to_id"),
                "created_at": reply_data["created_at"],
                "updated_at": reply_data["updated_at"],
                "html_url": reply_data.get("html_url"),
            }

        except Exception as e:
            logger.error(
                f"Error replying to review comment {comment_id} on PR {pr_number}: {e}"
            )
            raise TrackerResponseError(f"Failed to reply to review comment: {e}")

    @async_retry()
    async def resolve_review_thread(
        self,
        thread_id: str,
        resolved: bool,
    ) -> Dict[str, Any]:
        """
        Resolve or unresolve a review thread.

        Note: This requires GitHub GraphQL API as the REST API doesn't support
        thread resolution.

        Args:
            thread_id: The thread ID (GraphQL node_id).
            resolved: True to resolve, False to unresolve.

        Returns:
            Dict with thread resolution status.

        Raises:
            TrackerResponseError: If API call fails.
            TrackerAuthenticationError: If authentication fails.
            TrackerConnectionError: If connection fails.
        """
        # GitHub GraphQL endpoint
        graphql_url = "https://api.github.com/graphql"

        # Choose the appropriate mutation
        if resolved:
            mutation = """
                mutation ResolveThread($threadId: ID!) {
                    resolveReviewThread(input: {threadId: $threadId}) {
                        thread {
                            id
                            isResolved
                            viewerCanResolve
                            viewerCanUnresolve
                        }
                    }
                }
            """
        else:
            mutation = """
                mutation UnresolveThread($threadId: ID!) {
                    unresolveReviewThread(input: {threadId: $threadId}) {
                        thread {
                            id
                            isResolved
                            viewerCanResolve
                            viewerCanUnresolve
                        }
                    }
                }
            """

        variables = {"threadId": thread_id}

        # Get auth headers
        headers = await self._get_auth_headers()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    graphql_url,
                    headers=headers,
                    json={"query": mutation, "variables": variables},
                )

                if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                    raise TrackerAuthenticationError("GitHub authentication failed")
                elif response.status_code >= 400:
                    raise TrackerResponseError(
                        f"GitHub GraphQL API error: {response.status_code} - {response.text}"
                    )

                data = response.json()

                # Check for GraphQL errors
                if "errors" in data:
                    error_messages = [e.get("message", str(e)) for e in data["errors"]]
                    raise TrackerResponseError(
                        f"GraphQL errors: {'; '.join(error_messages)}"
                    )

                # Extract the thread data from the appropriate mutation response
                if resolved:
                    thread_data = (
                        data.get("data", {})
                        .get("resolveReviewThread", {})
                        .get("thread", {})
                    )
                else:
                    thread_data = (
                        data.get("data", {})
                        .get("unresolveReviewThread", {})
                        .get("thread", {})
                    )

                return {
                    "id": thread_data.get("id"),
                    "is_resolved": thread_data.get("isResolved"),
                    "viewer_can_resolve": thread_data.get("viewerCanResolve"),
                    "viewer_can_unresolve": thread_data.get("viewerCanUnresolve"),
                }

        except httpx.RequestError as e:
            raise TrackerConnectionError(f"GitHub connection error: {str(e)}")

    @staticmethod
    def _match_thread_comment(
        thread: Dict[str, Any],
        comment_id: str,
    ) -> Optional[str]:
        """Match a comment against a review thread node.

        Tries both the numeric database ID and the GraphQL node ID of every
        comment in the thread.

        Args:
            thread: A reviewThread node from the GraphQL response.
            comment_id: The comment's numeric ID or node_id.

        Returns:
            The thread node_id (PRRT_*) if matched, None otherwise.
        """
        thread_id = thread.get("id")
        comments = thread.get("comments", {}).get("nodes", [])

        for comment in comments:
            # Match by database ID (numeric)
            if str(comment.get("databaseId")) == str(comment_id):
                logger.info(f"Found thread {thread_id} for comment {comment_id}")
                return thread_id
            # Match by node_id
            if comment.get("id") == comment_id:
                logger.info(f"Found thread {thread_id} for comment {comment_id}")
                return thread_id

        return None

    @async_retry()
    async def get_thread_id_for_comment(
        self,
        pr_number: str,
        comment_id: str,
    ) -> Optional[str]:
        """Look up the review thread ID for a PR review comment.

        Uses GraphQL to find the thread node_id (PRRT_*) that contains
        a specific comment.

        Args:
            pr_number: The PR number.
            comment_id: The comment's numeric ID or node_id.

        Returns:
            The thread node_id (PRRT_*) if found, None otherwise.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            logger.warning("Owner/repo not found for thread lookup")
            return None

        # GraphQL query to page through review threads. Only the first
        # comments page is fetched here; deeper comment pages are loaded
        # per-thread via THREAD_COMMENTS_QUERY below so a comments cursor is
        # never applied to a sibling thread's connection.
        query = """
            query GetPRReviewThreads($owner: String!, $name: String!, $prNumber: Int!, $threadsCursor: String) {
                repository(owner: $owner, name: $name) {
                    pullRequest(number: $prNumber) {
                        reviewThreads(first: 100, after: $threadsCursor) {
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                            nodes {
                                id
                                comments(first: 50) {
                                    pageInfo {
                                        hasNextPage
                                        endCursor
                                    }
                                    nodes {
                                        id
                                        databaseId
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """

        # Thread-scoped pagination query: fetches deeper comment pages for a
        # single review thread via its node id.
        thread_comments_query = """
            query GetPRReviewThreadComments($threadId: ID!, $commentsCursor: String) {
                node(id: $threadId) {
                    ... on PullRequestReviewThread {
                        id
                        comments(first: 50, after: $commentsCursor) {
                            pageInfo {
                                hasNextPage
                                endCursor
                            }
                            nodes {
                                id
                                databaseId
                            }
                        }
                    }
                }
            }
        """

        variables = {
            "owner": owner,
            "name": repo,
            "prNumber": int(pr_number),
        }

        graphql_url = "https://api.github.com/graphql"
        headers = await self._get_auth_headers()

        try:
            async with httpx.AsyncClient() as client:
                threads_cursor: Optional[str] = None

                # Outer loop pages through reviewThreads; the inner loop
                # pages through each thread's comments before moving on.
                while True:
                    response = await client.post(
                        graphql_url,
                        headers=headers,
                        json={
                            "query": query,
                            "variables": {
                                **variables,
                                "threadsCursor": threads_cursor,
                            },
                        },
                        timeout=30.0,
                    )

                    if response.status_code >= 400:
                        logger.warning(f"GraphQL query failed: {response.status_code}")
                        return None

                    data = response.json()

                    if "errors" in data:
                        logger.warning(f"GraphQL errors: {data['errors']}")
                        return None

                    connection = (
                        data.get("data", {})
                        .get("repository", {})
                        .get("pullRequest", {})
                        .get("reviewThreads", {})
                    )
                    threads = connection.get("nodes", [])

                    for thread in threads:
                        thread_id = thread.get("id")
                        comments_cursor: Optional[str] = None

                        while True:
                            found = self._match_thread_comment(thread, comment_id)
                            if found:
                                return found

                            page_info = thread.get("comments", {}).get("pageInfo", {})
                            if not page_info.get("hasNextPage") or not page_info.get(
                                "endCursor"
                            ):
                                break
                            comments_cursor = page_info["endCursor"]

                            response = await client.post(
                                graphql_url,
                                headers=headers,
                                json={
                                    "query": thread_comments_query,
                                    "variables": {
                                        "threadId": thread_id,
                                        "commentsCursor": comments_cursor,
                                    },
                                },
                                timeout=30.0,
                            )

                            if response.status_code >= 400:
                                logger.warning(
                                    f"GraphQL query failed: {response.status_code}"
                                )
                                return None

                            data = response.json()

                            if "errors" in data:
                                logger.warning(f"GraphQL errors: {data['errors']}")
                                return None

                            updated = data.get("data", {}).get("node", {}) or {}
                            if not updated or updated.get("id") != thread_id:
                                break
                            thread = updated

                    page_info = connection.get("pageInfo", {})
                    if not page_info.get("hasNextPage") or not page_info.get(
                        "endCursor"
                    ):
                        break
                    threads_cursor = page_info["endCursor"]

                logger.warning(f"No thread found for comment {comment_id}")
                return None

        except Exception as e:
            logger.warning(f"Error looking up thread for comment {comment_id}: {e}")
            return None

    @async_retry()
    async def add_issue_reaction(
        self,
        issue_number: str,
        reaction: str,
    ) -> Dict[str, Any]:
        """Add a reaction to an issue or pull request.

        GitHub uses the same reaction API for issues and PRs.

        Args:
            issue_number: The issue or PR number.
            reaction: The reaction type. Valid values:
                +1, -1, laugh, confused, heart, hooray, rocket, eyes

        Returns:
            Dictionary with reaction details.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError(
                "Owner and repo are required for GitHub reactions"
            )

        url = (
            f"{self.API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/reactions"
        )
        headers = await self._get_auth_headers()
        # Need special accept header for reactions API
        headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json={"content": reaction},
                timeout=30.0,
            )

            if response.status_code in (HTTP_STATUS_OK, HTTP_STATUS_CREATED):
                data = response.json()
                logger.info(f"Added reaction '{reaction}' to issue {issue_number}")
                return {
                    "id": data.get("id"),
                    "content": data.get("content"),
                    "user": data.get("user", {}).get("login"),
                }
            else:
                error_msg = response.text
                logger.error(
                    f"Failed to add reaction to issue {issue_number}: {error_msg}"
                )
                raise TrackerResponseError(
                    f"Failed to add reaction: {response.status_code} - {error_msg}"
                )

    @async_retry()
    async def remove_issue_reaction(
        self,
        issue_number: str,
        reaction: str,
    ) -> bool:
        """Remove a reaction from an issue or pull request.

        This finds the current user's reaction of the specified type and removes it.

        Args:
            issue_number: The issue or PR number.
            reaction: The reaction type to remove.

        Returns:
            True if the reaction was removed, False if not found.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError(
                "Owner and repo are required for GitHub reactions"
            )

        # First, list reactions to find the one to delete
        list_url = (
            f"{self.API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/reactions"
        )
        headers = await self._get_auth_headers()
        headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"

        async with httpx.AsyncClient() as client:
            # Get current reactions
            response = await client.get(
                list_url,
                headers=headers,
                timeout=30.0,
            )

            if response.status_code != HTTP_STATUS_OK:
                logger.warning(f"Could not list reactions: {response.text}")
                return False

            reactions = response.json()

            # Get the authenticated user's login to only remove our own reactions
            # This could be a user token or a GitHub App installation token
            authenticated_user = None
            try:
                user_response = await client.get(
                    f"{self.API_BASE_URL}/user",
                    headers=headers,
                    timeout=10.0,
                )
                if user_response.status_code == HTTP_STATUS_OK:
                    user_data = user_response.json()
                    authenticated_user = user_data.get("login")
                    logger.debug(f"Authenticated as user: {authenticated_user}")
                elif user_response.status_code == 403:
                    # This might be a GitHub App installation token
                    # Installation tokens can't call /user or /app
                    # Check if we have an app_slug in connection_details
                    app_slug = self.connection_details.get("app_slug")
                    if app_slug:
                        # GitHub App bot username is {slug}[bot]
                        authenticated_user = f"{app_slug}[bot]"
                        logger.debug(
                            f"Using app_slug from connection_details: {authenticated_user}"
                        )
                    else:
                        # Try GET /app as a fallback (works with JWT tokens, not installation tokens)
                        logger.debug(
                            "GET /user returned 403, trying GET /app for GitHub App auth"
                        )
                        app_response = await client.get(
                            f"{self.API_BASE_URL}/app",
                            headers=headers,
                            timeout=10.0,
                        )
                        if app_response.status_code == HTTP_STATUS_OK:
                            app_data = app_response.json()
                            app_slug = app_data.get("slug")
                            if app_slug:
                                authenticated_user = f"{app_slug}[bot]"
                                logger.debug(
                                    f"Authenticated as GitHub App bot: {authenticated_user}"
                                )
                        else:
                            logger.debug(
                                f"GET /app also failed with {app_response.status_code}. "
                                "For GitHub App installation tokens, set 'app_slug' in "
                                "connection_details to enable reaction removal."
                            )
            except Exception as e:
                logger.warning(f"Could not get authenticated user: {e}")

            # Find the reaction matching the content type AND created by us
            reaction_to_delete = None

            # If we couldn't determine the authenticated user, fall back to
            # matching Bot-type users. GitHub App installations always react as
            # a "[bot]" user, so this is safe for the common case.
            if authenticated_user is None:
                logger.info(
                    "Could not determine authenticated user via /user or /app. "
                    "Falling back to matching Bot-type reactions."
                )
                for r in reactions:
                    if r.get("content") == reaction:
                        user_info = r.get("user", {})
                        if user_info.get("type") == "Bot":
                            reaction_to_delete = r
                            logger.info(
                                f"Matched Bot reaction by {user_info.get('login')} "
                                f"for removal"
                            )
                            break
            else:
                for r in reactions:
                    if r.get("content") == reaction:
                        reaction_user = r.get("user", {}).get("login")
                        # Only delete if it's our reaction
                        if reaction_user == authenticated_user:
                            reaction_to_delete = r
                            break
                        else:
                            logger.debug(
                                f"Skipping reaction by {reaction_user} (not ours: {authenticated_user})"
                            )

            if not reaction_to_delete:
                logger.info(
                    f"No '{reaction}' reaction found on issue {issue_number} to remove"
                )
                return False

            # Delete the reaction
            reaction_id = reaction_to_delete.get("id")
            delete_url = f"{self.API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/reactions/{reaction_id}"

            delete_response = await client.delete(
                delete_url,
                headers=headers,
                timeout=30.0,
            )

            if delete_response.status_code == HTTP_STATUS_NO_CONTENT:
                logger.info(f"Removed reaction '{reaction}' from issue {issue_number}")
                return True
            else:
                logger.warning(
                    f"Failed to remove reaction: {delete_response.status_code}"
                )
                return False

    @async_retry()
    async def create_commit_status(
        self,
        sha: str,
        state: Literal["pending", "success", "failure", "error"],
        context: str = "preloop",
        description: Optional[str] = None,
        target_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a commit status (check) on a specific commit.

        This appears as a check in the PR's "Checks" section.

        Args:
            sha: The commit SHA to create the status on.
            state: The state of the status: pending, success, failure, or error.
            context: A string label to differentiate this status from others.
                     Default is "preloop".
            description: A short description of the status (max 140 chars).
            target_url: URL to link to for more details (e.g., flow execution page).

        Returns:
            Dictionary with status details.
        """
        owner = self.connection_details.get("owner")
        repo = self.connection_details.get("repo")

        if not owner or not repo:
            raise TrackerResponseError(
                "Owner and repo are required for GitHub commit status"
            )

        url = f"{self.API_BASE_URL}/repos/{owner}/{repo}/statuses/{sha}"
        logger.info(
            f"[CommitStatus] GitHub API call: POST {url} "
            f"(owner={owner}, repo={repo}, sha={sha[:8]})"
        )

        headers = await self._get_auth_headers()

        payload = {
            "state": state,
            "context": context,
        }
        if description:
            # GitHub limits description to 140 chars
            payload["description"] = description[:140]
        if target_url:
            payload["target_url"] = target_url

        logger.debug(f"[CommitStatus] Payload: {payload}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=30.0,
            )

            if response.status_code == HTTP_STATUS_CREATED:
                data = response.json()
                logger.info(f"Created commit status '{context}' ({state}) on {sha[:8]}")
                return {
                    "id": data.get("id"),
                    "state": data.get("state"),
                    "context": data.get("context"),
                    "description": data.get("description"),
                    "target_url": data.get("target_url"),
                    "url": data.get("url"),
                }
            else:
                error_msg = response.text
                logger.error(f"Failed to create commit status: {error_msg}")
                raise TrackerResponseError(
                    f"Failed to create commit status: {response.status_code} - {error_msg}"
                )
