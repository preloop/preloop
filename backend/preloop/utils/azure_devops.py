"""Azure DevOps Services (Azure Repos) helpers.

Pure functions for the ``azure_devops`` tracker: configuration validation,
URL parsing, the service hook authentication check, normalization of
pull request service hook deliveries into Preloop's trigger contract, and the
delivery identity used for replay safety. Nothing here touches the database
or the network.

Contract, from Microsoft's documentation:

* REST: ``https://dev.azure.com/{organization}/{project}/_apis/git/...`` with
  ``api-version=7.1`` (https://learn.microsoft.com/rest/api/azure/devops/git/).
* A personal access token is sent as the password of HTTP Basic auth with an
  empty user name (https://learn.microsoft.com/azure/devops/organizations/
  accounts/use-personal-access-tokens-to-authenticate). Git over HTTPS accepts
  the same token as the password with any user name.
* Service hooks: publisher ``tfs``, events ``git.pullrequest.created`` and
  ``git.pullrequest.updated`` (plus the Azure Boards ``workitem.*`` events
  handled in ``preloop.utils.azure_devops_work_items``), consumer ``webHooks`` / ``httpRequest``
  (https://learn.microsoft.com/azure/devops/service-hooks/events and
  https://learn.microsoft.com/azure/devops/service-hooks/services/webhooks).
  Deliveries carry no signature. The subscription can send fixed HTTP headers
  or Basic credentials, so Preloop configures a per-tracker secret header and
  rejects every delivery without it.
* A pull request event resource carries ``repository``, ``pullRequestId``,
  ``sourceRefName`` / ``targetRefName`` (``refs/heads/<branch>``) and
  ``lastMergeSourceCommit.commitId``, the source branch head.

Azure DevOps Server (on-premises) and ``{org}.visualstudio.com`` URLs are not
supported: they are recognised and refused with a clear message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

AZURE_DEVOPS_TRACKER_TYPE = "azure_devops"
AZURE_DEVOPS_HOST = "dev.azure.com"
AZURE_DEVOPS_BASE_URL = "https://dev.azure.com"
AZURE_DEVOPS_API_VERSION = "7.1"
AZURE_DEVOPS_AUTH_API_TOKEN = "api_token"

# Azure Repos accepts any user name with a personal access token.
GIT_CREDENTIAL_USER = "azure-devops"

# Header the service hook subscription sends with the per-tracker secret.
WEBHOOK_SECRET_HEADER = "X-Preloop-Webhook-Secret"
LIFECYCLE_HEADER = "X-Preloop-Azure-Lifecycle"
WEBHOOK_MAX_BYTES = 1024 * 1024

# Service hook event id -> normalized Preloop event type.
PULL_REQUEST_COMMENT_EVENT = "ms.vss-code.git-pullrequest-comment-event"
PRELOOP_COMMENT_MARKER = "<!-- preloop:comment -->"

PULL_REQUEST_EVENT_MAP: Dict[str, str] = {
    "git.pullrequest.created": "pull_request_opened",
    "git.pullrequest.updated": "pull_request_updated",
}
# Azure Boards work item events (project scoped). ``workitem.updated``
# refines to issue_labeled / issue_status_changed / issue_unlabeled in
# ``preloop.sync.event_normalizer``.
WORK_ITEM_EVENT_MAP: Dict[str, str] = {
    "workitem.created": "issue_opened",
    "workitem.updated": "issue_updated",
    "workitem.commented": "comment_created",
}
AZURE_DEVOPS_EVENT_MAP: Dict[str, str] = {
    **PULL_REQUEST_EVENT_MAP,
    PULL_REQUEST_COMMENT_EVENT: "pull_request_updated",
    **WORK_ITEM_EVENT_MAP,
}
AZURE_DEVOPS_WEBHOOK_EVENTS: Tuple[str, ...] = tuple(AZURE_DEVOPS_EVENT_MAP)
PULL_REQUEST_WEBHOOK_EVENTS: Tuple[str, ...] = (
    *PULL_REQUEST_EVENT_MAP,
    PULL_REQUEST_COMMENT_EVENT,
)

LEGACY_HOST_MESSAGE = (
    "Azure DevOps URLs on {organization}.visualstudio.com are not supported. "
    "Use the organization's https://dev.azure.com/{organization} address."
)
ON_PREMISES_MESSAGE = (
    "Only Azure DevOps Services (https://dev.azure.com) is supported. Azure "
    "DevOps Server (on-premises) is not."
)

REASON_UNSUPPORTED_EVENT = "unsupported_event"
REASON_NOT_ACTIVE = "pull_request_not_active"
REASON_NO_SOURCE_COMMIT = "no_source_commit"

# Organization names: letters, digits and hyphens, not starting or ending
# with a hyphen (Azure DevOps organization naming rules).
_ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$")
# Project and repository names may contain spaces and most punctuation, but
# never path separators, URL syntax or control characters.
_NAME_FORBIDDEN = set('/\\?#%:*"<>|')
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_REFS_HEADS = "refs/heads/"


class AzureDevOpsConfigError(ValueError):
    """The tracker configuration is incomplete or not supported."""


class AzureDevOpsWebhookRejectedError(ValueError):
    """A delivery that must not be processed.

    Attributes:
        status_code: HTTP status to answer with.
        reason: Fixed, sanitized reason code.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        """Store the status and reason.

        Args:
            status_code: HTTP status to answer with.
            reason: Fixed reason code; never payload content.
        """
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass
class AzureDevOpsDelivery:
    """Outcome of normalizing one authenticated delivery.

    Attributes:
        accepted: True when the delivery should be dispatched.
        event_key: The service hook event id.
        reason: Sanitized reason when acknowledged without dispatch.
        event_type: Normalized Preloop event type, when mapped.
        payload: Normalized payload handed to the trigger pipeline.
        repository_id: Repository GUID (lower case).
        pull_request_id: Pull request id within the repository.
        head_sha: Source branch head commit.
    """

    accepted: bool
    event_key: str
    reason: Optional[str] = None
    event_type: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    repository_id: Optional[str] = None
    pull_request_id: Optional[int] = None
    head_sha: Optional[str] = None


# ----------------------------------------------------------------------
# Names and URLs
# ----------------------------------------------------------------------


def validate_organization(value: Any) -> str:
    """Return a valid organization name.

    Raises:
        AzureDevOpsConfigError: When missing or malformed.
    """
    name = str(value or "").strip()
    if not name:
        raise AzureDevOpsConfigError(
            "Azure DevOps tracker requires 'organization' in connection_details."
        )
    if not _ORG_RE.match(name):
        raise AzureDevOpsConfigError(
            "The Azure DevOps organization name may contain only letters, "
            "digits and hyphens."
        )
    return name


def validate_name(value: Any, what: str) -> Optional[str]:
    """Return a project or repository name, or None when empty.

    Args:
        value: Raw value.
        what: ``project`` or ``repository``, for the error message.

    Raises:
        AzureDevOpsConfigError: When the name has forbidden characters.
    """
    name = str(value or "").strip()
    if not name:
        return None
    if (
        len(name) > 256
        or any(ch in _NAME_FORBIDDEN for ch in name)
        or any(ord(ch) < 32 for ch in name)
        or name in {".", ".."}
    ):
        raise AzureDevOpsConfigError(f"Invalid Azure DevOps {what} name.")
    return name


def is_guid(value: Any) -> bool:
    """True when ``value`` is a GUID string."""
    return isinstance(value, str) and bool(_GUID_RE.match(value))


def check_base_url(url: Optional[str]) -> None:
    """Refuse URLs other than ``https://dev.azure.com``.

    Raises:
        AzureDevOpsConfigError: For legacy, on-premises or non-HTTPS URLs.
    """
    if not url:
        return
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if host.endswith(".visualstudio.com"):
        raise AzureDevOpsConfigError(
            LEGACY_HOST_MESSAGE.format(organization=host.split(".", 1)[0])
        )
    if host != AZURE_DEVOPS_HOST or parts.scheme != "https" or parts.port:
        raise AzureDevOpsConfigError(ON_PREMISES_MESSAGE)


def validate_azure_devops_config(
    *,
    api_key: Optional[str],
    auth_type: Optional[str],
    connection_details: Optional[Mapping[str, Any]],
    url: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a tracker configuration before any network call.

    Args:
        api_key: The personal access token.
        auth_type: Must be ``api_token``.
        connection_details: ``organization`` (required), ``project`` and
            ``repository`` (optional; a repository needs a project).
        url: The tracker URL; must be ``https://dev.azure.com`` when set.

    Returns:
        The normalized connection details.

    Raises:
        AzureDevOpsConfigError: When the configuration is not usable.
    """
    if (auth_type or AZURE_DEVOPS_AUTH_API_TOKEN) != AZURE_DEVOPS_AUTH_API_TOKEN:
        raise AzureDevOpsConfigError(
            "Azure DevOps trackers authenticate with a personal access token."
        )
    if not str(api_key or "").strip():
        raise AzureDevOpsConfigError(
            "Azure DevOps tracker requires a personal access token."
        )
    check_base_url(url)
    details = dict(connection_details or {})
    organization = validate_organization(details.get("organization"))
    project = validate_name(details.get("project"), "project")
    repository = validate_name(details.get("repository"), "repository")
    if repository and not project:
        raise AzureDevOpsConfigError(
            "Select the Azure DevOps project that holds the repository."
        )
    result: Dict[str, Any] = {
        key: value
        for key, value in details.items()
        if key not in {"organization", "project", "repository"}
    }
    vote = details.get("cast_token_owner_vote", False)
    if not isinstance(vote, bool):
        raise AzureDevOpsConfigError("cast_token_owner_vote must be a boolean.")
    result["cast_token_owner_vote"] = vote
    result["organization"] = organization
    if project:
        result["project"] = project
    if repository:
        result["repository"] = repository
    return result


def _seg(value: str) -> str:
    return quote(value, safe="")


def repository_full_name(organization: str, project: str, repository: str) -> str:
    """Preloop slug of a repository: ``org/project/repo``."""
    return f"{organization}/{project}/{repository}"


def clone_url(organization: str, project: str, repository: str) -> str:
    """HTTPS clone URL without credentials."""
    return (
        f"{AZURE_DEVOPS_BASE_URL}/{_seg(organization)}/{_seg(project)}"
        f"/_git/{_seg(repository)}"
    )


def pull_request_web_url(
    organization: str, project: str, repository: str, pr_id: int
) -> str:
    """Web URL of a pull request."""
    return f"{clone_url(organization, project, repository)}/pullrequest/{int(pr_id)}"


def parse_azure_url(
    url: str,
) -> Optional[Tuple[str, str, str, Optional[int]]]:
    """Parse a repository or pull request URL.

    Accepts ``https://[user@]dev.azure.com/{org}/{project}/_git/{repo}`` with an
    optional ``/pullrequest/{id}`` suffix.

    Args:
        url: The URL.

    Returns:
        ``(organization, project, repository, pr_id or None)``, or None when
        the URL is not an Azure Repos URL on dev.azure.com.

    Raises:
        AzureDevOpsConfigError: For a ``{org}.visualstudio.com`` URL.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if host.endswith(".visualstudio.com"):
        raise AzureDevOpsConfigError(
            LEGACY_HOST_MESSAGE.format(organization=host.split(".", 1)[0])
        )
    if host != AZURE_DEVOPS_HOST:
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if len(segments) < 4 or segments[2] != "_git":
        return None
    organization, project, _, repository = segments[:4]
    if repository.endswith(".git"):
        repository = repository[: -len(".git")]
    pr_id: Optional[int] = None
    rest = segments[4:]
    if rest and rest[0].lower() == "pullrequest":
        if len(rest) < 2 or not rest[1].isdigit():
            return None
        pr_id = int(rest[1])
    return organization, project, repository, pr_id


def branch_from_ref(ref: Any) -> Optional[str]:
    """``refs/heads/feature/x`` -> ``feature/x``."""
    if not isinstance(ref, str) or not ref:
        return None
    return ref[len(_REFS_HEADS) :] if ref.startswith(_REFS_HEADS) else ref


def user_name(user: Any) -> Optional[str]:
    """Display name of an identity, never the e-mail address."""
    if not isinstance(user, Mapping):
        return None
    name = user.get("displayName")
    return str(name) if name else None


# ----------------------------------------------------------------------
# Service hook authentication
# ----------------------------------------------------------------------


def generate_webhook_secret() -> str:
    """Return a new random per-tracker webhook secret."""
    return secrets.token_urlsafe(32)


def webhook_callback_path(tracker_id: Any) -> str:
    """Path of the intake endpoint for one tracker, below ``/api/v1``."""
    return f"/private/webhooks/{AZURE_DEVOPS_TRACKER_TYPE}/{tracker_id}"


def verify_webhook_secret(secret: Optional[str], headers: Mapping[str, str]) -> bool:
    """Check that a delivery carries the tracker's secret header.

    Only the ``X-Preloop-Webhook-Secret`` header is accepted; the comparison
    is constant time. (Basic credentials are not used: the API treats an
    ``Authorization`` header as a user credential.)

    Args:
        secret: The tracker's current webhook secret.
        headers: Request headers (case-insensitive mapping).

    Returns:
        False when the secret is unset or the header is missing or wrong.
    """
    presented = headers.get(WEBHOOK_SECRET_HEADER)
    if not secret or presented is None:
        return False
    return hmac.compare_digest(
        secret.encode("utf-8"), presented.strip().encode("utf-8")
    )


# ----------------------------------------------------------------------
# Delivery normalization
# ----------------------------------------------------------------------


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def _organization_of(url: Any) -> Optional[str]:
    """Organization named by a ``dev.azure.com`` REST or git URL."""
    if not isinstance(url, str) or not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.endswith(".visualstudio.com"):
        return host.split(".", 1)[0]
    if host != AZURE_DEVOPS_HOST:
        return None
    segments = [s for s in parts.path.split("/") if s]
    return unquote(segments[0]) if segments else None


def strip_userinfo(url: str) -> str:
    """Drop ``user@`` from a URL such as Azure's ``remoteUrl``."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    netloc = parts.netloc.rsplit("@", 1)[1]
    return parts._replace(netloc=netloc).geturl()


def build_object_attributes(pr: Mapping[str, Any]) -> Dict[str, Any]:
    """Map a normalized pull request onto ``object_attributes``.

    The reviewer preset reads ``trigger_event.payload.object_attributes``;
    every provider fills the same keys.

    Args:
        pr: The ``pull_request`` entry of a normalized payload.

    Returns:
        title, description, url, branches, state, draft, author, number,
        iid and last_commit.
    """
    number = pr.get("number")
    return {
        "title": pr.get("title"),
        "description": pr.get("body") or "",
        "url": pr.get("html_url"),
        "source_branch": _dig(pr, "head", "ref"),
        "target_branch": _dig(pr, "base", "ref"),
        "state": pr.get("state"),
        "draft": bool(pr.get("draft", False)),
        "author": _dig(pr, "user", "login"),
        "number": number,
        "iid": number,
        "last_commit": {"id": _dig(pr, "head", "sha")},
    }


def normalize_pull_request(
    resource: Mapping[str, Any], organization: str
) -> Dict[str, Any]:
    """Build the provider-neutral payload for a pull request resource.

    Used by the webhook intake and by manual reviewer runs, so both produce
    the same trigger shape.

    Args:
        resource: An Azure DevOps ``GitPullRequest`` (REST or service hook).
        organization: The tracker's organization.

    Returns:
        A payload with ``repository``, ``pull_request``, ``object_attributes``
        and ``azure_devops`` keys.

    Raises:
        AzureDevOpsWebhookRejectedError: When the repository or id is missing.
    """
    repo = resource.get("repository")
    if not isinstance(repo, Mapping) or not is_guid(repo.get("id")):
        raise AzureDevOpsWebhookRejectedError(400, "missing_repository")
    pr_id = resource.get("pullRequestId")
    if isinstance(pr_id, bool) or not isinstance(pr_id, int):
        raise AzureDevOpsWebhookRejectedError(400, "missing_pull_request")
    project = repo.get("project") if isinstance(repo.get("project"), Mapping) else {}
    project_name = str(project.get("name") or "")
    repo_name = str(repo.get("name") or "")
    repo_id = str(repo["id"]).lower()
    full_name = repository_full_name(organization, project_name, repo_name)
    git_url = (
        clone_url(organization, project_name, repo_name)
        if project_name and repo_name
        else strip_userinfo(str(repo.get("remoteUrl") or ""))
    )
    head_sha = _dig(resource, "lastMergeSourceCommit", "commitId")
    base_sha = _dig(resource, "lastMergeTargetCommit", "commitId")
    status = str(resource.get("status") or "").lower() or None
    state = {"active": "open", "completed": "merged", "abandoned": "closed"}.get(
        status or "", status
    )
    author = user_name(resource.get("createdBy"))
    html_url = pull_request_web_url(organization, project_name, repo_name, pr_id)
    pull_request: Dict[str, Any] = {
        "id": pr_id,
        "number": pr_id,
        "title": resource.get("title"),
        "body": resource.get("description") or "",
        "html_url": html_url,
        "state": state,
        "draft": bool(resource.get("isDraft", False)),
        "user": {"login": author},
        "head": {
            "ref": branch_from_ref(resource.get("sourceRefName")),
            "full_ref": resource.get("sourceRefName"),
            "sha": head_sha,
        },
        "base": {
            "ref": branch_from_ref(resource.get("targetRefName")),
            "full_ref": resource.get("targetRefName"),
            "sha": base_sha,
        },
    }
    return {
        "repository": {
            "id": repo_id,
            "name": repo_name,
            "full_name": full_name,
            "clone_url": git_url,
            "html_url": git_url,
            "default_branch": branch_from_ref(repo.get("defaultBranch")),
            "project": {"id": project.get("id"), "name": project_name or None},
        },
        "pull_request": pull_request,
        "object_attributes": build_object_attributes(pull_request),
        "azure_devops": {
            "organization": organization,
            "project": project_name or None,
            "project_id": project.get("id"),
            "repository_id": repo_id,
            "repository": repo_name or None,
            "pull_request_id": pr_id,
            "status": status,
            "head_sha": head_sha,
        },
    }


def is_test_notification(payload: Mapping[str, Any]) -> bool:
    """Recognize Azure's canned PR Test resource, never a live organization.

    Azure supplies no test flag. Both subscription Test events captured from
    notification history use this sample identity and 2014 commit pair.
    Require the complete fingerprint, not just the sample organization name.
    This only permits an authenticated no-op; it never authorizes dispatch.
    """
    resource = payload.get("resource")
    if not isinstance(resource, Mapping):
        return False
    sample_repo = "4bc14d40-c903-45e2-872e-0462c7748079"
    sample_url = (
        "https://fabrikam.visualstudio.com/DefaultCollection/_apis/git/repositories/"
        + sample_repo
    )
    return (
        payload.get("publisherId") == "tfs"
        and payload.get("eventType") in PULL_REQUEST_EVENT_MAP
        and payload.get("resourceVersion") == "1.0"
        and _dig(resource, "repository", "id") == sample_repo
        and _dig(resource, "repository", "url") == sample_url
        and resource.get("url") == sample_url + "/pullRequests/1"
        and resource.get("pullRequestId") == 1
        and resource.get("creationDate") == "2014-06-17T16:55:46.589889Z"
        and _dig(resource, "lastMergeSourceCommit", "commitId")
        == "53d54ac915144006c2c9e90d2c7d3880920db49c"
        and _dig(resource, "lastMergeTargetCommit", "commitId")
        == "a511f535b1ea495ee0c903badb68fbc83772c882"
    )


def normalize_review_comment(
    payload: Mapping[str, Any], organization: str
) -> AzureDevOpsDelivery:
    """Accept only the exact command from the captured Azure comment contract.

    Azure sends a comment and REST links, not a PR resource. Validate the
    organization before interpreting those links; never follow a payload URL.
    Preloop appends its marker before posting any comment, so its own writes
    cannot become commands, even when a person uses the same token identity.
    """
    key = PULL_REQUEST_COMMENT_EVENT
    resource = payload.get("resource") or {}
    self_url = _dig(resource, "_links", "self", "href")
    repo_url = _dig(resource, "_links", "repository", "href")
    for url in (self_url, repo_url):
        if (_organization_of(url) or "").casefold() != organization.casefold():
            raise AzureDevOpsWebhookRejectedError(403, "organization_mismatch")
    match = re.search(
        r"/_apis/git/repositories/([0-9a-fA-F-]{36})/pullRequests/(\d+)/threads/(\d+)/comments/(\d+)$",
        str(self_url),
    )
    if not match or not is_guid(match[1]):
        raise AzureDevOpsWebhookRejectedError(400, "invalid_comment_identity")
    content = resource.get("content")
    if not isinstance(content, str) or content.strip().casefold() != "/preloop review":
        return AzureDevOpsDelivery(False, key, reason="not_review_command")
    repo_id, number, thread, comment = match.groups()
    repo_id = repo_id.lower()
    return AzureDevOpsDelivery(
        True,
        key,
        event_type="pull_request_updated",
        repository_id=repo_id,
        pull_request_id=int(number),
        payload={
            "repository": {"id": repo_id},
            "pull_request": {"number": int(number)},
            "azure_devops": {
                "organization": organization,
                "repository_id": repo_id,
                "pull_request_id": int(number),
            },
            "azure_review_request": {
                "kind": "comment",
                "signal": f"{thread}:{comment}",
            },
        },
    )


def normalize_delivery(
    payload: Any,
    *,
    organization: str,
    bound_project: Optional[str] = None,
    bound_repository: Optional[str] = None,
    repository_known: Optional[Callable[[str], bool]] = None,
    lifecycle_only: bool = False,
) -> AzureDevOpsDelivery:
    """Validate an authenticated delivery and map it to the trigger contract.

    Args:
        payload: Parsed JSON body (a service hook ``Event``).
        organization: The tracker's organization.
        bound_project: Project name the tracker is limited to.
        bound_repository: Repository name the tracker is limited to.
        repository_known: Answers whether a repository GUID was imported for
            this tracker; an unknown repository is acknowledged without dispatch.

    Returns:
        The delivery outcome.

    Raises:
        AzureDevOpsWebhookRejectedError: For malformed payloads and
            deliveries about another organization.
    """
    if not isinstance(payload, Mapping):
        raise AzureDevOpsWebhookRejectedError(400, "invalid_payload")
    key = str(payload.get("eventType") or "").strip()
    if not key:
        raise AzureDevOpsWebhookRejectedError(400, "missing_event_type")
    if payload.get("publisherId") not in (None, "tfs"):
        raise AzureDevOpsWebhookRejectedError(400, "unsupported_publisher")
    if key == PULL_REQUEST_COMMENT_EVENT:
        return normalize_review_comment(payload, organization)
    event_type = PULL_REQUEST_EVENT_MAP.get(key)
    if event_type is None:
        return AzureDevOpsDelivery(
            accepted=False, event_key=key, reason=REASON_UNSUPPORTED_EVENT
        )
    if is_test_notification(payload):
        return AzureDevOpsDelivery(False, key, reason="test_notification")
    resource = payload.get("resource")
    if not isinstance(resource, Mapping):
        raise AzureDevOpsWebhookRejectedError(400, "missing_resource")
    repo = resource.get("repository")
    if not isinstance(repo, Mapping):
        raise AzureDevOpsWebhookRejectedError(400, "missing_repository")
    for url in (repo.get("url"), repo.get("remoteUrl"), resource.get("url")):
        owner = _organization_of(url)
        if owner is not None and owner.casefold() != organization.casefold():
            raise AzureDevOpsWebhookRejectedError(403, "organization_mismatch")
    normalized = normalize_pull_request(resource, organization)
    info = normalized["azure_devops"]
    repo_id = info["repository_id"]
    if bound_project and (info["project"] or "").casefold() != bound_project.casefold():
        return AzureDevOpsDelivery(False, key, reason="repository_outside_scope")
    if bound_repository and (
        (info["repository"] or "").casefold() != bound_repository.casefold()
    ):
        return AzureDevOpsDelivery(False, key, reason="repository_outside_scope")
    if repository_known is not None and not repository_known(repo_id):
        return AzureDevOpsDelivery(False, key, reason="repository_not_imported")

    normalized["event_key"] = key
    normalized["delivery"] = {
        "id": payload.get("id"),
        "notification_id": payload.get("notificationId"),
        "subscription_id": payload.get("subscriptionId"),
        "created_date": payload.get("createdDate"),
    }
    head_sha = info["head_sha"]
    outcome = AzureDevOpsDelivery(
        accepted=True,
        event_key=key,
        event_type=event_type,
        payload=normalized,
        repository_id=repo_id,
        pull_request_id=info["pull_request_id"],
        head_sha=head_sha,
    )
    if info["status"] in ("completed", "abandoned"):
        # Lifecycle events must reach the shared stop path, even on a seen head.
        outcome.event_type = (
            "pull_request_merged"
            if info["status"] == "completed"
            else "pull_request_closed"
        )
    elif info["status"] not in (None, "active"):
        outcome.accepted = False
        outcome.reason = REASON_NOT_ACTIVE
    elif lifecycle_only:
        outcome.accepted = False
        outcome.reason = "not_terminal_lifecycle_event"
    elif not head_sha:
        # Without the source commit there is no way to tell a push from a
        # vote, so the update is not dispatched.
        outcome.accepted = False
        outcome.reason = REASON_NO_SOURCE_COMMIT
    return outcome


def delivery_identity(tracker_id: Any, delivery: AzureDevOpsDelivery) -> str:
    """Tracker-scoped identity of a pull request head.

    Azure DevOps sends ``git.pullrequest.updated`` for votes, reviewer and
    title changes too, with no previous-commit field. Keying the delivery on
    the source commit makes every event for an already-seen head a
    duplicate, so only a created pull request or a moved source branch is
    dispatched.

    Args:
        tracker_id: The tracker.
        delivery: An accepted delivery.

    Returns:
        ``azure_devops:<tracker>:<repository>:pr:<id>:<sha>`` (under 200
        characters).
    """
    request = delivery.payload.get("azure_review_request")
    if request:
        return f"azure_devops:{tracker_id}:{delivery.repository_id}:pr:{delivery.pull_request_id}:comment:{request['signal']}"
    identity = (
        f"{AZURE_DEVOPS_TRACKER_TYPE}:{tracker_id}:{delivery.repository_id}"
        f":pr:{delivery.pull_request_id}:{delivery.head_sha}"
    )
    if delivery.event_type in {"pull_request_merged", "pull_request_closed"}:
        return f"{identity}:{delivery.event_type}"
    return identity


def basic_auth_header(token: str) -> str:
    """``Authorization`` value for a personal access token."""
    raw = f":{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def body_digest(raw_body: bytes) -> str:
    """SHA-256 of a raw body, for logs that must not carry content."""
    return hashlib.sha256(raw_body).hexdigest()[:16]
