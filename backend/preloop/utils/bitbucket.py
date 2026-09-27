"""Shared Bitbucket Cloud helpers.

Pure functions and constants used by the Bitbucket tracker client, the
webhook endpoint, the event normalizer, the MCP tools and the git credential
plumbing. Nothing here performs network I/O, so every caller can import it
without pulling in an HTTP client.

Verified against the Bitbucket Cloud documentation (September 2026):

* Webhooks sign the raw body with HMAC-SHA256 and send
  ``X-Hub-Signature: sha256=<hex>`` only when the hook has a secret.
* The event key is in ``X-Event-Key``; ``X-Request-UUID`` identifies the
  delivery.
* REST calls accept an API token as ``Authorization: Bearer <token>`` or as
  HTTP Basic ``<email>:<token>``.
* Git over HTTPS accepts an API token with the Bitbucket username or the
  static ``x-bitbucket-api-token-auth`` username. Repository, project and
  workspace access tokens and OAuth access tokens use ``x-token-auth``. The
  account email is never a valid git username.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

BITBUCKET_API_BASE_URL = "https://api.bitbucket.org/2.0"
BITBUCKET_WEB_BASE_URL = "https://bitbucket.org"
BITBUCKET_HOST = "bitbucket.org"

# Authentication modes stored on ``Tracker.auth_type``.
BITBUCKET_AUTH_API_TOKEN = "api_token"
BITBUCKET_AUTH_OAUTH_TOKEN = "oauth_token"
BITBUCKET_AUTH_TYPES = (BITBUCKET_AUTH_API_TOKEN, BITBUCKET_AUTH_OAUTH_TOKEN)

# Kinds of secret accepted under the ``api_token`` auth mode, stored in
# ``connection_details["token_kind"]``. A repository access token is bound to
# one repository, so it requires ``repository`` in the connection details.
TOKEN_KIND_API_TOKEN = "api_token"
TOKEN_KIND_ACCESS_TOKEN = "access_token"
BITBUCKET_TOKEN_KINDS = (TOKEN_KIND_API_TOKEN, TOKEN_KIND_ACCESS_TOKEN)

# Git usernames. Never the account email.
GIT_USERNAME_API_TOKEN = "x-bitbucket-api-token-auth"
GIT_USERNAME_ACCESS_TOKEN = "x-token-auth"

# App passwords are being retired in favour of API tokens. The ``ATBB``
# prefix is what app passwords have looked like in practice; the current
# Atlassian pages no longer document it, so it is a best-effort check in
# addition to the explicit ``app_password`` marker.
APP_PASSWORD_PREFIX = "ATBB"
APP_PASSWORD_MESSAGE = (
    "Bitbucket app passwords are not supported. Create a Bitbucket API token "
    "(Atlassian account settings, Security, API tokens) with the repository, "
    "pull request and webhook scopes, or use a repository access token."
)

BITBUCKET_SIGNATURE_HEADER = "X-Hub-Signature"
BITBUCKET_EVENT_HEADER = "X-Event-Key"
BITBUCKET_DELIVERY_HEADER = "X-Request-UUID"

# Repository webhook events Preloop subscribes to.
BITBUCKET_WEBHOOK_EVENTS: Tuple[str, ...] = (
    "repo:push",
    "pullrequest:created",
    "pullrequest:updated",
    "pullrequest:fulfilled",
    "pullrequest:rejected",
    "pullrequest:approved",
    "pullrequest:unapproved",
    "pullrequest:changes_request_created",
    "pullrequest:changes_request_removed",
    "pullrequest:comment_created",
    "pullrequest:comment_updated",
    "pullrequest:comment_deleted",
)

_PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)
_PR_PATH_RE = re.compile(r"^/?([^/]+)/([^/]+)/pull-requests/(\d+)/?$")


class BitbucketConfigError(ValueError):
    """Raised when Bitbucket tracker configuration is invalid."""


def looks_like_app_password(token: Optional[str]) -> bool:
    """Return True when ``token`` looks like a Bitbucket app password.

    Args:
        token: The secret the user supplied.

    Returns:
        True when the token carries the app password prefix.
    """
    return bool(token) and str(token).strip().startswith(APP_PASSWORD_PREFIX)


def validate_bitbucket_config(
    *,
    api_key: Optional[str],
    auth_type: Optional[str],
    connection_details: Optional[Mapping[str, Any]],
) -> None:
    """Validate a Bitbucket tracker configuration before any network call.

    Args:
        api_key: The API token, access token or OAuth access token.
        auth_type: ``api_token`` or ``oauth_token``. ``app_password`` is
            rejected with a message pointing to API tokens.
        connection_details: Tracker configuration. ``workspace`` is required;
            ``token_kind="access_token"`` also requires ``repository``.

    Raises:
        BitbucketConfigError: With a user-facing message when invalid.
    """
    details = dict(connection_details or {})
    mode = (auth_type or BITBUCKET_AUTH_API_TOKEN).strip().lower()
    token_kind = str(details.get("token_kind") or TOKEN_KIND_API_TOKEN).lower()

    if mode == "app_password" or token_kind == "app_password":
        raise BitbucketConfigError(APP_PASSWORD_MESSAGE)
    if mode not in BITBUCKET_AUTH_TYPES:
        raise BitbucketConfigError(
            f"Unsupported Bitbucket auth_type '{auth_type}'. "
            f"Use one of: {', '.join(BITBUCKET_AUTH_TYPES)}."
        )
    if not api_key:
        raise BitbucketConfigError("A Bitbucket token is required.")
    if looks_like_app_password(api_key):
        raise BitbucketConfigError(APP_PASSWORD_MESSAGE)
    if not str(details.get("workspace") or "").strip():
        raise BitbucketConfigError(
            "Bitbucket tracker requires 'workspace' in connection_details."
        )
    if mode == BITBUCKET_AUTH_API_TOKEN:
        if token_kind not in BITBUCKET_TOKEN_KINDS:
            raise BitbucketConfigError(
                f"Unsupported Bitbucket token_kind '{token_kind}'. "
                f"Use one of: {', '.join(BITBUCKET_TOKEN_KINDS)}."
            )
        if (
            token_kind == TOKEN_KIND_ACCESS_TOKEN
            and not str(details.get("repository") or "").strip()
        ):
            raise BitbucketConfigError(
                "A Bitbucket repository access token is bound to one repository: "
                "set 'repository' in connection_details."
            )
    username = str(details.get("username") or "")
    if "@" in username:
        raise BitbucketConfigError(
            "The Bitbucket username is used for git and must not be an email "
            "address. Put the account email in 'email' instead."
        )


def git_username_for(
    *,
    auth_type: Optional[str],
    connection_details: Optional[Mapping[str, Any]],
) -> str:
    """Return the git HTTPS username for a Bitbucket tracker credential.

    Args:
        auth_type: The tracker auth type.
        connection_details: The tracker connection details.

    Returns:
        ``x-token-auth`` for access tokens and OAuth tokens, the configured
        Bitbucket username for API tokens, otherwise
        ``x-bitbucket-api-token-auth``. Never the account email.
    """
    details = connection_details or {}
    mode = (auth_type or "").lower()
    token_kind = str(details.get("token_kind") or "").lower()
    if mode == BITBUCKET_AUTH_OAUTH_TOKEN or token_kind == TOKEN_KIND_ACCESS_TOKEN:
        return GIT_USERNAME_ACCESS_TOKEN
    username = str(details.get("username") or "").strip()
    if username and "@" not in username:
        return username
    return GIT_USERNAME_API_TOKEN


def compute_signature(secret: str, body: bytes) -> str:
    """Return the ``X-Hub-Signature`` value Bitbucket sends for ``body``.

    Args:
        secret: The webhook secret.
        body: The raw request body.

    Returns:
        ``sha256=<hex digest>``.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: Optional[str]) -> bool:
    """Verify a Bitbucket webhook signature in constant time.

    Args:
        secret: The webhook secret configured on the hook.
        body: The raw request body.
        header_value: The ``X-Hub-Signature`` header value.

    Returns:
        False when the secret or header is missing or does not match.
    """
    if not secret or not header_value:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected.strip(), header_value.strip())


def parse_pull_request_url(url: str) -> Optional[Tuple[str, str, int]]:
    """Parse a Bitbucket Cloud pull request URL.

    Args:
        url: A URL such as ``https://bitbucket.org/ws/repo/pull-requests/7``.

    Returns:
        ``(workspace, repo_slug, pr_id)`` or None when not a PR URL.
    """
    if not url:
        return None
    match = _PR_URL_RE.match(url.strip())
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def parse_pull_request_path(path: str) -> Optional[Tuple[str, str, int]]:
    """Parse the path part of a Bitbucket pull request URL.

    Args:
        path: A path such as ``/ws/repo/pull-requests/7``.

    Returns:
        ``(workspace, repo_slug, pr_id)`` or None.
    """
    match = _PR_PATH_RE.match(path or "")
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def normalize_uuid(value: Optional[str]) -> str:
    """Strip the braces Bitbucket puts around UUIDs.

    Args:
        value: A UUID such as ``{1234-...}``.

    Returns:
        The bare UUID, or an empty string.
    """
    return str(value or "").strip().strip("{}")


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


def user_name(user: Any) -> Optional[str]:
    """Return a stable display handle for a Bitbucket user object."""
    if not isinstance(user, Mapping):
        return None
    return user.get("nickname") or user.get("display_name") or user.get("uuid")


def pr_source_branch(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the PR source branch from a webhook payload."""
    return _dig(payload, "pullrequest", "source", "branch", "name")


def pr_target_branch(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the PR destination branch from a webhook payload."""
    return _dig(payload, "pullrequest", "destination", "branch", "name")


def payload_commit_hash(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the head commit hash for a PR or push payload."""
    sha = _dig(payload, "pullrequest", "source", "commit", "hash")
    if sha:
        return sha
    changes = _dig(payload, "push", "changes")
    if isinstance(changes, list):
        for change in reversed(changes):
            sha = _dig(change, "new", "target", "hash")
            if sha:
                return sha
    return None


def payload_push_branch(payload: Mapping[str, Any]) -> Optional[str]:
    """Return the branch name of the last change in a ``repo:push`` payload."""
    changes = _dig(payload, "push", "changes")
    if isinstance(changes, list):
        for change in reversed(changes):
            new = _dig(change, "new")
            if isinstance(new, Mapping) and new.get("name"):
                return new.get("name")
    return None


def repository_full_name(payload: Mapping[str, Any]) -> Optional[str]:
    """Return ``workspace/repo`` from a webhook payload."""
    return _dig(payload, "repository", "full_name") or None


def build_object_attributes(pr: Mapping[str, Any]) -> Dict[str, Any]:
    """Map a Bitbucket pull request object onto the shared trigger shape.

    The reviewer preset reads ``trigger_event.payload.object_attributes``.
    GitHub PRs are mapped onto the same keys, so Bitbucket follows suit and
    the preset prompt does not fork.

    Args:
        pr: The ``pullrequest`` object from a webhook or the REST API.

    Returns:
        A dict with title, description, url, branches, state, draft, author,
        number and iid.
    """
    pr_id = pr.get("id")
    return {
        "title": pr.get("title"),
        "description": pr.get("description") or "",
        "url": _dig(pr, "links", "html", "href"),
        "source_branch": _dig(pr, "source", "branch", "name"),
        "target_branch": _dig(pr, "destination", "branch", "name"),
        "state": str(pr.get("state") or "").lower() or None,
        "draft": bool(pr.get("draft", False)),
        "author": user_name(pr.get("author")),
        "number": pr_id,
        "iid": pr_id,
        "last_commit": {"id": _dig(pr, "source", "commit", "hash")},
    }


def pull_request_web_url(workspace: str, repo_slug: str, pr_id: int) -> str:
    """Return the web URL of a pull request."""
    return f"{BITBUCKET_WEB_BASE_URL}/{workspace}/{repo_slug}/pull-requests/{pr_id}"


def token_expiry_status(
    expires_at: Optional[str],
    *,
    now: Optional[datetime] = None,
    warn_days: int = 14,
) -> Optional[str]:
    """Classify a token expiry timestamp.

    Args:
        expires_at: ISO 8601 timestamp, or None when unknown.
        now: Current time, for tests.
        warn_days: How many days ahead of expiry to report ``expiring``.

    Returns:
        ``expired``, ``expiring``, ``ok`` or None when unknown or unparsable.
    """
    if not expires_at:
        return None
    try:
        text = str(expires_at).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    remaining = (parsed - current).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining <= warn_days * 86400:
        return "expiring"
    return "ok"
