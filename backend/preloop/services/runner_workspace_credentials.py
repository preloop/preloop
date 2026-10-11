"""Short-lived clone credentials for remote session checkouts (#1484).

A remote session may ask the runner to clone a repository of one of the
account's trackers into a fresh session directory. The runner receives the
credential inside the ``session_start`` message only; it is never written to
a database row, a log line or the runner's disk. This module decides whether
a tracker may back such a checkout at all and, if so, mints a credential
that is as short-lived and narrow as the provider allows:

* GitHub, App-authenticated tracker: an installation access token restricted
  to the one repository with ``contents: read`` (GitHub caps it at 60
  minutes). Reuses :func:`preloop.services.publication_credentials
  .mint_repository_lease`.
* Bitbucket Cloud, managed connection: the current OAuth access token from
  the managed grant (Bitbucket fixes its lifetime at two hours; the runner
  discards it right after the clone).
* Anything that would hand a long-lived secret to a laptop is refused with a
  named error before any credential exists: a GitHub PAT
  (``checkout_requires_app_or_oauth``), a Bitbucket API token, repository or
  workspace access token, app password or pasted OAuth token
  (``checkout_requires_oauth``).

The caller (the remote session start endpoint) calls
:func:`mint_checkout_credential` right before sending ``session_start`` and
puts ``credential.as_wire()`` under ``workspace.credential``. The audit row
``runner_session.checkout_credential_minted`` records provider, repository
and expiry; never the token.

Authorized directories (the other workspace kind) carry no credential. The
runner advertises them as ``{id, label, mode, harnesses}`` with register and
heartbeat; :func:`normalize_authorized_directories` bounds that
advertisement before it is stored on the runner row.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from preloop.models.crud import crud_audit_log
from preloop.services.managed_credentials import (
    ManagedCredentialError,
    is_managed_tracker,
)
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.tracker_git_token import (
    APP_AUTH_TYPES,
    resolve_managed_tracker_credential,
)
from preloop.services.trusted_publisher import PublicationError

logger = logging.getLogger(__name__)

AUDIT_CHECKOUT_CREDENTIAL_MINTED = "runner_session.checkout_credential_minted"
AUDIT_RESOURCE_TYPE = "runner_session"

PROVIDER_GITHUB = "github"
PROVIDER_BITBUCKET_CLOUD = "bitbucket_cloud"
CHECKOUT_PROVIDERS = (PROVIDER_GITHUB, PROVIDER_BITBUCKET_CLOUD)

#: Git usernames paired with the minted tokens.
GITHUB_APP_GIT_USERNAME = "x-access-token"

#: A managed Bitbucket credential with less than this many seconds left is
#: rotated before it is sent, so the clone cannot run out of time mid-way.
BITBUCKET_MIN_REMAINING_SECONDS = 300

#: The runner enforces the same shape; anything else is refused here first.
CHECKOUT_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

AUTHORIZED_DIR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
AUTHORIZED_DIR_MODES = ("read_only", "write")
HARNESS_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_AUTHORIZED_DIRECTORIES = 64
MAX_AUTHORIZED_DIR_LABEL = 80
MAX_AUTHORIZED_DIR_HARNESSES = 32

CREDENTIAL_KEY = "credential"


class CheckoutCredentialError(ValueError):
    """A named refusal or failure; ``code`` is safe to relay to the actor."""

    code = "checkout_credential_unavailable"

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CheckoutCredential:
    """The clone credential for one session start. Plaintext lives here only.

    ``as_wire()`` is the ``workspace.credential`` object of ``session_start``.
    The mapping protocol is supported for the same three keys so a caller
    may treat the credential as ``{username, token, expires_at}``.
    """

    username: str
    token: str = field(repr=False)
    expires_at: datetime
    provider: str
    repository: str

    def as_wire(self) -> Dict[str, str]:
        return {
            "username": self.username,
            "token": self.token,
            "expires_at": self.expires_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def __getitem__(self, key: str) -> str:
        return self.as_wire()[key]

    def keys(self) -> Iterator[str]:
        return iter(("username", "token", "expires_at"))

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        current = now or datetime.now(timezone.utc)
        return (self.expires_at - current).total_seconds()


def validate_checkout_repository(repository: Any) -> str:
    """Return ``owner/name`` or raise ``checkout_repository_invalid``."""
    value = str(repository or "").strip()
    if not CHECKOUT_REPOSITORY_RE.fullmatch(value) or any(
        segment in (".", "..") or segment.startswith("-")
        for segment in value.split("/")
    ):
        raise CheckoutCredentialError(
            "checkout_repository_invalid",
            "The repository must be given as owner/name.",
        )
    return value


def checkout_provider_for_tracker(tracker: Any) -> str:
    """Map a tracker row onto a wave-1 checkout provider.

    Raises:
        CheckoutCredentialError: ``checkout_provider_unsupported`` for every
            tracker type that cannot back a remote checkout yet (GitLab is
            #1489; Jira has no repositories).
    """
    tracker_type = str(getattr(tracker, "tracker_type", "") or "").lower()
    if tracker_type == "github":
        return PROVIDER_GITHUB
    if tracker_type == "bitbucket":
        return PROVIDER_BITBUCKET_CLOUD
    raise CheckoutCredentialError(
        "checkout_provider_unsupported",
        f"Remote checkouts are not available for {tracker_type or 'this'} "
        "trackers yet; use a GitHub App tracker or a managed Bitbucket Cloud "
        "connection.",
    )


def refuse_long_lived_tracker_credential(tracker: Any) -> str:
    """Refuse trackers whose credential cannot be minted per start.

    Returns the provider when the tracker qualifies. Raises before any
    credential is resolved, so a refused tracker never produces a token.
    """
    provider = checkout_provider_for_tracker(tracker)
    auth_type = str(getattr(tracker, "auth_type", "") or "").lower()
    details = getattr(tracker, "connection_details", None) or {}
    token_kind = str(details.get("token_kind") or "").lower()
    if provider == PROVIDER_GITHUB:
        installation = getattr(tracker, "oauth_installation", None)
        if auth_type in APP_AUTH_TYPES and getattr(installation, "external_id", None):
            return provider
        raise CheckoutCredentialError(
            "checkout_requires_app_or_oauth",
            "Remote checkouts need a GitHub App tracker: a personal access "
            "token cannot be narrowed to one repository or made short-lived "
            "before it reaches a laptop.",
        )
    if is_managed_tracker(tracker):
        return provider
    if auth_type == "app_password" or token_kind == "app_password":
        reason = "a Bitbucket app password"
    elif token_kind == "access_token":
        reason = "a repository or workspace access token"
    elif auth_type == "oauth_token":
        reason = "a pasted OAuth access token, which Preloop cannot renew"
    else:
        reason = "an API token"
    raise CheckoutCredentialError(
        "checkout_requires_oauth",
        f"Remote checkouts need a managed Bitbucket Cloud connection; this "
        f"tracker uses {reason}, which cannot be issued per session.",
    )


async def _mint_github(
    tracker: Any, repository: str, client: Optional[httpx.AsyncClient]
) -> CheckoutCredential:
    repository_url = f"https://github.com/{repository}"

    async def issue(http: httpx.AsyncClient) -> CheckoutCredential:
        lease = await mint_repository_lease(
            tracker,
            repository_url,
            write=False,
            client=http,
            allow_legacy_oauth_app=True,
        )
        return CheckoutCredential(
            username=GITHUB_APP_GIT_USERNAME,
            token=lease.token,
            expires_at=lease.expires_at,
            provider=PROVIDER_GITHUB,
            repository=repository,
        )

    try:
        if client is not None:
            return await issue(client)
        async with httpx.AsyncClient() as owned:
            return await issue(owned)
    except PublicationError as exc:
        raise CheckoutCredentialError(
            "checkout_credential_unavailable",
            f"GitHub did not issue a repository-scoped credential: {exc}",
        ) from None


async def _mint_bitbucket(tracker: Any, repository: str) -> CheckoutCredential:
    try:
        managed = await resolve_managed_tracker_credential(
            tracker, repository=repository
        )
        if managed.seconds_remaining() < BITBUCKET_MIN_REMAINING_SECONDS:
            managed = await resolve_managed_tracker_credential(
                tracker, repository=repository, force_refresh=True
            )
    except ManagedCredentialError as exc:
        raise CheckoutCredentialError(
            "checkout_credential_unavailable", exc.actionable_message()
        ) from None
    except ValueError as exc:
        raise CheckoutCredentialError("checkout_requires_oauth", str(exc)) from None
    return CheckoutCredential(
        username=managed.git_username,
        token=managed.access_token,
        expires_at=managed.expires_at,
        provider=PROVIDER_BITBUCKET_CLOUD,
        repository=repository,
    )


def _audit_details(
    credential: CheckoutCredential, tracker: Any, context: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        key: value
        for key, value in dict(context or {}).items()
        if key not in (CREDENTIAL_KEY, "token", "username")
    }
    details.update(
        {
            "provider": credential.provider,
            "repository": credential.repository,
            "tracker_id": str(getattr(tracker, "id", "") or ""),
            "expires_at": credential.as_wire()["expires_at"],
            "workspace_kind": "tracker_checkout",
        }
    )
    return details


async def mint_checkout_credential(
    tracker: Any,
    repository: str,
    *,
    db: Any = None,
    actor_user_id: Any = None,
    remote_session_id: Any = None,
    audit_context: Optional[Mapping[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> CheckoutCredential:
    """Mint the clone credential for one remote session checkout.

    Args:
        tracker: The account's ``Tracker`` row the repository belongs to.
        repository: ``owner/name`` on the provider.
        db: Session used to write the audit row; the row is skipped when
            None (tests of the provider path).
        actor_user_id: The user who starts the session.
        remote_session_id: The ``runner_remote_sessions`` id, used as the
            audit resource id.
        audit_context: Extra audit details (``runner_id``, ``runner_name``,
            ``host``, ``harness``, ``model``, ``workspace_label``). Any
            ``credential``, ``token`` or ``username`` key is dropped.
        client: Optional HTTP client for the GitHub App request.

    Returns:
        The credential. Callers must not persist it: not in
        ``runner_remote_sessions.workspace``, not in a job payload, not in a
        log line. ``persistable_job_payload`` strips it from stored leases.

    Raises:
        CheckoutCredentialError: ``checkout_repository_invalid``,
            ``checkout_provider_unsupported``, ``checkout_requires_app_or_oauth``
            (GitHub PAT), ``checkout_requires_oauth`` (Bitbucket token, app
            password or pasted OAuth token) or
            ``checkout_credential_unavailable`` (provider refused or the
            signing configuration is missing). Refusals happen before any
            credential is resolved.
    """
    repository = validate_checkout_repository(repository)
    provider = refuse_long_lived_tracker_credential(tracker)
    if provider == PROVIDER_GITHUB:
        credential = await _mint_github(tracker, repository, client)
    else:
        credential = await _mint_bitbucket(tracker, repository)
    logger.info(
        "Minted a %s checkout credential for tracker %s repository %s (expires %s)",
        provider,
        getattr(tracker, "id", "unknown"),
        repository,
        credential.as_wire()["expires_at"],
    )
    if db is not None:
        crud_audit_log.log_action(
            db,
            account_id=tracker.account_id,
            user_id=actor_user_id,
            action=AUDIT_CHECKOUT_CREDENTIAL_MINTED,
            resource_type=AUDIT_RESOURCE_TYPE,
            resource_id=str(remote_session_id) if remote_session_id else None,
            status="success",
            details=_audit_details(credential, tracker, audit_context),
        )
    return credential


def without_checkout_credentials(value: Any) -> Any:
    """Return a copy of ``value`` with every ``credential`` key removed.

    Applied to anything that is persisted or logged from a session start or
    a lease payload: the credential travels on the authenticated runner
    socket only.
    """
    if isinstance(value, dict):
        return {
            key: without_checkout_credentials(item)
            for key, item in value.items()
            if key != CREDENTIAL_KEY
        }
    if isinstance(value, list):
        return [without_checkout_credentials(item) for item in value]
    return value


def normalize_authorized_directories(raw: Any) -> List[Dict[str, Any]]:
    """Bound the runner's authorized directory advertisement.

    Keeps ``id``, ``label``, ``mode`` and ``harnesses`` (``"all"`` or a list
    of harness ids). Paths are never accepted even if a runner sent one.
    """
    items: Any
    if isinstance(raw, Mapping):
        items = raw.get("authorized_directories") or []
    else:
        items = raw or []
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items[:MAX_AUTHORIZED_DIRECTORIES]:
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not AUTHORIZED_DIR_ID_RE.fullmatch(
            identifier
        ):
            continue
        if identifier in seen:
            continue
        mode = item.get("mode")
        if mode not in AUTHORIZED_DIR_MODES:
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            label = identifier
        label = "".join(ch for ch in label if ch.isprintable())[
            :MAX_AUTHORIZED_DIR_LABEL
        ]
        harnesses_raw = item.get("harnesses", "all")
        harnesses: Any
        if harnesses_raw in ("all", None, []):
            harnesses = "all"
        elif isinstance(harnesses_raw, list):
            harnesses = [
                value
                for value in harnesses_raw[:MAX_AUTHORIZED_DIR_HARNESSES]
                if isinstance(value, str) and HARNESS_ID_RE.fullmatch(value)
            ]
            if not harnesses:
                continue
        else:
            continue
        seen.add(identifier)
        out.append(
            {"id": identifier, "label": label, "mode": mode, "harnesses": harnesses}
        )
    return out


def runner_authorized_directories(runner: Any) -> List[Dict[str, Any]]:
    """The advertised directories stored on a runner row (never paths)."""
    capabilities = getattr(runner, "capabilities", None) or {}
    return normalize_authorized_directories(
        capabilities.get("authorized_directories")
        if isinstance(capabilities, Mapping)
        else []
    )
