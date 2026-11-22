"""Azure DevOps Services (Azure Repos) tracker.

An Azure DevOps tracker is bound to one organization and, optionally, one
project and one repository. Each git repository becomes a Preloop project
(``slug`` ``org/project/repo``, ``identifier`` the repository GUID). Azure
Boards work items are not synced: the issue methods report that they are
unsupported, and the tracker focuses on pull request review.

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
from datetime import datetime
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
    IssueUpdate,
    ProjectMetadata,
    TrackerConnection,
)
from preloop.services.tracker_probe import probe_httpx_timeout
from preloop.utils.azure_devops import (
    AZURE_DEVOPS_API_VERSION,
    AZURE_DEVOPS_BASE_URL,
    AZURE_DEVOPS_WEBHOOK_EVENTS,
    WEBHOOK_SECRET_HEADER,
    branch_from_ref,
    clone_url,
    is_guid,
    pull_request_web_url,
    repository_full_name,
    user_name,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerError,
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
_ISSUES_UNSUPPORTED = (
    "Azure DevOps trackers sync repositories and pull requests only. Azure "
    "Boards work items are not supported."
)
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

    async def get_issues(
        self, organization_id: str, project_id: str, since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Return no issues: Azure Boards is not synced."""
        return []

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

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Return no issues: Azure Boards is not synced."""
        return [], 0

    async def get_issue(self, issue_id: str) -> Issue:
        """Unsupported for Azure DevOps."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Unsupported for Azure DevOps."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Unsupported for Azure DevOps."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Unsupported for Azure DevOps work items; use pull request threads."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Unsupported for Azure DevOps work items; use pull request threads."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Unsupported for Azure DevOps."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

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
            identity = (data.get("authenticatedUser") or {}).get("id")
            if not identity:
                raise TrackerResponseError("Azure DevOps returned no identity.")
            self._identity_id = str(identity)
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
        except TrackerAuthenticationError:
            raise
        except TrackerError as exc:
            # The review threads are already posted; a vote rule (400), a
            # missing right (403), a rate limit or a network error must not
            # fail the review. Authentication failures stay fatal.
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
        """Create or replace Preloop's two pull request subscriptions.

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
            if repository_id:
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
