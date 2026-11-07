"""The development compose stack forwards the same SMTP settings as the release one.

``docker-compose.yml`` lists its container environment explicitly, so a
``SMTP_*`` line in ``.env`` only reaches a service when the key is forwarded
in that service's ``environment`` block. The release compose forwards the
mail settings to every service that sends email; the development compose must
mirror it, otherwise approval, invitation and password-reset mail silently
never leaves a local stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
RELEASE_COMPOSE = REPO_ROOT / "docker-compose.release.yaml"

SMTP_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_FROM_NAME",
)


def _services(path: Path) -> Dict[str, Dict]:
    return yaml.safe_load(path.read_text())["services"]


def _smtp_env(spec: Dict) -> Dict[str, str]:
    env = spec.get("environment") or {}
    return {key: env[key] for key in SMTP_KEYS if key in env}


def test_release_compose_forwards_smtp_to_mail_sending_services() -> None:
    """The release file is the reference; it must forward all six keys somewhere."""
    holders = {
        name: _smtp_env(spec)
        for name, spec in _services(RELEASE_COMPOSE).items()
        if _smtp_env(spec)
    }
    assert holders, "release compose forwards no SMTP settings"
    for name, env in holders.items():
        assert set(env) == set(SMTP_KEYS), name


def test_dev_compose_mirrors_release_smtp_wiring() -> None:
    """Every service that gets SMTP settings in the release compose gets the

    same interpolated values in the development compose, so a ``.env`` file
    configures mail identically for both stacks.
    """
    release = _services(RELEASE_COMPOSE)
    dev = _services(DEV_COMPOSE)
    for name, spec in release.items():
        expected = _smtp_env(spec)
        if not expected:
            continue
        assert name in dev, f"{name} missing from the development compose"
        assert _smtp_env(dev[name]) == expected, name
