"""Factory for creating tracker clients."""

import logging
from typing import Any, Dict, Optional, Type

from .base import BaseTracker
from .bitbucket import BitbucketTracker
from .bitbucket_dc import BitbucketDCTracker
from .github import GitHubTracker
from .gitlab import GitLabTracker
from .jira import JiraTracker

logger = logging.getLogger(__name__)

# Tracker type to client class. create_tracker_client builds instances; this
# map answers class-level questions (hosts_repositories, hosts_issues) without
# credentials or network access. A new provider registers here once.
TRACKER_CLASSES: Dict[str, Type[BaseTracker]] = {
    "github": GitHubTracker,
    "gitlab": GitLabTracker,
    "jira": JiraTracker,
    "bitbucket": BitbucketTracker,
    "bitbucket_dc": BitbucketDCTracker,
}


def tracker_class_for_type(tracker_type: Optional[str]) -> Optional[Type[BaseTracker]]:
    """Return the client class for ``tracker_type``, or None if unsupported.

    Args:
        tracker_type: Tracker type string, case-insensitive.

    Returns:
        The ``BaseTracker`` subclass implementing that provider.
    """
    return TRACKER_CLASSES.get((tracker_type or "").lower())


def tracker_hosts_repositories(tracker_type: Optional[str]) -> bool:
    """Whether trackers of ``tracker_type`` host git repositories.

    Args:
        tracker_type: Tracker type string, case-insensitive.

    Returns:
        True for code hosts that declare ``hosts_repositories``, False for
        issue-only trackers, providers not yet wired for clone, or unknown
        types.
    """
    tracker_class = tracker_class_for_type(tracker_type)
    return bool(tracker_class and tracker_class.hosts_repositories)


def tracker_hosts_issues(tracker_type: Optional[str]) -> bool:
    """Whether trackers of ``tracker_type`` are issue-only triggers.

    Args:
        tracker_type: Tracker type string, case-insensitive.

    Returns:
        True for issue trackers (Jira) that may need a repository binding.
        Independent of ``hosts_repositories`` so a registered code host that
        has not set that flag is not treated as issue-only.
    """
    tracker_class = tracker_class_for_type(tracker_type)
    return bool(tracker_class and tracker_class.hosts_issues)


async def create_tracker_client(
    tracker_type: str,
    tracker_id: str,
    api_key: str,
    connection_details: Dict[str, Any],
) -> Optional[BaseTracker]:
    """Create a tracker client.

    Args:
        tracker_type: Type of tracker ("github", "jira", "gitlab", "bitbucket").
        tracker_id: ID of the tracker in the database (UUID string).
        api_key: API key or token for the tracker.
        connection_details: Connection details for the tracker.
            For GitHub App OAuth, include:
            - auth_type: "github_app" or "oauth_app"
            - github_installation_id: The GitHub App installation ID

    Returns:
        A tracker client or None if the tracker type is not supported.
    """
    try:
        if tracker_type == "github":
            # Check if this is a GitHub App OAuth tracker
            auth_type = connection_details.get("auth_type", "api_token")
            github_installation_id = connection_details.get("github_installation_id")

            # If no api_key but we have installation_id, use github_app auth
            if not api_key and github_installation_id:
                auth_type = "github_app"
                logger.info(
                    f"Creating GitHub tracker with GitHub App OAuth "
                    f"(installation_id: {github_installation_id})"
                )

            # Validate: if api_token auth but no api_key, that's an error
            if auth_type == "api_token" and not api_key:
                logger.error(
                    "GitHub tracker configured for api_token auth but no API key provided. "
                    "Check tracker configuration or use GitHub App OAuth."
                )
                raise ValueError(
                    "GitHub API token is required for api_token authentication. "
                    "Configure an API token or use GitHub App OAuth."
                )

            # Validate: if github_app/oauth_app auth but no installation_id, that's an error
            if auth_type in ("github_app", "oauth_app") and not github_installation_id:
                logger.error(
                    f"GitHub tracker configured for {auth_type} auth but no installation ID provided. "
                    "Check tracker configuration - github_installation_id is required."
                )
                raise ValueError(
                    f"GitHub installation ID is required for {auth_type} authentication. "
                    "Configure the GitHub App installation or use api_token authentication."
                )

            return GitHubTracker(
                tracker_id=tracker_id,
                api_key=api_key or "",
                connection_details=connection_details,
                auth_type=auth_type,
                github_installation_id=github_installation_id,
            )
        elif tracker_type == "gitlab":
            return GitLabTracker(tracker_id, api_key, connection_details)
        elif tracker_type == "jira":
            return JiraTracker(tracker_id, api_key, connection_details)
        elif tracker_type == "bitbucket_dc":
            return BitbucketDCTracker(tracker_id, api_key, connection_details)
        elif tracker_type == "bitbucket":
            return BitbucketTracker(tracker_id, api_key, connection_details)
        else:
            logger.warning(f"Unsupported tracker type: {tracker_type}")
            return None
    except Exception as e:
        logger.exception(f"Failed to create tracker client: {e}")
        return None
