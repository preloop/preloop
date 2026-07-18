"""Helpers for safe repository URL handling and credential injection."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse


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


def inject_oauth_token(
    repo_url: str,
    token: str,
    *,
    user: str = "oauth2",
    token_as_username: bool = False,
) -> str:
    """Insert credentials into an HTTPS repo URL using urlparse/urlunparse."""

    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https"}:
        return repo_url

    if parsed.username or parsed.password:
        return repo_url

    hostname = parsed.hostname or ""
    hostport = hostname
    if parsed.port is not None:
        hostport = f"{hostname}:{parsed.port}"

    if token_as_username:
        netloc = f"{token}@{hostport}"
    else:
        netloc = f"{user}:{token}@{hostport}"

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def repo_url_log_location(repo_url: str) -> str:
    """Return hostname and path for logging without userinfo or credentials."""

    parsed = urlparse(repo_url)
    hostname = parsed.hostname or "unknown"
    path = parsed.path or ""
    if parsed.port is not None:
        return f"{hostname}:{parsed.port}{path}"
    return f"{hostname}{path}"
