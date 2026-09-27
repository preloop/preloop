"""
Base tracker interface for preloop.sync.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import logging

from sqlalchemy.orm import Session
from preloop.models.models.project import Project
from preloop.models.models.organization import Organization
from preloop.models.models.webhook import Webhook
from preloop.services.issue_estimate import synced_estimate_fields
from preloop.schemas.tracker_models import (
    Issue,
    IssueComment,
    IssueCreate,
    IssueFilter,
    IssueUpdate,
    ProjectMetadata,
    TrackerConnection,
)

logger = logging.getLogger(__name__)


class BaseTracker(ABC):
    """Base class for all tracker implementations."""

    # Subclasses should override this
    tracker_type: str = "unknown"

    # True when the provider hosts git repositories that a flow can clone,
    # push to and open pull requests on. Issue-only trackers (Jira) leave it
    # False; a repository binding may only name a tracker that sets it.
    hosts_repositories: bool = False

    # True when the provider tracks issues and has no repository of its own.
    # A flow triggered from such a tracker may apply a repository binding.
    # Independent of hosts_repositories: a code host can leave both False
    # until its clone path is wired, without being treated as issue-only.
    hosts_issues: bool = False

    def __init__(
        self, tracker_id: str, api_key: str, connection_details: Dict[str, Any]
    ):
        """
        Initialize the tracker.

        Args:
            tracker_id: ID of the tracker in the database (UUID string).
            api_key: API key or token for the tracker.
            connection_details: Connection details for the tracker.
        """
        self.tracker_id = tracker_id
        self.api_key = api_key
        self.connection_details = connection_details

    @abstractmethod
    async def test_connection(self) -> TrackerConnection:
        """Test the connection to the tracker."""
        pass

    @abstractmethod
    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Get metadata about a project."""
        pass

    @abstractmethod
    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Search for issues in a project."""
        pass

    @abstractmethod
    async def get_issue(self, issue_id: str) -> Issue:
        """Get a specific issue by ID."""
        pass

    @abstractmethod
    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Create a new issue."""
        pass

    @abstractmethod
    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Update an existing issue."""
        pass

    @abstractmethod
    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Get comments for an issue."""
        pass

    @abstractmethod
    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Add a comment to an issue."""
        pass

    @abstractmethod
    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Add a relation between issues."""
        pass

    @abstractmethod
    async def get_organizations(self) -> List[Dict[str, Any]]:
        """Get organizations from the tracker."""
        pass

    @abstractmethod
    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """Get projects for an organization from the tracker."""
        pass

    @abstractmethod
    async def get_issues(
        self, organization_id: str, project_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get issues for a project from the tracker."""
        pass

    def transform_organization(self, org_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transform organization data to a format that can be stored in the database.
        """
        return {
            "identifier": str(org_data["id"]),
            "name": org_data["name"],
            "tracker_id": self.tracker_id,
        }

    def transform_project(
        self, proj_data: Dict[str, Any], organization_id: str
    ) -> Dict[str, Any]:
        """
        Transform project data to a format that can be stored in the database.
        """
        return {
            "organization_id": organization_id,
            "identifier": proj_data["id"],
            "name": proj_data["name"],
            "description": proj_data.get("description", ""),
            "meta_data": {
                "url": proj_data.get("url", ""),
                "external_id": proj_data.get("id", ""),
                "source": "preloop-sync",
            },
        }

    def transform_issue(
        self, issue_data: Dict[str, Any], project: Project
    ) -> Dict[str, Any]:
        """
        Transform issue data to a format that can be stored in the database.
        """
        status = issue_data.get("state", "open")
        if status.lower() in ["closed", "done", "completed", "fixed"]:
            status = "closed"
        elif status.lower() in ["open", "new", "todo", "to do"]:
            status = "open"

        issue_type = issue_data.get("type") or "task"
        if issue_type.lower() in ["bug", "defect", "error"]:
            issue_type = "bug"
        elif issue_type.lower() in ["feature", "enhancement", "improvement"]:
            issue_type = "feature"

        last_updated = issue_data.get("updated_at")
        if isinstance(last_updated, datetime):
            last_updated = last_updated.isoformat()

        created_at = issue_data.get("created_at")
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()

        description = issue_data.get("description", "")

        issue_url = issue_data.get("url", "")

        transformed = {
            "project_id": project.id,
            "external_id": issue_data.get("id", issue_data.get("external_id")),
            "key": issue_data.get("key"),
            "title": issue_data["title"],
            "description": description,
            "status": status,
            "issue_type": issue_type,
            "priority": issue_data.get("priority", None),
            "updated_at": issue_data.get("updated_at"),
            "last_updated_external": issue_data.get("updated_at"),
            "external_url": issue_url,
            "last_synced": datetime.now(),
            "meta_data": {
                "labels": issue_data.get("labels", []),
                "assignees": issue_data.get("assignees", []),
                "url": issue_url,
                "external_url": issue_url,
                "external_created_at": created_at,
                "external_updated_at": last_updated,
                "source": "preloop-sync",
                # Raw native estimate fields (GitLab time_estimate, weight)
                # for the issue cost report; empty for trackers without them.
                "estimate_fields": synced_estimate_fields(issue_data),
            },
            "tracker_id": self.tracker_id,
            "comments": issue_data.get("comments", []),
            "dependencies": issue_data.get("dependencies", []),
        }

        return transformed

    def transform_comment(
        self,
        comment_data: Dict[str, Any],
        issue_db_id: str,
        author_db_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transform comment data to a format that can be stored in the database.
        """
        external_id = str(comment_data.get("id"))
        return {
            "issue_id": issue_db_id,
            "external_id": external_id,
            "author": None,
            "body": comment_data.get("body", ""),
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

    async def find_open_pull_request_for_branch(
        self, branch: str
    ) -> Optional[Dict[str, Any]]:
        """The open pull or merge request whose head is ``branch``.

        Used to bind a pull request that deterministic publication code
        opened when its log marker was lost, and to read the forge's own
        ``created_at`` for it. Trackers without a pull request API return
        None; a forge adapter overrides this.

        Args:
            branch: Head (source) branch name.

        Returns:
            The pull request in the shared list shape (``url``,
            ``source_branch``, ``created_at``, ...), or None.
        """
        return None

    @staticmethod
    def _first_listed_for_branch(listing: Any, branch: str) -> Optional[Dict[str, Any]]:
        """First listed pull request whose head really is ``branch``.

        Providers filter by branch themselves; this guards an adapter or API
        that ignored the filter and returned unrelated pull requests.
        """
        items = listing.get("items") if isinstance(listing, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("source_branch") != branch or not item.get("url"):
                continue
            return item
        return None

    @abstractmethod
    async def register_webhook(self, **kwargs: Any) -> bool:
        """Register a webhook for the tracker."""
        pass

    @abstractmethod
    async def unregister_webhook(self, **kwargs: Any) -> bool:
        """Unregister a webhook for the tracker."""
        pass

    @abstractmethod
    async def is_webhook_registered(self, webhook: "Webhook") -> bool:
        """Check if a webhook is registered in the tracker."""
        pass

    @abstractmethod
    async def get_webhooks(self) -> List[Dict[str, Any]]:
        """Get all webhooks for the tracker."""
        pass

    @abstractmethod
    async def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """Delete a webhook from the tracker."""
        pass

    @abstractmethod
    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """Unregister all webhooks, optionally matching a URL pattern."""
        pass

    @abstractmethod
    async def is_webhook_registered_for_project(
        self, project: "Project", webhook_url: str
    ) -> bool:
        """Check if a webhook is registered for a project."""
        pass

    @abstractmethod
    async def is_webhook_registered_for_organization(
        self, organization: "Organization", webhook_url: str
    ) -> bool:
        """Check if a webhook is registered for an organization."""
        pass
