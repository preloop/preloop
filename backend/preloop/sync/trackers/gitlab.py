"""
GitLab tracker implementation for Preloop Sync using python-gitlab library.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Tuple
from typing_extensions import Literal

import asyncio
import gitlab
from sqlalchemy.orm import Session

from ..config import logger
from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerResponseError,
)
from .base import BaseTracker
from .utils import (
    is_not_found_error,
    is_authentication_error,
    is_conflict_error,
    async_retry,
    HTTP_STATUS_UNAUTHORIZED,
    HTTP_STATUS_NOT_FOUND,
    HTTP_STATUS_CONFLICT,
)
from preloop.models.models.project import Project
from preloop.models.models.organization import Organization
from preloop.models.models.webhook import Webhook
from preloop.models.crud import crud_webhook
from preloop.schemas.tracker_models import (
    Issue,
    IssueComment,
    IssueCreate,
    IssueFilter,
    IssuePriority,
    IssueStatus,
    IssueUpdate,
    IssueUser,
    ProjectMetadata,
    TrackerConnection,
)


def _discussion_notes(discussion: Any) -> List[Any]:
    """Return discussion notes from python-gitlab list or manager objects."""
    notes = getattr(discussion, "notes", [])
    if hasattr(notes, "list") and callable(notes.list):
        return list(notes.list())
    if isinstance(notes, list):
        return notes
    if notes:
        return list(notes)
    return []


class GitLabTracker(BaseTracker):
    """GitLab tracker implementation using python-gitlab."""

    tracker_type: str = "gitlab"

    def __init__(
        self, tracker_id: str, api_key: str, connection_details: Dict[str, Any]
    ):
        """
        Initialize the GitLab tracker.
        """
        super().__init__(tracker_id, api_key, connection_details)
        gitlab_url = connection_details.get("url")
        if not gitlab_url:
            gitlab_url = "https://gitlab.com"
        gitlab_url = gitlab_url.rstrip("/")
        if gitlab_url.endswith("/api/v4"):
            gitlab_url = gitlab_url[:-7]
        self.url = gitlab_url
        try:
            self.gl = gitlab.Gitlab(self.url, private_token=api_key)
            self.gl.auth()
        except gitlab.exceptions.GitlabAuthenticationError as e:
            raise TrackerAuthenticationError(f"GitLab authentication failed: {str(e)}")
        except gitlab.exceptions.GitlabHttpError as e:
            raise TrackerConnectionError(f"GitLab connection error: {str(e)}")

    def _get_project_id(self) -> str:
        """Get the project ID from connection details.

        Returns:
            The project ID string.

        Raises:
            TrackerResponseError: If project ID is not found.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")
        return project_id

    @async_retry()
    async def _make_request(self, method, *args, **kwargs):
        """
        Execute a GitLab API request with error handling in a separate thread.
        """
        try:
            # Run the synchronous method in a thread pool
            result = await asyncio.to_thread(method, *args, **kwargs)
            return result
        except gitlab.exceptions.GitlabAuthenticationError as e:
            raise TrackerAuthenticationError(f"GitLab authentication failed: {e}")
        except gitlab.exceptions.GitlabHttpError as e:
            if e.response_code == HTTP_STATUS_UNAUTHORIZED:
                raise TrackerAuthenticationError(f"GitLab authentication failed: {e}")
            else:
                raise TrackerResponseError(f"GitLab API error: {e.response_code} - {e}")
        except gitlab.exceptions.GitlabConnectionError as e:
            raise TrackerConnectionError(f"GitLab connection error: {e}")
        except Exception as e:
            # Catching potential exceptions from to_thread if the callable fails
            logger.error(
                f"An unexpected error occurred in GitLab request: {e}", exc_info=True
            )
            raise TrackerResponseError(f"GitLab API error: {e}")

    async def _make_request_no_retry(self, method, *args, **kwargs):
        """
        Execute a GitLab API request without retry logic.
        Used for operations where we want to immediately handle specific errors like 404.
        """
        try:
            # Run the synchronous method in a thread pool
            result = await asyncio.to_thread(method, *args, **kwargs)
            return result
        except gitlab.exceptions.GitlabAuthenticationError as e:
            raise TrackerAuthenticationError(f"GitLab authentication failed: {e}")
        except gitlab.exceptions.GitlabHttpError as e:
            if e.response_code == HTTP_STATUS_UNAUTHORIZED:
                raise TrackerAuthenticationError(f"GitLab authentication failed: {e}")
            else:
                raise TrackerResponseError(f"GitLab API error: {e.response_code} - {e}")
        except gitlab.exceptions.GitlabConnectionError as e:
            raise TrackerConnectionError(f"GitLab connection error: {e}")
        except Exception as e:
            # Catching potential exceptions from to_thread if the callable fails
            logger.error(
                f"An unexpected error occurred in GitLab request: {e}", exc_info=True
            )
            raise TrackerResponseError(f"GitLab API error: {e}")

    async def _parse_dependencies(self, issue_links: List[Any]) -> List[Dict[str, str]]:
        """Parse issue links from GitLab API response."""
        dependencies = []
        for link in issue_links:
            try:
                # This is inefficient, as it makes an API call per link.
                # A future optimization could be to cache project slugs.
                target_project = await self._make_request(
                    self.gl.projects.get, link.project_id
                )
                target_key = f"{target_project.path_with_namespace}#{link.iid}"

                # Normalize link_type: 'relates_to' -> 'relates to'
                relationship_type = link.link_type.replace("_", " ")

                dependencies.append(
                    {
                        "target_key": target_key,
                        "type": relationship_type,
                    }
                )
            except Exception as e:
                logger.error(
                    f"Could not process GitLab issue link for target iid {link.iid}: {e}"
                )
                continue
        return dependencies

    def _parse_gitlab_issue(self, issue_data: Dict[str, Any], project=None) -> Issue:
        """Parse a GitLab issue into our standard format.

        Args:
            issue_data: Raw GitLab issue data (as dict or GitLab API object).
            project: Optional GitLab project object (used to get path_with_namespace).

        Returns:
            Standardized Issue object.
        """
        # Handle both dict and GitLab API object
        if not isinstance(issue_data, dict):
            # Convert GitLab API object to dict
            issue_dict = issue_data.attributes
        else:
            issue_dict = issue_data

        # Parse assignee
        assignee = None
        assignee_data = issue_dict.get("assignee") or (
            issue_dict.get("assignees", []) and issue_dict.get("assignees")[0]
        )
        if assignee_data:
            if isinstance(assignee_data, dict):
                assignee = IssueUser(
                    id=str(assignee_data["id"]),
                    name=assignee_data.get("name", ""),
                    email=None,
                    avatar_url=assignee_data.get("avatar_url"),
                )

        # Parse author (reporter)
        reporter = None
        author_data = issue_dict.get("author")
        if author_data:
            if isinstance(author_data, dict):
                reporter = IssueUser(
                    id=str(author_data["id"]),
                    name=author_data.get("name", ""),
                    email=None,
                    avatar_url=author_data.get("avatar_url"),
                )

        # Parse status
        state = issue_dict.get("state", "opened")
        status_id = state
        status_name = "Closed" if state == "closed" else "Open"
        status_category = "done" if state == "closed" else "todo"

        status = IssueStatus(
            id=status_id,
            name=status_name,
            category=status_category,
        )

        # Parse labels
        labels = issue_dict.get("labels", [])

        # Parse priority from labels
        priority = None
        priority_map = {
            "priority::critical": IssuePriority(
                id="critical", name="Critical", level=4
            ),
            "priority::high": IssuePriority(id="high", name="High", level=3),
            "priority::medium": IssuePriority(id="medium", name="Medium", level=2),
            "priority::low": IssuePriority(id="low", name="Low", level=1),
        }

        for label in labels:
            if label.lower() in priority_map:
                priority = priority_map[label.lower()]
                break

        # Parse dates
        created_at = datetime.fromisoformat(
            issue_dict["created_at"].replace("Z", "+00:00")
        )
        updated_at = datetime.fromisoformat(
            issue_dict["updated_at"].replace("Z", "+00:00")
        )

        # Handle closed_at
        resolved_at = None
        if issue_dict.get("closed_at"):
            resolved_at = datetime.fromisoformat(
                issue_dict["closed_at"].replace("Z", "+00:00")
            )

        # Get project_id - prefer path_with_namespace from project object if available
        if project and hasattr(project, "path_with_namespace"):
            project_id = project.path_with_namespace
        else:
            project_id = self.connection_details.get("project_id", "")

        # Create issue key
        iid = issue_dict.get("iid", issue_dict.get("id"))
        issue_key = f"{project_id}#{iid}"

        return Issue(
            id=str(issue_dict["id"]),
            key=issue_key,
            title=issue_dict["title"],
            description=issue_dict.get("description") or "",
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
            url=issue_dict.get("web_url", ""),
            api_url=issue_dict.get("_links", {}).get("self", ""),
            tracker_type="gitlab",
            project_key=project_id,
            custom_fields={},
        )

    async def test_connection(self) -> TrackerConnection:
        """Test the connection to the tracker."""
        try:
            await self._make_request(self.gl.version)
            return TrackerConnection(connected=True, message="Connection successful")
        except (
            TrackerAuthenticationError,
            TrackerConnectionError,
            TrackerResponseError,
        ) as e:
            return TrackerConnection(connected=False, message=str(e))

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Get metadata about a GitLab project.

        Args:
            project_key: Project ID or path.

        Returns:
            Project metadata.
        """
        project_id = self.connection_details.get("project_id", project_key)
        project = await self._make_request(self.gl.projects.get, project_id)

        # GitLab has simple status model: opened/closed
        statuses = [
            IssueStatus(id="opened", name="Open", category="todo"),
            IssueStatus(id="closed", name="Closed", category="done"),
        ]

        # GitLab doesn't have built-in priorities, but commonly uses labels
        priorities = [
            IssuePriority(id="critical", name="priority::critical", level=4),
            IssuePriority(id="high", name="priority::high", level=3),
            IssuePriority(id="medium", name="priority::medium", level=2),
            IssuePriority(id="low", name="priority::low", level=1),
        ]

        return ProjectMetadata(
            key=project.path_with_namespace,
            name=project.name,
            description=project.description,
            statuses=statuses,
            priorities=priorities,
            url=project.web_url,
        )

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Search for issues in a GitLab project.

        Args:
            project_key: Project ID or path.
            filter_params: Filter parameters.
            limit: Maximum number of issues to return.
            offset: Pagination offset.

        Returns:
            Tuple of (list of issues, total count).
        """
        project_id = self.connection_details.get("project_id", project_key)
        project = await self._make_request(self.gl.projects.get, project_id)

        # Build query parameters
        kwargs = {
            "per_page": limit,
            "page": (offset // limit) + 1,
        }

        # Add search term if provided
        if filter_params.query:
            kwargs["search"] = filter_params.query

        # Add state filter if provided
        if filter_params.status:
            if any(s.lower() == "closed" for s in filter_params.status):
                kwargs["state"] = "closed"
            elif any(s.lower() in ["opened", "open"] for s in filter_params.status):
                kwargs["state"] = "opened"

        # Add label filter if provided
        if filter_params.labels:
            kwargs["labels"] = filter_params.labels

        # Add assignee filter if provided
        if filter_params.assigned_to:
            kwargs["assignee_username"] = filter_params.assigned_to

        # Add date filters if provided
        if filter_params.created_after:
            kwargs["created_after"] = filter_params.created_after.isoformat()

        if filter_params.created_before:
            kwargs["created_before"] = filter_params.created_before.isoformat()

        if filter_params.updated_after:
            kwargs["updated_after"] = filter_params.updated_after.isoformat()

        if filter_params.updated_before:
            kwargs["updated_before"] = filter_params.updated_before.isoformat()

        # Add sort parameters if provided
        if filter_params.sort_by:
            sort_map = {
                "created": "created_at",
                "updated": "updated_at",
                "priority": "priority",
            }
            kwargs["order_by"] = sort_map.get(filter_params.sort_by, "created_at")

        if filter_params.sort_direction:
            kwargs["sort"] = filter_params.sort_direction.lower()

        # Get issues
        issues_data = await self._make_request(project.issues.list, **kwargs)

        # Parse issues
        issues = []
        for issue_obj in issues_data:
            issues.append(self._parse_gitlab_issue(issue_obj))

        # Get total count (GitLab python library doesn't provide this easily, so we estimate)
        # For accurate count, we'd need to make a separate API call
        total_count = len(issues)

        return issues, total_count

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Create a new GitLab issue.

        Args:
            project_key: Project ID or path.
            issue_data: Issue data.

        Returns:
            Created issue.
        """
        project_id = self.connection_details.get("project_id", project_key)
        project = await self._make_request(self.gl.projects.get, project_id)

        # Build issue data
        issue_dict = {
            "title": issue_data.title,
            "description": issue_data.description or "",
        }

        # Add labels if provided
        if issue_data.labels:
            issue_dict["labels"] = ",".join(issue_data.labels)

        # Add assignee if provided (would need to look up user ID)
        if issue_data.assignee:
            issue_dict["assignee_ids"] = [issue_data.assignee]

        # Create the issue
        created_issue = await self._make_request(project.issues.create, issue_dict)

        # Parse and return the issue
        return self._parse_gitlab_issue(created_issue)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Update an existing GitLab issue.

        Args:
            issue_id: Issue IID or ID.
            issue_data: Updated issue data.

        Returns:
            Updated issue.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        # Extract issue IID from various formats
        issue_iid = issue_id
        if "#" in issue_id:
            issue_iid = issue_id.split("#")[-1]
        if "/" in issue_iid:
            issue_iid = issue_iid.split("/")[-1]

        project = await self._make_request(self.gl.projects.get, project_id)
        issue = await self._make_request(project.issues.get, issue_iid)

        # Build update data
        update_dict = {}

        if issue_data.title is not None:
            update_dict["title"] = issue_data.title

        if issue_data.description is not None:
            update_dict["description"] = issue_data.description

        if issue_data.status is not None:
            # Map status to GitLab state_event
            if issue_data.status.lower() == "closed":
                update_dict["state_event"] = "close"
            elif issue_data.status.lower() in ["opened", "open"]:
                update_dict["state_event"] = "reopen"

        if issue_data.labels is not None:
            update_dict["labels"] = ",".join(issue_data.labels)

        if issue_data.assignee is not None:
            update_dict["assignee_ids"] = [issue_data.assignee]

        # Update the issue
        for key, value in update_dict.items():
            setattr(issue, key, value)

        await self._make_request(issue.save)

        # Parse and return the issue
        return self._parse_gitlab_issue(issue)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Add a comment to a GitLab issue or merge request.

        Args:
            issue_id: Issue IID or MR IID.
            comment: Comment text.

        Returns:
            Created comment.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        # Extract IID from various formats
        iid = issue_id
        if "#" in issue_id:
            iid = issue_id.split("#")[-1]
        if "/" in iid:
            iid = iid.split("/")[-1]

        project = await self._make_request(self.gl.projects.get, project_id)

        # Try to get as merge request first, fall back to issue
        resource = None
        resource_type: str
        try:
            resource = await self._make_request(project.mergerequests.get, iid)
            resource_type = "merge_request"
        except Exception:
            # Not a merge request, try as issue
            try:
                resource = await self._make_request(project.issues.get, iid)
                resource_type = "issue"
            except Exception as e:
                raise TrackerResponseError(
                    f"Could not find issue or merge request with IID {iid}: {e}"
                )

        # Create the comment (note)
        note_dict = {"body": comment}
        note = await self._make_request(resource.notes.create, note_dict)

        # Parse and return the comment
        url_fragment = "issues" if resource_type == "issue" else "merge_requests"
        return IssueComment(
            id=str(note.id),
            body=note.body,
            created_at=datetime.fromisoformat(note.created_at.replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(note.updated_at.replace("Z", "+00:00")),
            author=IssueUser(
                id=str(note.author["id"]),
                name=note.author.get("name", ""),
                email=None,
                avatar_url=note.author.get("avatar_url"),
            ),
            url=f"{project.web_url}/-/{url_fragment}/{iid}#note_{note.id}",
        )

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Add a relation between GitLab issues.

        Args:
            issue_id: Source issue IID.
            related_issue_id: Target issue IID.
            relation_type: Relation type (ignored by GitLab).

        Returns:
            Whether the operation was successful.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        # Extract issue IIDs from various formats
        issue_iid = issue_id
        if "#" in issue_id:
            issue_iid = issue_id.split("#")[-1]
        if "/" in issue_iid:
            issue_iid = issue_iid.split("/")[-1]

        related_issue_iid = related_issue_id
        if "#" in related_issue_id:
            related_issue_iid = related_issue_id.split("#")[-1]
        if "/" in related_issue_iid:
            related_issue_iid = related_issue_iid.split("/")[-1]

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            issue = await self._make_request(project.issues.get, issue_iid)

            # Create link data
            link_dict = {
                "target_project_id": project_id,
                "target_issue_iid": related_issue_iid,
            }

            # Create the link
            await self._make_request(issue.links.create, link_dict)
            return True
        except Exception as e:
            logger.exception(f"Failed to add relation: {e}")
            return False

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """
        Get organizations (groups) from GitLab.
        """
        groups = await self._make_request(self.gl.groups.list, all=True)
        organizations = []
        for group in groups:
            organizations.append(
                {"id": str(group.id), "name": group.name, "url": group.web_url}
            )
        return organizations

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """
        Get projects for a group from GitLab.
        """
        group = await self._make_request(self.gl.groups.get, organization_id)
        projects = await self._make_request(group.projects.list, all=True)
        project_list = []
        for project in projects:
            project_attributes = project.attributes
            project_list.append(
                {
                    "id": str(project_attributes.get("id")),
                    "identifier": str(project_attributes.get("id")),
                    "name": project_attributes.get("name"),
                    "description": project_attributes.get("description", ""),
                    "url": project_attributes.get("web_url"),
                    "path_with_namespace": project_attributes.get(
                        "path_with_namespace"
                    ),
                    "meta_data": {
                        "created_at": project_attributes.get("created_at"),
                        "updated_at": project_attributes.get("last_activity_at"),
                    },
                }
            )
        return project_list

    def transform_project(
        self, proj_data: Dict[str, Any], organization_id: str
    ) -> Dict[str, Any]:
        """Transforms a GitLab project into the common format."""
        return {
            "organization_id": organization_id,
            "identifier": str(proj_data["id"]),
            "name": proj_data["name"],
            "description": proj_data.get("description", ""),
            "slug": proj_data.get("path_with_namespace", ""),
            "meta_data": {
                "url": proj_data.get("url", ""),
                "external_id": proj_data.get("id", ""),
                "source": "preloop-sync",
                "path_with_namespace": proj_data.get("path_with_namespace"),
            },
        }

    async def get_issues(
        self, organization_id: str, project_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Get issues for a project from GitLab.
        """
        project = await self._make_request(self.gl.projects.get, project_id)
        project_slug = project.path_with_namespace
        if not project_slug:
            raise TrackerResponseError(
                f"Missing path_with_namespace for GitLab project ID {project_id}"
            )

        kwargs = {"all": True, "include_metadata": True}
        if since:
            kwargs["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        gitlab_issues = await self._make_request(project.issues.list, **kwargs)

        issue_list_with_comments = []
        for issue_obj in gitlab_issues:
            try:
                notes = await self._make_request(
                    issue_obj.notes.list, all=True, sort="asc", order_by="created_at"
                )
            except Exception as e:
                logger.error(
                    f"Failed to fetch notes for GitLab issue {issue_obj.iid} in project {project_id}: {e}"
                )
                notes = []

            comments_data = []
            for note in notes:
                if note.system:
                    continue
                author_data = None
                if hasattr(note, "author") and isinstance(note.author, dict):
                    author_data = {
                        "id": str(note.author.get("id")),
                        "name": note.author.get("username"),
                        "avatar_url": note.author.get("avatar_url"),
                    }
                comments_data.append(
                    {
                        "id": str(note.id),
                        "body": note.body or "",
                        "author": author_data,
                        "created_at": datetime.strptime(
                            note.created_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "updated_at": datetime.strptime(
                            note.updated_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "url": f"{issue_obj.web_url}#note_{note.id}",
                    }
                )

            issue_data = issue_obj.attributes
            issue_data["comments"] = comments_data
            # Construct the issue key in format "group/project#iid"
            issue_data["key"] = f"{project_slug}#{issue_obj.iid}"

            # Parse dependencies from issue links if available
            dependencies = []
            if hasattr(issue_obj, "links") and issue_obj.links:
                # issue_obj.links is a ProjectIssueLinkManager, need to call list() to get actual links
                try:
                    links_list = await self._make_request(
                        issue_obj.links.list, all=True
                    )
                    dependencies = await self._parse_dependencies(links_list)
                except Exception as e:
                    logger.warning(
                        f"Failed to parse dependencies for issue {issue_obj.iid}: {e}"
                    )
                    dependencies = []
            issue_data["dependencies"] = dependencies

            issue_list_with_comments.append(issue_data)
        return issue_list_with_comments

    async def register_project_webhook(
        self, db: Session, project: Project, webhook_url: str, secret: str
    ) -> bool:
        """
        Register a webhook for the given GitLab project.
        """
        hook_attrs = {
            "url": webhook_url,
            "token": secret,
            "issues_events": True,
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "repository_update_events": True,
            "enable_ssl_verification": True,
        }

        try:
            gitlab_project = await self._make_request(
                self.gl.projects.get, project.identifier
            )
            existing_hooks = await self._make_request(
                gitlab_project.hooks.list, all=True
            )
            for h in existing_hooks:
                if h.url == webhook_url:
                    return True

            hook = await self._make_request(gitlab_project.hooks.create, hook_attrs)
            crud_webhook.create(
                db,
                obj_in={
                    "external_id": str(hook.id),
                    "url": webhook_url,
                    "secret": secret,
                    "project_id": project.id,
                    "events": [
                        "issues",
                        "push",
                        "merge_requests",
                        "notes",
                        "pipeline",
                        "job",
                        "repository_update",
                    ],
                },
            )
            return True
        except TrackerResponseError as e:
            if str(HTTP_STATUS_CONFLICT) in str(e):  # Conflict
                logger.warning(
                    f"Project webhook for GitLab project '{project.identifier}' (URL: {webhook_url}) already exists ({HTTP_STATUS_CONFLICT} Conflict)."
                )
                return True
            logger.error(
                f"Failed to create project webhook for GitLab project '{project.identifier}': {e}",
                exc_info=True,
            )
            return False
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during project webhook registration for GitLab project '{project.identifier}': {e}",
                exc_info=True,
            )
            return False

    async def register_group_webhook(
        self, db: Session, organization: Organization, webhook_url: str, secret: str
    ) -> Union[bool, Literal["group_hooks_not_supported"]]:
        """
        Register a webhook for the given GitLab group.

        Args:
            db: The database session.
            organization: The organization to register the webhook for.
            webhook_url: The target URL for the webhook.
            secret: The secret token to use for the webhook.

        Returns:
            True if registration was successful or webhook already exists.
            "group_hooks_not_supported" if the /hooks endpoint for the group returns 404.
            False for other errors.
        """
        logger.info(
            f"Attempting to register group webhook for GitLab group ID '{organization.identifier}' pointing to {webhook_url}"
        )

        hook_attrs = {
            "url": webhook_url,
            "token": secret,
            "issues_events": True,
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "repository_update_events": True,
            "enable_ssl_verification": True,
        }

        try:
            logger.info(
                f"GitLabTracker: Attempting self.gl.groups.get() for group webhook. org_identifier='{organization.identifier}', client API URL='{self.url}'"
            )
            group = await self._make_request(
                self.gl.groups.get, organization.identifier
            )

            # Try to list existing hooks. A 404 here indicates group hooks are not supported.
            try:
                existing_hooks = await self._make_request_no_retry(
                    group.hooks.list, all=True
                )
                for h in existing_hooks:
                    if h.url == webhook_url:
                        logger.warning(
                            f"Group webhook for GitLab group '{organization.identifier}' (URL: {webhook_url}) already exists (ID: {h.id})."
                        )
                        return True
            except Exception as e:
                # Check if this is a 404 error indicating group hooks not supported
                if is_not_found_error(e):
                    logger.info(
                        f"Listing group hooks for GitLab group '{organization.identifier}' failed with 404. Assuming group hooks are not supported (e.g., GitLab CE)."
                    )
                    return "group_hooks_not_supported"
                logger.error(
                    f"Error listing group hooks for GitLab group '{organization.identifier}': {e}",
                    exc_info=True,
                )
                return False  # Other errors during list are a failure

            # If list succeeded and hook doesn't exist, try to create it.
            logger.info(
                f"Attempting to create group hook for GitLab group '{organization.identifier}' (URL: {webhook_url})."
            )
            try:
                hook = await self._make_request_no_retry(group.hooks.create, hook_attrs)
                logger.info(
                    f"Successfully created group webhook (ID: {hook.id}) for GitLab group '{organization.identifier}'."
                )
                crud_webhook.create(
                    db,
                    obj_in={
                        "external_id": str(hook.id),
                        "url": webhook_url,
                        "secret": secret,
                        "organization_id": organization.id,
                        "events": [
                            "issues",
                            "push",
                            "merge_requests",
                            "notes",
                            "pipeline",
                            "job",
                            "repository_update",
                        ],
                    },
                )
                return True
            except Exception as e:
                # Check for various error conditions
                if is_conflict_error(e):  # Conflict
                    logger.warning(
                        f"Group webhook for GitLab group '{organization.identifier}' (URL: {webhook_url}) already exists (409 on create)."
                    )
                    return True
                elif is_not_found_error(e):  # Not Found on create
                    logger.warning(
                        f"Creating group hook for GitLab group '{organization.identifier}' failed with 404. Assuming group hooks are not supported (e.g., GitLab CE)."
                    )
                    return "group_hooks_not_supported"
                elif is_authentication_error(e):
                    logger.error(
                        f"GitLab authentication failed (401) creating group hook for '{organization.identifier}'."
                    )
                    raise TrackerAuthenticationError("GitLab authentication failed")
                else:
                    logger.error(
                        f"Failed to create group webhook for GitLab group '{organization.identifier}': {e}",
                        exc_info=True,
                    )
                    return False

        except TrackerAuthenticationError:
            raise
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during group webhook registration for GitLab group '{organization.identifier}': {e}",
                exc_info=True,
            )
            return False

    def transform_comment(
        self,
        comment_data: Dict[str, Any],
        issue_db_id: str,
        author_db_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transform GitLab comment data to a format that can be stored in the database.
        GitLab uses 'note' instead of 'body' for comment text.
        """
        external_id = str(comment_data.get("id"))
        return {
            "issue_id": issue_db_id,
            "external_id": external_id,
            "author": None,
            "body": comment_data.get("note", ""),  # GitLab uses 'note' not 'body'
            "type": "issue",
            "meta_data": {
                "comment_id": external_id,
                "external_author": str(comment_data.get("author"))
                if comment_data.get("author")
                else None,
                "url": comment_data.get("url"),
                "source": "preloop-sync",
            },
            "updated_at": comment_data.get("updated_at"),
            "created_at": comment_data.get("created_at"),
        }

    async def register_webhook(self, **kwargs: Any) -> bool:
        """Register a webhook for the tracker."""
        raise NotImplementedError

    async def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        raise NotImplementedError

    async def get_webhooks(self, organization_id: str) -> List[Dict[str, Any]]:
        """Get webhooks for an organization."""
        raise NotImplementedError

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

        if webhook.project:
            try:
                project = await self._make_request(
                    self.gl.projects.get, webhook.project.identifier
                )
                await self._make_request(project.hooks.get, webhook.external_id)
                return True
            except (TrackerResponseError, gitlab.exceptions.GitlabGetError) as e:
                if "404" in str(e):
                    return False
                logger.error(
                    f"Failed to check project webhook {webhook.external_id}: {e}"
                )
                return False
        elif webhook.organization:
            try:
                group = await self._make_request(
                    self.gl.groups.get, webhook.organization.identifier
                )
                await self._make_request(group.hooks.get, webhook.external_id)
                return True
            except (TrackerResponseError, gitlab.exceptions.GitlabGetError) as e:
                if "404" in str(e):
                    return False
                logger.error(
                    f"Failed to check group webhook {webhook.external_id}: {e}"
                )
                return False
        return False

    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Unregister all webhooks, optionally matching a URL pattern.

        This method iterates through all relevant scopes (organizations and projects)
        for the tracker and attempts to unregister webhooks.

        Args:
            db: Database session to use for operations.
            webhook_url_pattern: If provided, only unregister webhooks whose URL
                                 matches this pattern. Otherwise, removes all webhooks
                                 associated with this tracker.

        Returns:
            A dictionary summarizing the actions taken:
            {"unregistered": count, "failed": count, "not_found": count}.
        """
        results = {"unregistered": 0, "failed": 0, "not_found": 0}
        logger.info(f"Unregistering all webhooks for GitLab tracker {self.tracker_id}.")

        # Get all organizations for this tracker from preloop.models
        from preloop.models.crud import crud_organization, crud_project

        orgs = crud_organization.get_for_tracker(db, tracker_id=self.tracker_id)
        for org in orgs:
            await self._unregister_all_webhooks_for_organization(db, org, results)

        if results["unregistered"] == 0:
            # If no organization webhooks, try project webhooks
            projects = crud_project.get_for_tracker(db, tracker_id=self.tracker_id)
            for project in projects:
                await self._unregister_all_webhooks_for_project(db, project, results)

        if results["unregistered"] > 0:
            logger.info(
                f"Unregistered {results['unregistered']} webhooks for GitLab tracker {self.tracker_id}."
            )
        else:
            logger.info(f"No webhooks found for GitLab tracker {self.tracker_id}.")

        logger.info(f"GitLab unregister_all_webhooks summary: {results}")
        return results

    async def _unregister_all_webhooks_for_organization(
        self, db: Session, organization: Organization, results: Dict[str, int]
    ):
        """Unregister all webhooks for a specific organization."""
        webhooks = crud_webhook.get_all_by_organization(
            db, organization_id=organization.id
        )
        for webhook in webhooks:
            if await self.unregister_webhook(db, webhook):
                results["unregistered"] += 1
            else:
                results["failed"] += 1

    async def _unregister_all_webhooks_for_project(
        self, db: Session, project: Project, results: Dict[str, int]
    ):
        """Unregister all webhooks for a specific project."""
        webhooks = crud_webhook.get_all_by_project(db, project_id=project.id)
        for webhook in webhooks:
            if await self.unregister_webhook(db, webhook):
                results["unregistered"] += 1
            else:
                results["failed"] += 1

    async def unregister_webhook(self, db: Session, webhook: Webhook) -> bool:
        """Unregister a webhook."""
        try:
            if webhook.project:
                project = await self._make_request(
                    self.gl.projects.get, webhook.project.identifier
                )
                await self._make_request(project.hooks.delete, webhook.external_id)
            elif webhook.organization:
                group = await self._make_request(
                    self.gl.groups.get, webhook.organization.identifier
                )
                await self._make_request(group.hooks.delete, webhook.external_id)
            else:
                return False

            crud_webhook.remove(db, id=webhook.id)
            return True
        except Exception:
            return False

    async def is_webhook_registered_for_project(
        self, project: "Project", webhook_url: str
    ) -> bool:
        """
        Check if a webhook is registered for a specific project.
        """
        try:
            gitlab_project = await self._make_request(
                self.gl.projects.get, project.identifier
            )
            hooks = await self._make_request(gitlab_project.hooks.list, all=True)
            return any(h.url == webhook_url for h in hooks)
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(
                f"Failed to check webhooks for project {project.identifier}: {e}"
            )
            return False

    async def is_webhook_registered_for_organization(
        self, organization: "Organization", webhook_url: str
    ) -> bool:
        """
        Check if a webhook is registered for a specific organization (group).
        """
        try:
            group = await self._make_request(
                self.gl.groups.get, organization.identifier
            )
            hooks = await self._make_request(group.hooks.list, all=True)
            return any(h.url == webhook_url for h in hooks)
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(
                f"Failed to check webhooks for organization {organization.identifier}: {e}"
            )
            return False

    async def cleanup_stale_webhooks(self, preloop_url: str) -> Dict[str, int]:
        """
        Cleans up stale webhooks from GitLab, for both groups and projects.

        Stale webhooks are webhooks that:
        1. Have a URL starting with preloop_url (they point to our Preloop instance)
        2. Are NOT registered in our database (they were created but not tracked, or orphaned)

        Args:
            preloop_url: The base URL of the Preloop instance.

        Returns:
            A dictionary summarizing the actions taken, e.g., `{"unregistered": count, "failed": count}`.
        """
        results = {"unregistered": 0, "failed": 0}
        skip_group_webhooks = False

        # Check if this is GitLab CE by fetching tracker metadata
        from preloop.models.crud import crud_tracker, crud_webhook
        from preloop.models.db.session import get_db_session

        db = next(get_db_session())
        try:
            tracker = crud_tracker.get(db, id=self.tracker_id)
            if tracker and tracker.meta_data:
                is_gitlab_ce = tracker.meta_data.get("gitlab_ce", False)
                if is_gitlab_ce:
                    logger.info(
                        f"Tracker {self.tracker_id} is marked as GitLab CE, skipping group webhooks"
                    )
                    skip_group_webhooks = True
        finally:
            db.close()

        if not skip_group_webhooks:
            try:
                groups = await self._make_request(self.gl.groups.list, all=True)
            except (TrackerConnectionError, TrackerResponseError) as e:
                logger.error(
                    f"Failed to retrieve groups for stale webhook cleanup: {e}"
                )
                # If we can't list groups, skip to project webhooks
                skip_group_webhooks = True
                groups = []
        else:
            groups = []

        if not skip_group_webhooks:
            for group in groups:
                try:
                    hooks = await self._make_request_no_retry(
                        group.hooks.list, all=True
                    )
                    for hook in hooks:
                        # Only consider webhooks pointing to our Preloop instance
                        if not hook.url.startswith(preloop_url):
                            continue

                        # Check if this webhook exists in our database
                        db = next(get_db_session())
                        try:
                            existing_webhook = crud_webhook.get_by_external_id(
                                db, external_id=str(hook.id), tracker_id=self.tracker_id
                            )

                            if existing_webhook:
                                # Webhook is in our database, keep it
                                logger.debug(
                                    f"Group webhook {hook.id} for group {group.id} is registered in database, keeping it."
                                )
                                continue

                            # Webhook points to our Preloop but is NOT in database - it's stale
                            logger.info(
                                f"Found stale group webhook {hook.id} for group {group.id} pointing to {hook.url}. "
                                f"This webhook is not in our database. Deleting..."
                            )
                            try:
                                await self._make_request(hook.delete)
                                logger.info(
                                    f"Successfully deleted stale group webhook {hook.id} for group {group.id}."
                                )
                                results["unregistered"] += 1
                            except (
                                TrackerConnectionError,
                                TrackerResponseError,
                            ) as delete_error:
                                logger.error(
                                    f"Failed to delete stale group webhook {hook.id} for group {group.id}: {delete_error}"
                                )
                                results["failed"] += 1
                        finally:
                            db.close()
                except (TrackerConnectionError, TrackerResponseError) as list_error:
                    # Check if this is a 404 indicating group hooks not supported (GitLab CE)
                    if is_not_found_error(list_error):
                        logger.info(
                            "Group webhooks not supported (likely GitLab CE), skipping remaining groups"
                        )
                        skip_group_webhooks = True
                        break
                    logger.error(
                        f"Failed to list hooks for group {group.id}: {list_error}"
                    )
                    results["failed"] += 1

        try:
            projects = await self._make_request(self.gl.projects.list, all=True)
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(f"Failed to retrieve projects for stale webhook cleanup: {e}")
            return results

        for project in projects:
            try:
                hooks = await self._make_request(project.hooks.list, all=True)
                for hook in hooks:
                    # Only consider webhooks pointing to our Preloop instance
                    if not hook.url.startswith(preloop_url):
                        continue

                    # Check if this webhook exists in our database
                    db = next(get_db_session())
                    try:
                        existing_webhook = crud_webhook.get_by_external_id(
                            db, external_id=str(hook.id), tracker_id=self.tracker_id
                        )

                        if existing_webhook:
                            # Webhook is in our database, keep it
                            logger.debug(
                                f"Project webhook {hook.id} for project {project.id} is registered in database, keeping it."
                            )
                            continue

                        # Webhook points to our Preloop but is NOT in database - it's stale
                        logger.info(
                            f"Found stale project webhook {hook.id} for project {project.id} pointing to {hook.url}. "
                            f"This webhook is not in our database. Deleting..."
                        )
                        try:
                            await self._make_request(hook.delete)
                            logger.info(
                                f"Successfully deleted stale project webhook {hook.id} for project {project.id}."
                            )
                            results["unregistered"] += 1
                        except (
                            TrackerConnectionError,
                            TrackerResponseError,
                        ) as delete_error:
                            logger.error(
                                f"Failed to delete stale project webhook {hook.id} for project {project.id}: {delete_error}"
                            )
                            results["failed"] += 1
                    finally:
                        db.close()
            except (TrackerConnectionError, TrackerResponseError) as list_error:
                logger.error(
                    f"Failed to list hooks for project {project.id}: {list_error}"
                )
                results["failed"] += 1

        return results

    async def get_issue(self, issue_id: str) -> Issue:
        """Get a single issue from GitLab.

        Args:
            issue_id: GitLab issue IID.

        Returns:
            Issue object.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            issue = await self._make_request(project.issues.get, issue_id)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == HTTP_STATUS_NOT_FOUND:
                raise TrackerResponseError(
                    f"Issue {issue_id} not found in project {project_id}"
                )
            raise

        # Use the mapper to convert to Issue object, passing project for path_with_namespace
        return self._parse_gitlab_issue(issue, project=project)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Get comments for an issue."""
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            issue = await self._make_request(project.issues.get, issue_id)
        except gitlab.exceptions.GitlabGetError as e:
            if e.response_code == HTTP_STATUS_NOT_FOUND:
                raise TrackerResponseError(
                    f"Issue {issue_id} not found in project {project_id}"
                )
            raise

        comments_data = []
        try:
            notes = await self._make_request(
                issue.notes.list, all=True, sort="asc", order_by="created_at"
            )
            for note in notes:
                if note.system:
                    continue

                author_data = None
                if hasattr(note, "author") and isinstance(note.author, dict):
                    author_data = IssueUser(
                        id=str(note.author.get("id")),
                        name=note.author.get("username"),
                        avatar_url=note.author.get("avatar_url"),
                    )

                try:
                    created_at_dt = datetime.strptime(
                        note.created_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                    updated_at_dt = datetime.strptime(
                        note.updated_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                except (ValueError, TypeError):
                    created_at_dt = datetime.now()
                    if isinstance(note.created_at, str):
                        try:
                            created_at_dt = datetime.strptime(
                                note.created_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                            )
                        except ValueError:
                            pass
                    updated_at_dt = created_at_dt

                comments_data.append(
                    IssueComment(
                        id=str(note.id),
                        body=note.body or "",
                        author=author_data,
                        created_at=created_at_dt,
                        updated_at=updated_at_dt,
                        url=f"{issue.web_url}#note_{note.id}",
                    )
                )
        except Exception as e:
            logger.error(f"Failed to fetch notes for issue {issue_id}: {e}")

        return comments_data

    async def get_merge_request(self, mr_identifier: str) -> Dict[str, Any]:
        """
        Get details of a GitLab merge request.

        Args:
            mr_identifier: MR identifier (IID, slug, or URL)

        Returns:
            Dict with MR details including title, description, state, comments, and changes
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        # Extract MR IID from various formats
        mr_iid = mr_identifier
        if "merge_requests" in mr_identifier:
            # Handle URLs like "https://gitlab.com/owner/repo/-/merge_requests/1"
            parts = mr_identifier.split("merge_requests/")
            mr_iid = parts[-1].rstrip("/")
        elif "/" in mr_iid:
            # Handle formats like "owner/repo#1"
            parts = mr_iid.split("/")
            mr_iid = parts[-1]
        if "#" in mr_iid:
            mr_iid = mr_iid.split("#")[-1]

        try:
            # Get project and MR
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Get MR notes (comments)
            notes = await self._make_request(
                mr.notes.list, all=True, sort="asc", order_by="created_at"
            )

            all_comments = []
            for note in notes:
                if note.system:  # Skip system notes
                    continue

                all_comments.append(
                    {
                        "id": str(note.id),
                        "author": note.author.get("username")
                        if hasattr(note, "author") and isinstance(note.author, dict)
                        else None,
                        "body": note.body or "",
                        "created_at": note.created_at,
                        "type": "note",
                    }
                )

            # Get MR changes/diffs
            # Older python-gitlab: mr.changes is a callable method returning a dict.
            # python-gitlab v4+: mr.changes is a manager with a .get() method.
            # Detect which form we have and call the right one.
            changes_attr = mr.changes
            if callable(changes_attr):
                mr_changes = await self._make_request(changes_attr)
            elif hasattr(changes_attr, "get"):
                mr_changes = await self._make_request(changes_attr.get)
            else:
                mr_changes = {}

            # The result may be a RESTObject or a dict depending on the
            # python-gitlab version.  Normalise to a list of file diffs.
            if hasattr(mr_changes, "attributes"):
                file_changes = mr_changes.attributes.get("changes", [])
            elif isinstance(mr_changes, dict):
                file_changes = mr_changes.get("changes", [])
            else:
                file_changes = (
                    mr_changes.changes if hasattr(mr_changes, "changes") else []
                )

            changes = {
                "files_changed": len(file_changes),
                "additions": getattr(mr, "diff_stats", {}).get("additions", 0),
                "deletions": getattr(mr, "diff_stats", {}).get("deletions", 0),
                "changed_files": [
                    {
                        "old_path": change.get("old_path", ""),
                        "new_path": change.get("new_path", ""),
                        "new_file": change.get("new_file", False),
                        "renamed_file": change.get("renamed_file", False),
                        "deleted_file": change.get("deleted_file", False),
                        "diff": change.get("diff", ""),
                    }
                    for change in file_changes
                ],
            }

            return {
                "id": str(mr.id),
                "iid": mr.iid,
                "title": mr.title,
                "description": mr.description or "",
                "state": mr.state,
                "author": mr.author.get("username")
                if hasattr(mr, "author") and isinstance(mr.author, dict)
                else None,
                "assignees": [
                    a.get("username")
                    for a in (mr.assignees if hasattr(mr, "assignees") else [])
                ],
                "reviewers": [
                    r.get("username")
                    for r in (mr.reviewers if hasattr(mr, "reviewers") else [])
                ],
                "labels": mr.labels if hasattr(mr, "labels") else [],
                "url": mr.web_url,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "created_at": mr.created_at,
                "updated_at": mr.updated_at,
                "merged_at": mr.merged_at if hasattr(mr, "merged_at") else None,
                "work_in_progress": mr.work_in_progress
                if hasattr(mr, "work_in_progress")
                else False,
                "comments": all_comments,
                "changes": changes,
            }

        except Exception as e:
            logger.error(f"Error getting merge request {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to get merge request: {e}")

    async def update_merge_request(
        self,
        mr_identifier: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        state_event: Optional[str] = None,
        assignee_ids: Optional[List[int]] = None,
        reviewer_ids: Optional[List[int]] = None,
        labels: Optional[List[str]] = None,
        draft: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Update a GitLab merge request.

        Args:
            mr_identifier: MR identifier (IID, slug, or URL)
            title: New MR title
            description: New MR description
            state_event: State event ("close", "reopen")
            assignee_ids: List of assignee user IDs
            reviewer_ids: List of reviewer user IDs
            labels: List of label names
            draft: Whether to mark as draft (work in progress)

        Returns:
            Dict with updated MR details
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        # Extract MR IID from various formats
        mr_iid = mr_identifier
        if "merge_requests" in mr_identifier:
            parts = mr_identifier.split("merge_requests/")
            mr_iid = parts[-1].rstrip("/")
        elif "/" in mr_iid:
            parts = mr_iid.split("/")
            mr_iid = parts[-1]
        if "#" in mr_iid:
            mr_iid = mr_iid.split("#")[-1]

        try:
            # Get project and MR
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Build update payload
            update_data = {}
            if title is not None:
                update_data["title"] = title
            if description is not None:
                update_data["description"] = description
            if state_event is not None:
                update_data["state_event"] = state_event
            if assignee_ids is not None:
                update_data["assignee_ids"] = assignee_ids
            if reviewer_ids is not None:
                update_data["reviewer_ids"] = reviewer_ids
            if labels is not None:
                update_data["labels"] = ",".join(labels)
            if draft is not None:
                # In GitLab, draft is controlled by work_in_progress
                update_data["wip"] = draft

            # Update MR
            for key, value in update_data.items():
                setattr(mr, key, value)

            await self._make_request(mr.save)

            # Return updated MR data
            return {
                "id": str(mr.id),
                "iid": mr.iid,
                "title": mr.title,
                "description": mr.description or "",
                "state": mr.state,
                "url": mr.web_url,
                "work_in_progress": mr.work_in_progress
                if hasattr(mr, "work_in_progress")
                else False,
            }

        except Exception as e:
            logger.error(f"Error updating merge request {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to update merge request: {e}")

    async def create_merge_request(
        self,
        title: str,
        source_branch: str,
        target_branch: str,
        description: Optional[str] = None,
        draft: bool = False,
        assignee_ids: Optional[List[int]] = None,
        reviewer_ids: Optional[List[int]] = None,
        labels: Optional[List[str]] = None,
        milestone_id: Optional[int] = None,
        squash: Optional[bool] = None,
        remove_source_branch: Optional[bool] = None,
        allow_collaboration: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Create a GitLab merge request.

        Args:
            title: MR title
            source_branch: Branch containing the changes
            target_branch: Branch to merge into
            description: MR description
            draft: Whether to create as draft (WIP)
            assignee_ids: List of assignee user IDs
            reviewer_ids: List of reviewer user IDs
            labels: List of label names
            milestone_id: Milestone ID
            squash: Whether to squash commits when merging
            remove_source_branch: Whether to remove source branch when merged
            allow_collaboration: Allow commits from upstream members

        Returns:
            Dict with created MR details including id, iid, title, url
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)

            # Build create data
            create_data = {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": f"Draft: {title}" if draft else title,
            }

            if description is not None:
                create_data["description"] = description
            if assignee_ids:
                create_data["assignee_ids"] = assignee_ids
            if reviewer_ids:
                create_data["reviewer_ids"] = reviewer_ids
            if labels:
                create_data["labels"] = ",".join(labels)
            if milestone_id is not None:
                create_data["milestone_id"] = milestone_id
            if squash is not None:
                create_data["squash"] = squash
            if remove_source_branch is not None:
                create_data["remove_source_branch"] = remove_source_branch
            if allow_collaboration is not None:
                create_data["allow_collaboration"] = allow_collaboration

            # Create the merge request
            mr = await self._make_request(project.mergerequests.create, create_data)

            logger.info(f"Created merge request !{mr.iid}: {title}")

            return {
                "id": str(mr.id),
                "iid": mr.iid,
                "title": mr.title,
                "description": mr.description or "",
                "state": mr.state,
                "url": mr.web_url,
                "work_in_progress": getattr(mr, "work_in_progress", False),
                "source_branch": source_branch,
                "target_branch": target_branch,
            }

        except Exception as e:
            logger.error(f"Error creating merge request: {e}")
            raise TrackerResponseError(f"Failed to create merge request: {e}")

    async def approve_merge_request(self, mr_iid: str) -> Dict[str, Any]:
        """
        Approve a merge request.

        Args:
            mr_iid: MR internal ID (IID).

        Returns:
            Dict with approval details.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Approve the merge request
            approval = await self._make_request(mr.approve)

            return {
                "id": str(mr.id),
                "iid": mr.iid,
                "approved": True,
                "approval_details": approval if approval else {},
            }

        except Exception as e:
            logger.error(f"Error approving merge request {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to approve merge request: {e}")

    async def unapprove_merge_request(self, mr_iid: str) -> Dict[str, Any]:
        """
        Remove approval from a merge request.

        Args:
            mr_iid: MR internal ID (IID).

        Returns:
            Dict with unapproval confirmation.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Unapprove the merge request
            await self._make_request(mr.unapprove)

            return {
                "id": str(mr.id),
                "iid": mr.iid,
                "approved": False,
            }

        except Exception as e:
            logger.error(f"Error unapproving merge request {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to unapprove merge request: {e}")

    async def get_mr_discussions(
        self, mr_iid: str, filter_author: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all discussions on a merge request, optionally filtered by author.

        Args:
            mr_iid: MR internal ID (IID).
            filter_author: Optional username to filter discussions by.

        Returns:
            List of discussion dicts with id, author, body, notes, resolved status.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Get all discussions
            discussions = await self._make_request(mr.discussions.list, all=True)

            result = []
            for discussion in discussions:
                discussion_data = {
                    "id": discussion.id,
                    "individual_note": getattr(discussion, "individual_note", False),
                    "notes": [],
                }

                # Get notes from discussion
                for note in _discussion_notes(discussion):
                    note_author = None
                    if isinstance(note, dict):
                        note_author = note.get("author", {}).get("username")
                        note_data = {
                            "id": note.get("id"),
                            "author": note_author,
                            "body": note.get("body", ""),
                            "created_at": note.get("created_at"),
                            "updated_at": note.get("updated_at"),
                            "resolvable": note.get("resolvable", False),
                            "resolved": note.get("resolved", False),
                            "system": note.get("system", False),
                        }
                    else:
                        note_author = (
                            note.author.get("username")
                            if hasattr(note, "author") and isinstance(note.author, dict)
                            else None
                        )
                        note_data = {
                            "id": getattr(note, "id", None),
                            "author": note_author,
                            "body": getattr(note, "body", ""),
                            "created_at": getattr(note, "created_at", None),
                            "updated_at": getattr(note, "updated_at", None),
                            "resolvable": getattr(note, "resolvable", False),
                            "resolved": getattr(note, "resolved", False),
                            "system": getattr(note, "system", False),
                        }
                    discussion_data["notes"].append(note_data)

                # Apply author filter if specified
                if filter_author:
                    discussion_data["notes"] = [
                        n
                        for n in discussion_data["notes"]
                        if n.get("author") == filter_author
                    ]
                    # Only include discussion if it has matching notes
                    if not discussion_data["notes"]:
                        continue

                result.append(discussion_data)

            return result

        except Exception as e:
            logger.warning("Error getting MR discussions for %s: %s", mr_iid, e)
            raise TrackerResponseError(f"Failed to get MR discussions: {e}")

    async def update_mr_note(
        self, mr_iid: str, note_id: str, body: str
    ) -> Dict[str, Any]:
        """
        Update the body of an existing MR note.

        Args:
            mr_iid: MR internal ID (IID).
            note_id: The note ID to update.
            body: New note body.

        Returns:
            Dict with updated note details.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Get the note
            note = await self._make_request(mr.notes.get, note_id)

            # Update the note body
            note.body = body
            await self._make_request(note.save)

            return {
                "id": str(note.id),
                "body": note.body,
                "author": note.author.get("username")
                if hasattr(note, "author") and isinstance(note.author, dict)
                else None,
                "updated_at": getattr(note, "updated_at", None),
            }

        except Exception as e:
            logger.error(f"Error updating MR note {note_id} for MR {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to update MR note: {e}")

    async def resolve_mr_discussion(
        self, mr_iid: str, discussion_id: str, resolved: bool
    ) -> Dict[str, Any]:
        """
        Resolve or unresolve a discussion thread on a merge request.

        Args:
            mr_iid: MR internal ID (IID).
            discussion_id: The discussion ID.
            resolved: True to resolve, False to unresolve.

        Returns:
            Dict with discussion resolution status.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Get the discussion
            discussion = await self._make_request(mr.discussions.get, discussion_id)

            # Update resolved status
            discussion.resolved = resolved
            await self._make_request(discussion.save)

            return {
                "id": discussion.id,
                "resolved": resolved,
            }

        except Exception as e:
            logger.error(
                f"Error resolving MR discussion {discussion_id} for MR {mr_iid}: {e}"
            )
            raise TrackerResponseError(f"Failed to resolve MR discussion: {e}")

    async def create_mr_discussion(
        self, mr_iid: str, body: str, position: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a new discussion on a merge request.

        Args:
            mr_iid: MR internal ID (IID).
            body: Discussion comment body.
            position: Optional position dict for diff comments with:
                - base_sha, start_sha, head_sha: Commit SHAs
                - position_type: "text" for diff comments
                - new_path: File path in new version
                - new_line: Line number in new version
                - old_path: File path in old version (optional)
                - old_line: Line number in old version (optional)

        Returns:
            Dict with created discussion details.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Build discussion data
            discussion_data: Dict[str, Any] = {"body": body}

            # Add position if provided (for diff comments)
            if position:
                discussion_data["position"] = {
                    "base_sha": position.get("base_sha"),
                    "start_sha": position.get("start_sha"),
                    "head_sha": position.get("head_sha"),
                    "position_type": position.get("position_type", "text"),
                    "new_path": position.get("new_path"),
                    "new_line": position.get("new_line"),
                }
                # Add optional old path/line if provided
                if position.get("old_path"):
                    discussion_data["position"]["old_path"] = position["old_path"]
                if position.get("old_line"):
                    discussion_data["position"]["old_line"] = position["old_line"]

            # Create the discussion
            discussion = await self._make_request(
                mr.discussions.create, discussion_data
            )

            # Build response
            # discussion.notes may be a plain list (older python-gitlab)
            # or a manager object (v4+).  Try the plain attribute first,
            # then fall back to .attributes dict from the raw API response.
            raw_notes = getattr(discussion, "notes", None)
            if isinstance(raw_notes, list):
                notes = raw_notes
            elif hasattr(discussion, "attributes") and isinstance(
                discussion.attributes.get("notes"), list
            ):
                notes = discussion.attributes["notes"]
            else:
                notes = []
            first_note = notes[0] if notes else {}

            return {
                "id": discussion.id,
                "individual_note": getattr(discussion, "individual_note", False),
                "notes": [
                    {
                        "id": first_note.get("id")
                        if isinstance(first_note, dict)
                        else getattr(first_note, "id", None),
                        "body": first_note.get("body")
                        if isinstance(first_note, dict)
                        else getattr(first_note, "body", body),
                        "author": (
                            first_note.get("author", {}).get("username")
                            if isinstance(first_note, dict)
                            else (
                                first_note.author.get("username")
                                if hasattr(first_note, "author")
                                and isinstance(first_note.author, dict)
                                else None
                            )
                        ),
                    }
                ],
            }

        except Exception as e:
            logger.error(f"Error creating MR discussion for MR {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to create MR discussion: {e}")

    async def reply_to_mr_discussion(
        self, mr_iid: str, discussion_id: str, body: str
    ) -> Dict[str, Any]:
        """
        Reply to an existing merge request discussion (add a note to the thread).

        Args:
            mr_iid: MR internal ID (IID).
            discussion_id: The discussion ID to reply to.
            body: The reply body text.

        Returns:
            Dict with the created note details.

        Raises:
            TrackerResponseError: If project ID not found or API call fails.
        """
        project_id = self.connection_details.get("project_id")
        if not project_id:
            raise TrackerResponseError("Project ID not found in connection details")

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Get the discussion
            discussion = await self._make_request(mr.discussions.get, discussion_id)

            # Add a note to the discussion
            note = await self._make_request(discussion.notes.create, {"body": body})

            # Handle both dict and object responses
            if isinstance(note, dict):
                return {
                    "id": note.get("id"),
                    "discussion_id": discussion_id,
                    "body": note.get("body"),
                    "author": note.get("author", {}).get("username"),
                    "created_at": note.get("created_at"),
                }
            else:
                return {
                    "id": getattr(note, "id", None),
                    "discussion_id": discussion_id,
                    "body": getattr(note, "body", body),
                    "author": (
                        note.author.get("username")
                        if hasattr(note, "author") and isinstance(note.author, dict)
                        else None
                    ),
                    "created_at": getattr(note, "created_at", None),
                }

        except Exception as e:
            logger.error(
                f"Error replying to discussion {discussion_id} on MR {mr_iid}: {e}"
            )
            raise TrackerResponseError(f"Failed to reply to MR discussion: {e}")

    async def add_mr_award_emoji(
        self,
        mr_iid: str,
        emoji_name: str,
    ) -> Dict[str, Any]:
        """Add an award emoji (reaction) to a merge request.

        GitLab uses "award emoji" instead of "reactions".

        Args:
            mr_iid: The merge request IID.
            emoji_name: The emoji name (e.g., "thumbsup", "thumbsdown", "eyes", "rocket").

        Returns:
            Dictionary with award emoji details.
        """
        project_id = self._get_project_id()

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # Create the award emoji
            award = await self._make_request(
                mr.awardemojis.create, {"name": emoji_name}
            )

            # Handle both dict and object responses
            if isinstance(award, dict):
                return {
                    "id": award.get("id"),
                    "name": award.get("name"),
                    "user": award.get("user", {}).get("username"),
                }
            else:
                return {
                    "id": getattr(award, "id", None),
                    "name": getattr(award, "name", emoji_name),
                    "user": (
                        award.user.get("username")
                        if hasattr(award, "user") and isinstance(award.user, dict)
                        else None
                    ),
                }

        except Exception as e:
            # GitLab returns 404 with "has already been taken" when the emoji
            # already exists.  Treat this as idempotent success.
            if "already been taken" in str(e).lower():
                logger.info(f"Award emoji '{emoji_name}' already exists on MR {mr_iid}")
                return {
                    "id": None,
                    "name": emoji_name,
                    "user": None,
                    "already_existed": True,
                }
            logger.error(f"Error adding award emoji to MR {mr_iid}: {e}")
            raise TrackerResponseError(f"Failed to add award emoji: {e}")

    async def remove_mr_award_emoji(
        self,
        mr_iid: str,
        emoji_name: str,
    ) -> bool:
        """Remove an award emoji (reaction) from a merge request.

        This finds the current user's award emoji of the specified type and removes it.

        Args:
            mr_iid: The merge request IID.
            emoji_name: The emoji name to remove.

        Returns:
            True if the emoji was removed, False if not found.
        """
        project_id = self._get_project_id()

        try:
            project = await self._make_request(self.gl.projects.get, project_id)
            mr = await self._make_request(project.mergerequests.get, mr_iid)

            # List all award emojis on the MR
            awards = await self._make_request(mr.awardemojis.list)

            # Find the award matching the emoji name
            award_to_delete = None
            for award in awards:
                award_name = (
                    award.get("name")
                    if isinstance(award, dict)
                    else getattr(award, "name", None)
                )
                if award_name == emoji_name:
                    award_to_delete = award
                    break

            if not award_to_delete:
                logger.info(
                    f"No '{emoji_name}' award emoji found on MR {mr_iid} to remove"
                )
                return False

            # Get the award ID
            award_id = (
                award_to_delete.get("id")
                if isinstance(award_to_delete, dict)
                else getattr(award_to_delete, "id", None)
            )

            if not award_id:
                logger.warning("Could not get award ID for deletion")
                return False

            # Delete the award emoji
            await self._make_request(mr.awardemojis.delete, award_id)
            logger.info(f"Removed award emoji '{emoji_name}' from MR {mr_iid}")
            return True

        except Exception as e:
            logger.warning(f"Failed to remove award emoji from MR {mr_iid}: {e}")
            return False

    async def create_commit_status(
        self,
        sha: str,
        state: str,  # "pending", "running", "success", "failed", "canceled"
        context: str = "preloop",
        description: Optional[str] = None,
        target_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a commit status (pipeline status) on a specific commit.

        This appears as a pipeline/check in the MR's pipeline section.

        Args:
            sha: The commit SHA to create the status on.
            state: The state of the status. GitLab accepts:
                   pending, running, success, failed, canceled
            context: A string label (called "name" in GitLab) to differentiate
                     this status from others. Default is "preloop".
            description: A short description of the status.
            target_url: URL to link to for more details.

        Returns:
            Dictionary with status details.
        """
        project_id = self._get_project_id()

        # Map common state names to GitLab's expected values
        state_map = {
            "pending": "pending",
            "running": "running",
            "success": "success",
            "failure": "failed",
            "failed": "failed",
            "error": "failed",
            "canceled": "canceled",
        }
        gitlab_state = state_map.get(state.lower(), state)

        try:
            project = await self._make_request(self.gl.projects.get, project_id)

            # Create commit status using the commits API
            commit = await self._make_request(project.commits.get, sha)

            status_data = {
                "state": gitlab_state,
                "name": context,
            }
            if description:
                status_data["description"] = description
            if target_url:
                status_data["target_url"] = target_url

            # Create the status
            status = await self._make_request(commit.statuses.create, status_data)

            # Handle both dict and object responses
            if isinstance(status, dict):
                result = {
                    "id": status.get("id"),
                    "state": status.get("status"),
                    "name": status.get("name"),
                    "description": status.get("description"),
                    "target_url": status.get("target_url"),
                }
            else:
                result = {
                    "id": getattr(status, "id", None),
                    "state": getattr(status, "status", gitlab_state),
                    "name": getattr(status, "name", context),
                    "description": getattr(status, "description", description),
                    "target_url": getattr(status, "target_url", target_url),
                }

            logger.info(
                f"Created commit status '{context}' ({gitlab_state}) on {sha[:8]}"
            )
            return result

        except Exception as e:
            logger.error(f"Error creating commit status on {sha[:8]}: {e}")
            raise TrackerResponseError(f"Failed to create commit status: {e}")

    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        """Look up a GitLab user ID by username.

        Args:
            username: The GitLab username to look up.

        Returns:
            The user ID if found, None otherwise.
        """
        try:
            # Search for users with matching username
            users = await self._make_request(
                self.gl.users.list, username=username, per_page=10
            )

            # Find exact username match (case-insensitive)
            for user in users:
                user_dict = user if isinstance(user, dict) else user.attributes
                user_name = user_dict.get("username", "")
                if user_name.lower() == username.lower():
                    return user_dict.get("id")

            logger.warning(f"GitLab user not found: {username}")
            return None

        except Exception as e:
            logger.warning(f"Error looking up GitLab user '{username}': {e}")
            return None

    async def get_user_ids_by_usernames(self, usernames: List[str]) -> List[int]:
        """Look up multiple GitLab user IDs by usernames.

        Args:
            usernames: List of GitLab usernames to look up.

        Returns:
            List of found user IDs (may be shorter than input if some not found).
        """
        user_ids = []
        for username in usernames:
            user_id = await self.get_user_id_by_username(username)
            if user_id is not None:
                user_ids.append(user_id)
        return user_ids
