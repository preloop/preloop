"""Bitbucket Cloud tracker.

A Bitbucket tracker is bound to one workspace and, optionally, one repository.
Repositories become Preloop projects. Issues are not synced: teams on
Bitbucket Cloud keep issues in Jira, so the issue methods report that they are
unsupported and the tracker focuses on pull request review and webhooks.

Authentication (``Tracker.auth_type``):

* ``api_token``: an Atlassian API token for Bitbucket, or a repository access
  token (``connection_details["token_kind"] = "access_token"``, bound to one
  repository). REST calls send ``Authorization: Bearer <token>``. When a
  personal API token gets a 401 and the account email is configured, the
  client retries with HTTP Basic ``<email>:<token>`` and keeps using it.
* ``oauth_token``: a stored OAuth access token, sent as Bearer.

App passwords are rejected up front (see ``preloop.utils.bitbucket``).

The client never merges or declines a pull request. ``_request`` refuses any
path ending in ``/merge`` or ``/decline`` so a future caller cannot add one by
accident.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy.orm import Session

from preloop.models.crud import crud_webhook
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
from preloop.utils.bitbucket import (
    APP_PASSWORD_MESSAGE,
    BITBUCKET_API_BASE_URL,
    BITBUCKET_AUTH_API_TOKEN,
    BITBUCKET_WEBHOOK_EVENTS,
    TOKEN_KIND_API_TOKEN,
    build_object_attributes,
    normalize_uuid,
    pull_request_web_url,
    user_name,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerPermissionError,
    TrackerRateLimitError,
    TrackerResponseError,
)
from .base import BaseTracker

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30.0
PAGE_LENGTH = 100
MAX_PAGES = 50
_FORBIDDEN_SUFFIXES = ("/merge", "/decline")
_ISSUES_UNSUPPORTED = (
    "Bitbucket trackers sync repositories and pull requests only. "
    "Keep issues in Jira and bind the Jira project to the repository."
)


class BitbucketTracker(BaseTracker):
    """Bitbucket Cloud client for repositories, pull requests and webhooks."""

    tracker_type = "bitbucket"

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
            api_key: API token, repository access token or OAuth access token.
            connection_details: ``workspace`` (required), ``repository``,
                ``email``, ``username``, ``token_kind``, ``auth_type``,
                ``token_expires_at`` and, for bound clients,
                ``repo_full_name``.
            transport: Optional httpx transport, for tests.
        """
        super().__init__(tracker_id, api_key, connection_details or {})
        details = self.connection_details
        self.workspace: str = str(details.get("workspace") or "").strip()
        self.repository: Optional[str] = (
            str(details.get("repository")).strip()
            if details.get("repository")
            else None
        )
        self.email: Optional[str] = details.get("email") or None
        self.auth_type: str = str(
            details.get("auth_type") or BITBUCKET_AUTH_API_TOKEN
        ).lower()
        self.token_kind: str = str(
            details.get("token_kind") or TOKEN_KIND_API_TOKEN
        ).lower()
        self.api_base_url: str = str(
            details.get("api_url") or BITBUCKET_API_BASE_URL
        ).rstrip("/")
        repo_full_name = details.get("repo_full_name")
        if not repo_full_name and self.workspace and self.repository:
            repo_full_name = f"{self.workspace}/{self.repository}"
        self.repo_full_name: Optional[str] = repo_full_name or None
        self._transport = transport
        self._use_basic = False

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _can_fall_back_to_basic(self) -> bool:
        return bool(
            self.email
            and self.auth_type == BITBUCKET_AUTH_API_TOKEN
            and self.token_kind == TOKEN_KIND_API_TOKEN
        )

    def _auth(self) -> Tuple[Dict[str, str], Optional[httpx.BasicAuth]]:
        headers = {"Accept": "application/json"}
        if self._use_basic and self.email:
            return headers, httpx.BasicAuth(self.email, self.api_key or "")
        headers["Authorization"] = f"Bearer {self.api_key}"
        return headers, None

    def _url(self, path: str) -> str:
        """Resolve an API path, or check an absolute ``next`` link.

        Pagination links are absolute. They are only followed when they
        point at the configured API host, so the token is never sent
        elsewhere.

        Raises:
            TrackerResponseError: When an absolute URL leaves the API host.
        """
        if path.startswith("http://") or path.startswith("https://"):
            base = urlsplit(self.api_base_url)
            target = urlsplit(path)
            if (target.scheme, target.netloc) != (base.scheme, base.netloc):
                raise TrackerResponseError(
                    "Refusing to follow a Bitbucket link to another host."
                )
            return path
        return f"{self.api_base_url}/{path.lstrip('/')}"

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
    ) -> httpx.Response:
        headers, auth = self._auth()
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            return await client.request(
                method, url, params=params, json=json, headers=headers, auth=auth
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
        """Send one API request with Bearer auth and a Basic fallback.

        Args:
            method: HTTP method.
            path: Path under the API base URL, or an absolute ``next`` URL.
            params: Query parameters.
            json: JSON body.
            allow_status: Error statuses the caller handles itself.

        Returns:
            The response.

        Raises:
            TrackerAuthenticationError: On 401.
            TrackerPermissionError: On 403.
            TrackerRateLimitError: On 429.
            TrackerResponseError: On other error statuses.
            TrackerConnectionError: On network failure.
            ValueError: When asked to merge or decline a pull request.
        """
        bare_path = path.split("?", 1)[0].rstrip("/")
        if bare_path.endswith(_FORBIDDEN_SUFFIXES):
            raise ValueError(
                "Preloop never merges or declines Bitbucket pull requests."
            )
        url = self._url(path)
        try:
            response = await self._send(method, url, params=params, json=json)
            if (
                response.status_code == 401
                and not self._use_basic
                and self._can_fall_back_to_basic()
            ):
                self._use_basic = True
                response = await self._send(method, url, params=params, json=json)
                if response.status_code == 401:
                    self._use_basic = False
        except httpx.HTTPError as exc:
            raise TrackerConnectionError(
                f"Could not reach Bitbucket: {type(exc).__name__}"
            ) from exc

        status_code = response.status_code
        if status_code < 400 or status_code in set(allow_status):
            return response
        detail = _error_detail(response)
        if status_code == 401:
            raise TrackerAuthenticationError(
                "Bitbucket rejected the token (401). Check that it is an API "
                "token or access token with the required scopes and that it "
                f"has not expired. {APP_PASSWORD_MESSAGE}"
            )
        if status_code == 403:
            raise TrackerPermissionError(
                f"Bitbucket denied the request (403): {detail}", status_code=403
            )
        if status_code == 429:
            raise TrackerRateLimitError("Bitbucket rate limit reached (429).")
        raise TrackerResponseError(
            f"Bitbucket API error {status_code}: {detail}", status_code=status_code
        )

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        response = await self._request("GET", path, params=params)
        return response.json()

    async def _paginate(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Collect ``values`` across pages by following ``next``."""
        values: List[Dict[str, Any]] = []
        next_url: Optional[str] = path
        query = dict(params or {})
        query.setdefault("pagelen", PAGE_LENGTH)
        pages = 0
        while next_url and pages < MAX_PAGES:
            data = await self._get_json(next_url, params=query)
            query = None  # The next link already carries the query.
            values.extend(data.get("values") or [])
            next_url = data.get("next")
            pages += 1
        return values

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _repo(self, repo_full_name: Optional[str] = None) -> str:
        full_name = repo_full_name or self.repo_full_name
        if not full_name or "/" not in full_name:
            raise TrackerResponseError(
                "No Bitbucket repository selected. Pass a repository "
                "('workspace/repo') or bind the tracker to one."
            )
        workspace, slug = full_name.split("/", 1)
        return f"repositories/{quote(workspace, safe='')}/{quote(slug, safe='')}"

    def _pr(self, pr_id: int | str, repo_full_name: Optional[str] = None) -> str:
        return f"{self._repo(repo_full_name)}/pullrequests/{int(pr_id)}"

    # ------------------------------------------------------------------
    # Connection, organizations and projects
    # ------------------------------------------------------------------

    async def test_connection(self) -> TrackerConnection:
        """Check the token against the bound repository or the workspace."""
        if not self.workspace:
            return TrackerConnection(
                connected=False, message="Bitbucket workspace is not configured."
            )
        try:
            if self.repository:
                data = await self._get_json(
                    f"repositories/{quote(self.workspace, safe='')}/"
                    f"{quote(self.repository, safe='')}"
                )
                name = data.get("full_name") or self.repository
                message = f"Connected to Bitbucket repository {name}"
            else:
                await self._get_json(
                    f"repositories/{quote(self.workspace, safe='')}",
                    params={"pagelen": 1},
                )
                message = f"Connected to Bitbucket workspace {self.workspace}"
        except (
            TrackerAuthenticationError,
            TrackerResponseError,
            TrackerConnectionError,
            TrackerRateLimitError,
        ) as exc:
            return TrackerConnection(connected=False, message=str(exc))
        return TrackerConnection(
            connected=True,
            message=message,
            server_info={
                "workspace": self.workspace,
                "auth": "basic" if self._use_basic else "bearer",
            },
        )

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """Return the configured workspace as the only organization."""
        name = self.workspace
        try:
            data = await self._get_json(f"workspaces/{quote(self.workspace, safe='')}")
            name = data.get("name") or name
        except (TrackerResponseError, TrackerAuthenticationError) as exc:
            # Access tokens bound to a repository cannot read the workspace.
            logger.info("Bitbucket workspace lookup skipped: %s", exc)
        return [{"id": self.workspace, "name": name}]

    def _repository_to_project(self, repo: Dict[str, Any]) -> Dict[str, Any]:
        project = repo.get("project") or {}
        uuid = normalize_uuid(repo.get("uuid")) or repo.get("full_name")
        return {
            "id": uuid,
            "identifier": uuid,
            "name": repo.get("name") or repo.get("slug") or uuid,
            "description": repo.get("description") or "",
            "url": ((repo.get("links") or {}).get("html") or {}).get("href", ""),
            "group": project.get("name") or project.get("key"),
            "meta_data": {
                "full_name": repo.get("full_name"),
                "slug": repo.get("slug"),
                "uuid": repo.get("uuid"),
                "project_key": project.get("key"),
                "project_name": project.get("name"),
                "default_branch": (repo.get("mainbranch") or {}).get("name"),
                "is_private": repo.get("is_private"),
            },
        }

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """List repositories in the workspace, or the bound repository.

        Each entry carries ``group``, the Bitbucket project the repository
        belongs to, so the UI can group repositories.
        """
        workspace = organization_id or self.workspace
        if self.repository:
            repo = await self._get_json(
                f"repositories/{quote(workspace, safe='')}/"
                f"{quote(self.repository, safe='')}"
            )
            return [self._repository_to_project(repo)]
        repos = await self._paginate(f"repositories/{quote(workspace, safe='')}")
        return [self._repository_to_project(repo) for repo in repos]

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
        """Return no issues: Bitbucket trackers do not sync issues."""
        return []

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Return basic metadata for a repository (``workspace/repo``)."""
        repo = await self._get_json(self._repo(project_key))
        return ProjectMetadata(
            key=repo.get("full_name") or project_key,
            name=repo.get("name") or project_key,
            description=repo.get("description") or None,
            url=((repo.get("links") or {}).get("html") or {}).get("href", ""),
        )

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Return no issues: Bitbucket trackers do not sync issues."""
        return [], 0

    async def get_issue(self, issue_id: str) -> Issue:
        """Unsupported for Bitbucket."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Unsupported for Bitbucket."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Unsupported for Bitbucket."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Unsupported for Bitbucket issues; use pull request comments."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Unsupported for Bitbucket issues; use ``add_pr_comment``."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Unsupported for Bitbucket."""
        raise NotImplementedError(_ISSUES_UNSUPPORTED)

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    async def get_pull_request(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return the raw pull request object."""
        return await self._get_json(self._pr(pr_id, repo_full_name))

    async def list_pull_requests(
        self,
        state: str = "open",
        limit: int = 20,
        page: int = 1,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List pull requests in the shared PR list shape.

        Args:
            state: ``open`` (Bitbucket ``OPEN``) or another Bitbucket state.
            limit: Page size (``pagelen``, at most 50 on Bitbucket).
            page: 1-based page number.
            repo_full_name: ``workspace/repo``; defaults to the bound one.

        Returns:
            ``{"items": [...], "has_more": bool}``.
        """
        data = await self._get_json(
            f"{self._repo(repo_full_name)}/pullrequests",
            params={
                "state": state.upper(),
                "pagelen": max(1, min(int(limit), 50)),
                "page": max(1, int(page)),
                "sort": "-updated_on",
            },
        )
        items = [
            self._normalize_listed_pull_request(pr) for pr in data.get("values") or []
        ]
        return {"items": items, "has_more": bool(data.get("next"))}

    @staticmethod
    def _normalize_listed_pull_request(pr: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Bitbucket pull request onto the shared PR list shape."""
        attributes = build_object_attributes(pr)
        return {
            "number": int(pr.get("id") or 0),
            "iid": int(pr.get("id") or 0),
            "title": attributes["title"] or "",
            "description": attributes["description"],
            "url": attributes["url"] or "",
            "author": attributes["author"] or "",
            "source_branch": attributes["source_branch"] or "",
            "target_branch": attributes["target_branch"] or "",
            "state": attributes["state"] or "open",
            "draft": attributes["draft"],
            "created_at": pr.get("created_on"),
            "updated_at": pr.get("updated_on"),
        }

    async def get_pull_request_diff(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> str:
        """Return the unified diff (the API answers with a redirect)."""
        response = await self._request("GET", f"{self._pr(pr_id, repo_full_name)}/diff")
        return response.text

    async def get_pull_request_diffstat(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return per-file change statistics."""
        return await self._paginate(f"{self._pr(pr_id, repo_full_name)}/diffstat")

    async def get_pull_request_commits(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the commits on the pull request."""
        return await self._paginate(f"{self._pr(pr_id, repo_full_name)}/commits")

    async def get_pull_request_comments(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return all comments (general and inline) on the pull request."""
        return await self._paginate(f"{self._pr(pr_id, repo_full_name)}/comments")

    async def add_pull_request_comment(
        self,
        pr_id: int | str,
        body: str,
        *,
        repo_full_name: Optional[str] = None,
        path: Optional[str] = None,
        line: Optional[int] = None,
        old_line: Optional[int] = None,
        parent_id: Optional[int | str] = None,
    ) -> Dict[str, Any]:
        """Post a general, inline or reply comment.

        Args:
            pr_id: Pull request id.
            body: Markdown body.
            repo_full_name: ``workspace/repo``; defaults to the bound one.
            path: File path for an inline comment.
            line: New-file line for an inline comment (Bitbucket ``to``).
            old_line: Old-file line for a comment on a removed line
                (Bitbucket ``from``).
            parent_id: Comment id to reply to.

        Returns:
            The created comment object.
        """
        payload: Dict[str, Any] = {"content": {"raw": body}}
        if path:
            inline: Dict[str, Any] = {"path": path}
            if line is not None:
                inline["to"] = int(line)
            if old_line is not None:
                inline["from"] = int(old_line)
            payload["inline"] = inline
        if parent_id is not None:
            payload["parent"] = {"id": int(parent_id)}
        response = await self._request(
            "POST", f"{self._pr(pr_id, repo_full_name)}/comments", json=payload
        )
        return response.json()

    async def update_pull_request_comment(
        self,
        pr_id: int | str,
        comment_id: int | str,
        body: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replace the body of a comment."""
        response = await self._request(
            "PUT",
            f"{self._pr(pr_id, repo_full_name)}/comments/{int(comment_id)}",
            json={"content": {"raw": body}},
        )
        return response.json()

    async def delete_pull_request_comment(
        self,
        pr_id: int | str,
        comment_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Delete a comment."""
        await self._request(
            "DELETE",
            f"{self._pr(pr_id, repo_full_name)}/comments/{int(comment_id)}",
        )
        return True

    async def set_comment_resolved(
        self,
        pr_id: int | str,
        comment_id: int | str,
        resolved: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Resolve or reopen a comment thread.

        A 409 means the thread is already in the requested state and is
        treated as success.
        """
        await self._request(
            "POST" if resolved else "DELETE",
            f"{self._pr(pr_id, repo_full_name)}/comments/{int(comment_id)}/resolve",
            allow_status=(409,),
        )
        return True

    async def set_approval(
        self,
        pr_id: int | str,
        approved: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Approve the pull request, or withdraw the approval."""
        await self._request(
            "POST" if approved else "DELETE",
            f"{self._pr(pr_id, repo_full_name)}/approve",
            allow_status=(409,) if approved else (404,),
        )
        return True

    async def set_changes_requested(
        self,
        pr_id: int | str,
        requested: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Request changes on the pull request, or remove the request."""
        await self._request(
            "POST" if requested else "DELETE",
            f"{self._pr(pr_id, repo_full_name)}/request-changes",
            allow_status=(409,) if requested else (404,),
        )
        return True

    async def create_pull_request_task(
        self,
        pr_id: int | str,
        content: str,
        *,
        repo_full_name: Optional[str] = None,
        comment_id: Optional[int | str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a pull request task, if the token may.

        Tasks are optional. A 403 (for example a token without the pull
        request write scope, or a plan without tasks) is logged and skipped.

        Returns:
            The task object, or None when skipped.
        """
        payload: Dict[str, Any] = {"content": {"raw": content}}
        if comment_id is not None:
            payload["comment"] = {"id": int(comment_id)}
        try:
            response = await self._request(
                "POST", f"{self._pr(pr_id, repo_full_name)}/tasks", json=payload
            )
        except TrackerPermissionError:
            logger.info(
                "Skipping Bitbucket pull request task on PR %s: token lacks "
                "permission (403)",
                pr_id,
            )
            return None
        return response.json()

    async def update_pull_request(
        self,
        pr_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the title or description of a pull request."""
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if not payload:
            return await self.get_pull_request(pr_id, repo_full_name)
        response = await self._request(
            "PUT", self._pr(pr_id, repo_full_name), json=payload
        )
        return response.json()

    def pull_request_url(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> str:
        """Return the web URL of a pull request."""
        full_name = repo_full_name or self.repo_full_name or ""
        workspace, _, slug = full_name.partition("/")
        return pull_request_web_url(workspace, slug, int(pr_id))

    @staticmethod
    def normalize_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Bitbucket comment onto the shape GitHub comments use."""
        inline = comment.get("inline") or {}
        parent = comment.get("parent") or {}
        resolution = comment.get("resolution")
        return {
            "id": comment.get("id"),
            "author": user_name(comment.get("user")),
            "body": (comment.get("content") or {}).get("raw", ""),
            "created_at": comment.get("created_on"),
            "updated_at": comment.get("updated_on"),
            "type": "review_comment" if inline else "issue_comment",
            "path": inline.get("path"),
            "line": inline.get("to") if inline.get("to") is not None else None,
            "old_line": inline.get("from"),
            "side": (
                "LEFT"
                if inline and inline.get("to") is None and inline.get("from")
                else ("RIGHT" if inline else None)
            ),
            "in_reply_to_id": parent.get("id"),
            "html_url": ((comment.get("links") or {}).get("html") or {}).get("href"),
            "thread_id": parent.get("id") or comment.get("id"),
            "resolved": bool(resolution),
            "deleted": bool(comment.get("deleted", False)),
        }

    @staticmethod
    def object_attributes(pr: Dict[str, Any]) -> Dict[str, Any]:
        """Map a pull request onto the shared trigger ``object_attributes``."""
        return build_object_attributes(pr)

    # ------------------------------------------------------------------
    # Webhooks (repository hooks)
    # ------------------------------------------------------------------

    @staticmethod
    def _project_full_name(project: Project) -> Optional[str]:
        meta = getattr(project, "meta_data", None) or {}
        return getattr(project, "slug", None) or meta.get("full_name")

    async def _list_hooks(self, repo_full_name: str) -> List[Dict[str, Any]]:
        return await self._paginate(f"{self._repo(repo_full_name)}/hooks")

    async def register_webhook(self, **kwargs: Any) -> bool:
        """Create (or update) the repository hook for a project.

        Keyword Args:
            db: Database session.
            project: The Preloop project (repository).
            webhook_url: Target URL.
            secret: Shared secret; Bitbucket signs deliveries with it.

        Returns:
            True when the hook exists afterwards.
        """
        db: Session = kwargs["db"]
        project: Project = kwargs["project"]
        webhook_url: str = kwargs["webhook_url"]
        secret: str = kwargs["secret"]
        full_name = self._project_full_name(project)
        if not full_name:
            logger.error("Bitbucket project %s has no repository name", project.id)
            return False
        body = {
            "description": "Preloop",
            "url": webhook_url,
            "active": True,
            "secret": secret,
            "events": list(BITBUCKET_WEBHOOK_EVENTS),
        }
        try:
            existing = next(
                (
                    hook
                    for hook in await self._list_hooks(full_name)
                    if hook.get("url") == webhook_url
                ),
                None,
            )
            if existing:
                hook_uuid = existing.get("uuid")
                response = await self._request(
                    "PUT",
                    f"{self._repo(full_name)}/hooks/{quote(str(hook_uuid), safe='')}",
                    json=body,
                )
            else:
                response = await self._request(
                    "POST", f"{self._repo(full_name)}/hooks", json=body
                )
            hook = response.json()
        except (TrackerResponseError, TrackerAuthenticationError) as exc:
            logger.error(
                "Failed to register Bitbucket webhook for %s: %s", full_name, exc
            )
            return False

        external_id = str(hook.get("uuid") or "")
        if not crud_webhook.get_by_external_id(
            db, external_id=external_id, tracker_id=str(self.tracker_id)
        ):
            crud_webhook.create(
                db,
                obj_in={
                    "external_id": external_id,
                    "url": webhook_url,
                    "secret": secret,
                    "project_id": project.id,
                    "events": list(BITBUCKET_WEBHOOK_EVENTS),
                },
            )
        return True

    async def register_project_webhook(
        self, db: Session, project: Project, webhook_url: str, secret: str
    ) -> bool:
        """Alias matching the GitLab client."""
        return await self.register_webhook(
            db=db, project=project, webhook_url=webhook_url, secret=secret
        )

    async def is_webhook_registered_for_project(
        self, project: Project, webhook_url: str
    ) -> bool:
        """Return True when the repository has a hook for ``webhook_url``."""
        full_name = self._project_full_name(project)
        if not full_name:
            return False
        try:
            hooks = await self._list_hooks(full_name)
        except (TrackerResponseError, TrackerAuthenticationError) as exc:
            logger.error("Failed to list Bitbucket hooks for %s: %s", full_name, exc)
            return False
        return any(hook.get("url") == webhook_url for hook in hooks)

    async def is_webhook_registered_for_organization(
        self, organization: Organization, webhook_url: str
    ) -> bool:
        """Workspace hooks are not used; hooks are per repository."""
        return False

    async def is_webhook_registered(self, webhook: Webhook) -> bool:
        """Return True when the stored hook still exists in Bitbucket."""
        project = getattr(webhook, "project", None)
        full_name = self._project_full_name(project) if project else None
        if not webhook.external_id or not full_name:
            return False
        try:
            response = await self._request(
                "GET",
                f"{self._repo(full_name)}/hooks/"
                f"{quote(str(webhook.external_id), safe='')}",
                allow_status=(404,),
            )
        except TrackerResponseError as exc:
            logger.error("Failed to check Bitbucket hook: %s", exc)
            return False
        return response.status_code == 200

    async def get_webhooks(self) -> List[Dict[str, Any]]:
        """List hooks on the bound repository, if any."""
        if not self.repo_full_name:
            return []
        return await self._list_hooks(self.repo_full_name)

    async def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """Delete a hook given ``{"uuid", "repo_full_name"}``."""
        full_name = webhook.get("repo_full_name") or self.repo_full_name
        hook_uuid = webhook.get("uuid") or webhook.get("id")
        if not full_name or not hook_uuid:
            return False
        await self._request(
            "DELETE",
            f"{self._repo(full_name)}/hooks/{quote(str(hook_uuid), safe='')}",
            allow_status=(404,),
        )
        return True

    async def unregister_webhook(self, **kwargs: Any) -> bool:
        """Delete one stored hook in Bitbucket and in the database."""
        db: Optional[Session] = kwargs.get("db")
        webhook: Optional[Webhook] = kwargs.get("webhook")
        if db is None or webhook is None:
            return False
        project = getattr(webhook, "project", None)
        full_name = self._project_full_name(project) if project else None
        try:
            if full_name and webhook.external_id:
                await self.delete_webhook(
                    {"uuid": webhook.external_id, "repo_full_name": full_name}
                )
        except (TrackerResponseError, TrackerAuthenticationError) as exc:
            logger.error("Failed to delete Bitbucket hook: %s", exc)
            return False
        crud_webhook.remove(db, id=webhook.id)
        return True

    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """Delete every stored hook for this tracker's repositories."""
        from preloop.models.crud import crud_project

        results = {"unregistered": 0, "failed": 0, "not_found": 0}
        for project in crud_project.get_for_tracker(db, tracker_id=self.tracker_id):
            for webhook in crud_webhook.get_all_by_project(db, project_id=project.id):
                if webhook_url_pattern and webhook_url_pattern not in (
                    webhook.url or ""
                ):
                    continue
                if await self.unregister_webhook(db=db, webhook=webhook):
                    results["unregistered"] += 1
                else:
                    results["failed"] += 1
        return results


def _error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
    return str(data)[:300]


__all__ = ["BitbucketTracker"]
