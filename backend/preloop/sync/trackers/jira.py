"""
Jira tracker implementation for preloop.sync.
"""

import base64
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import logging
import urllib.parse
import requests

import httpx
from jira import JIRA, JIRAError
from sqlalchemy.orm import Session

from preloop.schemas.readiness import TicketCreationEvidence
from preloop.models.crud import (
    crud_organization,
    crud_project,
    crud_tracker,
    crud_webhook,
)
from preloop.services.issue_estimate import synced_estimate_fields
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
    TrackerError,
)
from preloop.services.managed_credentials import (
    CredentialSource,
    ManagedCredential,
    ManagedCredentialError,
    ManagedCredentialUnavailableError,
    is_managed_auth_type,
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

_URL_RE = re.compile(r"https?://[^\s<>\"]+")


# Atlassian API gateway: the only origin a managed (3LO) token is sent to.
ATLASSIAN_API_GATEWAY = "https://api.atlassian.com"
MANAGED_HTTP_TIMEOUT_SECONDS = 20.0
_CLOUD_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
# A Jira project key ("PRJ") or a numeric project id ("10000"); both are
# valid on the left of ``project =`` / ``project IN`` in dynamic-webhook JQL.
_PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}|[0-9]{1,18}")
# Events a managed tracker subscribes to with a dynamic webhook.
DYNAMIC_WEBHOOK_EVENTS = (
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_deleted",
    "comment_created",
    "comment_updated",
)
# A scoped API token cannot call the dynamic webhook API. Manual setup uses
# the issue and comment events flows actually consume.
MANUAL_JIRA_WEBHOOK_EVENTS = (
    "jira:issue_created",
    "jira:issue_updated",
    "comment_created",
    "comment_updated",
)
WEBHOOK_STATUS_MANUAL_REQUIRED = "manual_required"
_WEBHOOK_BLOCKED_STATUSES = frozenset({401, 403})
# One label under atlassian.net. The gateway host is api.atlassian.com and
# must not match, so a pasted gateway URL is not treated as a site.
_ATLASSIAN_CLOUD_HOST_RE = re.compile(
    r"(?i)^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.atlassian\.net$"
)


def _jira_timestamp(issue_data: Dict[str, Any], name: str) -> Optional[datetime]:
    """Read a Jira ``created``/``updated`` time as naive UTC.

    Webhook payloads may carry the value at the top level; REST results nest it
    under ``fields``. Jira writes ``2026-10-21T20:26:13.725+0000``; ISO forms
    with ``Z``, ``+00:00`` or no fraction are accepted too.

    Args:
        issue_data: Jira issue payload.
        name: ``created`` or ``updated``.

    Returns:
        The timestamp in naive UTC, or None when absent or unparseable.
    """
    fields = issue_data.get("fields")
    value = issue_data.get(name)
    if value is None and isinstance(fields, dict):
        value = fields.get(name)
    if isinstance(value, datetime):
        parsed: Optional[datetime] = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _safe_error_text(response: httpx.Response, limit: int = 300) -> str:
    """A bounded error body for messages; never includes request headers."""
    try:
        text = response.text or ""
    except Exception:  # noqa: BLE001 - undecodable body
        return ""
    return text[:limit]


def _adf_inline(line: str) -> List[Dict[str, Any]]:
    """ADF text nodes for one line, with URLs as links."""
    nodes: List[Dict[str, Any]] = []
    position = 0
    for match in _URL_RE.finditer(line):
        url = match.group(0).rstrip(".,;:)")
        end = match.start() + len(url)
        if match.start() > position:
            nodes.append({"type": "text", "text": line[position : match.start()]})
        nodes.append(
            {
                "type": "text",
                "text": url,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            }
        )
        position = end
    if position < len(line):
        nodes.append({"type": "text", "text": line[position:]})
    return nodes


def text_to_adf(text: str) -> Dict[str, Any]:
    """Convert plain text to an Atlassian Document Format document.

    Each non-empty line becomes a paragraph, and ``http(s)`` URLs become
    link marks so they are clickable in Jira.

    Args:
        text: Plain text, possibly multi-line.

    Returns:
        An ADF ``doc`` node.
    """
    paragraphs = [
        {"type": "paragraph", "content": _adf_inline(line)}
        for line in (text or "").splitlines()
        if line.strip()
    ]
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": paragraphs}


DEFAULT_JIRA_WEBHOOK_EVENTS = [
    "jira:issue_created",
    "jira:issue_updated",
    "comment_created",
]


def _webhook_status_blocked(status_code: Any) -> bool:
    """True only for a real 401 or 403.

    A ``MagicMock`` status compares equal to everything, so the check has to
    require an int. Otherwise a test double would look like a refused webhook.
    """
    return isinstance(status_code, int) and status_code in _WEBHOOK_BLOCKED_STATUSES


def pasted_gateway_site_detail(url: object) -> Optional[str]:
    """Reject a pasted Atlassian API gateway URL used as a Jira site.

    A scoped token is accepted at ``https://api.atlassian.com/ex/jira/<cloudId>``,
    but that host is not a site. Storing it names the organization
    ``api.atlassian.com`` and builds browse links on the gateway. The site
    URL is ``https://<site>.atlassian.net``.

    Args:
        url: Candidate site URL from the tracker row or connection details.

    Returns:
        A message for the caller, or None when the value is not the gateway.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    host = (urllib.parse.urlsplit(url.strip()).hostname or "").lower()
    if host != "api.atlassian.com":
        return None
    return (
        "Paste the Jira site URL (https://<site>.atlassian.net), "
        "not the Atlassian API gateway (https://api.atlassian.com)."
    )


def _webhook_id_from_registration(webhook_data: Any) -> Optional[str]:
    """Read a webhook id from a classic or dynamic registration body.

    Classic cloud returns ``self`` (and sometimes ``id``). The dynamic API
    returns ``webhookRegistrationResult[].createdWebhookId``.

    Args:
        webhook_data: Parsed JSON body.

    Returns:
        The id as a string, or None when the body has no created webhook.
    """
    if not isinstance(webhook_data, dict):
        return None
    webhook_id = webhook_data.get("id")
    self_url = webhook_data.get("self")
    if not webhook_id and isinstance(self_url, str) and self_url.strip():
        webhook_id = self_url.rstrip("/").split("/")[-1]
    results = webhook_data.get("webhookRegistrationResult")
    if not webhook_id and isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            created = item.get("createdWebhookId")
            if created is not None and str(created).strip():
                webhook_id = created
                break
    if webhook_id is None or not str(webhook_id).strip():
        return None
    return str(webhook_id)


def manual_webhook_record(webhook_url: str) -> Dict[str, Any]:
    """Organization callback instructions. The secret stays on the organization.

    Args:
        webhook_url: Callback passed into registration, before any project query.

    Returns:
        ``organization_id``, a query-free ``url``, and the events to select.
    """
    parsed = urllib.parse.urlsplit(webhook_url)
    clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    organization_id = parsed.path.rstrip("/").split("/")[-1]
    return {
        "organization_id": organization_id,
        "url": clean,
        "events": list(MANUAL_JIRA_WEBHOOK_EVENTS),
    }


class JiraWebhookCallbackConflictError(TrackerResponseError):
    """Provider requires an existing callback; never trust it without DB proof."""

    def __init__(self, callback_url: str) -> None:
        super().__init__("Jira requires the OAuth user's existing webhook callback.")
        self.callback_url = callback_url


class JiraTracker(BaseTracker):
    """Jira tracker implementation."""

    tracker_type: str = "jira"
    hosts_issues: bool = True

    def __init__(
        self,
        tracker_id: str,
        api_key: str,
        connection_details: Dict[str, Any],
        *,
        initialize_client: bool = True,
        credential_source: Optional[CredentialSource] = None,
    ) -> None:
        """
        Initialize the Jira tracker.

        Args:
            tracker_id: Tracker id.
            api_key: Atlassian API token for Basic auth; empty when managed.
            connection_details: ``url``/``jira_url`` and ``username`` for
                Basic auth. A scoped token also stores ``cloud_id`` after
                the site returns 401; links keep using the site URL. A
                managed grant (``auth_type: managed_oauth``) carries
                ``cloud_id`` and ``site_url`` instead.
            initialize_client: Build the ``jira`` client (Basic auth only).
            credential_source: Managed grants only: async callable returning
                a fresh ``ManagedCredential`` before every request.
        """
        super().__init__(tracker_id, api_key, connection_details)

        self.managed: bool = credential_source is not None or is_managed_auth_type(
            connection_details.get("auth_type")
        )
        self._credential_source = credential_source
        self._last_credential: Optional[ManagedCredential] = None
        self.cloud_id: Optional[str] = None
        self._scoped_gateway = False
        self._gateway_probe_done = False
        self._cloud_id_from_config = False
        self.webhook_status: Optional[str] = None

        jira_url = (
            (
                connection_details.get("site_url")
                if self.managed and connection_details.get("site_url")
                else None
            )
            or connection_details.get("jira_url")
            or connection_details.get("url")
        )
        if not jira_url:
            raise ValueError("Jira URL is required in connection_details")

        if self.managed:
            self._init_managed(api_key, connection_details, jira_url)
            return

        if initialize_client and "username" not in connection_details:
            raise ValueError("Jira username is required in connection_details")

        self.jira_url = jira_url.rstrip("/")
        self.base_url = self.jira_url  # Alias for compatibility
        self.username = connection_details.get("username", "")
        # Classic tokens call the site. A stored cloud_id means a previous
        # 401 already proved this token only works on the gateway.
        self._configure_basic_origin(connection_details)

        auth_str = f"{self.username}:{api_key}"
        encoded_auth = base64.b64encode(auth_str.encode()).decode()
        self.headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/json",
        }

        self.jira_client: Optional[JIRA] = None
        if initialize_client and self.jira_url and self.username and api_key:
            try:
                self.jira_client = JIRA(
                    server=self.jira_url,
                    basic_auth=(self.username, api_key),
                    timeout=20,
                    max_retries=3,
                )
            except JIRAError as e:
                # A scoped token is rejected by the site during the library
                # client's serverInfo call. Leave the client unset so
                # test_connection can promote REST to the gateway. A classic
                # token that the site accepts still builds the client here.
                if (
                    e.status_code == HTTP_STATUS_UNAUTHORIZED
                    and self._site_is_atlassian_cloud()
                ):
                    logger.info(
                        "Jira site rejected the API token for tracker %s; "
                        "the gateway probe runs on the next request.",
                        self.tracker_id,
                    )
                elif e.status_code == HTTP_STATUS_UNAUTHORIZED:
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

    def _site_host(self) -> str:
        """Hostname of the human-facing Jira site, without a port.

        A non-string URL (a test double, or a row that never stored one) is
        not a cloud site. ``urlsplit`` cannot parse those values.
        """
        url = self.jira_url
        if not isinstance(url, str) or not url.strip():
            return ""
        return (urllib.parse.urlsplit(url).hostname or "").lower()

    def _site_is_atlassian_cloud(self) -> bool:
        """True for ``<site>.atlassian.net``, never for the API gateway."""
        return _ATLASSIAN_CLOUD_HOST_RE.fullmatch(self._site_host()) is not None

    def _configure_basic_origin(self, connection_details: Dict[str, Any]) -> None:
        """Point REST at the gateway when a scoped token's cloud_id is stored.

        The site URL stays on ``jira_url`` / ``base_url`` for names and links.
        """
        stored = str(connection_details.get("cloud_id") or "").strip()
        status = connection_details.get("webhook_status")
        if status == WEBHOOK_STATUS_MANUAL_REQUIRED:
            self.webhook_status = status
        if self._site_is_atlassian_cloud() and _CLOUD_ID_RE.fullmatch(stored):
            self.cloud_id = stored
            self.api_root = f"{ATLASSIAN_API_GATEWAY}/ex/jira/{stored}"
            self._scoped_gateway = True
            self._cloud_id_from_config = True
            self._gateway_probe_done = True
            return
        self.api_root = self.jira_url
        self._scoped_gateway = False

    def _remember_cloud_id(self) -> None:
        """Keep the resolved cloud id on the client, never the token."""
        if not self._scoped_gateway or not self.cloud_id:
            return
        if isinstance(self.connection_details, dict):
            self.connection_details["cloud_id"] = self.cloud_id
            self.connection_details.pop("api_key", None)
            self.connection_details.pop("token", None)

    def _rollback_promoted_gateway(self) -> None:
        """Drop a cloud id learned during this call when the gateway rejects it."""
        if self._cloud_id_from_config:
            return
        self.cloud_id = None
        self._scoped_gateway = False
        self.api_root = self.jira_url
        if isinstance(self.connection_details, dict):
            self.connection_details.pop("cloud_id", None)

    def _basic_client_kwargs(self) -> Dict[str, Any]:
        """httpx options for a Basic-auth call. Gateway calls are pinned."""
        kwargs: Dict[str, Any] = {"follow_redirects": False}
        if self._scoped_gateway:
            kwargs["timeout"] = MANAGED_HTTP_TIMEOUT_SECONDS
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return kwargs

    def _refuse_token_in_url(self, url: str) -> None:
        """A pasted token must never be written into a request URL."""
        if self.api_key and self.api_key in url:
            raise TrackerConnectionError("Refusing to place a Jira token in a URL.")

    async def _send_basic(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        api_version: str,
    ) -> httpx.Response:
        """One Basic-auth REST call against the current API origin."""
        url = f"{self.api_root}/rest/api/{api_version}/{endpoint.lstrip('/')}"
        self._refuse_token_in_url(url)
        async with httpx.AsyncClient(**self._basic_client_kwargs()) as client:
            return await client.request(
                method.upper(),
                url,
                headers=self.headers,
                params=params,
                json=json_data,
            )

    async def _read_tenant_cloud_id(self) -> Optional[str]:
        """Read ``cloudId`` from the site edge. The token is not sent."""
        url = f"{self.jira_url}/_edge/tenant_info"
        self._refuse_token_in_url(url)
        kwargs = self._basic_client_kwargs()
        # tenant_info is public. Do not attach the Basic header, and do not
        # follow a redirect that could carry a later retry somewhere else.
        kwargs.pop("timeout", None)
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        cloud_id = str(payload.get("cloudId") or "").strip()
        if not _CLOUD_ID_RE.fullmatch(cloud_id):
            return None
        return cloud_id

    async def _promote_scoped_gateway(self) -> bool:
        """Switch REST to the gateway after the site rejects a scoped token."""
        try:
            cloud_id = await self._read_tenant_cloud_id()
        except httpx.RequestError:
            logger.info(
                "Could not read Atlassian tenant info for tracker %s", self.tracker_id
            )
            return False
        if not cloud_id:
            return False
        self.cloud_id = cloud_id
        self.api_root = f"{ATLASSIAN_API_GATEWAY}/ex/jira/{cloud_id}"
        self._scoped_gateway = True
        logger.info(
            "Jira tracker %s is using the Atlassian API gateway for cloud %s",
            self.tracker_id,
            cloud_id,
        )
        return True

    def _interpret_basic_response(self, response: httpx.Response) -> Any:
        """Turn a Basic-auth response into a body or a typed tracker error."""
        status_code = response.status_code
        if self._scoped_gateway and 300 <= status_code < 400:
            raise TrackerResponseError(
                "Refusing to follow a Jira redirect with an API token."
            )
        if status_code == HTTP_STATUS_UNAUTHORIZED:
            raise TrackerAuthenticationError("Jira authentication failed")
        if HTTP_SUCCESS_MIN <= status_code <= HTTP_SUCCESS_MAX:
            self._remember_cloud_id()
            if status_code == HTTP_STATUS_NO_CONTENT or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text
        body = (
            _safe_error_text(response)
            if self._scoped_gateway
            else (response.text or "")
        )
        raise TrackerResponseError(f"Jira API error: {status_code} - {body}")

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        api_version: str = "2",
    ) -> Any:
        """Make a request to the Jira API using httpx."""
        if self.managed:
            return await self._make_managed_request(
                method,
                endpoint,
                params=params,
                json_data=json_data,
                api_version=api_version,
            )
        try:
            response = await self._send_basic(
                method, endpoint, params, json_data, api_version
            )
        except httpx.RequestError as e:
            raise TrackerConnectionError(f"Jira connection error: {str(e)}") from e

        promoted = False
        if (
            response.status_code == HTTP_STATUS_UNAUTHORIZED
            and not self._scoped_gateway
            and not self._gateway_probe_done
            and self._site_is_atlassian_cloud()
        ):
            self._gateway_probe_done = True
            promoted = await self._promote_scoped_gateway()
            if promoted:
                try:
                    response = await self._send_basic(
                        method, endpoint, params, json_data, api_version
                    )
                except httpx.RequestError as e:
                    self._rollback_promoted_gateway()
                    raise TrackerConnectionError(
                        f"Jira connection error: {str(e)}"
                    ) from e
                if not (HTTP_SUCCESS_MIN <= response.status_code <= HTTP_SUCCESS_MAX):
                    try:
                        return self._interpret_basic_response(response)
                    finally:
                        self._rollback_promoted_gateway()

        return self._interpret_basic_response(response)

    # ------------------------------------------------------------------
    # Managed (Atlassian OAuth 2.0 3LO) transport
    # ------------------------------------------------------------------

    def _init_managed(
        self, api_key: str, connection_details: Dict[str, Any], site_url: str
    ) -> None:
        """Configure a managed grant: Bearer through the pinned gateway.

        Raises:
            ValueError: A pasted token, a missing or malformed ``cloud_id``, or
                a non-HTTPS site URL.
        """
        if api_key:
            raise ValueError(
                "A managed Jira connection does not accept a pasted token."
            )
        cloud_id = str(connection_details.get("cloud_id") or "").strip()
        if not _CLOUD_ID_RE.fullmatch(cloud_id):
            raise ValueError(
                "A managed Jira connection requires the Atlassian site cloud_id."
            )
        site = site_url.rstrip("/")
        if urllib.parse.urlsplit(site).scheme != "https":
            raise ValueError("A managed Jira site URL must use https.")
        self.cloud_id = cloud_id
        # Human-facing links (browse, comment anchors) use the site; every
        # API call goes to the gateway, the only origin the token may reach.
        self.jira_url = site
        self.base_url = site
        self.api_root = f"{ATLASSIAN_API_GATEWAY}/ex/jira/{cloud_id}"
        self.username = ""
        self.headers = {"Content-Type": "application/json"}
        self.jira_client = None

    @property
    def managed_credential(self) -> Optional[ManagedCredential]:
        """The credential used by the most recent managed request, if any."""
        return self._last_credential

    async def _managed_token(self, *, force_refresh: bool = False) -> str:
        """Resolve the current managed access token before a request.

        Raises:
            TrackerAuthenticationError: No resolver is installed, the grant
                needs reconnect, or the provider refused.
        """
        if self._credential_source is None:
            error = ManagedCredentialUnavailableError(
                "resolver_missing", provider=self.tracker_type
            )
            raise TrackerAuthenticationError(error.actionable_message())
        try:
            credential = await self._credential_source(force_refresh=force_refresh)
        except ManagedCredentialError as exc:
            if exc.provider is None:
                exc.provider = self.tracker_type
            raise TrackerAuthenticationError(exc.actionable_message()) from exc
        self._last_credential = credential
        return credential.access_token

    async def _send_managed(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]],
        json_data: Optional[Any],
        token: str,
    ) -> httpx.Response:
        headers = dict(self.headers)
        headers["Accept"] = "application/json"
        headers["Authorization"] = f"Bearer {token}"
        # The token is pinned to the gateway origin: a redirect could carry it
        # elsewhere, so it is reported instead of followed.
        async with httpx.AsyncClient(
            timeout=MANAGED_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            return await client.request(
                method.upper(), url, headers=headers, params=params, json=json_data
            )

    _transport: Optional[httpx.AsyncBaseTransport] = None

    async def _make_managed_request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        api_version: str = "2",
    ) -> Any:
        """Send one gateway request with a freshly resolved Bearer token.

        A 401 forces exactly one rotation and retry; a second 401 is
        reported. There is no Basic fallback and no stale-token fallback.
        """
        url = f"{self.api_root}/rest/api/{api_version}/{endpoint.lstrip('/')}"
        try:
            token = await self._managed_token()
            response = await self._send_managed(
                method, url, params=params, json_data=json_data, token=token
            )
            if response.status_code == HTTP_STATUS_UNAUTHORIZED:
                token = await self._managed_token(force_refresh=True)
                response = await self._send_managed(
                    method, url, params=params, json_data=json_data, token=token
                )
        except httpx.RequestError as e:
            raise TrackerConnectionError(
                f"Jira connection error: {type(e).__name__}"
            ) from e

        status_code = response.status_code
        if 300 <= status_code < 400:
            raise TrackerResponseError(
                "Refusing to follow a Jira redirect with a managed credential."
            )
        if status_code == HTTP_STATUS_UNAUTHORIZED:
            raise TrackerAuthenticationError(
                "Jira refused the managed credential after a refresh. Reconnect "
                "the Jira connection from the tracker page."
            )
        if HTTP_SUCCESS_MIN <= status_code <= HTTP_SUCCESS_MAX:
            if status_code == HTTP_STATUS_NO_CONTENT or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text
        raise TrackerResponseError(
            f"Jira API error: {status_code} - {_safe_error_text(response)}"
        )

    def _handle_jira_error(self, error: Exception, context: str) -> None:
        """Raise the typed tracker error for a ``jira``/``requests`` failure.

        Args:
            error: ``JIRAError`` or ``requests`` exception.
            context: What was being attempted, for the message.

        Raises:
            TrackerAuthenticationError: 401.
            TrackerResponseError: Any other HTTP status.
            TrackerConnectionError: No HTTP status (network failure).
        """
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)
        text = getattr(error, "text", None) or str(error)
        if status_code == HTTP_STATUS_UNAUTHORIZED:
            raise TrackerAuthenticationError(
                f"Jira authentication failed while {context}"
            ) from error
        if status_code is None:
            raise TrackerConnectionError(
                f"Jira connection error while {context}: {text}"
            ) from error
        raise TrackerResponseError(
            f"Jira API error {status_code} while {context}: {text}"
        ) from error

    # ------------------------------------------------------------------
    # Dynamic webhooks (managed grants only)
    # ------------------------------------------------------------------

    @staticmethod
    def dynamic_webhook_jql(project_keys: List[str]) -> str:
        """JQL filter covering every bound project.

        Dynamic webhooks accept only ``project`` with ``=``/``IN`` here.

        Raises:
            ValueError: No keys, or a value that is neither a Jira project key
                nor a numeric project id.
        """
        keys = sorted({str(k).strip().upper() for k in project_keys if str(k).strip()})
        if not keys:
            raise ValueError("At least one Jira project key is required.")
        for key in keys:
            if not _PROJECT_KEY_RE.fullmatch(key):
                raise ValueError(f"Invalid Jira project key: {key!r}")
        if len(keys) == 1:
            return f'project = "{keys[0]}"'
        return "project IN (" + ", ".join(f'"{k}"' for k in keys) + ")"

    async def register_dynamic_webhook(
        self, webhook_url: str, project_keys: List[str]
    ) -> str:
        """Register one dynamic webhook covering ``project_keys``.

        One webhook per tracker keeps the grant well under Atlassian's limit
        of 5 dynamic webhooks per OAuth app, user and site.

        Returns:
            The ``createdWebhookId`` as a string.

        Raises:
            TrackerResponseError: Atlassian rejected the registration.
        """
        if not self.managed:
            raise ValueError("Dynamic webhooks are only used by managed Jira trackers.")
        body = {
            "url": webhook_url,
            "webhooks": [
                {
                    "jqlFilter": self.dynamic_webhook_jql(project_keys),
                    "events": list(DYNAMIC_WEBHOOK_EVENTS),
                }
            ],
        }
        result = await self._make_request(
            "POST", "webhook", json_data=body, api_version="3"
        )
        entries = (result or {}).get("webhookRegistrationResult") or []
        first = entries[0] if entries else {}
        created = first.get("createdWebhookId")
        if created is None:
            errors = first.get("errors") or ["no webhook id returned"]
            for error in errors:
                match = re.fullmatch(
                    r"Only a single URL per user is allowed to be registered via REST API\. "
                    r"The currently used URL: (\S+)",
                    str(error),
                )
                if match:
                    raise JiraWebhookCallbackConflictError(match.group(1))
            raise TrackerResponseError(
                "Jira rejected the dynamic webhook: " + "; ".join(map(str, errors))
            )
        return str(created)

    @staticmethod
    def _project_jql_key(project: Any) -> str:
        """The Jira project key (``Project.slug``), else the numeric id.

        ``Project.identifier`` holds Jira's numeric project id; the key is the
        slug. Either is valid JQL, the key reads better in Jira's UI.
        """
        return str(getattr(project, "slug", None) or project.identifier)

    async def list_dynamic_webhook_ids(self) -> set[str]:
        """Ids of the dynamic webhooks this app registered (bounded pages)."""
        ids: set[str] = set()
        start = 0
        for _ in range(10):
            page = await self._make_request(
                "GET",
                "webhook",
                params={"startAt": start, "maxResults": 100},
                api_version="3",
            )
            values = (page or {}).get("values") or [] if isinstance(page, dict) else []
            ids.update(str(v.get("id")) for v in values if isinstance(v, dict))
            if not values or (page or {}).get("isLast", True):
                break
            start += len(values)
        return ids

    async def refresh_dynamic_webhooks(self, webhook_ids: List[str]) -> Optional[str]:
        """Extend dynamic webhooks by another 30 days.

        Returns:
            The new ``expirationDate`` reported by Jira, if any.
        """
        ids = [int(i) for i in webhook_ids if str(i).strip().isdigit()]
        if not ids:
            return None
        result = await self._make_request(
            "PUT", "webhook/refresh", json_data={"webhookIds": ids}, api_version="3"
        )
        expiration = (
            (result or {}).get("expirationDate") if isinstance(result, dict) else None
        )
        return str(expiration) if expiration is not None else None

    async def delete_dynamic_webhooks(self, webhook_ids: List[str]) -> None:
        """Delete dynamic webhooks; unknown ids are ignored by Jira."""
        ids = [int(i) for i in webhook_ids if str(i).strip().isdigit()]
        if ids:
            await self._make_request(
                "DELETE", "webhook", json_data={"webhookIds": ids}, api_version="3"
            )

    def _validated_shared_callback(
        self, db: Session, requested_url: str, callback_url: str
    ) -> str:
        """Accept an existing callback only with exact local registration proof."""
        requested = urllib.parse.urlsplit(requested_url)
        callback = urllib.parse.urlsplit(callback_url)
        prefix = requested.path.rsplit("/", 1)[0] + "/"
        try:
            import uuid

            anchor_id = uuid.UUID(callback.path.removeprefix(prefix))
        except ValueError:
            anchor_id = None
        if (
            requested.scheme != "https"
            or (callback.scheme, callback.netloc)
            != (requested.scheme, requested.netloc)
            or callback.username
            or callback.password
            or callback.query
            or callback.fragment
            or not callback.path.startswith(prefix)
            or anchor_id is None
            or any(ord(c) < 33 for c in callback_url)
        ):
            raise TrackerResponseError(
                "Jira callback conflict requires administrator attention."
            )
        context = crud_webhook.jira_callback_context(
            db, tracker_id=str(self.tracker_id)
        )
        if context:
            projects = crud_webhook.get_jira_callback_projects(
                db,
                callback_url=callback_url,
                cloud_id=context[0],
                provider_subject=context[1],
            )
            anchor = crud_organization.get_with_tracker(db, id=str(anchor_id))
            anchor_context = (
                crud_webhook.jira_callback_context(
                    db, tracker_id=str(anchor.tracker_id)
                )
                if anchor is not None
                else None
            )
            stored_context = crud_webhook.jira_callback_namespace(
                db, callback_url=callback_url
            )
            if projects and context in (anchor_context, stored_context):
                return callback_url
        raise TrackerResponseError(
            "Jira callback conflict has no authorized local registration."
        )

    async def sync_dynamic_webhooks(
        self, db: Session, projects: List[Project], webhook_url: str
    ) -> Dict[str, Any]:
        """Keep one dynamic webhook covering ``projects`` alive.

        The webhook id is stored on each project's ``Webhook`` row. When the
        covered project set and URL are unchanged the webhook is refreshed
        (``PUT /rest/api/3/webhook/refresh``, another 30 days); otherwise the
        old one is deleted and a new one registered.

        Returns:
            ``{"action": "refreshed"|"registered"|"none", "webhook_id": ...}``.
        """
        if not projects:
            return {"action": "none", "webhook_id": None}
        context = crud_webhook.jira_callback_context(
            db, tracker_id=str(self.tracker_id)
        )
        if context is None:
            raise TrackerResponseError(
                "Jira callback registration requires a bound managed grant."
            )
        rows = {
            project.id: crud_webhook.get_by_project_id(db, project_id=project.id)
            for project in projects
        }
        existing_ids = {row.external_id for row in rows.values() if row is not None}
        existing_urls = {row.url for row in rows.values() if row is not None}
        effective_url = webhook_url
        if len(existing_urls) == 1 and webhook_url not in existing_urls:
            effective_url = self._validated_shared_callback(
                db, webhook_url, next(iter(existing_urls))
            )
        coverage_ok = (
            len(existing_ids) == 1
            and all(row is not None for row in rows.values())
            and all(row.url == effective_url for row in rows.values())
        )
        if coverage_ok:
            webhook_id = next(iter(existing_ids))
            # A stored row is not proof: the webhook may have been removed on
            # the Jira side or purged after expiry. Refresh only one that the
            # app still owns; otherwise register a replacement below.
            if webhook_id in await self.list_dynamic_webhook_ids():
                expires = await self.refresh_dynamic_webhooks([webhook_id])
                crud_webhook.set_jira_callback_namespace(
                    db,
                    tracker_id=str(self.tracker_id),
                    external_id=webhook_id,
                    callback_url=effective_url,
                    cloud_id=context[0],
                    provider_subject=context[1],
                )
                return {
                    "action": "refreshed",
                    "webhook_id": webhook_id,
                    "expires": expires,
                }

        keys = [self._project_jql_key(project) for project in projects]
        try:
            webhook_id = await self.register_dynamic_webhook(effective_url, keys)
        except JiraWebhookCallbackConflictError as exc:
            effective_url = self._validated_shared_callback(
                db, webhook_url, exc.callback_url
            )
            webhook_id = await self.register_dynamic_webhook(effective_url, keys)
        stale = [i for i in existing_ids if i != webhook_id]
        for row in rows.values():
            if row is not None:
                crud_webhook.remove(db, id=row.id)
        for project in projects:
            crud_webhook.create(
                db,
                obj_in={
                    "project_id": project.id,
                    "external_id": webhook_id,
                    "url": effective_url,
                    "jira_cloud_id": context[0],
                    "jira_provider_subject": context[1],
                    # Dynamic deliveries are authenticated by the app's JWT,
                    # not by a per-hook secret.
                    "secret": "",
                    "events": list(DYNAMIC_WEBHOOK_EVENTS),
                },
            )
        if stale:
            try:
                await self.delete_dynamic_webhooks(stale)
            except Exception as e:  # noqa: BLE001 - expiry removes leftovers
                logger.warning(
                    "Could not delete superseded Jira dynamic webhooks for tracker %s: %s",
                    self.tracker_id,
                    type(e).__name__,
                )
        return {"action": "registered", "webhook_id": webhook_id}

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

    async def get_ticket_creation_evidence(
        self, issue_key: str
    ) -> "TicketCreationEvidence":
        """Fetch authoritative Jira creation without the mapper's fallback."""
        from datetime import UTC
        from uuid import UUID

        from preloop.schemas.readiness import parse_jira_created

        from urllib.parse import quote

        reason = "ticket_creation_unavailable"
        created = None
        try:
            data = await self._make_request(
                "GET",
                f"issue/{quote(issue_key, safe='')}",
                params={"fields": "created"},
                api_version="3",
            )
            if isinstance(data, dict) and data.get("key") == issue_key:
                created = parse_jira_created((data.get("fields") or {}).get("created"))
            else:
                reason = "ticket_identity_mismatch"
        except TrackerError:
            reason = "ticket_creation_unreadable"
        return TicketCreationEvidence(
            created_at=created,
            tracker_id=UUID(self.tracker_id),
            issue_key=issue_key,
            retrieved_at=datetime.now(UTC),
            reason=None if created is not None else reason,
        )

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

    @staticmethod
    def _adf_to_plain_text(adf_node: Optional[Any]) -> str:
        """Converts Jira Atlassian Document Format (ADF) to plain text.

        A static method so callers that only have a webhook body can render
        it without constructing a tracker client.
        """
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
        # The body is Atlassian Document Format, which only REST v3 accepts:
        # v2 takes a plain string and rejects an ADF object.
        comment_body = {"body": text_to_adf(comment)}

        # Add the comment
        comment_data = await self._make_request(
            "POST",
            f"issue/{issue_id}/comment",
            json_data=comment_body,
            api_version="3",
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

    async def add_remote_link(
        self,
        issue_id: str,
        *,
        global_id: str,
        url: str,
        title: str,
        summary: Optional[str] = None,
        resolved: bool = False,
        relationship: str = "implemented by",
    ) -> Dict[str, Any]:
        """Create or update a remote issue link on a Jira issue.

        ``POST /rest/api/3/issue/{issueIdOrKey}/remotelink`` updates the link
        whose ``globalId`` matches and creates one otherwise, so a stable
        ``global_id`` makes a repeated call update the same link. Fields left
        out of the request are set to null on update, so the full object is
        always sent. Requires issue linking to be enabled on the site.

        Args:
            issue_id: Issue key or numeric id.
            global_id: Stable identifier of the remote object.
            url: Remote object URL (the pull request).
            title: Link title shown on the issue.
            summary: Optional one-line summary.
            resolved: Whether the remote object is done (shown struck through).
            relationship: Relationship label shown on the issue.

        Returns:
            Jira's response: the link ``id`` and ``self`` URL.
        """
        remote_object: Dict[str, Any] = {
            "url": url,
            "title": title,
            "status": {"resolved": bool(resolved)},
        }
        if summary:
            remote_object["summary"] = summary
        payload = {
            "globalId": global_id,
            "application": {"type": "ai.preloop", "name": "Preloop"},
            "relationship": relationship,
            "object": remote_object,
        }
        response = await self._make_request(
            "POST",
            f"issue/{issue_id}/remotelink",
            json_data=payload,
            api_version="3",
        )
        return response if isinstance(response, dict) else {}

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
                    "identifier": str(project["id"]),
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

        # Extract timestamps. REST search results nest them under ``fields``;
        # reading only the top level stamped every synced issue with ``now()``.
        created_at = _jira_timestamp(issue_data, "created")
        updated_at = _jira_timestamp(issue_data, "updated")

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
            # Never the sync time: a resync is not activity on the issue.
            "last_updated_external": updated_at,
            "last_synced": datetime.now(),
            "external_url": issue_url,
            "meta_data": {
                "labels": labels,
                "assignees": assignees,
                "url": issue_url,
                "source": "preloop-sync",
                # Raw "Original Estimate" (seconds) for the issue cost report.
                "estimate_fields": synced_estimate_fields(issue_data),
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
                "timeoriginalestimate",
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

    def _post_webhook(self, url: str, payload: Dict[str, Any]) -> httpx.Response:
        """POST a webhook body with Basic auth. Redirects are not followed."""
        self._refuse_token_in_url(url)
        headers = {
            "Authorization": self.headers.get("Authorization", ""),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client(**self._basic_client_kwargs()) as client:
            return client.post(url, headers=headers, json=payload)

    def _register_webhook_alternate(
        self, payload: Dict[str, Any]
    ) -> Optional[httpx.Response]:
        """Try the other host and the dynamic webhook API.

        Returns:
            A response that is not 401 or 403, or None when every attempt is
            blocked. A redirect is refused instead of followed.
        """
        site = self.jira_url.rstrip("/")
        gateway = self.api_root.rstrip("/")
        if gateway != site:
            classic = self._post_webhook(
                f"{gateway}/rest/webhooks/1.0/webhook", payload
            )
            if _webhook_status_blocked(classic.status_code):
                pass
            elif (
                isinstance(classic.status_code, int)
                and 300 <= classic.status_code < 400
            ):
                raise TrackerResponseError(
                    "Refusing to follow a Jira redirect with an API token."
                )
            else:
                return classic
        dynamic = {
            "url": payload.get("url"),
            "webhooks": [
                {
                    "events": list(
                        payload.get("events") or DEFAULT_JIRA_WEBHOOK_EVENTS
                    ),
                    "jqlFilter": payload.get("jqlFilter") or "",
                }
            ],
        }
        response = self._post_webhook(f"{gateway}/rest/api/3/webhook", dynamic)
        if _webhook_status_blocked(response.status_code):
            return None
        if isinstance(response.status_code, int) and 300 <= response.status_code < 400:
            raise TrackerResponseError(
                "Refusing to follow a Jira redirect with an API token."
            )
        return response

    def _record_manual_webhook(self, db: Optional[Session], webhook_url: str) -> None:
        """Remember that an admin has to create the webhook. Do not store the token."""
        instructions = manual_webhook_record(webhook_url)
        self.webhook_status = WEBHOOK_STATUS_MANUAL_REQUIRED
        if isinstance(self.connection_details, dict):
            self.connection_details["webhook_status"] = WEBHOOK_STATUS_MANUAL_REQUIRED
            self.connection_details["manual_webhook"] = instructions
            self.connection_details.pop("api_key", None)
            self.connection_details.pop("token", None)
        if db is None:
            return
        row = crud_tracker.get(db, id=self.tracker_id)
        if row is None:
            return
        details = dict(row.connection_details or {})
        details["webhook_status"] = WEBHOOK_STATUS_MANUAL_REQUIRED
        details["manual_webhook"] = instructions
        details.pop("api_key", None)
        details.pop("token", None)
        crud_tracker.update(db, db_obj=row, obj_in={"connection_details": details})

    def register_webhook(
        self,
        db: Optional[Session] = None,
        project: Optional[Project] = None,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        events: Optional[List[str]] = None,
    ) -> bool:
        """Register a webhook for the Jira project."""
        # A scoped token never gets a site-bound library client (the site
        # returns 401 from serverInfo). Cloud registration still posts with
        # Basic auth and records manual setup when Jira refuses.
        if not self.jira_client and not self._site_is_atlassian_cloud():
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

            classic_url = f"{self.jira_url}/rest/webhooks/1.0/webhook"
            if self.jira_client is not None:
                try:
                    response = self.jira_client._session.post(
                        classic_url,
                        json=webhook_payload,
                    )
                except JIRAError as error:
                    # The library raises for denied requests instead of returning
                    # a response. Scoped tokens must still try the fallback APIs.
                    if not self._scoped_gateway or not _webhook_status_blocked(
                        error.status_code
                    ):
                        raise
                    response = None
            else:
                response = self._post_webhook(classic_url, webhook_payload)
            if response is None or _webhook_status_blocked(response.status_code):
                alternate = self._register_webhook_alternate(webhook_payload)
                if alternate is None:
                    self._record_manual_webhook(db, webhook_url)
                    logger.info(
                        "Jira webhook registration is unavailable for tracker %s; "
                        "manual setup is required.",
                        self.tracker_id,
                    )
                    return False
                response = alternate

            # Log the response details for debugging
            logger.info(
                f"Webhook registration response status: {response.status_code}, "
                f"Response text: {response.text[:500]}"
            )

            response.raise_for_status()
            webhook_data = response.json()

            # Classic cloud puts the id in ``self``. The dynamic API puts it
            # in webhookRegistrationResult[].createdWebhookId.
            webhook_id = _webhook_id_from_registration(webhook_data)

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

            # Verify the webhook was actually created by fetching it back (with retries).
            # A scoped token has no library session; the 2xx body is the record.
            max_retries = 3
            verified = self.jira_client is None
            for attempt in range(max_retries):
                if self.jira_client is None:
                    break
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
        except TrackerAuthenticationError:
            if not self._scoped_gateway:
                raise
            # Scoped tokens can read issues through the gateway while Jira
            # denies webhook administration on the site. Let registration try
            # the supported endpoints and persist manual setup if all refuse.
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
