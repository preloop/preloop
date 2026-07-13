"""
Jira tracker implementation for preloop.sync.
"""

import base64
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import logging
import urllib.parse
import requests

import httpx
from jira import JIRA, JIRAError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_webhook, crud_organization, crud_project
from preloop.schemas.tracker_models import (
    Issue,
    IssueComment,
    IssueCreate,
    IssueFilter,
    IssuePriority,
    IssueRelation,
    IssueStatus,
    IssueUpdate,
    IssueUser,
    ProjectMetadata,
    TrackerConnection,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerResponseError,
)
from .base import BaseTracker
from .utils import (
    HTTP_STATUS_NO_CONTENT,
    HTTP_STATUS_UNAUTHORIZED,
    HTTP_STATUS_NOT_FOUND,
    HTTP_SUCCESS_MIN,
    HTTP_SUCCESS_MAX,
    JIRA_DEFAULT_PAGE_SIZE,
)
from preloop.models.models.project import Project
from preloop.models.models.webhook import Webhook
from preloop.models.models.organization import Organization


logger = logging.getLogger(__name__)

DEFAULT_JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "comment_created",
]


class JiraTracker(BaseTracker):
    """Jira tracker implementation."""

    tracker_type: str = "jira"

    def __init__(
        self, tracker_id: str, api_key: str, connection_details: Dict[str, Any]
    ):
        """
        Initialize the Jira tracker.
        """
        super().__init__(tracker_id, api_key, connection_details)

        jira_url = connection_details.get("jira_url") or connection_details.get("url")
        if not jira_url:
            raise ValueError("Jira URL is required in connection_details")

        if "username" not in connection_details:
            raise ValueError("Jira username is required in connection_details")

        self.jira_url = jira_url.rstrip("/")
        self.base_url = self.jira_url  # Alias for compatibility
        self.username = connection_details["username"]

        auth_str = f"{self.username}:{api_key}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        self.headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/json",
        }

        self.jira_client: Optional[JIRA] = None
        if self.jira_url and self.username and api_key:
            try:
                self.jira_client = JIRA(
                    server=self.jira_url,
                    basic_auth=(self.username, api_key),
                    timeout=20,
                    max_retries=3,
                )
            except JIRAError as e:
                if e.status_code == HTTP_STATUS_UNAUTHORIZED:
                    raise TrackerAuthenticationError(
                        f"Jira client authentication failed: {e.text}"
                    )
                else:
                    raise TrackerConnectionError(
                        f"Jira client connection/setup failed: {e.text}"
                    )
            except Exception as e:
                raise TrackerConnectionError(
                    f"Unexpected error initializing Jira client: {str(e)}"
                )

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        api_version: str = "2",
    ) -> Any:
        """Make a request to the Jira API using httpx."""
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.jira_url}/rest/api/{api_version}/{endpoint.lstrip('/')}"
                response = await client.request(
                    method.upper(),
                    url,
                    headers=self.headers,
                    params=params,
                    json=json_data,
                )

                if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                    raise TrackerAuthenticationError("Jira authentication failed")

                if HTTP_SUCCESS_MIN <= response.status_code <= HTTP_SUCCESS_MAX:
                    if response.status_code == HTTP_STATUS_NO_CONTENT:
                        return None
                    if response.content:
                        try:
                            return response.json()
                        except ValueError:
                            return response.text
                    return None

                raise TrackerResponseError(
                    f"Jira API error: {response.status_code} - {response.text}"
                )
            except httpx.RequestError as e:
                raise TrackerConnectionError(f"Jira connection error: {str(e)}")

    async def test_connection(self) -> TrackerConnection:
        """Test the connection to the tracker."""
        try:
            await self._make_request("GET", "myself")
            return TrackerConnection(connected=True, message="Connection successful")
        except (
            TrackerAuthenticationError,
            TrackerConnectionError,
            TrackerResponseError,
        ) as e:
            return TrackerConnection(connected=False, message=str(e))

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Get metadata about a Jira project.

        Args:
            project_key: Project key in Jira.

        Returns:
            Project metadata.
        """
        # Get project details
        project_data = await self._make_request("GET", f"project/{project_key}")

        # Get available statuses
        statuses_data = await self._make_request(
            "GET", f"project/{project_key}/statuses"
        )

        statuses = []
        for issue_type in statuses_data:
            for status in issue_type.get("statuses", []):
                status_obj = self._map_jira_status(status)
                if status_obj not in statuses:  # Avoid duplicates
                    statuses.append(status_obj)

        # Get available priorities
        priorities_data = await self._make_request("GET", "priority")
        priorities = [self._map_jira_priority(p) for p in priorities_data]

        return ProjectMetadata(
            key=project_key,
            name=project_data.get("name", project_key),
            description=project_data.get("description"),
            statuses=statuses,
            priorities=priorities,
            url=f"{self.base_url}/projects/{project_key}",
        )

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Search for issues in a Jira project.

        Args:
            project_key: Project key in Jira.
            filter_params: Filter parameters.
            limit: Maximum number of issues to return.
            offset: Pagination offset.

        Returns:
            Tuple of (list of issues, total count).
        """
        # Build JQL query
        jql_parts = [f"project = '{project_key}'"]

        if filter_params.query:
            jql_parts.append(
                f"(summary ~ '{filter_params.query}' OR description ~ '{filter_params.query}')"
            )

        if filter_params.status:
            status_clause = " OR ".join(
                [f"status = '{s}'" for s in filter_params.status]
            )
            jql_parts.append(f"({status_clause})")

        if filter_params.labels:
            label_clause = " OR ".join(
                [f"labels = '{label}'" for label in filter_params.labels]
            )
            jql_parts.append(f"({label_clause})")

        if filter_params.created_after:
            jql_parts.append(
                f"created >= '{filter_params.created_after.strftime('%Y-%m-%d')}'"
            )

        if filter_params.created_before:
            jql_parts.append(
                f"created <= '{filter_params.created_before.strftime('%Y-%m-%d')}'"
            )

        if filter_params.updated_after:
            jql_parts.append(
                f"updated >= '{filter_params.updated_after.strftime('%Y-%m-%d')}'"
            )

        if filter_params.updated_before:
            jql_parts.append(
                f"updated <= '{filter_params.updated_before.strftime('%Y-%m-%d')}'"
            )

        if filter_params.assigned_to:
            jql_parts.append(f"assignee = '{filter_params.assigned_to}'")

        if filter_params.reported_by:
            jql_parts.append(f"reporter = '{filter_params.reported_by}'")

        jql_query = " AND ".join(jql_parts)

        # Add sorting
        if filter_params.sort_by:
            direction = "DESC" if filter_params.sort_direction == "desc" else "ASC"
            jql_query += f" ORDER BY {filter_params.sort_by} {direction}"
        else:
            jql_query += " ORDER BY updated DESC"

        # Fields to retrieve
        fields = [
            "summary",
            "description",
            "status",
            "priority",
            "created",
            "updated",
            "resolutiondate",
            "reporter",
            "assignee",
            "labels",
            "components",
            "parent",
            "issuelinks",
            "comment",
        ]

        # Make search request
        search_data = await self._make_request(
            "POST",
            "search",
            json_data={
                "jql": jql_query,
                "startAt": offset,
                "maxResults": limit,
                "fields": fields,
                "expand": ["names", "renderedFields"],
            },
        )

        # Map results
        total = search_data.get("total", 0)
        issues = [
            self._map_jira_issue(issue, project_key)
            for issue in search_data.get("issues", [])
        ]

        return issues, total

    async def get_issue(self, issue_id: str) -> Issue:
        """Get a specific issue by ID or key.

        Args:
            issue_id: Issue ID or key in Jira.

        Returns:
            Issue object.
        """
        try:
            issue_data = await self._make_request(
                "GET", f"issue/{issue_id}", api_version="3"
            )
        except TrackerResponseError as e:
            if "404" in str(e):
                raise TrackerResponseError(f"Issue {issue_id} not found")
            raise

        # Extract project key from issue data or issue key
        fields = issue_data.get("fields", {})
        project_key = fields.get("project", {}).get("key", "")
        if not project_key:
            # Extract from issue key (e.g., "TEST-123" -> "TEST")
            issue_key = issue_data.get("key", "")
            if "-" in issue_key:
                project_key = issue_key.split("-")[0]

        # Use the mapper to convert to Issue object
        return self._map_jira_issue(issue_data, project_key)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Get comments for an issue."""
        try:
            comments_data = await self._make_request(
                "GET", f"issue/{issue_id}/comment", api_version="3"
            )
        except TrackerResponseError as e:
            if "404" in str(e):
                raise TrackerResponseError(f"Issue {issue_id} not found")
            raise

        comments = []
        for comment_data in comments_data.get("comments", []):
            try:
                created_at = datetime.strptime(
                    comment_data.get("created", ""), "%Y-%m-%dT%H:%M:%S.%f%z"
                ).replace(tzinfo=None)
            except (ValueError, TypeError):
                created_at = datetime.now()

            try:
                updated_at = datetime.strptime(
                    comment_data.get("updated", ""), "%Y-%m-%dT%H:%M:%S.%f%z"
                ).replace(tzinfo=None)
            except (ValueError, TypeError):
                updated_at = datetime.now()

            author_data = comment_data.get("author", {})
            if author_data:
                author = IssueUser(
                    id=author_data.get("accountId", ""),
                    name=author_data.get("displayName", ""),
                    avatar_url=author_data.get("avatarUrls", {}).get("48x48", ""),
                )
            else:
                # Create a default IssueUser for anonymous comments
                author = IssueUser(
                    id="",
                    name="Anonymous",
                    avatar_url=None,
                )

            comments.append(
                IssueComment(
                    id=comment_data["id"],
                    body=self._convert_description_to_string(
                        comment_data.get("body", "")
                    ),
                    author=author,
                    created_at=created_at,
                    updated_at=updated_at,
                    url=f"{self.base_url}/browse/{issue_id}?focusedCommentId={comment_data['id']}",
                )
            )

        return comments

    def _convert_description_to_string(self, description) -> str:
        """Convert Jira description to string format.

        Jira can return descriptions in different formats:
        - String (legacy)
        - Dict (Atlassian Document Format - ADF)

        For storage in the database, we convert ADF to plain text
        to avoid nested JSON encoding issues.
        """
        if isinstance(description, dict):
            # Handle Atlassian Document Format (ADF) - convert to plain text
            return self._adf_to_plain_text(description)
        elif isinstance(description, str):
            return description
        elif description is None:
            return ""
        else:
            return str(description)

    def _parse_jira_datetime(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse Jira datetime string to Python datetime.

        Args:
            date_str: Jira datetime string

        Returns:
            Parsed datetime or None
        """
        if not date_str:
            return None

        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            logger.warning(f"Failed to parse Jira datetime: {date_str}")
            return None

    def _adf_to_plain_text(self, adf_node: Optional[Any]) -> str:
        """Converts Jira Atlassian Document Format (ADF) to plain text."""
        if adf_node is None:
            return ""
        # If it's already plain text (e.g., older Jira server or non-ADF field)
        if isinstance(adf_node, str):
            return adf_node
        # If it's not a dict, we can't parse it as ADF, return empty string
        if not isinstance(adf_node, dict):
            logger.warning(
                f"ADF description was not a dict, got {type(adf_node)}. Returning empty string."
            )
            return ""

        texts: List[str] = []

        # Inner function to recursively traverse ADF nodes
        def _recursive_extract(current_node: Dict[str, Any]):
            node_type = current_node.get("type")

            if node_type == "text":
                texts.append(current_node.get("text", ""))
            elif node_type == "hardBreak":
                if not texts or (texts and not texts[-1].endswith("\n")):
                    texts.append("\n")

            if "content" in current_node and isinstance(current_node["content"], list):
                for child_node in current_node["content"]:
                    if isinstance(child_node, dict):
                        _recursive_extract(child_node)

                # Add a newline after certain block elements if content was processed
                # and the last text part doesn't already end with a newline.
                if node_type in [
                    "paragraph",
                    "heading",
                    "listItem",
                    "codeBlock",
                    "blockquote",
                    "rule",
                ]:
                    if (
                        texts
                        and not texts[-1].endswith("\n")
                        and texts[-1].strip() != ""
                    ):
                        texts.append("\n")

        _recursive_extract(adf_node)

        plain_text = "".join(texts)
        plain_text = re.sub(r"\n{3,}", "\n\n", plain_text)  # Collapse 3+ newlines to 2
        plain_text = (
            plain_text.strip()
        )  # Remove leading/trailing whitespace and newlines

        return plain_text

    def _extract_text_from_adf_content(self, adf_node: Dict[str, Any]) -> str:
        """
        Extract raw text content from ADF, used to detect if text is still JSON.

        This is a simpler version that just extracts text nodes without formatting.

        Args:
            adf_node: ADF document node

        Returns:
            Raw text content
        """
        texts = []

        def recursive_extract(node: Dict[str, Any]):
            if not isinstance(node, dict):
                return

            node_type = node.get("type")

            if node_type == "text":
                texts.append(node.get("text", ""))

            if "content" in node and isinstance(node["content"], list):
                for child in node["content"]:
                    recursive_extract(child)

        recursive_extract(adf_node)
        return "".join(texts)

    def _map_jira_status(self, jira_status: Dict[str, Any]) -> IssueStatus:
        """Map Jira status to Preloop status.

        Args:
            jira_status: Jira status object

        Returns:
            Mapped status
        """
        if not jira_status:
            return IssueStatus(id="unknown", name="Unknown", category="other")

        status_id = str(jira_status.get("id", "unknown"))
        status_name = jira_status.get("name", "Unknown")

        # Map status category
        category_key = jira_status.get("statusCategory", {}).get("key", "")
        category_map = {
            "new": "todo",
            "indeterminate": "in_progress",
            "done": "done",
        }
        category = category_map.get(category_key, "other")

        return IssueStatus(id=status_id, name=status_name, category=category)

    def _map_jira_priority(self, jira_priority: Dict[str, Any]) -> IssuePriority:
        """Map Jira priority to Preloop priority.

        Args:
            jira_priority: Jira priority object

        Returns:
            Mapped priority
        """
        priority_id = jira_priority["id"]
        priority_name = jira_priority["name"]

        # Extract numeric level from Jira priority
        # Typically higher number = higher priority in Jira
        level_map = {
            "Highest": 5,
            "High": 4,
            "Medium": 3,
            "Low": 2,
            "Lowest": 1,
        }
        level = level_map.get(priority_name, 3)  # Default to medium priority

        return IssuePriority(id=priority_id, name=priority_name, level=level)

    def _map_jira_user(self, jira_user: Dict[str, Any]) -> IssueUser:
        """Map Jira user to Preloop user.

        Args:
            jira_user: Jira user object

        Returns:
            Mapped user
        """
        user_id = jira_user.get("accountId", "")
        user_name = jira_user.get("displayName", "")
        user_email = jira_user.get("emailAddress")
        user_avatar = jira_user.get("avatarUrls", {}).get("48x48")

        return IssueUser(
            id=user_id,
            name=user_name,
            email=user_email,
            avatar_url=user_avatar,
        )

    def _map_jira_issue(self, jira_issue: Dict[str, Any], project_key: str) -> Issue:
        """Map Jira issue to Preloop issue.

        Args:
            jira_issue: Jira issue object
            project_key: Project key

        Returns:
            Mapped issue
        """
        issue_id = jira_issue["id"]
        issue_key = jira_issue["key"]
        fields = jira_issue["fields"]

        # Core issue data
        title = fields.get("summary", "")
        raw_description = fields.get("description")
        description_text = self._adf_to_plain_text(raw_description)

        # Status and priority
        status = self._map_jira_status(fields.get("status", {}))
        priority = None
        if "priority" in fields and fields["priority"]:
            priority = self._map_jira_priority(fields["priority"])

        # Timeline
        created_at = self._parse_jira_datetime(fields.get("created")) or datetime.now()
        updated_at = self._parse_jira_datetime(fields.get("updated")) or created_at
        resolved_at = self._parse_jira_datetime(fields.get("resolutiondate"))

        # People
        reporter = None
        if "reporter" in fields and fields["reporter"]:
            reporter = self._map_jira_user(fields["reporter"])

        assignee = None
        if "assignee" in fields and fields["assignee"]:
            assignee = self._map_jira_user(fields["assignee"])

        # Labels and components
        labels = fields.get("labels", [])
        components = [c["name"] for c in fields.get("components", [])]

        # Issue relations
        parent = None
        if "parent" in fields:
            parent_data = fields["parent"]
            parent = IssueRelation(
                relation_type="parent",
                issue_id=parent_data["id"],
                issue_key=parent_data["key"],
                summary=parent_data["fields"].get("summary"),
            )

        relations = []
        if "issuelinks" in fields:
            for link in fields["issuelinks"]:
                relation_type = link.get("type", {}).get("name", "relates_to").lower()

                # Jira has inward/outward relations
                if "inwardIssue" in link:
                    related = link["inwardIssue"]
                    relations.append(
                        IssueRelation(
                            relation_type=relation_type,
                            issue_id=related["id"],
                            issue_key=related["key"],
                            summary=related["fields"].get("summary"),
                        )
                    )
                elif "outwardIssue" in link:
                    related = link["outwardIssue"]
                    relations.append(
                        IssueRelation(
                            relation_type=relation_type,
                            issue_id=related["id"],
                            issue_key=related["key"],
                            summary=related["fields"].get("summary"),
                        )
                    )

        # Comments
        comments = []
        if "comment" in fields and "comments" in fields["comment"]:
            for comment_data in fields["comment"]["comments"]:
                comment_id = comment_data["id"]
                comment_body = self._adf_to_plain_text(comment_data.get("body", ""))
                comment_created = (
                    self._parse_jira_datetime(comment_data.get("created"))
                    or datetime.now()
                )
                comment_updated = self._parse_jira_datetime(comment_data.get("updated"))
                comment_author = self._map_jira_user(comment_data.get("author", {}))

                comments.append(
                    IssueComment(
                        id=comment_id,
                        body=comment_body,
                        created_at=comment_created,
                        updated_at=comment_updated,
                        author=comment_author,
                        url=f"{self.base_url}/browse/{issue_key}?focusedCommentId={comment_id}",
                    )
                )

        # URLs
        url = f"{self.base_url}/browse/{issue_key}"
        api_url = f"{self.jira_url}/rest/api/3/issue/{issue_id}"

        # Custom fields
        custom_fields = {}
        for field_key, field_value in fields.items():
            if field_key.startswith("customfield_") and field_value is not None:
                custom_fields[field_key] = field_value

        return Issue(
            id=issue_id,
            key=issue_key,
            title=title,
            description=description_text,
            status=status,
            priority=priority,
            created_at=created_at,
            updated_at=updated_at,
            resolved_at=resolved_at,
            reporter=reporter,
            assignee=assignee,
            labels=labels,
            components=components,
            parent=parent,
            relations=relations,
            comments=comments,
            url=url,
            api_url=api_url,
            tracker_type="jira",
            project_key=project_key,
            custom_fields=custom_fields,
        )

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Create a new Jira issue.

        Args:
            project_key: Project key in Jira.
            issue_data: Issue data.

        Returns:
            Created issue.
        """
        # Map Preloop issue data to Jira fields
        fields = {
            "project": {"key": project_key},
            "summary": issue_data.title,
        }

        # Only add non-empty fields
        if issue_data.description:
            fields["description"] = issue_data.description

        # Set issue type (default to Task if not specified)
        fields["issuetype"] = {"name": "Task"}

        if issue_data.status:
            # Note: Jira doesn't allow setting status directly on creation
            # Status transitions would need to be handled post-creation
            pass

        if issue_data.priority:
            fields["priority"] = {"name": issue_data.priority}

        if issue_data.assignee:
            fields["assignee"] = {"name": issue_data.assignee}

        if issue_data.labels:
            fields["labels"] = issue_data.labels

        if issue_data.components:
            fields["components"] = [{"name": c} for c in issue_data.components]

        if issue_data.parent:
            fields["parent"] = {"key": issue_data.parent}

        # Add custom fields if specified
        if issue_data.custom_fields:
            for field_key, field_value in issue_data.custom_fields.items():
                fields[field_key] = field_value

        # Create the issue
        creation_data = await self._make_request(
            "POST",
            "issue",
            json_data={"fields": fields},
        )

        # Get the created issue
        issue_id = creation_data.get("id")
        if not issue_id:
            raise ValueError("Failed to create Jira issue: no issue ID returned")

        return await self.get_issue(issue_id)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Update an existing Jira issue.

        Args:
            issue_id: Issue ID in Jira. Can be the numeric ID or the issue key.
            issue_data: Updated issue data.

        Returns:
            Updated issue.
        """
        # Map Preloop issue data to Jira fields
        fields = {}

        if issue_data.title is not None:
            fields["summary"] = issue_data.title

        if issue_data.description is not None:
            # Check if description is already in ADF format (JSON string or dict)
            import json

            logger.info(
                f"Processing description update: type={type(issue_data.description).__name__}, "
                f"length={len(str(issue_data.description))}, "
                f"first 200 chars: {str(issue_data.description)[:200]}"
            )

            desc_to_parse = None  # Will hold string to parse recursively, or None if no parsing needed

            if isinstance(issue_data.description, dict):
                # Already a dict, but check if text content is nested JSON
                text_content = self._extract_text_from_adf_content(
                    issue_data.description
                )
                logger.debug(
                    f"Description is dict (ADF), extracted text length: {len(text_content)}, first 100 chars: {text_content[:100]}"
                )

                # Check if extracted text looks like JSON
                if text_content and (
                    text_content.startswith("{") or "\\" in text_content
                ):
                    # Text content appears to be JSON, need to parse recursively
                    logger.info(
                        "Description dict contains JSON text, will parse the extracted text"
                    )
                    # Use the extracted text content for recursive parsing, not the whole dict
                    desc_to_parse = text_content
                else:
                    # Clean ADF with plain text, use as-is
                    fields["description"] = issue_data.description
                    logger.debug("Description is clean ADF, using as-is")
            elif isinstance(issue_data.description, str):
                # String input, needs recursive parsing
                desc_to_parse = issue_data.description

            # Recursive parsing logic for strings (and dicts converted to strings)
            if desc_to_parse is not None:
                # Try to parse as JSON first (might already be ADF)
                # Handle multiple levels of JSON encoding by recursively parsing
                desc = desc_to_parse
                max_parse_attempts = 10  # Increased to handle deeply nested JSON
                parsed_desc = None

                logger.info(
                    f"Attempting to parse description (length: {len(desc)}), first 200 chars: {desc[:200]}"
                )

                for attempt in range(max_parse_attempts):
                    try:
                        # Use JSONDecoder to handle cases where desc has extra text after JSON
                        decoder = json.JSONDecoder()
                        parsed, json_end_index = decoder.raw_decode(desc)
                        logger.info(
                            f"Parse attempt {attempt + 1}: parsed type={type(parsed).__name__}, "
                            f"JSON ends at char {json_end_index}/{len(desc)}, first 100 chars: {str(parsed)[:100]}"
                        )

                        if isinstance(parsed, dict):
                            # Successfully parsed to a dict, check if it's ADF
                            if parsed.get("type") == "doc":
                                # Check if the text content inside is still JSON
                                # Extract text from the ADF structure
                                text_content = self._extract_text_from_adf_content(
                                    parsed
                                )
                                logger.info(
                                    f"Extracted text from ADF (length: {len(text_content)}), first 100 chars: {text_content[:100]}"
                                )

                                # Check if extracted text is still JSON
                                if text_content and (
                                    text_content.startswith("{") or "\\" in text_content
                                ):
                                    # Text content looks like JSON, try to parse it
                                    logger.info(
                                        "Text content appears to be JSON, attempting to parse it"
                                    )
                                    try:
                                        # Try to parse the extracted text as JSON
                                        # Use JSONDecoder to find where valid JSON ends
                                        decoder = json.JSONDecoder()
                                        nested_parsed, json_end_index = (
                                            decoder.raw_decode(text_content)
                                        )

                                        # Successfully parsed the JSON part
                                        # Continue parsing by using the extracted JSON
                                        desc = json.dumps(nested_parsed)
                                        logger.info(
                                            f"Successfully extracted nested JSON (ends at char {json_end_index}), continuing (new desc length: {len(desc)})"
                                        )
                                        continue
                                    except (
                                        json.JSONDecodeError,
                                        ValueError,
                                    ) as nested_e:
                                        # The text content looks like JSON but isn't valid JSON
                                        # This means it's probably escaped/malformed
                                        # Treat the whole text_content as plain text
                                        logger.warning(
                                            f"Text content looks like JSON but failed to parse: {nested_e}. "
                                            f"Converting to plain text."
                                        )
                                        # Use the plain text version (the text_content, not the whole desc)
                                        parsed_desc = {
                                            "type": "doc",
                                            "version": 1,
                                            "content": [
                                                {
                                                    "type": "paragraph",
                                                    "content": [
                                                        {
                                                            "type": "text",
                                                            "text": text_content,
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                        break
                                else:
                                    # Real ADF with plain text content
                                    # Check if the text is just test suffixes (artifacts from integration tests)
                                    import re

                                    test_suffix_pattern = (
                                        r"^[\s]*(test_[a-f0-9]+[\s]*)+$"
                                    )
                                    if re.match(test_suffix_pattern, text_content):
                                        logger.warning(
                                            f"After unwrapping, description contains only test suffixes: '{text_content}'. "
                                            "Treating as empty description."
                                        )
                                        # Create ADF with empty text
                                        parsed_desc = {
                                            "type": "doc",
                                            "version": 1,
                                            "content": [
                                                {
                                                    "type": "paragraph",
                                                    "content": [],
                                                }
                                            ],
                                        }
                                    else:
                                        parsed_desc = parsed
                                        logger.info(
                                            f"Successfully parsed description to ADF format after {attempt + 1} attempts"
                                        )
                                    break
                            else:
                                # Dict but not ADF, treat current desc as plain text
                                logger.debug(
                                    f"Parsed to dict but not ADF (type={parsed.get('type')}), using as plain text"
                                )
                                break
                        elif isinstance(parsed, str):
                            # Still a string after parsing, try parsing again
                            desc = parsed
                            logger.debug(
                                f"Still a string after parsing, continuing (new length: {len(desc)})"
                            )
                            continue
                        else:
                            # Some other type, stop parsing
                            logger.debug(
                                f"Parsed to unexpected type {type(parsed).__name__}, stopping"
                            )
                            break
                    except (json.JSONDecodeError, ValueError) as e:
                        # Not JSON, stop parsing
                        logger.info(f"JSON parse failed at attempt {attempt + 1}: {e}")
                        logger.info(
                            f"Failed to parse desc (length {len(desc)}): {desc[:500]}"
                        )
                        break

                if parsed_desc and isinstance(parsed_desc, dict):
                    # Successfully parsed to ADF format
                    fields["description"] = parsed_desc
                else:
                    # Not ADF or failed to parse, wrap as plain text
                    # Use the last successfully parsed string or original
                    logger.info(
                        f"Using description as plain text (final length: {len(desc)})"
                    )
                    fields["description"] = {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": desc}],
                            }
                        ],
                    }

        if issue_data.priority is not None:
            fields["priority"] = {"name": issue_data.priority}

        if issue_data.assignee is not None:
            if issue_data.assignee == "":
                # Unassign the issue
                fields["assignee"] = None
            else:
                fields["assignee"] = {"name": issue_data.assignee}

        if issue_data.labels is not None:
            fields["labels"] = issue_data.labels

        if issue_data.components is not None:
            fields["components"] = [{"name": c} for c in issue_data.components]

        # Add custom fields if specified
        if issue_data.custom_fields:
            for field_key, field_value in issue_data.custom_fields.items():
                fields[field_key] = field_value

        # Update the issue
        # Note: Jira API v2 uses plain text for descriptions, not ADF
        # Convert ADF description to plain text if present
        if "description" in fields and isinstance(fields["description"], dict):
            fields["description"] = self._adf_to_plain_text(fields["description"])

        logger.info(f"Sending update request for {issue_id} with fields: {fields}")
        await self._make_request(
            "PUT",
            f"issue/{issue_id}",
            json_data={"fields": fields},
        )

        # Handle status transitions separately if needed
        if issue_data.status is not None:
            # Get available transitions
            transitions = await self._make_request(
                "GET", f"issue/{issue_id}/transitions"
            )

            # Find matching transition
            for transition in transitions.get("transitions", []):
                if transition.get("to", {}).get("name", "") == issue_data.status:
                    # Execute the transition
                    await self._make_request(
                        "POST",
                        f"issue/{issue_id}/transitions",
                        json_data={"transition": {"id": transition["id"]}},
                    )
                    break

        # Get the updated issue
        return await self.get_issue(issue_id)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Add a comment to a Jira issue.

        Args:
            issue_id: Issue ID in Jira. Can be the numeric ID or the issue key.
            comment: Comment text.

        Returns:
            Created comment.
        """
        # Format comment in Jira Atlassian Document Format
        comment_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": comment}],
                    }
                ],
            }
        }

        # Add the comment
        comment_data = await self._make_request(
            "POST",
            f"issue/{issue_id}/comment",
            json_data=comment_body,
        )

        # Extract comment details
        comment_id = comment_data.get("id", "")
        comment_body_text = comment_data.get("body", "")

        if isinstance(comment_body_text, dict):
            # Extract text from Atlassian Document Format
            comment_body_text = self._adf_to_plain_text(comment_body_text)

        comment_created = (
            self._parse_jira_datetime(comment_data.get("created")) or datetime.now()
        )
        comment_updated = self._parse_jira_datetime(comment_data.get("updated"))

        # Map the author
        author = self._map_jira_user(comment_data.get("author", {}))

        return IssueComment(
            id=comment_id,
            body=comment_body_text,
            created_at=comment_created,
            updated_at=comment_updated,
            author=author,
            url=f"{self.base_url}/browse/{issue_id}?focusedCommentId={comment_id}",
        )

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Add a relation between Jira issues.

        Args:
            issue_id: Source issue ID.
            related_issue_id: Target issue ID.
            relation_type: Relation type.

        Returns:
            Whether the operation was successful.
        """
        # Map Preloop relation type to Jira link type
        relation_map = {
            "blocks": "Blocks",
            "blocked_by": "Blocked by",
            "relates_to": "Relates",
            "duplicates": "Duplicates",
            "duplicated_by": "Duplicated by",
        }

        # Get the Jira link type or default to "Relates"
        link_type = relation_map.get(relation_type, "Relates")

        # Build the request body
        link_data = {
            "type": {"name": link_type},
            "inwardIssue": {"key": related_issue_id},
            "outwardIssue": {"key": issue_id},
        }

        # Create the link
        try:
            await self._make_request(
                "POST",
                "issueLink",
                json_data=link_data,
            )
            return True
        except Exception as e:
            logger.exception(f"Failed to add relation: {e}")
            return False

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """Get organizations from Jira."""
        import re

        domain_match = re.search(r"https?://([^/]+)", self.jira_url)
        org_name = domain_match.group(1) if domain_match else "Jira Instance"
        return [{"id": org_name, "name": org_name, "url": self.jira_url}]

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """Get projects from Jira."""
        projects_data = await self._make_request("GET", "project")
        projects = []
        for project in projects_data:
            projects.append(
                {
                    "id": project["id"],
                    "key": project["key"],  # Add the project key explicitly
                    "identifier": project["key"],
                    "name": project["name"],
                    "description": project.get("description", ""),
                    "url": f"{self.jira_url}/projects/{project['key']}",
                }
            )
        return projects

    def transform_project(
        self, proj_data: Dict[str, Any], organization_id: str
    ) -> Dict[str, Any]:
        """
        Transform Jira project data to database format.

        For Jira:
        - identifier: The numeric project ID (for internal use)
        - slug: The project key (used in webhooks and URLs, e.g., "SCRUM")
        """
        return {
            "organization_id": organization_id,
            "identifier": proj_data["id"],  # Numeric ID like "10000"
            "slug": proj_data.get("key", ""),  # Project key like "SCRUM"
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
        Transform Jira issue data to database format.

        Jira uses different field names than the base class expects:
        - "summary" instead of "title"
        - "status.name" instead of "status"
        """
        # Extract status - handle both webhook format and API format
        status = issue_data.get("status")
        if isinstance(status, dict):
            status_name = status.get("name", "")
        elif isinstance(status, IssueStatus):
            status_name = status.name
        else:
            status_name = status or ""

        # If status_name is still empty, try to get from fields
        if not status_name:
            fields_status = issue_data.get("fields", {}).get("status")
            if isinstance(fields_status, dict):
                status_name = fields_status.get("name", "")
            elif isinstance(fields_status, IssueStatus):
                status_name = fields_status.name

            if not status_name:
                logger.warning(
                    f"Could not extract status for Jira issue {issue_data.get('key', 'unknown')}. "
                    f"Top-level status: {issue_data.get('status')}, "
                    f"Fields status: {fields_status}"
                )

        # Extract timestamps
        created_at = None
        if "created" in issue_data:
            try:
                created_at = datetime.strptime(
                    issue_data["created"], "%Y-%m-%dT%H:%M:%S.%f%z"
                )
            except (ValueError, TypeError):
                pass

        updated_at = None
        if "updated" in issue_data:
            try:
                updated_at = datetime.strptime(
                    issue_data["updated"], "%Y-%m-%dT%H:%M:%S.%f%z"
                )
            except (ValueError, TypeError):
                pass

        # Get fields from either top level (webhook) or nested in fields (API)
        fields = issue_data.get("fields", issue_data)

        # Extract external_id - handle both webhook and API formats
        external_id = issue_data.get("id")
        if not external_id:
            logger.warning(
                f"Missing 'id' field in issue_data for key {issue_data.get('key')}. "
                f"Available keys: {list(issue_data.keys())}"
            )

        # Extract labels and assignees for meta_data
        labels = fields.get("labels", [])
        assignees = (
            [fields["assignee"]["displayName"]] if fields.get("assignee") else []
        )
        issue_url = f"{self.jira_url}/browse/{issue_data.get('key', '')}"

        return {
            "project_id": project.id,
            "external_id": str(external_id) if external_id else "",
            "key": issue_data.get("key", ""),
            "title": fields.get("summary", ""),  # Jira uses "summary" not "title"
            "description": self._convert_description_to_string(
                fields.get("description", "")
            ),
            "status": status_name,
            "created_at": created_at or datetime.now(),
            "updated_at": updated_at or datetime.now(),
            "last_updated_external": updated_at or datetime.now(),
            "last_synced": datetime.now(),
            "external_url": issue_url,
            "meta_data": {
                "labels": labels,
                "assignees": assignees,
                "url": issue_url,
                "source": "preloop-sync",
            },
            "tracker_id": self.tracker_id,
        }

    async def get_issues(
        self, organization_id: str, project_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Get issues for a project from Jira using the new JQL API.

        Uses the new /search/jql endpoint as per Jira API migration requirements.
        """
        jql = f"project = {project_id}"
        if since:
            jql += f" AND updated >= '{since.strftime('%Y-%m-%d %H:%M')}'"

        # Use the new search/jql endpoint with proper JSON payload
        payload = {
            "jql": jql,
            "maxResults": JIRA_DEFAULT_PAGE_SIZE,
            "fields": [
                # Note: "id" and "key" are top-level fields, not in "fields" object
                "summary",
                "description",
                "status",
                "created",
                "updated",
                "labels",
                "assignee",
                "issuetype",
                "comment",
                "issuelinks",
            ],
        }

        all_issues = []
        next_page_token = None

        while True:
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            try:
                # Use the new API v3 search/jql endpoint
                issues_response = await self._make_request(
                    "POST", "search/jql", json_data=payload, api_version="3"
                )
            except TrackerResponseError as e:
                logger.error(f"Failed to get Jira issues for project {project_id}: {e}")
                break

            issues_data = issues_response.get("issues", [])
            if not issues_data:
                break

            # Process issues - return raw format with comments and dependencies added
            for issue_data in issues_data:
                # Log the structure to debug missing external_id/title
                logger.debug(
                    f"Processing issue from get_issues: "
                    f"top-level keys={list(issue_data.keys())}, "
                    f"id={issue_data.get('id')}, "
                    f"key={issue_data.get('key')}, "
                    f"fields.summary={issue_data.get('fields', {}).get('summary')}"
                )

                fields = issue_data.get("fields", {})

                # Process comments (scanner expects these at top level)
                comments_data = []
                if fields.get("comment", {}).get("comments"):
                    comments_list = fields["comment"]["comments"]
                    for comment_item in comments_list:
                        comment_url = f"{self.jira_url}/browse/{issue_data['key']}?focusedCommentId={comment_item['id']}"
                        comments_data.append(
                            {
                                "id": str(comment_item["id"]),
                                "body": self._convert_description_to_string(
                                    comment_item.get("body", "")
                                ),
                                "author": comment_item["author"]["displayName"]
                                if comment_item.get("author")
                                else None,
                                "created_at": datetime.strptime(
                                    comment_item["created"], "%Y-%m-%dT%H:%M:%S.%f%z"
                                )
                                if comment_item.get("created")
                                else None,
                                "updated_at": datetime.strptime(
                                    comment_item["updated"], "%Y-%m-%dT%H:%M:%S.%f%z"
                                )
                                if comment_item.get("updated")
                                else None,
                                "url": comment_url,
                            }
                        )
                issue_data["comments"] = comments_data

                # Parse dependencies from issue links (scanner expects these at top level)
                if fields.get("issuelinks"):
                    issue_data["dependencies"] = await self._parse_dependencies(
                        fields["issuelinks"]
                    )
                else:
                    issue_data["dependencies"] = []

                # Return raw issue_data in the same format as the API returns it
                # This allows transform_issue() to process it consistently
                all_issues.append(issue_data)

            # Check for next page
            next_page_token = issues_response.get("nextPageToken")
            if not next_page_token:
                break

        return all_issues

    async def _parse_dependencies(
        self, issuelinks: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """Parse Jira issue links into dependencies."""
        dependencies = []
        for link in issuelinks:
            try:
                # Handle both inward and outward links
                if "outwardIssue" in link:
                    target_key = link["outwardIssue"]["key"]
                    relationship_type = link["type"]["outward"]
                elif "inwardIssue" in link:
                    target_key = link["inwardIssue"]["key"]
                    relationship_type = link["type"]["inward"]
                else:
                    continue

                dependencies.append(
                    {
                        "target_key": target_key,
                        "type": relationship_type,
                    }
                )
            except (KeyError, TypeError) as e:
                logger.warning(f"Could not parse Jira issue link: {e}")
                continue
        return dependencies

    def register_webhook(
        self,
        db: Optional[Session] = None,
        project: Optional[Project] = None,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        events: Optional[List[str]] = None,
    ) -> bool:
        """Register a webhook for the Jira project."""
        if not self.jira_client:
            return False
        if db is None or project is None or not webhook_url or not secret:
            logger.error(
                "Jira webhook registration requires db, project, URL, and secret."
            )
            return False

        existing_webhook = crud_webhook.get_by_project_id(db, project_id=project.id)
        if existing_webhook:
            return True

        actual_events = events or DEFAULT_JIRA_WEBHOOK_EVENTS
        webhook_name = f"Preloop Sync for {project.identifier}"

        parsed_url = urllib.parse.urlparse(webhook_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        query_params["project_key"] = [project.identifier]
        new_query_string = urllib.parse.urlencode(query_params, doseq=True)
        url_with_secret_and_project = parsed_url._replace(
            query=new_query_string
        ).geturl()

        jql_filter = f"project = {project.identifier.upper()}"

        try:
            logger.info(
                f"Registering webhook for project {project.identifier} in Jira. "
                f"Webhook name: {webhook_name}, URL: {url_with_secret_and_project}, "
                f"JQL: {jql_filter}"
            )

            # Prepare the webhook payload
            # Jira Cloud supports HMAC-SHA256 webhook signing with the 'secret' field
            webhook_payload = {
                "name": webhook_name,
                "url": url_with_secret_and_project,
                "events": actual_events,
                "jqlFilter": jql_filter,
                "excludeIssueDetails": False,
                "secret": secret,  # Jira will sign webhooks with this secret using HMAC-SHA256
            }

            logger.debug(
                f"Webhook payload (secret redacted): {webhook_payload.copy() | {'secret': '***'}}"
            )

            response = self.jira_client._session.post(
                f"{self.jira_url}/rest/webhooks/1.0/webhook",
                json=webhook_payload,
            )

            # Log the response details for debugging
            logger.info(
                f"Webhook registration response status: {response.status_code}, "
                f"Response text: {response.text[:500]}"
            )

            response.raise_for_status()
            webhook_data = response.json()

            # Extract webhook ID - Jira API returns it in the 'self' URL, not as an 'id' field
            # The 'self' URL looks like: https://example.atlassian.net/rest/webhooks/1.0/webhook/123
            webhook_id = webhook_data.get(
                "id"
            )  # Try id first (for tests/older API versions)
            if not webhook_id and "self" in webhook_data:
                # Extract from self URL (actual Jira Cloud API behavior)
                self_url = webhook_data["self"]
                webhook_id = self_url.split("/")[-1]

            if not webhook_id:
                logger.error(
                    f"Could not extract webhook ID from response. "
                    f"Response: {webhook_data}"
                )
                return False

            webhook_id = str(webhook_id)  # Ensure it's a string

            logger.info(
                f"Webhook created in Jira with ID: {webhook_id}. "
                f"Full response: {webhook_data}"
            )

            # Store webhook in database
            crud_webhook.create(
                db,
                obj_in={
                    "project_id": project.id,
                    "external_id": webhook_id,
                    "url": url_with_secret_and_project,
                    "secret": secret,
                    "events": actual_events,
                },
            )

            # Verify the webhook was actually created by fetching it back (with retries)
            max_retries = 3
            verified = False
            for attempt in range(max_retries):
                try:
                    import time

                    if attempt > 0:
                        # Wait a bit before retrying
                        time.sleep(2**attempt)  # Exponential backoff: 2, 4, 8 seconds

                    verify_response = self.jira_client._session.get(
                        f"{self.jira_url}/rest/webhooks/1.0/webhook/{webhook_id}"
                    )
                    if verify_response.status_code == 200:
                        logger.info(
                            f"Verified: Webhook {webhook_id} exists in Jira for project {project.identifier} (attempt {attempt + 1}/{max_retries})."
                        )
                        verified = True
                        break
                    else:
                        logger.warning(
                            f"Could not verify webhook {webhook_id} exists in Jira (attempt {attempt + 1}/{max_retries}). "
                            f"Status: {verify_response.status_code}, Response: {verify_response.text[:200]}"
                        )
                except Exception as verify_error:
                    logger.warning(
                        f"Error verifying webhook {webhook_id} in Jira (attempt {attempt + 1}/{max_retries}): {verify_error}"
                    )

            # If verification failed after all retries, notify admins (but not in test/dev environments)
            if not verified:
                error_msg = (
                    f"Failed to verify webhook {webhook_id} for project {project.identifier} "
                    f"after {max_retries} attempts. The webhook may not be properly registered in Jira."
                )
                logger.error(error_msg)

                # Only send notifications in production environments
                # Skip notifications if:
                # - Running tests (pytest detected)
                # - Development environment (localhost URLs)
                # - Test project identifiers
                import sys

                is_test_env = (
                    "pytest" in sys.modules
                    or "localhost" in self.jira_url.lower()
                    or project.identifier.upper() == "TEST"
                )

                if not is_test_env:
                    try:
                        from preloop.sync.tasks import notify_admins

                        notify_admins(
                            subject=f"Jira Webhook Verification Failed: {project.identifier}",
                            message=error_msg,
                        )
                    except Exception as notify_error:
                        logger.error(
                            f"Failed to send admin notification: {notify_error}",
                            exc_info=True,
                        )
                else:
                    logger.warning(
                        f"Skipping admin notification in test/dev environment for project {project.identifier}"
                    )

            logger.info(
                f"Successfully registered webhook {webhook_id} for project {project.identifier} "
                f"and stored in database."
            )
            return True
        except JIRAError as e:
            if (
                e.status_code == 400
                and "webhook with same name and url already exists" in e.text.lower()
            ):
                logger.warning(
                    f"Webhook for project {project.identifier} already exists in Jira. Assuming it's ours."
                )
                return True
            self._handle_jira_error(
                e, f"registering webhook for project {project.identifier}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error registering webhook for {project.identifier}: {e}",
                exc_info=True,
            )
            raise TrackerConnectionError(
                f"Unexpected error registering webhook for {project.identifier}: {str(e)}"
            )

    def unregister_webhook(
        self, db: Optional[Session] = None, webhook: Optional[Webhook] = None
    ) -> bool:
        """Unregister a webhook for a project using the database record."""
        if not self.jira_client:
            logger.error("Jira client not initialized. Cannot unregister webhook.")
            return False
        if db is None or webhook is None:
            logger.error("Jira webhook unregistration requires db and webhook.")
            return False

        try:
            logger.info(
                f"Attempting to unregister webhook with external ID {webhook.external_id}."
            )
            self.jira_client._session.delete(
                f"{self.jira_url}/rest/webhooks/1.0/webhook/{webhook.external_id}"
            )
            logger.info(
                f"Successfully unregistered webhook {webhook.external_id} from Jira."
            )
        except JIRAError as e:
            if e.status_code == HTTP_STATUS_NOT_FOUND:
                logger.warning(
                    f"Webhook {webhook.external_id} not found in Jira. Assuming already deleted."
                )
            else:
                self._handle_jira_error(
                    e, f"unregistering webhook {webhook.external_id}"
                )
                return False

        crud_webhook.remove(db, id=webhook.id)
        logger.info(
            f"Removed webhook record for project_id {webhook.project_id} from database."
        )
        return True

    def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """Unregister all webhooks for all projects in an organization."""
        results = {"unregistered": 0, "failed": 0, "not_found": 0}
        logger.info(f"Unregistering all webhooks for Jira tracker {self.tracker_id}.")

        organizations = crud_organization.get_for_tracker(
            db, tracker_id=self.tracker_id
        )
        if not organizations:
            logger.warning(
                f"No organizations found for tracker {self.tracker_id}. No webhooks to unregister."
            )
            return results

        organization_id = organizations[0].id
        projects = crud_project.get_for_organization(
            db, organization_id=organization_id
        )

        if not projects:
            logger.info(
                f"No projects found for organization {organization_id}. No webhooks to unregister."
            )
            return results

        logger.info(
            f"Starting unregistration of all webhooks for organization {organization_id}..."
        )
        for proj in projects:
            try:
                webhook = crud_webhook.get_by_project_id(db, project_id=proj.id)
                if not webhook:
                    logger.warning(
                        f"No webhook found for project {proj.name} ({proj.identifier}). Skipping."
                    )
                    continue
                logger.info(
                    f"Unregistering webhook for project: {proj.name} ({proj.identifier})"
                )
                if self.unregister_webhook(db, webhook=webhook):
                    results["unregistered"] += 1
                else:
                    results["not_found"] += 1
            except Exception as e:
                logger.error(
                    f"Failed to unregister webhook for project {proj.identifier}: {e}",
                    exc_info=True,
                )
                results["failed"] += 1
        logger.info(
            f"Finished unregistering webhooks for organization {organization_id}."
        )
        logger.info(f"Jira unregister_all_webhooks summary: {results}")
        return results

    def cleanup_stale_webhooks(self, preloop_url: str) -> Dict[str, int]:
        """
        Deletes stale webhooks from Jira.

        Stale webhooks are webhooks that:
        1. Have a URL starting with preloop_url (they point to our Preloop instance)
        2. Are NOT registered in our database (they were created but not tracked, or orphaned)

        Args:
            preloop_url: The base URL of the Preloop instance whose stale webhooks should be removed.

        Returns:
            A dictionary with counts of unregistered and failed deletions.
            Example: {"unregistered": 5, "failed": 1}
        """
        if not self.jira_client:
            logger.error("Jira client not initialized. Cannot clean up webhooks.")
            return {"unregistered": 0, "failed": 0}

        logger.info(f"Starting cleanup of stale webhooks for URL: {preloop_url}")
        results = {"unregistered": 0, "failed": 0}

        try:
            response = self.jira_client._session.get(
                f"{self.jira_url}/rest/webhooks/1.0/webhook"
            )
            response.raise_for_status()
            all_webhooks = response.json()
        except (JIRAError, requests.RequestException) as e:
            text = getattr(e, "text", str(e))
            logger.error(f"Failed to retrieve webhooks from Jira: {text}")
            if isinstance(e, JIRAError):
                self._handle_jira_error(e, "retrieving webhooks for cleanup")
            results["failed"] = 1
            return results

        # Filter webhooks that point to our Preloop instance
        preloop_webhooks = [
            hook for hook in all_webhooks if hook.get("url", "").startswith(preloop_url)
        ]

        if not preloop_webhooks:
            logger.info("No webhooks pointing to Preloop URL found.")
            return results

        logger.info(
            f"Found {len(preloop_webhooks)} webhooks pointing to Preloop. "
            f"Checking which are stale (not in database)..."
        )

        # Import database utilities
        from preloop.models.crud import crud_webhook
        from preloop.models.db.session import get_db_session

        for webhook in preloop_webhooks:
            webhook_id = str(webhook.get("id"))
            webhook_url = webhook.get("url", "")

            # Check if this webhook exists in our database
            db = next(get_db_session())
            try:
                existing_webhook = crud_webhook.get_by_external_id(
                    db, external_id=webhook_id, tracker_id=self.tracker_id
                )

                if existing_webhook:
                    # Webhook is in our database, keep it
                    logger.debug(
                        f"Webhook {webhook_id} is registered in database, keeping it."
                    )
                    continue

                # Webhook points to our Preloop but is NOT in database - it's stale
                logger.info(
                    f"Found stale webhook {webhook_id} pointing to {webhook_url}. "
                    f"This webhook is not in our database. Deleting..."
                )
                try:
                    url = f"{self.jira_url}/rest/webhooks/1.0/webhook/{webhook_id}"
                    response = self.jira_client._session.delete(url)
                    response.raise_for_status()
                    logger.info(f"Successfully deleted stale webhook ID: {webhook_id}")
                    results["unregistered"] += 1
                except (JIRAError, requests.RequestException) as e:
                    text = getattr(e, "text", str(e))
                    logger.error(
                        f"Failed to delete stale webhook ID {webhook_id}: {text}"
                    )
                    results["failed"] += 1
                except Exception as e:
                    logger.error(
                        f"An unexpected error occurred while deleting webhook ID {webhook_id}: {e}",
                        exc_info=True,
                    )
                    results["failed"] += 1
            finally:
                db.close()

        logger.info(
            f"Webhook cleanup summary: {results['unregistered']} unregistered, {results['failed']} failed."
        )
        return results

    def is_webhook_registered(self, webhook: "Webhook") -> bool:
        """
        Check if a webhook is registered in the tracker.

        Args:
            webhook: The webhook to check.

        Returns:
            Whether the webhook is registered.
        """
        if not self.jira_client:
            logger.error("Jira client not initialized. Cannot check webhook.")
            return False

        if not webhook.external_id:
            return False

        try:
            all_webhooks = self.get_webhooks()
            for wh in all_webhooks:
                if str(wh.get("id")) == webhook.external_id:
                    return True
            return False
        except (TrackerConnectionError, TrackerResponseError) as e:
            logger.error(
                f"Failed to check webhook {webhook.external_id} due to API error: {e}"
            )
            return False

    def get_webhooks(
        self, organization_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all webhooks for the tracker.

        Returns:
            A list of webhooks.
        """
        if not self.jira_client:
            logger.error("Jira client not initialized. Cannot get webhooks.")
            return []

        try:
            response = self.jira_client._session.get(
                f"{self.jira_url}/rest/webhooks/1.0/webhook"
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise TrackerConnectionError(
                f"Jira connection error while getting webhooks: {str(e)}"
            )
        except JIRAError as e:
            self._handle_jira_error(e, "getting webhooks")
            return []

    def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """
        Delete a webhook from the tracker.

        Args:
            webhook: The webhook to delete.

        Returns:
            Whether the webhook was deleted successfully.
        """
        if not self.jira_client:
            logger.error("Jira client not initialized. Cannot delete webhook.")
            return False

        webhook_id = webhook.get("id")
        if not webhook_id:
            return False

        try:
            self._make_request("DELETE", f"/rest/webhooks/1.0/webhook/{webhook_id}")
            return True
        except TrackerResponseError as e:
            if str(HTTP_STATUS_NOT_FOUND) in str(e):
                logger.warning(
                    f"Webhook {webhook_id} not found in Jira, considering it deleted."
                )
                return True
            logger.error(f"Failed to delete webhook {webhook_id}: {e}")
            return False

    def is_webhook_registered_for_project(
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
        if not self.jira_client:
            return False

        try:
            hooks = self.get_webhooks()
            for hook in hooks:
                if hook.get("url") == webhook_url:
                    # Check if the hook is for the correct project
                    jql = hook.get("jqlFilter", "")
                    if f"project = {project.identifier.upper()}" in jql:
                        return True
            return False
        except (TrackerConnectionError, TrackerResponseError):
            return False

    def is_webhook_registered_for_organization(
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
        # Jira webhooks are not registered at the organization level, but at the project level.
        # This method will return False to indicate that organization-level webhooks are not supported.
        return False
