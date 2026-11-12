"""Compose services forward the HSTS settings from ``.env`` (#2279).

The compose files list container environment explicitly, so an ``HSTS_*``
line in ``.env`` only reaches a service when that service forwards it. Without
the forwarding the documented opt-out (``HSTS_ENABLED=false``) and the short
first-rollout ``HSTS_MAX_AGE`` silently do nothing on a Compose install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "HSTS_ENABLED": "${HSTS_ENABLED:-true}",
    "HSTS_MAX_AGE": "${HSTS_MAX_AGE:-31536000}",
    "HSTS_INCLUDE_SUBDOMAINS": "${HSTS_INCLUDE_SUBDOMAINS:-false}",
    "HSTS_PRELOAD": "${HSTS_PRELOAD:-false}",
}


def _env(path: Path, service: str) -> Dict[str, str]:
    env = yaml.safe_load(path.read_text())["services"][service].get("environment")
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return env or {}


@pytest.mark.parametrize(
    ("compose", "service"),
    [
        ("docker-compose.release.yaml", "api"),
        ("docker-compose.release.yaml", "gateway"),
        ("docker-compose.release.yaml", "console"),
        ("docker-compose.yml", "api"),
        ("docker-compose.yml", "gateway"),
        ("frontend/docker-compose.yml", "frontend"),
    ],
)
def test_service_forwards_hsts_settings(compose: str, service: str) -> None:
    """protects=the Compose opt-out and max-age reaching nginx and the API;
    fails_when=a service that sends HSTS does not forward an HSTS_* key or
    uses another default; no_claim=docker compose interpolation itself."""
    env = _env(REPO_ROOT / compose, service)
    assert {key: env.get(key) for key in EXPECTED} == EXPECTED


def test_env_example_documents_hsts_settings() -> None:
    """protects=operators finding the settings; fails_when=.env.example omits
    one; no_claim=none beyond protects."""
    text = (REPO_ROOT / ".env.example").read_text()
    for key in EXPECTED:
        assert f"# {key}=" in text
