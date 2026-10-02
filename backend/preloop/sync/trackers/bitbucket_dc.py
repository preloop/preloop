"""Bitbucket Data Center tracker (10.2 LTS baseline).

A ``bitbucket_dc`` tracker is bound to one administrator-approved instance
(canonical HTTPS origin plus context path) and optionally to one project and
one repository. Repositories become Preloop projects; the repository's
immutable numeric id is the project identifier, so a slug rename keeps the
identity. Data Center is a repository host: issues stay in Jira and the issue
methods report that they are unsupported.

This adapter is separate from the Bitbucket Cloud tracker and shares nothing
with it. It speaks the REST 1.0 contract documented for 10.2 (see
``preloop.utils.bitbucket_dc`` for provenance): ``fromRef``/``toRef`` full
refs, ``text``/``anchor`` comment payloads, ``participants/{userSlug}``
reviewer verdicts, ``blocker-comments`` tasks, ``commits/{sha}/builds`` build
statuses and ``start``/``limit``/``nextPageStart`` paging.

Security:

* Authentication is a user personal access token sent as ``Authorization:
  Bearer``; no other scheme is attempted.
* Every request targets the approved instance only. The host is resolved at
  connection time, every resolved address is checked against the
  link-local/metadata/private-network policy, and the connection is pinned to
  the validated address while TLS still verifies the configured hostname
  (``sni_hostname`` plus ``Host``). Redirects are never followed; absolute
  links from payloads are never requested; ``trust_env`` is off so proxy and
  CA environment variables cannot divert traffic.
* Merging, declining and deleting pull requests are refused at every public
  operation and again inside the transport layer.
* Repository and pull request payloads are checked against the requested
  project key, slug and bound repository id so a response for another
  tenant's repository is rejected rather than acted upon.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import ssl
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote

import httpx
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.tracker_models import (
    Issue,
    IssueComment,
    IssueCreate,
    IssueFilter,
    IssueUpdate,
    ProjectMetadata,
    TrackerConnection,
)
from preloop.utils.bitbucket_dc import (
    BITBUCKET_DC_AUTH_API_TOKEN,
    BITBUCKET_DC_DEFAULT_VERSION,
    BITBUCKET_DC_SUPPORTED_VERSIONS,
    BITBUCKET_DC_TRACKER_TYPE,
    BUILD_STATUS_STATES,
    CURRENT_USER_HEADER,
    PARTICIPANT_APPROVED,
    PARTICIPANT_NEEDS_WORK,
    PARTICIPANT_UNAPPROVED,
    PAT_MESSAGE,
    BitbucketDCConfigError,
    BitbucketDCIdentityError,
    BitbucketDCPaginationError,
    InstanceIdentity,
    Resolver,
    approved_instance_for,
    build_comment_anchor,
    build_object_attributes,
    ca_bundle_path,
    clone_links,
    full_ref,
    next_page_start,
    normalize_comment,
    path_text,
    pull_request_web_url,
    ref_branch,
    reject_cloud_payload,
    repository_identity,
    repository_web_url,
    resolve_pinned_address,
    self_link,
    user_name,
    validate_project_key,
    validate_repository_id,
    validate_repository_slug,
    validate_user_slug,
)

from ..exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerPermissionError,
    TrackerRateLimitError,
    TrackerResponseError,
)
from .base import BaseTracker

Organization = models.Organization
Project = models.Project
Webhook = models.Webhook

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30.0
PAGE_LIMIT = 100
MAX_PAGES = 100
MAX_BRANCH_PAGES = 10
_FORBIDDEN_SUFFIXES = ("/merge", "/decline")
_ISSUES_UNSUPPORTED = (
    "Bitbucket Data Center trackers sync repositories and pull requests only. "
    "Keep issues in Jira and bind the Jira project to the repository."
)
_WEBHOOKS_UNSUPPORTED = (
    "Bitbucket Data Center webhook ingestion is not part of this adapter; "
    "repository webhooks are managed by a separate integration."
)

# Operations the adapter supports against a 10.2 instance and those it
# deliberately refuses or has not validated. The console and MCP tools read
# this so they never present an unsupported action as available.
CAPABILITIES: Dict[str, bool] = {
    "discover_projects": True,
    "discover_repositories": True,
    "branch_lookup": True,
    "pull_request_read": True,
    "pull_request_diff": True,
    "pull_request_create": True,
    "pull_request_update": True,
    "pull_request_comments": True,
    "pull_request_inline_comments": True,
    "pull_request_comment_replies": True,
    "pull_request_approve": True,
    "pull_request_request_changes": True,
    "pull_request_tasks": True,
    "commit_build_status": True,
    "pull_request_merge": False,
    "pull_request_decline": False,
    "pull_request_delete": False,
    "issues": False,
    "webhooks": False,
    "oauth": False,
}
UNSUPPORTED_OPERATIONS: Tuple[str, ...] = tuple(
    name for name, supported in CAPABILITIES.items() if not supported
)


class BitbucketDCUnsupportedOperationError(NotImplementedError):
    """Raised for an operation the Data Center adapter does not perform.

    Never caught and turned into a fake success: the caller has to surface it.
    """


# Short alias kept for readers of the capability table; same class.
BitbucketDCUnsupportedOperation = BitbucketDCUnsupportedOperationError


class BitbucketDCConflictError(TrackerResponseError):
    """A versioned edit lost to a concurrent change and was not retried."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


class BitbucketDCTracker(BaseTracker):
    """Bitbucket Data Center client for repositories, pull requests and reviews."""

    tracker_type = BITBUCKET_DC_TRACKER_TYPE
    hosts_repositories: bool = True
    capabilities: Dict[str, bool] = dict(CAPABILITIES)
    unsupported_operations: Tuple[str, ...] = UNSUPPORTED_OPERATIONS

    def __init__(
        self,
        tracker_id: str,
        api_key: str,
        connection_details: Dict[str, Any],
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        resolver: Optional[Resolver] = None,
    ) -> None:
        """Initialise the client.

        Args:
            tracker_id: ID of the tracker in the database.
            api_key: User personal access token.
            connection_details: ``instance_url`` (required, approved
                canonical HTTPS URL), optional ``project_key``,
                ``repository_slug``, integer ``repository_id``, ``version``
                (default ``10.2``), ``username`` (reviewer user slug) and
                ``auth_type`` (``api_token``).
            transport: Optional httpx transport, for deterministic tests.
                When given without ``resolver`` the canonical URL is sent
                as-is so a mock can route on it.
            resolver: Optional DNS resolver ``(host, port) -> [address]``
                used for destination pinning, for tests.

        Raises:
            BitbucketDCConfigError: When the instance is not approved, the
                auth type is not ``api_token`` or an identity field is invalid.
        """
        super().__init__(tracker_id, api_key, connection_details or {})
        details = self.connection_details
        self.auth_type: str = str(
            details.get("auth_type") or BITBUCKET_DC_AUTH_API_TOKEN
        ).lower()
        if self.auth_type != BITBUCKET_DC_AUTH_API_TOKEN:
            raise BitbucketDCConfigError(
                f"Unsupported Bitbucket Data Center auth_type '{self.auth_type}'. "
                f"{PAT_MESSAGE}"
            )
        self.identity: InstanceIdentity = approved_instance_for(
            details.get("instance_url") or details.get("url")
        )
        self.instance_url: str = self.identity.base_url
        self.version: str = str(
            details.get("version") or BITBUCKET_DC_DEFAULT_VERSION
        ).strip()
        self.version_validated: bool = self.version in BITBUCKET_DC_SUPPORTED_VERSIONS
        self.project_key: Optional[str] = (
            validate_project_key(details["project_key"])
            if details.get("project_key")
            else None
        )
        self.repository_slug: Optional[str] = (
            validate_repository_slug(details["repository_slug"])
            if details.get("repository_slug")
            else None
        )
        if self.repository_slug and not self.project_key:
            raise BitbucketDCConfigError(
                "repository_slug requires project_key in connection_details."
            )
        self.repository_id: Optional[int] = (
            validate_repository_id(details["repository_id"])
            if details.get("repository_id") not in (None, "")
            else None
        )
        self._current_user: Optional[str] = (
            validate_user_slug(details["username"]) if details.get("username") else None
        )
        self._repository_aliases: set[str] = (
            {self.repository_slug} if self.repository_slug else set()
        )
        self._transport = transport
        self._resolver = resolver
        self._ssl_context: Optional[ssl.SSLContext] = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @property
    def repo_full_name(self) -> Optional[str]:
        """``PROJECT/slug`` of the bound repository, if any."""
        if self.project_key and self.repository_slug:
            return f"{self.project_key}/{self.repository_slug}"
        return None

    @property
    def current_user(self) -> Optional[str]:
        """The reviewer's user slug, configured or learned from a response."""
        return self._current_user

    def _ssl(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            context = ssl.create_default_context(cafile=ca_bundle_path())
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            self._ssl_context = context
        return self._ssl_context

    def _headers(self, accept: str) -> Dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.api_key}",
            "X-Atlassian-Token": "no-check",
        }

    @staticmethod
    def _refuse_forbidden(method: str, path: str) -> None:
        bare = path.split("?", 1)[0].rstrip("/")
        if bare.endswith(_FORBIDDEN_SUFFIXES):
            raise BitbucketDCUnsupportedOperationError(
                "Preloop never merges or declines Bitbucket pull requests."
            )
        if method.upper() == "DELETE" and "/pull-requests/" in bare:
            tail = bare.rsplit("/pull-requests/", 1)[1]
            if tail.isdigit():
                raise BitbucketDCUnsupportedOperationError(
                    "Preloop never deletes Bitbucket pull requests."
                )

    async def _pinned_target(
        self, url: httpx.URL
    ) -> Tuple[httpx.URL, Dict[str, str], Dict[str, Any]]:
        """Resolve the instance host now and pin the connection to it.

        Returns:
            The URL to send (host replaced by the validated address), extra
            headers (``Host`` for the canonical name) and request extensions
            (``sni_hostname`` so TLS still verifies the configured name).
        """
        if self._transport is not None and self._resolver is None:
            return url, {}, {}
        if self._resolver is None:
            address = await asyncio.to_thread(resolve_pinned_address, self.identity)
        else:
            address = resolve_pinned_address(self.identity, resolver=self._resolver)
        host_header = self.identity.host
        if ":" in host_header:
            host_header = f"[{host_header}]"
        if self.identity.port != 443:
            host_header = f"{host_header}:{self.identity.port}"
        pinned = url.copy_with(host=address, port=self.identity.port)
        return pinned, {"Host": host_header}, {"sni_hostname": self.identity.host}

    async def _verify_repository_request(
        self, path: str, payload: Any
    ) -> Tuple[str, Any]:
        """Resolve immutable identity before any request below a repository.

        A slug is a mutable locator. Check its numeric identity before reads as
        well as writes; after a rename, route to the resolved locator and keep
        PR reference payloads on that same repository.
        """
        parts = path.split("/")
        if len(parts) < 4 or parts[0] != "projects" or parts[2] != "repos":
            return path, payload
        key, slug = self._split_repo(f"{unquote(parts[1])}/{unquote(parts[3])}")
        if self.repository_slug or self.repository_id is not None:
            await self.resolve_repository()
            slug = self.repository_slug or slug
        else:
            # An unbound discovery tracker still resolves the requested repo
            # before acting, but does not become globally bound to its first one.
            repo_response = await self._send_request(
                "GET", self._repo_path(f"{key}/{slug}")
            )
            self._assert_repository(repo_response.json(), key, slug)
        if isinstance(payload, dict) and payload.get("url") == repository_web_url(
            self.identity, key, unquote(parts[3])
        ):
            payload = {**payload, "url": repository_web_url(self.identity, key, slug)}
        parts[3] = quote(slug, safe="")
        if isinstance(payload, dict) and ("fromRef" in payload or "toRef" in payload):
            payload = copy.deepcopy(payload)
            for ref_name in ("fromRef", "toRef"):
                ref = payload.get(ref_name)
                if not isinstance(ref, dict) or not isinstance(
                    ref.get("repository"), dict
                ):
                    continue
                repository = ref["repository"]
                ref_key = (repository.get("project") or {}).get("key")
                ref_slug = repository.get("slug")
                if ref_key != key or ref_slug not in self._repository_aliases | {slug}:
                    raise BitbucketDCIdentityError(
                        "PR reference is outside the resolved repository."
                    )
                repository["slug"] = slug
                if self.repository_id is not None:
                    repository["id"] = self.repository_id
        return "/".join(parts), payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        allow_status: Iterable[int] = (),
        accept: str = "application/json",
    ) -> httpx.Response:
        # Reject dangerous aliases before even the identity lookup sends a PAT.
        self._validate_request_path(method, path)
        path, json = await self._verify_repository_request(path, json)
        return await self._send_request(
            method,
            path,
            params=params,
            json=json,
            allow_status=allow_status,
            accept=accept,
        )

    @classmethod
    def _validate_request_path(cls, method: str, path: str) -> None:
        if path.startswith(("http://", "https://", "//", "/")):
            raise TrackerResponseError("Refusing a path outside the instance REST API.")
        decoded = unquote(path)
        cls._refuse_forbidden(method, decoded)
        if (
            any(c in decoded for c in ("?", "#", "\\"))
            or any(ord(c) < 32 for c in decoded)
            or any(segment in (".", "..", "") for segment in decoded.split("/"))
            or "%" in decoded
        ):
            raise TrackerResponseError("Refusing an ambiguous REST path.")

    async def _send_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        allow_status: Iterable[int] = (),
        accept: str = "application/json",
    ) -> httpx.Response:
        """Send one REST 1.0 request to the approved instance.

        Args:
            method: HTTP method.
            path: Path below ``/rest/api/1.0``. Absolute URLs are refused.
            params: Query parameters.
            json: JSON body.
            allow_status: Error statuses the caller handles itself.
            accept: ``Accept`` header value.

        Returns:
            The response.

        Raises:
            BitbucketDCUnsupportedOperationError: For merge/decline/delete paths.
            TrackerAuthenticationError: On 401.
            TrackerPermissionError: On 403.
            TrackerRateLimitError: On 429.
            TrackerResponseError: On redirects and other error statuses.
            TrackerConnectionError: On network, TLS or resolution failure.
        """
        if path.startswith(("http://", "https://", "//")) or path.startswith("/"):
            raise TrackerResponseError(
                "Refusing to request a link outside the instance REST path."
            )
        decoded = unquote(path)
        self._refuse_forbidden(method, decoded)
        if (
            any(c in decoded for c in ("?", "#", "\\"))
            or any(ord(c) < 32 for c in decoded)
            or any(segment in (".", "..", "") for segment in decoded.split("/"))
            or "%" in decoded
        ):
            raise TrackerResponseError("Refusing an ambiguous REST path.")
        url = httpx.URL(f"{self.identity.rest_base_url}/{path}")
        headers = self._headers(accept)
        try:
            target, extra_headers, extensions = await self._pinned_target(url)
        except BitbucketDCConfigError as exc:
            raise TrackerConnectionError(str(exc)) from exc
        headers.update(extra_headers)
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
                verify=self._ssl() if self._transport is None else True,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    target,
                    params=params,
                    json=json,
                    headers=headers,
                    extensions=extensions,
                )
        except httpx.HTTPError as exc:
            raise TrackerConnectionError(
                f"Could not reach Bitbucket Data Center: {type(exc).__name__}"
            ) from exc

        learned = response.headers.get(CURRENT_USER_HEADER)
        if learned and not self._current_user:
            try:
                self._current_user = validate_user_slug(learned)
            except BitbucketDCConfigError:
                logger.debug("Ignoring unusable %s header", CURRENT_USER_HEADER)

        status = response.status_code
        if 300 <= status < 400:
            raise TrackerResponseError(
                "Bitbucket Data Center answered with a redirect; redirects are "
                "not followed. Check the instance URL and context path.",
                status_code=status,
            )
        if status < 400 or status in set(allow_status):
            return response
        detail = _error_detail(response)
        if self.api_key:
            detail = detail.replace(self.api_key, "[redacted]")
        if status == 401:
            raise TrackerAuthenticationError(
                "Bitbucket Data Center rejected the token (401). Check that the "
                f"personal access token is valid and not expired. {PAT_MESSAGE}"
            )
        if status == 403:
            raise TrackerPermissionError(
                f"Bitbucket Data Center denied the request (403): {detail}",
                status_code=403,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            suffix = f" Retry after {retry_after}s." if retry_after else ""
            raise TrackerRateLimitError(
                f"Bitbucket Data Center rate limit reached (429).{suffix}"
            )
        if status == 409:
            raise BitbucketDCConflictError(
                f"Bitbucket Data Center reported a conflict (409): {detail}"
            )
        raise TrackerResponseError(
            f"Bitbucket Data Center API error {status}: {detail}", status_code=status
        )

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        response = await self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise TrackerResponseError(
                "Bitbucket Data Center returned a non-JSON body."
            ) from exc

    async def _paginate(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        max_pages: int = MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Collect ``values`` across pages using ``start``/``nextPageStart``.

        Stops on ``isLastPage``. A missing, repeated or non-progressing
        cursor, a non-page body or more than ``max_pages`` pages raises.

        Raises:
            TrackerResponseError: When the paging contract is violated.
        """
        values: List[Dict[str, Any]] = []
        query = dict(params or {})
        query.setdefault("limit", PAGE_LIMIT)
        start = 0
        seen: set[int] = set()
        for _ in range(max_pages):
            query["start"] = start
            page = await self._get_json(path, params=query)
            try:
                nxt = next_page_start(page, current_start=start, seen_starts=seen)
            except BitbucketDCPaginationError as exc:
                raise TrackerResponseError(
                    f"Bitbucket Data Center paging error on {path}: {exc}"
                ) from exc
            values.extend(dict(v) for v in page["values"] if isinstance(v, Mapping))
            if nxt is None:
                return values
            start = nxt
        raise TrackerResponseError(
            f"Bitbucket Data Center paging on {path} exceeded {max_pages} pages."
        )

    # ------------------------------------------------------------------
    # Repository identity
    # ------------------------------------------------------------------

    def _split_repo(self, repo_full_name: Optional[str]) -> Tuple[str, str]:
        """Return ``(project_key, slug)`` for an explicit or bound repository."""
        name = repo_full_name or self.repo_full_name
        if not name or name.count("/") != 1:
            raise TrackerResponseError(
                "No Bitbucket Data Center repository selected. Pass a "
                "repository ('PROJECT/slug') or bind the tracker to one."
            )
        key, slug = name.split("/", 1)
        try:
            key, slug = validate_project_key(key), validate_repository_slug(slug)
            if self.project_key and key.casefold() != self.project_key.casefold():
                raise BitbucketDCIdentityError(
                    "Repository is outside the bound project."
                )
            if self.repository_slug and slug.casefold() not in {
                name.casefold()
                for name in self._repository_aliases | {self.repository_slug}
            }:
                raise BitbucketDCIdentityError(
                    "Repository is outside the bound repository."
                )
            if self.repository_id is not None and not self.repository_slug:
                raise BitbucketDCIdentityError(
                    "Resolve the bound repository ID before using repository operations."
                )
            return key, slug
        except BitbucketDCConfigError as exc:
            raise TrackerResponseError(str(exc)) from exc

    def _repo_path(self, repo_full_name: Optional[str] = None) -> str:
        key, slug = self._split_repo(repo_full_name)
        return f"projects/{quote(key, safe='~')}/repos/{quote(slug, safe='')}"

    @staticmethod
    def _comment_id(value: int | str) -> int:
        try:
            number = int(str(value))
        except (TypeError, ValueError) as exc:
            raise TrackerResponseError("Invalid comment id.") from exc
        if number <= 0:
            raise TrackerResponseError("Invalid comment id.")
        return number

    def _pr_path(self, pr_id: int | str, repo_full_name: Optional[str] = None) -> str:
        try:
            number = int(str(pr_id).strip().lstrip("#"))
        except (TypeError, ValueError) as exc:
            raise TrackerResponseError(f"Invalid pull request id {pr_id!r}.") from exc
        if number <= 0:
            raise TrackerResponseError(f"Invalid pull request id {pr_id!r}.")
        return f"{self._repo_path(repo_full_name)}/pull-requests/{number}"

    def _is_bound(self, project_key: str, slug: str) -> bool:
        return bool(
            self.project_key
            and self.repository_slug
            and project_key.casefold() == self.project_key.casefold()
            and slug.casefold() == self.repository_slug.casefold()
        )

    def _assert_repository(
        self, repo: Any, project_key: str, slug: str, *, what: str = "repository"
    ) -> Dict[str, Any]:
        """Check that a repository object is the one that was requested.

        Raises:
            BitbucketDCIdentityError: On a Cloud payload, a different project
                key or slug, or a bound repository whose id changed.
        """
        reject_cloud_payload(repo, what)
        ident = repository_identity(repo)
        if ident is None:
            raise BitbucketDCIdentityError(
                f"Bitbucket Data Center {what} has no integer repository id."
            )
        if (
            self.repository_id is not None
            and ident["repository_id"] == self.repository_id
            and slug in self._repository_aliases
            and self.repository_slug
        ):
            slug = self.repository_slug
        if (
            ident["project_key"].casefold() != project_key.casefold()
            or ident["repository_slug"].casefold() != slug.casefold()
        ):
            raise BitbucketDCIdentityError(
                f"Bitbucket Data Center {what} belongs to "
                f"{ident['project_key']}/{ident['repository_slug']}, not the "
                f"requested {project_key}/{slug}."
            )
        if (
            self.repository_id is not None
            and self._is_bound(project_key, slug)
            and ident["repository_id"] != self.repository_id
        ):
            raise BitbucketDCIdentityError(
                f"Repository {project_key}/{slug} now has id "
                f"{ident['repository_id']}; this tracker is bound to repository "
                f"id {self.repository_id}. Re-bind the tracker explicitly."
            )
        return ident

    def _assert_pull_request(
        self, pr: Any, project_key: str, slug: str
    ) -> Dict[str, Any]:
        """Check that a pull request targets the requested repository."""
        reject_cloud_payload(pr, "pull request")
        if not isinstance(pr, Mapping) or not isinstance(pr.get("id"), int):
            raise TrackerResponseError(
                "Bitbucket Data Center returned a malformed pull request."
            )
        to_repo = (pr.get("toRef") or {}).get("repository")
        self._assert_repository(to_repo, project_key, slug, what="pull request")
        return dict(pr)

    async def resolve_repository(self) -> Dict[str, Any]:
        """Return the bound repository, following a slug rename by id.

        A 404 on the bound slug with a known ``repository_id`` lists the
        project's repositories and adopts the entry with that id; the new
        slug is kept for the rest of this client's life and reported in
        ``meta_data`` so the caller can persist it.

        Raises:
            TrackerResponseError: When no repository is bound or found.
            BitbucketDCIdentityError: When the slug is now a different repository.
        """
        if not self.project_key or not (self.repository_slug or self.repository_id):
            raise TrackerResponseError(
                "A bound repository needs its project key and slug or ID."
            )
        response = None
        if self.repository_slug:
            response = await self._send_request(
                "GET", self._repo_path(), allow_status=(404,)
            )
        if response is not None and response.status_code == 200:
            repo: Dict[str, Any] = response.json()
            ident = repository_identity(repo)
            if ident is None:
                raise BitbucketDCIdentityError(
                    "Repository response has no immutable identity."
                )
            if (
                self.repository_id is None
                or ident["repository_id"] == self.repository_id
            ):
                self._assert_repository(
                    repo, self.project_key, self.repository_slug or ""
                )
                self.repository_id = ident["repository_id"]
                self.connection_details["repository_id"] = self.repository_id
                return repo
            # The old slug was reused. Search by the known ID, never act on it.
        if self.repository_id is None:
            raise TrackerResponseError(
                f"Repository {self.repo_full_name} was not found (404).",
                status_code=404,
            )
        candidates = await self._paginate(
            f"projects/{quote(self.project_key, safe='~')}/repos"
        )
        for candidate in candidates:
            found = repository_identity(candidate)
            if found and found["repository_id"] == self.repository_id:
                self._assert_repository(
                    candidate, self.project_key, found["repository_slug"]
                )
                logger.info(
                    "Bitbucket Data Center repository id %s was renamed %s -> %s",
                    self.repository_id,
                    self.repository_slug,
                    found["repository_slug"],
                )
                self._repository_aliases.add(found["repository_slug"])
                self.repository_slug = found["repository_slug"]
                self.connection_details["repository_slug"] = self.repository_slug
                return dict(candidate)
        raise TrackerResponseError(
            f"Repository id {self.repository_id} no longer exists in project "
            f"{self.project_key}.",
            status_code=404,
        )

    # ------------------------------------------------------------------
    # Connection, organizations and projects
    # ------------------------------------------------------------------

    async def get_server_version(self) -> Dict[str, Any]:
        """Return ``application-properties`` (version, display name)."""
        data = await self._get_json("application-properties")
        if not isinstance(data, Mapping):
            raise TrackerResponseError("Malformed application-properties response.")
        return dict(data)

    def _baseline_matches(self, server_version: Optional[str]) -> bool:
        if not server_version:
            return False
        parts = str(server_version).split(".")
        return ".".join(parts[:2]) in BITBUCKET_DC_SUPPORTED_VERSIONS

    async def test_connection(self) -> TrackerConnection:
        """Check the token against the bound repository, project or instance.

        The server version is compared with the fixture-tested baseline; a
        different release is reported as unsupported/unvalidated and does not
        pass the connection gate.
        """
        try:
            properties = await self.get_server_version()
            if self.project_key and (self.repository_slug or self.repository_id):
                repo = await self.resolve_repository()
                target = f"repository {self.project_key}/{repo.get('slug')}"
            elif self.project_key:
                project = await self._get_json(
                    f"projects/{quote(self.project_key, safe='~')}"
                )
                reject_cloud_payload(project, "project")
                target = f"project {project.get('key') or self.project_key}"
            else:
                await self._get_json("projects", params={"limit": 1})
                target = "instance"
        except (
            TrackerAuthenticationError,
            TrackerResponseError,
            TrackerConnectionError,
            TrackerRateLimitError,
            BitbucketDCIdentityError,
        ) as exc:
            return TrackerConnection(connected=False, message=str(exc))
        server_version = str(properties.get("version") or "")
        validated = self._baseline_matches(server_version) and self.version_validated
        message = f"Connected to Bitbucket Data Center {target} at {self.instance_url}"
        if not validated:
            message += (
                f" (server version {server_version or 'unknown'} is not the "
                f"validated {', '.join(BITBUCKET_DC_SUPPORTED_VERSIONS)} baseline; "
                "capability is unvalidated)"
            )
        return TrackerConnection(
            connected=validated,
            message=message,
            server_info={
                "instance_url": self.instance_url,
                "version": server_version,
                "display_name": properties.get("displayName"),
                "baseline": self.version,
                "validated": validated,
                "auth": "bearer",
                "current_user": self._current_user,
                "capabilities": {
                    name: supported and validated
                    for name, supported in self.capabilities.items()
                },
            },
        )

    @staticmethod
    def _project_entry(project: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(project.get("key") or ""),
            "name": project.get("name") or project.get("key") or "",
            "project_id": project.get("id"),
            "type": project.get("type"),
        }

    async def get_organizations(self) -> List[Dict[str, Any]]:
        """Return Bitbucket projects as organizations (bound project or all)."""
        if self.project_key:
            project = await self._get_json(
                f"projects/{quote(self.project_key, safe='~')}"
            )
            reject_cloud_payload(project, "project")
            if str(project.get("key") or "").casefold() != self.project_key.casefold():
                raise BitbucketDCIdentityError(
                    f"Project {project.get('key')} is not the bound {self.project_key}."
                )
            return [self._project_entry(project)]
        return [
            self._project_entry(p)
            for p in await self._paginate("projects")
            if p.get("key")
        ]

    def _repository_to_project(self, repo: Mapping[str, Any]) -> Dict[str, Any]:
        ident = repository_identity(repo) or {}
        key = ident.get("project_key") or ""
        slug = ident.get("repository_slug") or ""
        return {
            "id": str(ident.get("repository_id")),
            "identifier": str(ident.get("repository_id")),
            "name": repo.get("name") or slug,
            "description": repo.get("description") or "",
            "url": self_link(repo, self.identity)
            or repository_web_url(self.identity, key, slug),
            "group": (repo.get("project") or {}).get("name") or key,
            "meta_data": {
                "full_name": f"{key}/{slug}",
                "instance_url": self.instance_url,
                "project_key": key,
                "project_id": ident.get("project_id"),
                "repository_id": ident.get("repository_id"),
                "repository_slug": slug,
                "default_branch": repo.get("defaultBranch"),
                "hierarchy_id": repo.get("hierarchyId"),
                "archived": bool(repo.get("archived", False)),
                "public": bool(repo.get("public", False)),
                "clone_links": clone_links(repo),
                "provider": BITBUCKET_DC_TRACKER_TYPE,
            },
        }

    async def get_projects(self, organization_id: str) -> List[Dict[str, Any]]:
        """List repositories of a project, or the bound repository.

        ``organization_id`` is the project key. Each entry's ``id`` is the
        immutable repository id; ``meta_data`` carries the slug.
        """
        key = validate_project_key(organization_id or self.project_key)
        if self.project_key and key.casefold() != self.project_key.casefold():
            raise BitbucketDCIdentityError(
                f"Project {key} is outside this tracker's bound project "
                f"{self.project_key}."
            )
        if (self.repository_slug or self.repository_id) and self.project_key:
            repo = await self.resolve_repository()
            return [self._repository_to_project(repo)]
        repos = await self._paginate(f"projects/{quote(key, safe='~')}/repos")
        entries = []
        for repo in repos:
            reject_cloud_payload(repo, "repository")
            ident = repository_identity(repo)
            if ident is None or ident["project_key"].casefold() != key.casefold():
                logger.warning(
                    "Skipping repository outside project %s: %r", key, repo.get("slug")
                )
                continue
            entries.append(self._repository_to_project(repo))
        return entries

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
        """Return no issues: Data Center trackers do not sync issues."""
        return []

    async def get_project_metadata(self, project_key: str) -> ProjectMetadata:
        """Return basic metadata for a repository (``PROJECT/slug``)."""
        key, slug = self._split_repo(project_key)
        repo = await self._get_json(self._repo_path(project_key))
        self._assert_repository(repo, key, slug)
        return ProjectMetadata(
            key=f"{key}/{repo.get('slug') or slug}",
            name=repo.get("name") or slug,
            description=repo.get("description") or None,
            url=self_link(repo, self.identity)
            or repository_web_url(self.identity, key, slug),
        )

    async def search_issues(
        self,
        project_key: str,
        filter_params: IssueFilter,
        limit: int = 10,
        offset: int = 0,
    ) -> Tuple[List[Issue], int]:
        """Return no issues: Data Center trackers do not sync issues."""
        return [], 0

    async def get_issue(self, issue_id: str) -> Issue:
        """Unsupported for Bitbucket Data Center."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    async def create_issue(self, project_key: str, issue_data: IssueCreate) -> Issue:
        """Unsupported for Bitbucket Data Center."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    async def update_issue(self, issue_id: str, issue_data: IssueUpdate) -> Issue:
        """Unsupported for Bitbucket Data Center."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    async def get_comments(self, issue_id: str) -> List[IssueComment]:
        """Unsupported; use ``get_pull_request_comments``."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    async def add_comment(self, issue_id: str, comment: str) -> IssueComment:
        """Unsupported; use ``add_pull_request_comment``."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    async def add_relation(
        self, issue_id: str, related_issue_id: str, relation_type: str
    ) -> bool:
        """Unsupported for Bitbucket Data Center."""
        raise BitbucketDCUnsupportedOperationError(_ISSUES_UNSUPPORTED)

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    async def branch_exists(
        self, branch: str, repo_full_name: Optional[str] = None
    ) -> bool:
        """Whether ``branch`` exists, by exact ``displayId`` match.

        ``filterText`` is a substring filter, so the page is scanned for the
        exact name. Errors other than a clean answer propagate.
        """
        name = str(branch or "").strip()
        if not name or name.startswith("-") or any(c.isspace() for c in name):
            raise TrackerResponseError(f"Invalid branch name {branch!r}.")
        branches = await self._paginate(
            f"{self._repo_path(repo_full_name)}/branches",
            params={"filterText": name, "limit": PAGE_LIMIT},
            max_pages=MAX_BRANCH_PAGES,
        )
        return any(
            b.get("displayId") == name or b.get("id") == full_ref(name)
            for b in branches
        )

    async def get_default_branch(
        self, repo_full_name: Optional[str] = None
    ) -> Optional[str]:
        """Return the default branch name, or None when unset."""
        response = await self._request(
            "GET",
            f"{self._repo_path(repo_full_name)}/default-branch",
            allow_status=(404,),
        )
        if response.status_code == 404:
            return None
        return ref_branch(response.json())

    # ------------------------------------------------------------------
    # Pull requests: read
    # ------------------------------------------------------------------

    async def get_pull_request(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return the raw pull request object after an identity check."""
        key, slug = self._split_repo(repo_full_name)
        pr = await self._get_json(self._pr_path(pr_id, repo_full_name))
        return self._assert_pull_request(pr, key, slug)

    async def list_pull_requests(
        self,
        state: str = "open",
        limit: int = 20,
        page: int = 1,
        repo_full_name: Optional[str] = None,
        source_branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List pull requests in the shared PR list shape.

        Args:
            state: ``open``, ``merged``, ``declined`` or ``all``.
            limit: Page size.
            page: 1-based page, reached by following returned ``nextPageStart`` cursors.
            repo_full_name: ``PROJECT/slug``; defaults to the bound one.
            source_branch: Only pull requests from this branch
                (``at=refs/heads/<branch>&direction=OUTGOING``).

        Returns:
            ``{"items": [...], "has_more": bool}``.
        """
        key, slug = self._split_repo(repo_full_name)
        size = max(1, min(int(limit), PAGE_LIMIT))
        params: Dict[str, Any] = {
            "state": str(state or "open").upper(),
            "limit": size,
            "start": 0,
            "order": "NEWEST",
        }
        if source_branch:
            params["at"] = full_ref(source_branch)
            params["direction"] = "OUTGOING"
        requested_page = max(1, int(page))
        if requested_page > MAX_PAGES:
            raise TrackerResponseError(
                "Requested pull request page exceeds the pagination limit."
            )
        seen: set[int] = set()
        for current_page in range(1, requested_page + 1):
            data = await self._get_json(
                f"{self._repo_path(repo_full_name)}/pull-requests", params=params
            )
            try:
                cursor = next_page_start(
                    data, current_start=params["start"], seen_starts=seen
                )
            except BitbucketDCPaginationError as exc:
                raise TrackerResponseError(
                    f"Malformed pull request page: {exc}"
                ) from exc
            if current_page == requested_page:
                break
            if cursor is None:
                return {"items": [], "has_more": False}
            params["start"] = cursor
        items = []
        for pr in data["values"]:
            self._assert_pull_request(pr, key, slug)
            normalized = self._normalize_listed_pull_request(pr)
            if source_branch and normalized["source_branch"] != source_branch:
                continue
            items.append(normalized)
        return {"items": items, "has_more": data.get("isLastPage") is False}

    async def list_open_pull_requests_by_source_branch(
        self, branch: str
    ) -> Dict[str, Any]:
        """Open pull requests whose source is ``branch``, in the shared shape."""
        return await self.list_pull_requests(
            state="open", limit=5, page=1, source_branch=branch
        )

    def _normalize_listed_pull_request(self, pr: Mapping[str, Any]) -> Dict[str, Any]:
        attributes = build_object_attributes(pr, self.identity)
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
            "version": attributes["version"],
            "created_at": pr.get("createdDate"),
            "updated_at": pr.get("updatedDate"),
        }

    def _normalize_created_pull_request(self, pr: Mapping[str, Any]) -> Dict[str, Any]:
        attributes = build_object_attributes(pr, self.identity)
        return {
            "id": str(pr.get("id") or 0),
            "number": int(pr.get("id") or 0),
            "title": attributes["title"] or "",
            "description": attributes["description"],
            "state": attributes["state"] or "open",
            "url": attributes["url"] or "",
            "is_draft": attributes["draft"],
            "source_branch": attributes["source_branch"] or "",
            "target_branch": attributes["target_branch"] or "",
            "version": attributes["version"],
        }

    async def get_pull_request_diff(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> str:
        """Return the raw unified diff (``pull-requests/{id}.diff``)."""
        response = await self._request(
            "GET", f"{self._pr_path(pr_id, repo_full_name)}.diff", accept="text/plain"
        )
        return response.text

    async def get_pull_request_changes(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the raw ``changes`` entries of the pull request."""
        return await self._paginate(
            f"{self._pr_path(pr_id, repo_full_name)}/changes",
            params={"withComments": "false"},
        )

    async def get_pull_request_diffstat(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return per-file change entries in a diffstat-like shape.

        Data Center reports change types, not line counts; ``lines_added``
        and ``lines_removed`` are therefore None rather than zero.
        """
        result = []
        for change in await self.get_pull_request_changes(pr_id, repo_full_name):
            change_type = str(change.get("type") or "MODIFY").upper()
            new_path = path_text(change.get("path"))
            old_path = path_text(change.get("srcPath"))
            result.append(
                {
                    "path": new_path,
                    "old_path": old_path if change_type in ("MOVE", "COPY") else None,
                    "status": {
                        "ADD": "added",
                        "DELETE": "removed",
                        "MOVE": "renamed",
                        "COPY": "copied",
                    }.get(change_type, "modified"),
                    "type": change_type,
                    "lines_added": None,
                    "lines_removed": None,
                }
            )
        return result

    async def get_pull_request_commits(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the commits on the pull request."""
        return await self._paginate(f"{self._pr_path(pr_id, repo_full_name)}/commits")

    async def get_pull_request_comment(
        self,
        pr_id: int | str,
        comment_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return one raw comment (carries ``version``)."""
        data = await self._get_json(
            f"{self._pr_path(pr_id, repo_full_name)}/comments/{self._comment_id(comment_id)}"
        )
        reject_cloud_payload(data, "comment")
        if not isinstance(data, Mapping):
            raise TrackerResponseError("Malformed comment response.")
        return dict(data)

    async def get_pull_request_comments(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return all comments (general, inline, replies) normalised.

        Comments are read from the ``activities`` feed, the only resource
        that yields inline comments together with their anchors; replies
        nested under ``comment.comments`` are flattened with ``thread_id``
        set to the root comment. Orphaned anchors are reported as
        ``outdated``.
        """
        activities = await self._paginate(
            f"{self._pr_path(pr_id, repo_full_name)}/activities"
        )
        comments: List[Dict[str, Any]] = []
        for activity in activities:
            if activity.get("action") != "COMMENTED":
                continue
            root = activity.get("comment")
            if not isinstance(root, Mapping):
                continue
            anchor = activity.get("commentAnchor")
            anchor = anchor if isinstance(anchor, Mapping) else None
            self._flatten_thread(root, anchor, None, None, comments)
        return comments

    def _flatten_thread(
        self,
        comment: Mapping[str, Any],
        anchor: Optional[Mapping[str, Any]],
        parent_id: Optional[int],
        thread_id: Optional[int],
        out: List[Dict[str, Any]],
    ) -> None:
        normalized = normalize_comment(
            comment, anchor=anchor, parent_id=parent_id, thread_id=thread_id
        )
        out.append(normalized)
        root_id = thread_id or normalized["id"]
        for reply in comment.get("comments") or []:
            if isinstance(reply, Mapping):
                self._flatten_thread(reply, anchor, normalized["id"], root_id, out)

    # ------------------------------------------------------------------
    # Pull requests: write
    # ------------------------------------------------------------------

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
        close_source_branch: bool = False,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a pull request with full ``fromRef``/``toRef`` refs.

        Reviewers are user slugs (``{"user": {"name": slug}}``). Assignees,
        labels, milestones and ``close_source_branch`` have no supported Data Center
        equivalent on this resource and raise before any request.

        Returns:
            Dict with ``id``, ``number``, ``title``, ``description``,
            ``state``, ``url``, ``is_draft``, ``source_branch``,
            ``target_branch`` and ``version``.
        """
        key, slug = self._split_repo(repo_full_name)
        repository = {"slug": slug, "project": {"key": key}}
        payload: Dict[str, Any] = {
            "title": title,
            "description": description or "",
            "fromRef": {"id": full_ref(source_branch), "repository": repository},
            "toRef": {"id": full_ref(target_branch), "repository": repository},
        }
        if draft:
            payload["draft"] = True
        if reviewers:
            payload["reviewers"] = [
                {"user": {"name": validate_user_slug(r)}} for r in reviewers
            ]
        if assignees or labels or milestone or close_source_branch:
            raise BitbucketDCUnsupportedOperationError(
                "Data Center PR creation does not support assignees, labels, "
                "milestones or closing source branches."
            )
        response = await self._request(
            "POST", f"{self._repo_path(repo_full_name)}/pull-requests", json=payload
        )
        pr = self._assert_pull_request(response.json(), key, slug)
        logger.info(
            "Created Bitbucket Data Center pull request #%s: %s", pr["id"], title
        )
        return self._normalize_created_pull_request(pr)

    async def update_pull_request(
        self,
        pr_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update the title or description with optimistic versioning.

        The pull request is read for its ``version``; a 409 re-reads it and
        retries once, but only if the fields being edited were not changed
        by the concurrent update. Otherwise the conflict is returned.

        Raises:
            BitbucketDCConflictError: When the edit cannot be applied safely.
        """
        key, slug = self._split_repo(repo_full_name)
        edits: Dict[str, Any] = {}
        if title is not None:
            edits["title"] = title
        if description is not None:
            edits["description"] = description
        if not edits:
            return await self.get_pull_request(pr_id, repo_full_name)
        path = self._pr_path(pr_id, repo_full_name)
        current = await self.get_pull_request(pr_id, repo_full_name)
        if current.get("state", "OPEN") != "OPEN":
            raise BitbucketDCConflictError(
                f"Pull request #{pr_id} is {current.get('state')}; only open "
                "pull requests can be edited."
            )
        baseline = {field: current.get(field) for field in edits}
        for attempt in (1, 2):
            body = {"version": current.get("version"), **edits}
            try:
                response = await self._request("PUT", path, json=body)
            except BitbucketDCConflictError:
                if attempt == 2:
                    raise
                current = await self.get_pull_request(pr_id, repo_full_name)
                changed = [f for f in edits if current.get(f) != baseline[f]]
                if changed or current.get("state", "OPEN") != "OPEN":
                    raise BitbucketDCConflictError(
                        f"Pull request #{pr_id} changed concurrently "
                        f"({', '.join(changed) or current.get('state')}); not retried."
                    )
                continue
            return self._assert_pull_request(response.json(), key, slug)
        raise AssertionError("unreachable")  # pragma: no cover

    def pull_request_url(
        self, pr_id: int | str, repo_full_name: Optional[str] = None
    ) -> str:
        """Return the web URL of a pull request on the instance."""
        key, slug = self._split_repo(repo_full_name)
        return pull_request_web_url(self.identity, key, slug, int(pr_id))

    async def merge_pull_request(self, *args: Any, **kwargs: Any) -> None:
        """Always refused: Preloop never merges pull requests."""
        raise BitbucketDCUnsupportedOperationError(
            "Preloop never merges Bitbucket pull requests."
        )

    async def decline_pull_request(self, *args: Any, **kwargs: Any) -> None:
        """Always refused: Preloop never declines pull requests."""
        raise BitbucketDCUnsupportedOperationError(
            "Preloop never declines Bitbucket pull requests."
        )

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

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
        start_line: Optional[int] = None,
        line_type: Optional[str] = None,
        from_hash: Optional[str] = None,
        to_hash: Optional[str] = None,
        diff_type: str = "EFFECTIVE",
        src_path: Optional[str] = None,
        task: bool = False,
    ) -> Dict[str, Any]:
        """Post a general, inline, multi-line or reply comment.

        Args:
            pr_id: Pull request id.
            body: Comment text (Markdown).
            repo_full_name: ``PROJECT/slug``; defaults to the bound one.
            path: File path for an inline comment.
            line: New-file line (``fileType=TO``).
            old_line: Old-file line (``fileType=FROM``).
            parent_id: Comment id to reply to. A reply inherits the parent's
                anchor; ``path``/``line`` are ignored for replies.
            start_line: First line of a multi-line range ending at ``line``.
            line_type: ``ADDED``/``REMOVED``/``CONTEXT`` override.
            from_hash: ``sinceId`` for ``COMMIT``/``RANGE`` anchors.
            to_hash: ``untilId`` for ``COMMIT``/``RANGE`` anchors.
            diff_type: ``EFFECTIVE`` (default), ``COMMIT`` or ``RANGE``.
            src_path: Previous path for moves and copies.
            task: Create the comment as a task (``severity=BLOCKER``).

        Returns:
            The created comment object (raw, carries ``version``).

        Raises:
            ValueError: For an inconsistent anchor.
        """
        if not body or not str(body).strip():
            raise ValueError("Comment text must not be empty.")
        payload: Dict[str, Any] = {"text": body}
        if parent_id is not None:
            payload["parent"] = {"id": self._comment_id(parent_id)}
        elif path:
            payload["anchor"] = build_comment_anchor(
                path=path,
                from_hash=from_hash,
                to_hash=to_hash,
                line=line,
                old_line=old_line,
                line_type=line_type,
                start_line=start_line,
                src_path=src_path,
                diff_type=diff_type,
            )
        elif line is not None or old_line is not None:
            raise ValueError("An inline comment needs a file path.")
        if task:
            payload["severity"] = "BLOCKER"
        resource = "blocker-comments" if task else "comments"
        response = await self._request(
            "POST", f"{self._pr_path(pr_id, repo_full_name)}/{resource}", json=payload
        )
        created = response.json()
        reject_cloud_payload(created, "comment")
        if not isinstance(created, Mapping):
            raise TrackerResponseError("Malformed comment response.")
        return dict(created)

    async def _versioned_comment_update(
        self,
        pr_id: int | str,
        comment_id: int | str,
        edits: Dict[str, Any],
        *,
        repo_full_name: Optional[str],
        guard_fields: Tuple[str, ...],
    ) -> Dict[str, Any]:
        """PUT a comment with its current ``version``; retry once on 409.

        The retry only happens when the fields named in ``guard_fields`` are
        unchanged by the concurrent edit; otherwise the conflict is raised.
        """
        path = f"{self._pr_path(pr_id, repo_full_name)}/comments/{self._comment_id(comment_id)}"
        current = await self.get_pull_request_comment(
            pr_id, comment_id, repo_full_name=repo_full_name
        )
        baseline = {f: current.get(f) for f in guard_fields}
        for attempt in (1, 2):
            body = {"version": current.get("version"), **edits}
            try:
                response = await self._request("PUT", path, json=body)
            except BitbucketDCConflictError:
                if attempt == 2:
                    raise
                current = await self.get_pull_request_comment(
                    pr_id, comment_id, repo_full_name=repo_full_name
                )
                changed = [f for f in guard_fields if current.get(f) != baseline[f]]
                if changed:
                    raise BitbucketDCConflictError(
                        f"Comment {comment_id} changed concurrently "
                        f"({', '.join(changed)}); not retried."
                    )
                if all(current.get(f) == v for f, v in edits.items()):
                    # The concurrent change already applied this exact edit.
                    return current
                continue
            data = response.json()
            reject_cloud_payload(data, "comment")
            if not isinstance(data, Mapping):
                raise TrackerResponseError("Malformed comment response.")
            return dict(data)
        raise AssertionError("unreachable")  # pragma: no cover

    async def update_pull_request_comment(
        self,
        pr_id: int | str,
        comment_id: int | str,
        body: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replace the text of a comment (versioned, one safe retry)."""
        return await self._versioned_comment_update(
            pr_id,
            comment_id,
            {"text": body},
            repo_full_name=repo_full_name,
            guard_fields=("text",),
        )

    async def delete_pull_request_comment(
        self,
        pr_id: int | str,
        comment_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Delete a comment (versioned, one retry when it is still deletable).

        Returns:
            True when the comment is gone afterwards.

        Raises:
            BitbucketDCConflictError: When the comment has replies or keeps
                changing.
        """
        path = f"{self._pr_path(pr_id, repo_full_name)}/comments/{self._comment_id(comment_id)}"
        current = await self.get_pull_request_comment(
            pr_id, comment_id, repo_full_name=repo_full_name
        )
        for attempt in (1, 2):
            try:
                await self._request(
                    "DELETE",
                    path,
                    params={"version": current.get("version")},
                    allow_status=(404,),
                )
            except BitbucketDCConflictError:
                if attempt == 2:
                    raise
                response = await self._request("GET", path, allow_status=(404,))
                if response.status_code == 404:
                    return True
                current = response.json()
                if current.get("comments"):
                    raise BitbucketDCConflictError(
                        f"Comment {comment_id} has replies and cannot be deleted."
                    )
                continue
            return True
        raise AssertionError("unreachable")  # pragma: no cover

    async def set_comment_resolved(
        self,
        pr_id: int | str,
        comment_id: int | str,
        resolved: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Resolve or reopen a thread, or a task when the comment is one.

        Tasks (``severity=BLOCKER``) toggle ``state``; ordinary comments
        toggle ``threadResolved``. Both are versioned with one safe retry.
        """
        current = await self.get_pull_request_comment(
            pr_id, comment_id, repo_full_name=repo_full_name
        )
        if str(current.get("severity") or "NORMAL").upper() == "BLOCKER":
            edits: Dict[str, Any] = {"state": "RESOLVED" if resolved else "OPEN"}
            guard: Tuple[str, ...] = ("text",)
        else:
            edits = {"threadResolved": bool(resolved)}
            guard = ("text",)
        await self._versioned_comment_update(
            pr_id, comment_id, edits, repo_full_name=repo_full_name, guard_fields=guard
        )
        return True

    # ------------------------------------------------------------------
    # Reviewer verdicts
    # ------------------------------------------------------------------

    async def _require_current_user(
        self, pr_id: int | str, repo_full_name: Optional[str]
    ) -> str:
        """Return the reviewer's slug, learning it from a response if needed."""
        if not self._current_user:
            await self.get_pull_request(pr_id, repo_full_name)
        if not self._current_user:
            raise BitbucketDCConfigError(
                "The reviewer's user slug is unknown. Set 'username' in the "
                "tracker connection details to the personal access token's user."
            )
        return self._current_user

    async def set_participant_status(
        self,
        pr_id: int | str,
        status: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set the current user's verdict via ``participants/{userSlug}``.

        Args:
            pr_id: Pull request id.
            status: ``APPROVED``, ``NEEDS_WORK`` or ``UNAPPROVED``.
            repo_full_name: ``PROJECT/slug``; defaults to the bound one.

        Returns:
            The participant object.

        Raises:
            ValueError: For an unknown status.
            BitbucketDCConflictError: When the pull request is not open.
        """
        verdict = str(status or "").upper()
        if verdict not in (
            PARTICIPANT_APPROVED,
            PARTICIPANT_NEEDS_WORK,
            PARTICIPANT_UNAPPROVED,
        ):
            raise ValueError(f"Unknown participant status {status!r}.")
        slug = await self._require_current_user(pr_id, repo_full_name)
        response = await self._request(
            "PUT",
            f"{self._pr_path(pr_id, repo_full_name)}/participants/{quote(slug, safe='')}",
            json={"status": verdict},
        )
        data = response.json()
        if not isinstance(data, dict) or data.get("status") != verdict:
            raise TrackerResponseError("Malformed reviewer verdict response.")
        return data

    async def set_approval(
        self,
        pr_id: int | str,
        approved: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Approve the pull request, or withdraw the approval."""
        await self.set_participant_status(
            pr_id,
            PARTICIPANT_APPROVED if approved else PARTICIPANT_UNAPPROVED,
            repo_full_name=repo_full_name,
        )
        return True

    async def set_changes_requested(
        self,
        pr_id: int | str,
        requested: bool,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Request changes (``NEEDS_WORK``) or clear the verdict."""
        await self.set_participant_status(
            pr_id,
            PARTICIPANT_NEEDS_WORK if requested else PARTICIPANT_UNAPPROVED,
            repo_full_name=repo_full_name,
        )
        return True

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def create_pull_request_task(
        self,
        pr_id: int | str,
        content: str,
        *,
        repo_full_name: Optional[str] = None,
        comment_id: Optional[int | str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a task (blocker comment), optionally as a reply.

        A 403 is logged and reported as None (skipped), never as success.
        """
        try:
            return await self.add_pull_request_comment(
                pr_id,
                content,
                repo_full_name=repo_full_name,
                parent_id=comment_id,
                task=True,
            )
        except TrackerPermissionError:
            logger.info(
                "Skipping Bitbucket Data Center task on PR %s: token lacks "
                "permission (403)",
                pr_id,
            )
            return None

    async def list_pull_request_tasks(
        self,
        pr_id: int | str,
        *,
        repo_full_name: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the pull request's tasks, normalised."""
        params: Dict[str, Any] = {}
        if state:
            params["state"] = str(state).upper()
        tasks = await self._paginate(
            f"{self._pr_path(pr_id, repo_full_name)}/blocker-comments", params=params
        )
        return [normalize_comment(task) for task in tasks]

    async def resolve_pull_request_task(
        self,
        pr_id: int | str,
        task_id: int | str,
        resolved: bool = True,
        *,
        repo_full_name: Optional[str] = None,
    ) -> bool:
        """Resolve or reopen a task."""
        return await self.set_comment_resolved(
            pr_id, task_id, resolved, repo_full_name=repo_full_name
        )

    # ------------------------------------------------------------------
    # Build status
    # ------------------------------------------------------------------

    async def create_commit_status(
        self,
        sha: str,
        state: str,
        context: str = "preloop",
        description: Optional[str] = None,
        target_url: Optional[str] = None,
        repo_full_name: Optional[str] = None,
        refname: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store a build status on a commit (``commits/{sha}/builds``).

        Args:
            sha: Commit hash.
            state: ``pending``, ``success``, ``failure``, ``error`` or
                ``cancelled``.
            context: Status key; also used as the display name.
            description: Short description.
            target_url: Absolute link shown next to the status; the
                repository page is used when missing.
            repo_full_name: ``PROJECT/slug``; defaults to the bound one.
            refname: Branch the build was for (``ref=refs/heads/<name>``).

        Returns:
            Dict with ``key``, ``state``, ``description`` and ``url``.

        Raises:
            ValueError: For an unknown state or malformed commit hash.
        """
        dc_state = BUILD_STATUS_STATES.get(str(state).lower())
        if dc_state is None:
            raise ValueError(f"Unsupported commit status state: {state}")
        commit = str(sha or "").strip()
        if not commit or not all(c in "0123456789abcdefABCDEF" for c in commit):
            raise ValueError(f"Invalid commit hash {sha!r}.")
        key, slug = self._split_repo(repo_full_name)
        status_key = (context or "preloop")[:255]
        url = target_url
        if not url or not str(url).startswith("https://"):
            url = repository_web_url(self.identity, key, slug)
        payload: Dict[str, Any] = {
            "key": status_key,
            "name": status_key,
            "state": dc_state,
            "url": url,
        }
        if description:
            payload["description"] = description[:255]
        if refname:
            payload["ref"] = full_ref(refname)
        await self._request(
            "POST",
            f"{self._repo_path(repo_full_name)}/commits/{quote(commit, safe='')}/builds",
            json=payload,
        )
        logger.info(
            "Posted Bitbucket Data Center build status '%s' (%s) on %s",
            status_key,
            state,
            commit[:8],
        )
        if not target_url or not str(target_url).startswith("https://"):
            current_key, current_slug = self._split_repo(repo_full_name)
            if self.repository_slug:
                current_slug = self.repository_slug
            url = repository_web_url(self.identity, current_key, current_slug)
        return {
            "key": status_key,
            "state": dc_state,
            "description": description,
            "url": url,
        }

    async def get_commit_status(
        self,
        sha: str,
        key: str,
        *,
        repo_full_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the build status with ``key`` on ``sha``, or None."""
        response = await self._request(
            "GET",
            f"{self._repo_path(repo_full_name)}/commits/{quote(str(sha), safe='')}/builds",
            params={"key": key},
            allow_status=(404,),
        )
        if response.status_code == 404:
            return None
        data = response.json()
        return dict(data) if isinstance(data, Mapping) else None

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Data Center comment onto the shared review comment shape."""
        return normalize_comment(comment)

    def object_attributes(self, pr: Dict[str, Any]) -> Dict[str, Any]:
        """Map a pull request onto the shared trigger ``object_attributes``."""
        return build_object_attributes(pr, self.identity)

    @staticmethod
    def author_handle(user: Any) -> Optional[str]:
        """Return a stable handle for a user object."""
        return user_name(user)

    # ------------------------------------------------------------------
    # Webhooks: explicitly unsupported in this adapter
    # ------------------------------------------------------------------

    async def register_webhook(self, **kwargs: Any) -> bool:
        """Unsupported: webhook ingestion is a separate integration."""
        raise BitbucketDCUnsupportedOperationError(_WEBHOOKS_UNSUPPORTED)

    async def unregister_webhook(self, **kwargs: Any) -> bool:
        """Unsupported: webhook ingestion is a separate integration."""
        raise BitbucketDCUnsupportedOperationError(_WEBHOOKS_UNSUPPORTED)

    async def is_webhook_registered(self, webhook: Webhook) -> bool:
        """Always False: this adapter manages no webhooks."""
        return False

    async def get_webhooks(self) -> List[Dict[str, Any]]:
        """Always empty: this adapter manages no webhooks."""
        return []

    async def delete_webhook(self, webhook: Dict[str, Any]) -> bool:
        """Unsupported: webhook ingestion is a separate integration."""
        raise BitbucketDCUnsupportedOperationError(_WEBHOOKS_UNSUPPORTED)

    async def unregister_all_webhooks(
        self, db: Session, webhook_url_pattern: Optional[str] = None
    ) -> Dict[str, int]:
        """Nothing to do: this adapter manages no webhooks."""
        return {"unregistered": 0, "failed": 0, "not_found": 0}

    async def is_webhook_registered_for_project(
        self, project: Project, webhook_url: str
    ) -> bool:
        """Always False: this adapter manages no webhooks."""
        return False

    async def is_webhook_registered_for_organization(
        self, organization: Organization, webhook_url: str
    ) -> bool:
        """Always False: this adapter manages no webhooks."""
        return False


def _error_detail(response: httpx.Response) -> str:
    """Extract the first ``errors[].message`` of a Data Center error body."""
    try:
        data = response.json()
    except ValueError:
        return (response.text or "")[:300]
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first)[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return str(data)[:300]


__all__ = [
    "CAPABILITIES",
    "UNSUPPORTED_OPERATIONS",
    "BitbucketDCConflictError",
    "BitbucketDCTracker",
    "BitbucketDCUnsupportedOperation",
    "BitbucketDCUnsupportedOperationError",
]
