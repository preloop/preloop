"""Helpers for safe repository URL handling.

Credential handling deliberately lives in ``preloop.utils.git_credentials``:
URLs here are only ever parsed or sanitized, never given a secret.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def tracker_host_kind(url: str) -> Optional[str]:
    """Return ``github``/``gitlab`` based on hostname allowlist, not substring match."""

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return None

    if hostname == "github.com" or hostname.endswith(".github.com"):
        return "github"

    if hostname == "gitlab.com" or hostname.endswith(".gitlab.com"):
        return "gitlab"

    if any(part == "gitlab" for part in hostname.split(".")):
        return "gitlab"

    return None


# NOTE: ``inject_oauth_token`` used to live here. It embedded a tracker token
# in the clone URL, which made the secret part of the repository's ``origin``
# remote and leaked it into flow execution logs via ``git remote -v``
# (issue #173). It has been removed rather than deprecated so it cannot be
# reintroduced by autocomplete. Use ``preloop.utils.git_credentials`` instead,
# which supplies the token through a git credential helper and leaves the
# remote URL clean.


def repo_url_log_location(repo_url: str) -> str:
    """Return hostname and path for logging without userinfo or credentials."""

    parsed = urlparse(repo_url)
    hostname = parsed.hostname or "unknown"
    path = parsed.path or ""
    if parsed.port is not None:
        return f"{hostname}:{parsed.port}{path}"
    return f"{hostname}{path}"
