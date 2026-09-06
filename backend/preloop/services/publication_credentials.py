"""Repository-scoped GitHub App leases for isolated clone/publication.

No stored tracker PAT is accepted: unlike installation tokens, its permissions
cannot be reduced before it crosses into an agent runtime.
"""

from __future__ import annotations

import base64
import binascii
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx
import jwt

from preloop.config import settings
from preloop.models import models
from preloop.services.trusted_publisher import PublicationError, PublicationLease


def validate_publication_tracker(tracker: models.Tracker) -> None:
    """Reject unsupported credentials before starting an isolated flow."""
    installation = getattr(tracker, "oauth_installation", None)
    if (
        tracker.tracker_type != "github"
        or tracker.auth_type != "github_app"
        or not getattr(installation, "external_id", None)
    ):
        raise PublicationError(
            "Isolated publication requires a GitHub App installation; PAT/GitLab credentials cannot yet be safely downscoped. Use an App tracker or explicitly retain legacy publication mode."
        )
    if not settings.github_app.app_id or not settings.github_app.private_key:
        raise PublicationError(
            "Isolated publication requires the GitHub App signing configuration on the trusted control plane"
        )


async def mint_repository_lease(
    tracker: models.Tracker,
    repository_url: str,
    *,
    write: bool,
    client: httpx.AsyncClient,
) -> PublicationLease:
    """Ask GitHub to enforce exact repository and read/write permissions."""
    validate_publication_tracker(tracker)
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PublicationError(
            "GitHub App leases require a canonical GitHub repository URL"
        )
    project = parsed.path.strip("/").removesuffix(".git")
    if len(project.split("/")) != 2:
        raise PublicationError("Invalid GitHub repository binding")
    key = settings.github_app.private_key
    if not key.startswith("-----BEGIN"):
        try:
            key = base64.b64decode(key, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise PublicationError("Invalid GitHub App signing configuration") from exc
    now = int(time.time())
    try:
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": settings.github_app.app_id},
            key,
            algorithm="RS256",
        )
    except (ValueError, jwt.PyJWTError) as exc:
        raise PublicationError("GitHub App signing failed") from exc
    permissions = {"contents": "write" if write else "read"}
    if write:
        permissions["pull_requests"] = "write"
    installation_id = str(tracker.oauth_installation.external_id)
    if not installation_id.isdecimal():
        raise PublicationError("Invalid GitHub App installation identifier")
    try:
        response = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
            json={"repositories": [project.split("/")[1]], "permissions": permissions},
            timeout=30,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        repositories = data.get("repositories", [])
        returned = data.get("permissions")
        # GitHub implicitly grants metadata:read. No other additional scope,
        # including read access to issues/checks, is part of this capability.
        allowed = {**permissions, "metadata": "read"}
        valid_permissions = isinstance(returned, dict) and (
            all(returned.get(key) == value for key, value in permissions.items())
            and all(allowed.get(key) == value for key, value in returned.items())
        )
        if (
            len(repositories) != 1
            or repositories[0].get("full_name") != project
            or not valid_permissions
        ):
            raise PublicationError(
                "GitHub did not return the requested repository/permission scope"
            )
        lease = PublicationLease(
            token=data["token"],
            repository_url=repository_url,
            expires_at=datetime.fromisoformat(
                data["expires_at"].replace("Z", "+00:00")
            ),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(
            "Could not issue a scoped GitHub App credential"
        ) from exc
    return lease


async def revoke_repository_lease(
    lease: PublicationLease, client: httpx.AsyncClient
) -> None:
    """Revoke the publisher credential on success or failure; expiry is fallback."""
    try:
        response = await client.delete(
            "https://api.github.com/installation/token",
            headers={"Authorization": f"Bearer {lease.token}"},
            timeout=15,
            follow_redirects=False,
        )
        # The helper and controller both revoke independently. GitHub returns
        # 401 when the known token was already revoked/expired, which proves it
        # is unusable. Authorization/server errors still fail closed.
        if response.status_code != 401:
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PublicationError(
            "Publisher token revocation failed; token will expire at its issued deadline"
        ) from exc
