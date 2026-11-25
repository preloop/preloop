"""Generate an installation-owned personal Teams app package, without secrets."""

from __future__ import annotations

import io
import json
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID
from zipfile import ZipFile, ZIP_DEFLATED

from preloop.services.chat_providers import ChatProviderError

MANIFEST_SCHEMA = "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json"


def manifest_url(value: str) -> str:
    """Require an absolute HTTPS information URL without credentials."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or len(value) > 2048
            or "\\" in value
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError()
    except ValueError as exc:
        raise ChatProviderError("Teams requires public HTTPS manifest URLs") from exc
    return value


def teams_manifest(
    app_id: str,
    package_id: str,
    origin: str,
    *,
    developer_url: str = "",
    privacy_url: str = "",
    terms_url: str = "",
) -> dict[str, Any]:
    """Build schema 1.30 with only personal bot scope and installation URLs."""
    parsed = urlsplit(manifest_url(origin))
    installation_origin = f"https://{parsed.netloc}"
    return {
        "$schema": MANIFEST_SCHEMA,
        "manifestVersion": "1.30",
        "version": "1.0.0",
        "id": str(UUID(package_id)),
        "developer": {
            "name": "Preloop",
            "websiteUrl": manifest_url(developer_url or installation_origin),
            "privacyUrl": manifest_url(privacy_url or installation_origin),
            "termsOfUseUrl": manifest_url(terms_url or installation_origin),
        },
        "name": {"short": "Preloop", "full": "Preloop chat"},
        "description": {
            "short": "Ask questions and review approvals in your personal Preloop chat.",
            "full": "Link your Preloop identity to ask questions, run enabled flows and receive approval notices in personal chat.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#242424",
        "bots": [
            {
                "botId": str(UUID(app_id)),
                "scopes": ["personal"],
                "isNotificationOnly": False,
                "supportsFiles": False,
            }
        ],
        "validDomains": [parsed.hostname],
    }


def teams_package(
    app_id: str,
    package_id: str,
    origin: str,
    **urls: str,
) -> bytes:
    """Zip the manifest and existing brand icons; never accept credentials."""
    manifest = teams_manifest(app_id, package_id, origin, **urls)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name in ("color.png", "outline.png"):
            archive.writestr(
                name, files("preloop").joinpath("static", "teams", name).read_bytes()
            )
    return output.getvalue()
