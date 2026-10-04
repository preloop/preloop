"""Resolve a git-usable token for a tracker.

A tracker authenticates in one of two ways:

* ``api_token``: a personal access token stored (encrypted) on the tracker row
  and read through ``Tracker.resolved_api_key``.
* ``github_app`` / ``oauth_app``: no token is stored at all. The credential is
  a short-lived installation access token minted on demand from the GitHub App
  installation attached to the tracker.
* ``managed_oauth``: no token is stored either. A provider plugin resolves a
  fresh access token from the tenant-bound grant (see
  ``preloop.services.managed_credentials``). Unlike the other modes this one
  never degrades to "no credential": an unavailable resolver or a grant that
  needs reconnect raises, so the flow fails with an actionable message
  instead of cloning anonymously or pushing with a stale token.

Every git path (clone inside the agent container, the post-execution push,
PR/MR creation) needs the second case too. Reading ``resolved_api_key``
directly returns an empty string for App-authenticated trackers, which is why
an App-installed repository cloned fine (public repository, no credential
needed) and then failed the post-execution push with "could not read Username
for 'https://github.com'".

The token is returned to the caller and never logged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from preloop.services.managed_credentials import (
    ManagedCredential,
    is_managed_tracker,
    tracker_credential_source,
)

logger = logging.getLogger(__name__)

# Tracker auth types whose credential is an installation access token rather
# than a stored secret.
APP_AUTH_TYPES = {"github_app", "oauth_app"}


async def resolve_managed_tracker_credential(
    tracker: Any, *, repository: Optional[str] = None, force_refresh: bool = False
) -> ManagedCredential:
    """Resolve the fresh credential of a managed tracker, or raise.

    Args:
        tracker: A managed ``Tracker`` ORM instance.
        repository: Repository slug the caller is about to touch.
        force_refresh: Rotate even if the stored credential is fresh.

    Returns:
        The ephemeral credential (token, UTC expiry, rotation version, git
        username ``x-token-auth``).

    Raises:
        ManagedCredentialError: Typed resolver failure (unavailable,
            reconnect required, permission). Never swallowed here.
    """
    source = tracker_credential_source(tracker, repository=repository)
    if source is None:
        raise ValueError("tracker is not a managed grant")
    return await source(force_refresh=force_refresh)


async def resolve_tracker_git_token(tracker: Any) -> Optional[str]:
    """Return a token usable for git and REST calls against ``tracker``.

    Args:
        tracker: A ``Tracker`` ORM instance (or any object exposing
            ``resolved_api_key``, ``auth_type`` and ``oauth_installation``).

    Returns:
        The token, or None when the tracker has neither a stored key nor a
        usable App installation. A failure to mint the App token degrades to
        "no credential", exactly as a missing PAT does today.

    Raises:
        ManagedCredentialError: A managed grant could not provide a fresh
            credential. Managed trackers never degrade to anonymous git.
    """

    if tracker is None:
        return None

    if is_managed_tracker(tracker):
        credential = await resolve_managed_tracker_credential(tracker)
        logger.info(
            "Resolved a managed %s credential for tracker %s (rotation %s)",
            tracker.tracker_type,
            getattr(tracker, "id", "unknown"),
            credential.rotation_version,
        )
        return credential.access_token

    stored = getattr(tracker, "resolved_api_key", None)
    if stored:
        return stored

    auth_type = (getattr(tracker, "auth_type", None) or "").lower()
    if auth_type not in APP_AUTH_TYPES:
        return None

    installation = getattr(tracker, "oauth_installation", None)
    installation_id = getattr(installation, "external_id", None)
    if not installation_id:
        logger.warning(
            "Tracker %s uses %s auth but has no OAuth installation; "
            "git operations will run unauthenticated",
            getattr(tracker, "id", "unknown"),
            auth_type,
        )
        return None

    try:
        # Imported lazily: the GitHub App service ships as a proprietary
        # plugin, so open-source deployments simply have no App trackers.
        from preloop.plugins.proprietary.github_app.service import (
            get_github_app_service,
        )

        token = await get_github_app_service().get_installation_access_token(
            installation_id
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never fail the flow
        logger.warning(
            "Could not mint an installation access token for tracker %s: %s",
            getattr(tracker, "id", "unknown"),
            exc,
        )
        return None

    if not token:
        return None

    logger.info(
        "Resolved a GitHub App installation token for tracker %s "
        "(expires within the hour; minted at execution start and consumed "
        "by the same container's post-execution git push)",
        getattr(tracker, "id", "unknown"),
    )
    return token


def resolve_tracker_git_username(tracker: Any) -> Optional[str]:
    """Return the git HTTPS username a tracker's token must be paired with.

    Only Bitbucket needs a per-tracker answer: an API token authenticates git
    with the account's Bitbucket username (or ``x-bitbucket-api-token-auth``),
    while repository access tokens and OAuth tokens use ``x-token-auth``. The
    account email, which Bitbucket REST accepts for Basic auth, is never
    returned.

    Args:
        tracker: A ``Tracker`` ORM instance or a compatible object.

    Returns:
        The username, or None when the provider-wide default applies.
    """
    if tracker is None:
        return None
    tracker_type = str(getattr(tracker, "tracker_type", "") or "").lower()
    if tracker_type != "bitbucket":
        return None
    from preloop.utils.bitbucket import git_username_for

    return git_username_for(
        auth_type=getattr(tracker, "auth_type", None),
        connection_details=getattr(tracker, "connection_details", None) or {},
    )
