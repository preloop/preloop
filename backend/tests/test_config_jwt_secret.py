"""Tests for placeholder JWT secret detection and the startup guard."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from preloop.config import (
    Settings,
    is_placeholder_jwt_secret,
    logger as config_logger,
    warn_or_reject_placeholder_jwt_secret,
)

REAL_SECRET = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"


class _CapturingHandler(logging.Handler):
    """Collect records from preloop.config (propagate=False after configure_logging)."""

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


@pytest.mark.parametrize(
    "secret",
    [
        "change-this-in-production",
        "CHANGE-THIS-IN-PRODUCTION",
        "change_this_in_production",
        "changethisinproduction",
        "development_secret_key_do_not_use_in_production",
        "replace-this-in-production",
        "please-change-this",
    ],
)
def test_known_placeholder_jwt_secrets(secret: str) -> None:
    """Helm, compose, and docs placeholders are recognised regardless of case."""
    assert is_placeholder_jwt_secret(secret) is True


@pytest.mark.parametrize(
    "secret",
    [
        REAL_SECRET,
        "openssl-style-hex-is-not-a-placeholder",
        "",
        "   ",
    ],
)
def test_real_jwt_secrets_are_not_placeholders(secret: str) -> None:
    """Ordinary secrets and empty values are not treated as placeholders."""
    assert is_placeholder_jwt_secret(secret) is False


def test_production_rejects_helm_placeholder() -> None:
    """ENVIRONMENT=production already requires SECRET_KEY; placeholders fail closed too."""
    with pytest.raises(ValueError, match="published placeholder"):
        warn_or_reject_placeholder_jwt_secret(
            "change-this-in-production", environment="production"
        )


def test_production_rejects_placeholder_case_insensitively() -> None:
    with pytest.raises(ValueError, match="published placeholder"):
        warn_or_reject_placeholder_jwt_secret(
            "CHANGE_THIS_IN_PRODUCTION", environment="Production"
        )


def test_non_production_logs_unmissable_warning(
    config_logs: _CapturingHandler,
) -> None:
    """Chart default ENVIRONMENT is development, so upgrades still start with a banner."""
    warn_or_reject_placeholder_jwt_secret(
        "change-this-in-production", environment="development"
    )
    text = "\n".join(config_logs.messages)
    assert "INSECURE JWT SECRET" in text
    assert "published placeholder" in text
    assert "change-this-in-production" not in text


def test_real_secret_is_silent_in_production(
    config_logs: _CapturingHandler,
) -> None:
    warn_or_reject_placeholder_jwt_secret(REAL_SECRET, environment="production")
    assert config_logs.messages == []


def test_settings_from_env_rejects_placeholder_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "change-this-in-production")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@db/preloop"
    )
    with pytest.raises(ValueError, match="published placeholder"):
        Settings.from_env()


def test_settings_from_env_warns_in_development(
    monkeypatch: pytest.MonkeyPatch, config_logs: _CapturingHandler
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SECRET_KEY", "change-this-in-production")
    loaded = Settings.from_env()
    assert loaded.security.secret_key == "change-this-in-production"
    assert loaded.environment == "development"
    assert "INSECURE JWT SECRET" in "\n".join(config_logs.messages)
