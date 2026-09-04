"""Resolve a git-usable token for a tracker.

A tracker authenticates in one of two ways:

* ``api_token``: a personal access token stored (encrypted) on the tracker row
  and read through ``Tracker.resolved_api_key``.
* ``github_app`` / ``oauth_app``: no token is stored at all. The credential is
  a short-lived installation access token minted on demand from the GitHub App
  installation attached to the tracker.

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

logger = logging.getLogger(__name__)

# Tracker auth types whose credential is an installation access token rather
# than a stored secret.
APP_AUTH_TYPES = {"github_app", "oauth_app"}


async def resolve_tracker_git_token(tracker: Any) -> Optional[str]:
    """Return a token usable for git and REST calls against ``tracker``.

    Args:
        tracker: A ``Tracker`` ORM instance (or any object exposing
            ``resolved_api_key``, ``auth_type`` and ``oauth_installation``).

    Returns:
        The token, or None when the tracker has neither a stored key nor a
        usable App installation. Never raises: a failure to mint the App token
        degrades to "no credential", exactly as a missing PAT does today.
    """

    if tracker is None:
        return None

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
