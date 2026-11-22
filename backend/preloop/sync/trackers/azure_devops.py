"""Azure DevOps Services (Azure Repos) tracker.

An Azure DevOps tracker is bound to one organization and, optionally, one
project and one repository. Each git repository becomes a Preloop project
(``slug`` ``org/project/repo``, ``identifier`` the repository GUID).

Azure Boards work items are the tracker's issues (key ``org/project#id``).
A work item belongs to an Azure DevOps project, not a repository, so each
project's work items attach to one of its repositories' Preloop projects,
chosen by :func:`preloop.utils.azure_devops_work_items.work_item_home`.

Authentication is a personal access token sent as the password of HTTP
Basic auth with an empty user name. Every request goes to
``https://dev.azure.com``; pagination continuation tokens are passed back as
query parameters, never followed as URLs, so the token cannot leave that
origin. Redirects are not followed.

The client never completes (merges) or abandons a pull request.

Service hook subscriptions are managed through the hooks API
(``/_apis/hooks/subscriptions``), see :meth:`ensure_service_hooks`; the
scanner does not register them automatically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.webhook import Webhook
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
from preloop.services.tracker_probe import probe_httpx_timeout
from preloop.utils.azure_devops import (
    AZURE_DEVOPS_API_VERSION,
    AZURE_DEVOPS_BASE_URL,
    AZURE_DEVOPS_WEBHOOK_EVENTS,
    PULL_REQUEST_WEBHOOK_EVENTS,
    WEBHOOK_SECRET_HEADER,
    branch_from_ref,
    clone_url,
    is_guid,
    pull_request_web_url,
    repository_full_name,
    user_name,
)
from preloop.utils.azure_devops_html import markdown_to_html
from preloop.utils.azure_devops_work_items import (
    ARTIFACT_LINK,
    COMMENTS_API_VERSION,
    DEFAULT_WORK_ITEM_TYPES,
    FIELD_PROJECT,
    FIELD_STATE,
    FIELD_TYPE,
    PULL_REQUEST_ARTIFACT_NAME,
    RELATION_LINK_TYPES,
    WORK_ITEMS_BATCH_LIMIT,
    build_wiql,
    comment_summary,
    parse_datetime,
    parse_priority,
    parse_work_item_id,
    parse_work_item_url,
    patch_ops,
    pull_request_artifact_uri,
    states_snapshot,
    work_item_home,
    work_item_summary,
    work_item_web_url,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerPermissionError,
    TrackerRateLimitError,
    TrackerResponseError,
)
from .base import BaseTracker
from .utils import parse_retry_after

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30.0
MAX_PAGES = 50
PAGE_SIZE = 100
CONTINUATION_HEADER = "x-ms-continuationtoken"
# GitPullRequestCommentThread status values
# (https://learn.microsoft.com/rest/api/azure/devops/git/pull-request-threads).
THREAD_ACTIVE = "active"
THREAD_FIXED = "fixed"
# Reviewer votes (https://learn.microsoft.com/rest/api/azure/devops/git/
# pull-request-reviewers): 10 approved, -5 waiting for author, 0 no vote.
VOTE_APPROVE = 10
VOTE_WAIT_FOR_AUTHOR = -5
VOTE_NONE = 0


class AzureDevOpsTracker(BaseTracker):
    """Azure DevOps Services client for repositories and pull requests."""

    tracker_type = "azure_devops"
    hosts_repositories: bool = True

    def __init__(
        self,
        tracker_id: str,
        api_key: str,
        connection_details: Dict[str, Any],
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """Initialize the client.

        Args:
            tracker_id: ID of the tracker in the database.
            api_key: Personal access token.
            connection_details: ``organization`` (required), ``project`` and
                ``repository`` (optional) and, for bound clients,
                ``repo_full_name`` (``org/project/repo``).
            transport: Optional httpx transport, for tests.
        """
        super().__init__(tracker_id, api_key, connection_details or {})
        details = self.connection_details
        self.organization: str = str(details.get("organization") or "").strip()
        self.project: Optional[str] = (
            str(details.get("project")).strip() if details.get("project") else None
        )
        self.repository: Optional[str] = (
            str(details.get("repository")).strip()
            if details.get("repository")
            else None
        )
        repo_full_name = details.get("repo_full_name")
        if not repo_full_name and self.organization and self.project:
            if self.repository:
                repo_full_name = repository_full_name(
                    self.organization, self.project, self.repository
                )
        self.repo_full_name: Optional[str] = repo_full_name or None
        self._transport = transport
        self._identity_id: Optional[str] = None
        self._identity_unique_name: Optional[str] = None
        self._states: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
        self._type_names: Dict[str, List[str]] = {}
        self._repos: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Resolve a path below the organization.

        Args:
            path: Path after ``https://dev.azure.com/{org}/``.

        Returns:
            The absolute URL.
        """
        return (
            f"{AZURE_DEVOPS_BASE_URL}/{quote(self.organization, safe='')}/"
            f"{path.lstrip('/')}"
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        allow_status: Iterable[int] = (),
    ) -> httpx.Response:
        """Send one API request with Basic PAT auth.

        Args:
            method: HTTP method.
            path: Path below the organization.
            params: Query parameters; ``api-version`` is added.
            json: JSON body.
            allow_status: Error statuses the caller handles itself.

        Returns:
            The response.

        Raises:
            TrackerAuthenticationError: On 401 or a sign-in redirect.
            TrackerPermissionError: On 403.
            TrackerRateLimitError: On 429.
            TrackerResponseError: On other error statuses.
            TrackerConnectionError: On network failure.
        """
        query = {"api-version": AZURE_DEVOPS_API_VERSION, **(params or {})}
        headers = {"Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = (
                "application/json-patch+json"
                if isinstance(json, list)
                else "application/json"
            )
        try:
            async with httpx.AsyncClient(
                timeout=probe_httpx_timeout(HTTP_TIMEOUT_SECONDS),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    self._url(path),
                    params=query,
                    json=json,
                    headers=headers,
                    auth=httpx.BasicAuth("", self.api_key or ""),
                )
        except httpx.HTTPError as exc:
            raise TrackerConnectionError(
                f"Could not reach Azure DevOps: {type(exc).__name__}"
            ) from exc

        status_code = response.status_code
        if status_code in set(allow_status):
            return response
        # An invalid or expired PAT is answered with a 203 or a redirect to
        # the sign-in page rather than a 401.
        if status_code == 203 or 300 <= status_code < 400 or status_code == 401:
            raise TrackerAuthenticationError(
                "Azure DevOps rejected the personal access token. Check that it "
                "has not expired, belongs to this organization and has the "
                "required scopes."
            )
        if status_code < 400:
            return response
        detail = _error_detail(response)
        if status_code == 403:
            raise TrackerPermissionError(
                f"Azure DevOps denied the request (403): {detail}", status_code=403
            )
        if status_code == 429:
            raise TrackerRateLimitError(
                "Azure DevOps rate limit reached (429).",
                retry_after=parse_retry_after(response.headers),
            )
        raise TrackerResponseError(
            f"Azure DevOps API error {status_code}: {detail}", status_code=status_code
        )

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = await self._request("GET", path, params=params)
        data = response.json()
        return data if isinstance(data, dict) else {"value": data}

    async def _list(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Collect ``value`` across pages using the continuation token."""
        values: List[Dict[str, Any]] = []
        query = dict(params or {})
        for _ in range(MAX_PAGES):
            response = await self._request("GET", path, params=query)
            data = response.json()
            values.extend((data or {}).get("value") or [])
            token = response.headers.get(CONTINUATION_HEADER)
            if not token:
                break
            query = {**query, "continuationToken": token}
        return values

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _split(self, repo_full_name: Optional[str]) -> Tuple[str, str]:
        """Return ``(project, repository)`` for ``org/project/repo``.

        Raises:
            TrackerResponseError: When no repository is selected or it
                belongs to another organization.
        """
        full_name = repo_full_name or self.repo_full_name
        parts = (full_name or "").split("/")
        if len(parts) != 3 or not all(parts):
            raise TrackerResponseError(
                "No Azure DevOps repository selected. Pass a repository "
                "('organization/project/repository') or bind the tracker to one."
            )
        if parts[0].casefold() != self.organization.casefold():
            raise TrackerResponseError(
                "The repository belongs to another Azure DevOps organization."
            )
        if (self.project and parts[1].casefold() != self.project.casefold()) or (
            self.repository and parts[2].casefold() != self.repository.casefold()
        ):
            raise TrackerPermissionError(
                "The repository is outside this tracker's Azure DevOps project "
                "or repository binding.",
                status_code=403,
            )
        return parts[1], parts[2]

    def _repo(self, repo_full_name: Optional[str] = None) -> str:
        project, repository = self._split(repo_full_name)
        return (
            f"{quote(project, safe='')}/_apis/git/repositories/"
            f"{quote(repository, safe='')}"
        )

    def _pr(self, pr_id: int | str, repo_full_name: Optional[str] = None) -> str:
        return f"{self._repo(repo_full_name)}/pullRequests/{int(pr_id)}"

    # ------------------------------------------------------------------
    # Connection, organizations and projects
    # ------------------------------------------------------------------

    async def test_connection(self) -> TrackerConnection:
        """Check the token against the bound repository, project or organization."""
        if not self.organization:
            return TrackerConnection(
                connected=False, message="Azure DevOps organization is not configured."
            )
        try:
            if self.project and self.repository:
                data = await self._get_json(self._repo())
                message = f"Connected to Azure Repos repository {data.get('name')}"
            elif self.project:
                await self._get_json(
                    f"{quote(self.project, safe='')}/_apis/git/repositories"
                )
                message = f"Connected to Azure DevOps project {self.project}"
            else:
                await self._get_json("_apis/git/repositories")
                message = f"Connected to Azure DevOps organization {self.organization}"
        except (
            TrackerAuthenticationError,
            TrackerPermissionError,
            TrackerResponseError,
            TrackerConnectionError,
            TrackerRateLimitError,
        ) as exc:
            return TrackerConnection(connected=False, message=str(exc))
        return TrackerConnection(
            connected=True,
            message=message,
            server_info={"organization": self.organization, "auth": "pat"},
        )

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """Return the configured organization as the only organization."""
        return [{"id": self.organization, "name": self.organization}]

    def _repository_to_project(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        project = repo.get("project") or {}
        project_name = str(project.get("name") or "")
        name = str(repo.get("name") or "")
        repo_id = str(repo.get("id") or "").lower()
        full_name = repository_full_name(self.organization, project_name, name)
        web_url = clone_url(self.organization, project_name, name)
        return {
            "id": repo_id,
            "identifier": repo_id,
            "name": name,
            "description": "",
            "url": web_url,
            "group": project_name or None,
            "meta_data": {
                "full_name": full_name,
                "repository_id": repo_id,
                "repository": name,
                "project": project_name,
                "project_id": project.get("id"),
                "clone_url": web_url,
                "default_branch": branch_from_ref(repo.get("defaultBranch")),
                "is_disabled": bool(repo.get("isDisabled", False)),
            },
        }

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """List git repositories, grouped by Azure DevOps project.

        Disabled repositories cannot be cloned and are left out.
        """
        if self.project and self.repository:
            repos = [await self._get_json(self._repo())]
        elif self.project:
            repos = await self._list(
                f"{quote(self.project, safe='')}/_apis/git/repositories"
            )
        else:
            repos = await self._list("_apis/git/repositories")
        return [
            self._repository_to_project(repo)
            for repo in repos
            if is_guid(repo.get("id")) and not repo.get("isDisabled")
        ]

    def transform_project(
        self, proj_data: Dict[str, Any], organization_id: str
    ) -> Dict[str, Any]:
        """Transform a repository entry into a Preloop project."""
        meta = proj_data.get("meta_data") or {}
        return {
            "identifier": str(proj_data["id"]),
            "name": proj_data["name"],
            "description": proj_data.get("description"),
            "organization_id": organization_id,
            "slug": meta.get("full_name") or "",
            "meta_data": {**meta, "url": proj_data.get("url", "")},
        }

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Return metadata for a repository (``org/project/repo``)."""
        repo = await self._get_json(self._repo(project_key))
        entry = self._repository_to_project(repo)
        return ProjectMetadata(
            key=entry["meta_data"]["full_name"],
            name=entry["name"] or project_key,
            description=None,
            url=entry["url"],
        )

    # ------------------------------------------------------------------
    # Azure Boards work items
    # ------------------------------------------------------------------

    def _board_project(self, project_key: Optional[str]) -> str:
        """Azure DevOps project for a Preloop project key.

        Accepts ``org/project/repo`` (a repository's Preloop slug),
        ``org/project`` or a bare project name; falls back to the bound
        project.

        Raises:
            TrackerResponseError: When no project can be determined or it
                belongs to another organization.
            TrackerPermissionError: When it is outside the bound project.
        """
        parts = [p for p in str(project_key or "").split("/") if p]
        if len(parts) >= 2:
            if parts[0].casefold() != self.organization.casefold():
                raise TrackerResponseError(
                    "The project belongs to another Azure DevOps organization."
                )
            project: Optional[str] = parts[1]
        elif len(parts) == 1 and not is_guid(parts[0]):
            project = parts[0]
        else:
            project = self.project
        if not project:
            raise TrackerResponseError(
                "No Azure DevOps project selected. Pass 'organization/project' "
                "or bind the tracker to a project."
            )
        self._check_board_project(project)
        return project

    def _check_board_project(self, project: Optional[str]) -> None:
        if self.project and (project or "").casefold() != self.project.casefold():
            raise TrackerPermissionError(
                "The work item is outside this tracker's Azure DevOps project.",
                status_code=403,
            )

    async def _project_states(self, project: str) -> Dict[str, List[Dict[str, str]]]:
        """``{type (casefolded): [{"name", "category"}, ...]}``, cached.

        One ``workitemtypes`` call returns every type's states with their
        category. Process templates rename and translate states, so
        open/closed is decided by the category, never by the name.
        """
        cache_key = project.casefold()
        if cache_key not in self._states:
            values = await self._list(
                f"{quote(project, safe='')}/_apis/wit/workitemtypes"
            )
            self._states[cache_key] = {
                str(t.get("name") or "").casefold(): [
                    {"name": str(st["name"]), "category": str(st.get("category") or "")}
                    for st in t.get("states") or []
                    if isinstance(st, dict) and st.get("name")
                ]
                for t in values
                if isinstance(t, dict) and t.get("name")
            }
            self._type_names[cache_key] = [
                str(t["name"]) for t in values if isinstance(t, dict) and t.get("name")
            ]
        return self._states[cache_key]

    async def _state_categories(
        self, project: str, work_item_type: str
    ) -> Dict[str, str]:
        """``{state name (casefolded): category}`` for a work item type."""
        states = (await self._project_states(project)).get(
            work_item_type.casefold(), []
        )
        return {st["name"].casefold(): st["category"] for st in states}

    async def _category_of(self, work_item: Dict[str, Any]) -> Optional[str]:
        fields = work_item.get("fields") or {}
        project = fields.get(FIELD_PROJECT)
        wi_type = fields.get(FIELD_TYPE)
        state = fields.get(FIELD_STATE)
        if not (project and wi_type and state):
            return None
        try:
            categories = await self._state_categories(str(project), str(wi_type))
        except (TrackerPermissionError, TrackerResponseError) as exc:
            logger.info(
                "Azure DevOps state lookup failed for tracker %s: %s",
                self.tracker_id,
                type(exc).__name__,
            )
            return None
        return categories.get(str(state).casefold())

    async def _resolve_state(
        self, project: str, work_item_type: str, wanted: str
    ) -> str:
        """State name for ``open``, ``closed`` or a state name.

        ``closed`` picks the first ``Completed`` state, ``open`` the first
        ``Proposed`` one, in the order the process defines them.

        Raises:
            TrackerResponseError: When the type has no such state.
        """
        states = (await self._project_states(project)).get(
            work_item_type.casefold(), []
        )
        categories = {st["name"].casefold(): st["category"] for st in states}
        names = [st["name"] for st in states]
        target = wanted.strip()
        if target.casefold() in categories:
            return next(n for n in names if n.casefold() == target.casefold())
        category = {"closed": "Completed", "open": "Proposed"}.get(target.casefold())
        for name in names:
            if category and categories.get(name.casefold()) == category:
                return name
        raise TrackerResponseError(
            f"Work item type '{work_item_type}' has no state '{target}'. "
            f"Valid states: {', '.join(names)}."
        )

    async def _work_item(self, work_item_id: int) -> Dict[str, Any]:
        data = await self._get_json(
            f"_apis/wit/workitems/{int(work_item_id)}", params={"$expand": "relations"}
        )
        self._check_board_project((data.get("fields") or {}).get(FIELD_PROJECT))
        return data

    async def _to_issue(
        self, work_item: Dict[str, Any], comments: Optional[List[IssueComment]] = None
    ) -> Issue:
        category = await self._category_of(work_item)
        summary = work_item_summary(
            work_item, self.organization, state_category=category
        )
        return self._summary_to_issue(summary, comments or [])

    def _summary_to_issue(
        self, summary: Dict[str, Any], comments: List[IssueComment]
    ) -> Issue:
        def user(value: Optional[Dict[str, Any]]) -> Optional[IssueUser]:
            if not value:
                return None
            return IssueUser(
                id=str(value.get("id")), name=str(value.get("name")), email=None
            )

        priority = summary.get("priority")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        relations = [
            IssueRelation(
                relation_type=r["relation_type"],
                issue_id=str(r["id"]),
                issue_key=str(r["id"]),
            )
            for r in summary.get("relations") or []
            if r.get("id") is not None
        ]
        parent = next((r for r in relations if r.relation_type == "parent"), None)
        return Issue(
            id=str(summary["id"]),
            key=summary["key"],
            title=summary["title"],
            description=summary["body"],
            status=IssueStatus(
                id=summary["state_name"] or summary["state"],
                name=summary["state_name"] or summary["state"],
                category=summary["state"],
            ),
            priority=(
                IssuePriority(
                    id=priority["id"], name=priority["name"], level=priority["level"]
                )
                if priority
                else None
            ),
            created_at=parse_datetime(summary.get("created_at")) or now,
            updated_at=parse_datetime(summary.get("updated_at")) or now,
            resolved_at=parse_datetime(summary.get("closed_at")),
            reporter=user(summary.get("author")),
            assignee=user(summary.get("assignee")),
            labels=list(summary.get("labels") or []),
            parent=parent,
            relations=relations,
            comments=comments,
            url=summary.get("html_url") or "",
            api_url=self._url(f"_apis/wit/workitems/{summary['id']}"),
            tracker_type=self.tracker_type,
            project_key=f"{self.organization}/{summary.get('project') or ''}",
            custom_fields={
                "work_item_type": summary.get("work_item_type"),
                "state_category": summary.get("state_category"),
                "revision": summary.get("revision"),
            },
        )

    async def _work_items_batch(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Read work items in batches of 200, keeping the order of ``ids``."""
        items: Dict[int, Dict[str, Any]] = {}
        for start in range(0, len(ids), WORK_ITEMS_BATCH_LIMIT):
            chunk = ids[start : start + WORK_ITEMS_BATCH_LIMIT]
            response = await self._request(
                "POST",
                "_apis/wit/workitemsbatch",
                json={"ids": chunk, "$expand": "Relations", "errorPolicy": "Omit"},
            )
            for item in (response.json() or {}).get("value") or []:
                if isinstance(item, dict) and isinstance(item.get("id"), int):
                    items[item["id"]] = item
        return [items[i] for i in ids if i in items]

    async def _wiql_ids(self, project: str, query: str, top: int) -> List[int]:
        response = await self._request(
            "POST",
            f"{quote(project, safe='')}/_apis/wit/wiql",
            # timePrecision: compare date fields to the second, not the day.
            params={"$top": max(1, min(int(top), 20000)), "timePrecision": "true"},
            json={"query": query},
        )
        return [
            int(w["id"])
            for w in (response.json() or {}).get("workItems") or []
            if isinstance(w, dict) and isinstance(w.get("id"), int)
        ]

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Search work items with WIQL.

        ``status`` accepts ``open`` / ``closed`` (matched through the state
        category, so any process template and language works) or state
        names. ``query`` matches the title or description.

        Returns:
            The requested page and the total number of matches (capped at
            20000, the WIQL limit).
        """
        project = self._board_project(project_key)
        query = build_wiql(
            project, filter_params, states=await self._project_states(project)
        )
        ids = await self._wiql_ids(project, query, 20000)
        page = ids[max(0, offset) : max(0, offset) + max(1, limit)]
        items = await self._work_items_batch(page)
        return [await self._to_issue(item) for item in items], len(ids)

    async def get_issue(self, issue_id: str) -> Issue:
        """Read a work item with its links and comments.

        Args:
            issue_id: Work item id, issue key (``org/project#id``) or URL.
        """
        work_item_id = self._work_item_id(issue_id)
        item = await self._work_item(work_item_id)
        comments = await self._comments(item)
        return await self._to_issue(item, comments)

    def _work_item_id(self, issue_id: Any) -> int:
        parsed = parse_work_item_url(str(issue_id))
        if parsed and parsed[0].casefold() != self.organization.casefold():
            raise TrackerResponseError(
                "The work item belongs to another Azure DevOps organization."
            )
        work_item_id = parse_work_item_id(issue_id)
        if work_item_id is None:
            raise TrackerResponseError(f"Not an Azure Boards work item: {issue_id!r}")
        return work_item_id

    async def _default_type(self, project: str) -> str:
        """The requirement-level type of the project's process."""
        await self._project_states(project)
        names = self._type_names.get(project.casefold()) or []
        for preferred in DEFAULT_WORK_ITEM_TYPES:
            for name in names:
                if name.casefold() == preferred.casefold():
                    return name
        if not names:
            raise TrackerResponseError("The project has no work item types.")
        return names[0]

    def _relation_value(self, relation_type: str, target_id: int) -> Dict[str, Any]:
        link = RELATION_LINK_TYPES.get(relation_type)
        if link is None:
            raise TrackerResponseError(
                f"Unsupported relation type '{relation_type}'. Use one of: "
                f"{', '.join(RELATION_LINK_TYPES)}."
            )
        return {"rel": link, "url": self._url(f"_apis/wit/workItems/{target_id}")}

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Create a work item.

        The type is ``custom_fields["work_item_type"]`` or the process's
        requirement type (Issue, User Story, Product Backlog Item,
        Requirement). Other ``custom_fields`` entries named like
        ``Namespace.Field`` are written as fields.
        """
        project = self._board_project(project_key)
        custom = dict(issue_data.custom_fields or {})
        wi_type = str(custom.pop("work_item_type", "") or "") or (
            await self._default_type(project)
        )
        ops = patch_ops(
            title=issue_data.title,
            description_html=(
                markdown_to_html(issue_data.description)
                if issue_data.description
                else None
            ),
            assignee=issue_data.assignee or None,
            labels=issue_data.labels or None,
            priority=parse_priority(issue_data.priority),
            extra_fields=custom,
        )
        parent_id = parse_work_item_id(issue_data.parent) if issue_data.parent else None
        if parent_id:
            ops.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": self._relation_value("parent", parent_id),
                }
            )
        response = await self._request(
            "POST",
            f"{quote(project, safe='')}/_apis/wit/workitems/${quote(wi_type, safe='')}",
            json=ops,
        )
        item = response.json()
        if issue_data.status and issue_data.status.strip().casefold() != "open":
            state = await self._resolve_state(project, wi_type, issue_data.status)
            item = await self._patch(int(item["id"]), patch_ops(state=state))
        return await self._to_issue(item)

    async def _patch(
        self, work_item_id: int, ops: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        response = await self._request(
            "PATCH",
            f"_apis/wit/workitems/{int(work_item_id)}",
            params={"$expand": "relations"},
            json=ops,
        )
        return response.json()

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Update fields with one JSON Patch request.

        ``labels`` replaces the tag list; ``status`` takes ``open``,
        ``closed`` or a state name of the item's type.
        """
        work_item_id = self._work_item_id(issue_id)
        current = await self._work_item(work_item_id)
        fields = current.get("fields") or {}
        state = None
        if issue_data.status:
            state = await self._resolve_state(
                str(fields.get(FIELD_PROJECT)),
                str(fields.get(FIELD_TYPE)),
                issue_data.status,
            )
        custom = dict(issue_data.custom_fields or {})
        custom.pop("work_item_type", None)
        ops = patch_ops(
            title=issue_data.title,
            description_html=(
                markdown_to_html(issue_data.description)
                if issue_data.description is not None
                else None
            ),
            state=state,
            assignee=issue_data.assignee,
            labels=issue_data.labels,
            priority=parse_priority(issue_data.priority),
            extra_fields=custom,
        )
        if not ops:
            return await self._to_issue(current)
        return await self._to_issue(await self._patch(work_item_id, ops))

    def _comments_path(self, project: str, work_item_id: int) -> str:
        return f"{quote(project, safe='')}/_apis/wit/workItems/{int(work_item_id)}/comments"

    async def _raw_comments(self, work_item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Undeleted ``Comment`` objects of a work item, oldest first."""
        project = str((work_item.get("fields") or {}).get(FIELD_PROJECT) or "")
        path = self._comments_path(project, int(work_item["id"]))
        comments: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"api-version": COMMENTS_API_VERSION, "$top": 200}
        for _ in range(MAX_PAGES):
            data = await self._get_json(path, params=params)
            comments.extend(data.get("comments") or [])
            token = data.get("continuationToken")
            if not token:
                break
            params = {**params, "continuationToken": token}
        return [c for c in comments if isinstance(c, dict) and not c.get("isDeleted")]

    async def _comments(self, work_item: Dict[str, Any]) -> List[IssueComment]:
        project = str((work_item.get("fields") or {}).get(FIELD_PROJECT) or "")
        return [
            self._to_comment(c, project, int(work_item["id"]))
            for c in await self._raw_comments(work_item)
        ]

    def _to_comment(
        self, comment: Dict[str, Any], project: str, work_item_id: int
    ) -> IssueComment:
        summary = comment_summary(comment)
        author = summary.get("author") or {"id": "unknown", "name": "unknown"}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return IssueComment(
            id=str(summary["id"]),
            body=summary["body"],
            created_at=parse_datetime(summary.get("created_at")) or now,
            updated_at=parse_datetime(summary.get("updated_at")),
            author=IssueUser(id=str(author["id"]), name=str(author["name"])),
            url=(
                f"{work_item_web_url(self.organization, project, work_item_id)}"
                f"#{summary['id']}"
            ),
        )

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Comments on a work item, oldest first, as markdown."""
        item = await self._work_item(self._work_item_id(issue_id))
        return await self._comments(item)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Add a comment; the markdown is stored as HTML."""
        work_item_id = self._work_item_id(issue_id)
        item = await self._work_item(work_item_id)
        project = str((item.get("fields") or {}).get(FIELD_PROJECT) or "")
        response = await self._request(
            "POST",
            self._comments_path(project, work_item_id),
            params={"api-version": COMMENTS_API_VERSION},
            json={"text": markdown_to_html(comment)},
        )
        return self._to_comment(response.json(), project, work_item_id)

    async def update_comment(
        self, issue_id: str, comment_id: str, comment: str
    ) -> IssueComment:
        """Replace the text of a work item comment."""
        work_item_id = self._work_item_id(issue_id)
        item = await self._work_item(work_item_id)
        project = str((item.get("fields") or {}).get(FIELD_PROJECT) or "")
        response = await self._request(
            "PATCH",
            f"{self._comments_path(project, work_item_id)}/{int(comment_id)}",
            params={"api-version": COMMENTS_API_VERSION},
            json={"text": markdown_to_html(comment)},
        )
        return self._to_comment(response.json(), project, work_item_id)

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Link two work items.

        ``relation_type``: relates_to, parent, child, blocks, blocked_by,
        duplicates or duplicated_by. Returns False when the link exists.
        """
        work_item_id = self._work_item_id(issue_id)
        target_id = self._work_item_id(related_issue_id)
        value = self._relation_value(relation_type, target_id)
        current = await self._work_item(work_item_id)
        for rel in current.get("relations") or []:
            if rel.get("rel") == value["rel"] and str(rel.get("url", "")).rstrip(
                "/"
            ).endswith(f"/{target_id}"):
                return False
        await self._patch(
            work_item_id, [{"op": "add", "path": "/relations/-", "value": value}]
        )
        return True

    async def link_pull_request(
        self,
        issue_id: str,
        *,
        project_id: str,
        repository_id: str,
        pr_id: int,
    ) -> bool:
        """Add the Development link from a work item to a pull request.

        Args:
            issue_id: Work item id or key.
            project_id: GUID of the repository's Azure DevOps project.
            repository_id: Repository GUID.
            pr_id: Pull request id.

        Returns:
            True when the link was added, False when it already existed.
        """
        work_item_id = self._work_item_id(issue_id)
        uri = pull_request_artifact_uri(project_id, repository_id, int(pr_id))
        current = await self._work_item(work_item_id)
        for rel in current.get("relations") or []:
            if str(rel.get("url") or "").casefold() == uri.casefold():
                return False
        await self._patch(
            work_item_id,
            [
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": ARTIFACT_LINK,
                        "url": uri,
                        "attributes": {"name": PULL_REQUEST_ARTIFACT_NAME},
                    },
                }
            ],
        )
        return True

    async def token_identity(self) -> str:
        """Identity id of the token owner (for the self-update guard)."""
        return await self._own_identity()

    async def get_issues(
        self, organization_id: str, project_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Work items for the Preloop project of one repository.

        A repository receives the work items of its Azure DevOps project
        only when it is that project's work item home (see
        :func:`preloop.utils.azure_devops_work_items.work_item_home`);
        other repositories of the project get none, so each work item is
        stored once.

        Args:
            organization_id: Unused (one organization per tracker).
            project_id: Repository GUID (the Preloop project identifier).
            since: Only items changed after this time.

        Returns:
            Work item summaries for :meth:`transform_issue`.
        """
        if self._repos is None:
            # One listing per client, not per synced repository.
            self._repos = await self.get_projects(organization_id)
        repos = self._repos
        repo = next(
            (r for r in repos if r["identifier"] == str(project_id).lower()), None
        )
        if repo is None:
            return []
        board_project = repo["meta_data"]["project"]
        siblings = [
            r["meta_data"]["repository"]
            for r in repos
            if r["meta_data"]["project"].casefold() == board_project.casefold()
        ]
        home = work_item_home(
            board_project,
            siblings,
            bound_repository=self.repository,
            overrides=self.connection_details.get("work_item_repositories"),
        )
        if (
            home is None
            or home.casefold() != repo["meta_data"]["repository"].casefold()
        ):
            return []
        query = build_wiql(
            board_project,
            IssueFilter(updated_after=since) if since else IssueFilter(),
            states={},
        )
        ids = await self._wiql_ids(board_project, query, 20000)
        items = await self._work_items_batch(ids)
        out: List[Dict[str, Any]] = []
        for item in items:
            category = await self._category_of(item)
            summary = work_item_summary(
                item, self.organization, state_category=category
            )
            # Existing comments are synced too, not only those delivered by
            # workitem.commented; items without comments cost no request.
            summary["comments"] = []
            if (item.get("fields") or {}).get("System.CommentCount"):
                for raw in await self._raw_comments(item):
                    comment = comment_summary(raw)
                    comment["url"] = f"{summary['html_url']}#{comment['id']}"
                    summary["comments"].append(comment)
            out.append(summary)
        return out

    def transform_issue(
        self, issue_data: Dict[str, Any], project: Project
    ) -> Dict[str, Any]:
        """Map a work item summary onto issue columns."""
        created = parse_datetime(issue_data.get("created_at"))
        updated = parse_datetime(issue_data.get("updated_at")) or created
        url = issue_data.get("html_url") or ""
        priority = issue_data.get("priority") or None
        assignee = issue_data.get("assignee") or None
        wi_type = str(issue_data.get("work_item_type") or "")
        issue_type = (
            "bug"
            if wi_type.casefold() == "bug"
            else "feature"
            if wi_type.casefold() in {"feature", "epic", "user story"}
            else "task"
        )
        return {
            "project_id": project.id,
            "tracker_id": self.tracker_id,
            "external_id": str(issue_data["id"]),
            "key": issue_data.get("key") or str(issue_data["id"]),
            "title": issue_data.get("title") or "",
            "description": issue_data.get("body") or "",
            "status": issue_data.get("state") or "open",
            "issue_type": issue_type,
            "priority": priority["name"] if isinstance(priority, dict) else None,
            "created_at": created,
            "updated_at": updated,
            "last_updated_external": updated,
            "external_url": url,
            "last_synced": datetime.now(timezone.utc).replace(tzinfo=None),
            "meta_data": {
                "labels": list(issue_data.get("labels") or []),
                "assignees": [assignee["name"]] if assignee else [],
                "url": url,
                "external_url": url,
                "external_created_at": issue_data.get("created_at"),
                "external_updated_at": issue_data.get("updated_at"),
                "work_item_type": wi_type or None,
                "state_name": issue_data.get("state_name"),
                "state_category": issue_data.get("state_category"),
                "azure_project": issue_data.get("project"),
                "parent_id": issue_data.get("parent_id"),
                "relations": issue_data.get("relations") or [],
                "revision": issue_data.get("revision"),
                "source": "preloop-sync",
            },
            "comments": issue_data.get("comments", []),
        }

    def transform_comment(
        self,
        comment_data: Dict[str, Any],
        issue_db_id: str,
        author_db_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Map a work item comment summary onto comment columns."""
        author = comment_data.get("author") or {}
        created = parse_datetime(comment_data.get("created_at"))
        return {
            "issue_id": issue_db_id,
            "external_id": str(comment_data.get("id")),
            "author": None,
            "body": comment_data.get("body") or "",
            "type": "issue",
            "meta_data": {
                "comment_id": str(comment_data.get("id")),
                "external_author": author.get("name") if author else None,
                "url": comment_data.get("url"),
                "source": "preloop-sync",
            },
            "created_at": created,
            "updated_at": parse_datetime(comment_data.get("updated_at")) or created,
        }

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    async def get_pull_request(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return the raw ``GitPullRequest``."""
        return await self._get_json(self._pr(pr_id, repo_full_name))

    def pull_request_url(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> str:
        """Return the web URL of a pull request."""
        project, repository = self._split(repo_full_name)
        return pull_request_web_url(self.organization, project, repository, int(pr_id))

    async def get_pull_request_threads(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the comment threads on a pull request."""
        return await self._list(f"{self._pr(pr_id, repo_full_name)}/threads")

    async def get_pull_request_changes(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the change entries of the latest iteration.

        Azure DevOps has no unified diff endpoint; the reviewer works from
        the clone at the source commit and this file list.
        """
        base = self._pr(pr_id, repo_full_name)
        iterations = await self._list(f"{base}/iterations")
        ids = [it.get("id") for it in iterations if isinstance(it.get("id"), int)]
        if not ids:
            return []
        data = await self._get_json(
            f"{base}/iterations/{max(ids)}/changes", params={"$top": 2000}
        )
        return list(data.get("changeEntries") or [])

    async def add_pull_request_comment(
        self,
        pr_id: int | str,
        body: str,
        *,
        repo_full_name: Optional[str] = None,
        thread_id: Optional[int] = None,
        parent_comment_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start a thread, or reply in one.

        Args:
            pr_id: Pull request id.
            body: Markdown body.
            repo_full_name: ``org/project/repo``.
            thread_id: Reply in this thread instead of starting one.
            parent_comment_id: Comment replied to; defaults to the first.

        Returns:
            ``{"thread_id", "comment_id", "url"}``.
        """
        base = self._pr(pr_id, repo_full_name)
        if thread_id is None:
            response = await self._request(
                "POST",
                f"{base}/threads",
                json={
                    "comments": [
                        {"parentCommentId": 0, "content": body, "commentType": 1}
                    ],
                    "status": THREAD_ACTIVE,
                },
            )
            thread = response.json()
            comments = thread.get("comments") or [{}]
            new_thread = int(thread.get("id"))
            comment_id = int(comments[0].get("id") or 1)
        else:
            response = await self._request(
                "POST",
                f"{base}/threads/{int(thread_id)}/comments",
                json={
                    "content": body,
                    "parentCommentId": int(parent_comment_id or 1),
                    "commentType": 1,
                },
            )
            comment = response.json()
            new_thread = int(thread_id)
            comment_id = int(comment.get("id"))
        url = (
            f"{self.pull_request_url(pr_id, repo_full_name)}?discussionId={new_thread}"
        )
        return {"thread_id": new_thread, "comment_id": comment_id, "url": url}

    async def update_pull_request_comment(
        self,
        pr_id: int | str,
        thread_id: int,
        comment_id: int,
        body: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Edit a comment body."""
        response = await self._request(
            "PATCH",
            f"{self._pr(pr_id, repo_full_name)}/threads/{int(thread_id)}"
            f"/comments/{int(comment_id)}",
            json={"content": body},
        )
        return response.json()

    async def set_thread_status(
        self,
        pr_id: int | str,
        thread_id: int,
        resolved: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve (``fixed``) or reopen (``active``) a thread."""
        response = await self._request(
            "PATCH",
            f"{self._pr(pr_id, repo_full_name)}/threads/{int(thread_id)}",
            json={"status": THREAD_FIXED if resolved else THREAD_ACTIVE},
        )
        return response.json()

    async def _own_identity(self) -> str:
        """Identity id of the token owner (``connectionData``)."""
        if self._identity_id is None:
            data = await self._get_json(
                "_apis/connectionData", params={"api-version": "7.1-preview"}
            )
            user = data.get("authenticatedUser") or {}
            identity = user.get("id")
            if not identity:
                raise TrackerResponseError("Azure DevOps returned no identity.")
            self._identity_id = str(identity)
            account = ((user.get("properties") or {}).get("Account") or {}).get(
                "$value"
            )
            self._identity_unique_name = str(account) if account else None
        return self._identity_id

    async def set_vote(
        self, pr_id: int | str, vote: int, *, repo_full_name: Optional[str] = None
    ) -> bool:
        """Cast the token owner's vote; False when the token may not vote.

        Args:
            pr_id: Pull request id.
            vote: 10 approve, -5 wait for author, 0 reset.
            repo_full_name: ``org/project/repo``.

        Returns:
            True when the vote was recorded.
        """
        try:
            reviewer = await self._own_identity()
            await self._request(
                "PUT",
                f"{self._pr(pr_id, repo_full_name)}/reviewers/{quote(reviewer)}",
                json={"vote": int(vote)},
            )
        except (TrackerPermissionError, TrackerResponseError) as exc:
            # The review threads are already posted; a vote rule (400), a
            # missing right (403) or a transient error must not fail the
            # review. Authentication failures stay fatal.
            logger.info(
                "Azure DevOps vote skipped for tracker %s: %s (status %s)",
                self.tracker_id,
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            return False
        return True

    async def update_pull_request(
        self,
        pr_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Change the title or description. Status is never sent."""
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        response = await self._request(
            "PATCH", self._pr(pr_id, repo_full_name), json=body
        )
        return response.json()

    @staticmethod
    def normalize_listed_pull_request(
        pr: Dict[str, Any], organization: str
    ) -> Dict[str, Any]:
        """Map a ``GitPullRequest`` onto the shared PR list shape."""
        repo = pr.get("repository") or {}
        project = (repo.get("project") or {}).get("name") or ""
        number = int(pr.get("pullRequestId") or 0)
        status = str(pr.get("status") or "").lower()
        return {
            "number": number,
            "iid": number,
            "title": pr.get("title") or "",
            "description": pr.get("description") or "",
            "url": pull_request_web_url(
                organization, project, repo.get("name") or "", number
            ),
            "author": user_name(pr.get("createdBy")) or "",
            "source_branch": branch_from_ref(pr.get("sourceRefName")) or "",
            "target_branch": branch_from_ref(pr.get("targetRefName")) or "",
            "head_sha": (pr.get("lastMergeSourceCommit") or {}).get("commitId"),
            "state": {"active": "open", "completed": "merged"}.get(status, "closed"),
            "draft": bool(pr.get("isDraft", False)),
            "created_at": pr.get("creationDate"),
            "updated_at": pr.get("closedDate") or pr.get("creationDate"),
        }

    async def list_pull_requests(
        self,
        state: str = "open",
        limit: int = 20,
        page: int = 1,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List pull requests in the shared PR list shape.

        Args:
            state: ``open``, ``merged``, ``closed`` or ``all``.
            limit: Page size (at most 50).
            page: 1-based page number.
            repo_full_name: ``org/project/repo``; defaults to the bound one.

        Returns:
            ``{"items": [...], "has_more": bool}``.
        """
        status = {"open": "active", "merged": "completed", "closed": "abandoned"}.get(
            state, "all"
        )
        size = max(1, min(int(limit), 50))
        data = await self._get_json(
            f"{self._repo(repo_full_name)}/pullrequests",
            params={
                "searchCriteria.status": status,
                "$top": size + 1,
                "$skip": (max(1, int(page)) - 1) * size,
            },
        )
        values = list(data.get("value") or [])
        items = [
            self.normalize_listed_pull_request(pr, self.organization)
            for pr in values[:size]
        ]
        return {"items": items, "has_more": len(values) > size}

    # ------------------------------------------------------------------
    # Service hooks
    # ------------------------------------------------------------------

    async def _project_id(self, project: str) -> str:
        data = await self._get_json(f"_apis/projects/{quote(project, safe='')}")
        project_id = data.get("id")
        if not is_guid(project_id):
            raise TrackerResponseError("Azure DevOps returned no project id.")
        return str(project_id)

    async def _repository_id(self, repo_full_name: str) -> Tuple[str, str]:
        data = await self._get_json(self._repo(repo_full_name))
        repo_id = data.get("id")
        project_id = (data.get("project") or {}).get("id")
        if not is_guid(repo_id) or not is_guid(project_id):
            raise TrackerResponseError("Azure DevOps returned no repository id.")
        return str(project_id), str(repo_id)

    async def list_service_hooks(self, callback_url: str) -> List[Dict[str, Any]]:
        """Return Preloop's subscriptions (those calling ``callback_url``)."""
        subscriptions = await self._list("_apis/hooks/subscriptions")
        return [
            sub
            for sub in subscriptions
            if sub.get("consumerId") == "webHooks"
            and (sub.get("consumerInputs") or {}).get("url") == callback_url
        ]

    async def inspect_service_hooks(self, callback_url: str) -> Dict[str, Any]:
        """Report which required events have an enabled subscription."""
        subs = await self.list_service_hooks(callback_url)
        enabled = {
            sub.get("eventType")
            for sub in subs
            if str(sub.get("status") or "enabled").lower().startswith("enabled")
        }
        missing = [e for e in AZURE_DEVOPS_WEBHOOK_EVENTS if e not in enabled]
        return {
            "status": "registered" if not missing else "events_missing",
            "missing_events": missing,
            "ids": [sub.get("id") for sub in subs],
        }

    async def ensure_service_hooks(
        self,
        callback_url: str,
        secret: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or replace Preloop's pull request and work item subscriptions.

        Scope is the bound repository when there is one, else the bound
        project. Only subscriptions that call ``callback_url`` are touched;
        they are updated in place so the secret header follows a rotation.

        Args:
            callback_url: Preloop's intake URL for this tracker.
            secret: The per-tracker secret sent as a header.
            repo_full_name: Repository override for an unbound tracker.

        Returns:
            ``{"ids": [...], "created": n}``.

        Raises:
            TrackerResponseError: When neither project nor repository is set.
            TrackerPermissionError: When the token lacks service hook scope.
        """
        target = repo_full_name or self.repo_full_name
        repository_id: Optional[str] = None
        if target:
            project_id, repository_id = await self._repository_id(target)
        elif self.project:
            project_id = await self._project_id(self.project)
        else:
            raise TrackerResponseError(
                "Select an Azure DevOps project or repository before registering "
                "service hooks."
            )
        existing = {
            sub.get("eventType"): sub
            for sub in await self.list_service_hooks(callback_url)
        }
        ids: List[str] = []
        created = 0
        for event in AZURE_DEVOPS_WEBHOOK_EVENTS:
            publisher_inputs = {"projectId": project_id}
            if repository_id and event in PULL_REQUEST_WEBHOOK_EVENTS:
                # Work item events are project scoped; only pull request
                # events take a repository filter.
                publisher_inputs["repository"] = repository_id
            if event == "git.pullrequest.updated":
                # Only source branch pushes; votes, reviewer and status
                # changes are not delivered at all.
                publisher_inputs["notificationType"] = "PushNotification"
            body = {
                "publisherId": "tfs",
                "eventType": event,
                "resourceVersion": "1.0",
                "consumerId": "webHooks",
                "consumerActionId": "httpRequest",
                "publisherInputs": publisher_inputs,
                "consumerInputs": {
                    "url": callback_url,
                    "httpHeaders": f"{WEBHOOK_SECRET_HEADER}:{secret}",
                    "resourceDetailsToSend": "all",
                    "messagesToSend": "none",
                    "detailedMessagesToSend": "none",
                },
            }
            current = existing.get(event)
            if current and current.get("id"):
                response = await self._request(
                    "PUT",
                    f"_apis/hooks/subscriptions/{quote(str(current['id']), safe='')}",
                    json=body,
                )
            else:
                response = await self._request(
                    "POST", "_apis/hooks/subscriptions", json=body
                )
                created += 1
            ids.append(str(response.json().get("id")))
        return {"ids": ids, "created": created}

    async def work_item_context(
        self, repo_full_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Settings the webhook intake needs to handle work item events.

        Best effort: a token without Work Items read gets no snapshot, and
        the intake then falls back to ``ClosedDate``.

        Args:
            repo_full_name: Repository override for an unbound tracker.

        Returns:
            ``token_identity_id`` and ``token_identity_unique_name`` (the
            self-update guard) and
            ``work_item_states`` (state categories of the project).
        """
        context: Dict[str, Any] = {"token_identity_id": await self._own_identity()}
        if self._identity_unique_name:
            # Service hook bodies name the changer as "Name <unique name>" on
            # created and commented events, by GUID on updates.
            context["token_identity_unique_name"] = self._identity_unique_name
        target = repo_full_name or self.repo_full_name
        project = self._split(target)[0] if target else self.project
        if project:
            try:
                states = await self._project_states(project)
            except (TrackerPermissionError, TrackerResponseError) as exc:
                logger.info(
                    "Azure DevOps states snapshot skipped for tracker %s: %s",
                    self.tracker_id,
                    type(exc).__name__,
                )
            else:
                context["work_item_states"] = states_snapshot(project, states)
        return context

    # Scanner hooks: service hooks are managed from the tracker page only.

    async def register_webhook(self, **kwargs: Any) -> bool:
        """Not used: see :meth:`ensure_service_hooks`."""
        return False

    async def unregister_webhook(self, **kwargs: Any) -> bool:
        """Not used: subscriptions are removed in Azure DevOps."""
        return False

    async def is_webhook_registered(self, webhook: Webhook) -> bool:
        """Not tracked in the webhook table."""
        return False

    async def get_webhooks(self) -> List[Dict[str, Any]]:
        """Not tracked in the webhook table."""
        return []

    async def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """Not used."""
        return False

    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """Not used."""
        return {"deleted": 0, "failed": 0}

    async def is_webhook_registered_for_project(
        self, project: Project, webhook_url: str
    ) -> bool:
        """Not tracked in the webhook table."""
        return False

    async def is_webhook_registered_for_organization(
        self, organization: Organization, webhook_url: str
    ) -> bool:
        """Not tracked in the webhook table."""
        return False


def _error_detail(response: httpx.Response) -> str:
    """Short, token-free error text from an Azure DevOps error body."""
    try:
        data = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])[:300]
    return response.reason_phrase or "error"
