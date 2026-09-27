"""Guards for the development-only credential defaults in docker-compose.yml.

The development compose stack must keep working with no configuration, but
its credential defaults have to be visibly development-only: interpolated
from the environment (so a ``.env`` file overrides them) and recognised by
the backend's startup warnings when the placeholder is actually used.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Dict

import pytest
import yaml

from preloop.config import (
    is_placeholder_jwt_secret,
    logger as config_logger,
    warn_default_database_credentials,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

SECRET_KEY_DEFAULT = "${SECRET_KEY:-development_secret_key_do_not_use_in_production}"
POSTGRES_PASSWORD_DEFAULT = "${POSTGRES_PASSWORD:-postgres}"
DATABASE_URL_DEFAULT = (
    "postgresql+psycopg://postgres:${POSTGRES_PASSWORD:-postgres}@postgres/preloop"
)


class _CapturingHandler(logging.Handler):
    """Collect records from preloop.config (propagate=False after setup)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def config_logs() -> Iterator[_CapturingHandler]:
    handler = _CapturingHandler()
    config_logger.addHandler(handler)
    try:
        yield handler
    finally:
        config_logger.removeHandler(handler)


def _compose_services() -> Dict[str, Dict]:
    return yaml.safe_load(COMPOSE_FILE.read_text())["services"]


def test_secret_key_defaults_are_env_interpolated() -> None:
    """Every SECRET_KEY is overridable and falls back to the dev placeholder."""
    services = _compose_services()
    holders = {
        name: spec["environment"]["SECRET_KEY"]
        for name, spec in services.items()
        if "SECRET_KEY" in (spec.get("environment") or {})
    }
    assert holders, "no compose service carries SECRET_KEY"
    for name, value in holders.items():
        assert value == SECRET_KEY_DEFAULT, name


def test_postgres_password_default_is_env_interpolated() -> None:
    """The postgres password is overridable and falls back to the dev default."""
    services = _compose_services()
    postgres_env = services["postgres"]["environment"]
    assert postgres_env["POSTGRES_PASSWORD"] == POSTGRES_PASSWORD_DEFAULT
    for name, spec in services.items():
        url = (spec.get("environment") or {}).get("DATABASE_URL")
        if url is not None:
            assert url == DATABASE_URL_DEFAULT, name


def test_compose_secret_key_fallback_triggers_the_startup_warning() -> None:
    """The compose fallback is a known placeholder, so startup warns on it."""
    fallback = SECRET_KEY_DEFAULT.split(":-", 1)[1].rstrip("}")
    assert is_placeholder_jwt_secret(fallback) is True


def test_default_database_credentials_warn(
    config_logs: _CapturingHandler,
) -> None:
    """postgres:postgres credentials produce a startup warning, value-free."""
    warn_default_database_credentials(
        "postgresql+psycopg://postgres:postgres@postgres/preloop"
    )
    text = "\n".join(config_logs.messages)
    assert "development default" in text
    assert "postgres/preloop" not in text


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://postgres:s3cure-real-password@postgres/preloop",
        "postgresql+psycopg://app_user:postgres@postgres/preloop",
        "postgresql+psycopg://postgres@postgres/preloop",
        "not a url at all",
    ],
)
def test_non_default_database_credentials_stay_silent(
    url: str, config_logs: _CapturingHandler
) -> None:
    """Real credentials (and unparseable URLs) produce no warning."""
    warn_default_database_credentials(url)
    assert config_logs.messages == []
