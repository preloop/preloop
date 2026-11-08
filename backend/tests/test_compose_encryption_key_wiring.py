"""Compose stacks forward the dedicated credential encryption key.

Both compose files list container environment explicitly. Without a
``SECURITY__ENCRYPTION_KEY`` entry an operator cannot give a compose install a
key separate from the JWT signing secret, and setting it on some services but
not others would leave them sealing with different keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.release.yaml",
)


def _services(path: Path) -> Dict[str, Dict]:
    return yaml.safe_load(path.read_text())["services"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_service_with_secret_key_gets_encryption_key(path: Path) -> None:
    holders = {
        name: spec.get("environment") or {}
        for name, spec in _services(path).items()
        if "SECRET_KEY" in (spec.get("environment") or {})
    }
    assert holders
    for name, env in holders.items():
        assert env.get("SECURITY__ENCRYPTION_KEY") == (
            "${SECURITY__ENCRYPTION_KEY:-}"
        ), name
