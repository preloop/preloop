"""Database credentials never reach log lines or connection error messages."""

import logging
from unittest.mock import MagicMock

import pytest
from loguru import logger
from sqlalchemy.exc import OperationalError

import preloop.models.db.session as session_module
from preloop.models.db.session import redact_secrets_in_text, redact_url

PASSWORD = "s3cret-Pa55word"
URL = f"postgresql+psycopg://appuser:{PASSWORD}@db.example.internal:6543/appdb"


@pytest.fixture
def loguru_caplog(caplog):
    """Route loguru records into pytest's caplog at DEBUG."""
    handler_id = logger.add(
        lambda msg: logging.getLogger("loguru").log(
            msg.record["level"].no, msg.record["message"]
        ),
        level="DEBUG",
    )
    caplog.set_level(logging.DEBUG, logger="loguru")
    yield caplog
    logger.remove(handler_id)


@pytest.fixture(autouse=True)
def reset_engines(monkeypatch):
    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_async_engine", None)
    monkeypatch.setattr(session_module, "check_pgvector_extension", lambda e: True)
    monkeypatch.setattr(session_module, "install_pool_hold_diagnostics", MagicMock())


def test_redact_url_masks_password_and_keeps_location():
    rendered = redact_url(URL)
    assert PASSWORD not in rendered
    for part in ("postgresql+psycopg", "db.example.internal", "6543", "appdb"):
        assert part in rendered


def test_redact_url_never_echoes_unparseable_input():
    assert PASSWORD not in redact_url(f"not a url {PASSWORD}")


def test_connected_debug_line_hides_password(monkeypatch, loguru_caplog):
    monkeypatch.setattr(session_module, "create_engine", MagicMock())
    session_module.get_engine(URL)
    text = loguru_caplog.text
    assert "Connected to database using" in text
    assert PASSWORD not in text
    assert "db.example.internal" in text and "appdb" in text


def test_async_connected_debug_line_hides_password(monkeypatch, loguru_caplog):
    monkeypatch.setattr(session_module, "create_async_engine", MagicMock())
    session_module.get_async_engine(URL)
    text = loguru_caplog.text
    assert "Connected to async database using" in text
    assert PASSWORD not in text
    assert "db.example.internal" in text and "appdb" in text


def test_connection_failure_message_hides_password(monkeypatch, loguru_caplog):
    """A driver error that quotes the URL must not leak it via log or raise."""
    error = OperationalError("SELECT 1", {}, Exception(f"cannot connect to {URL}"))
    monkeypatch.setattr(session_module, "create_engine", MagicMock(side_effect=error))
    with pytest.raises(Exception) as exc_info:
        session_module.get_engine(URL)
    assert "Database connection failed" in str(exc_info.value)
    assert PASSWORD not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert PASSWORD not in loguru_caplog.text
    assert "db.example.internal" in loguru_caplog.text


def test_redact_secrets_in_text_strips_bare_password():
    message = f"auth failed with password {PASSWORD}"
    assert PASSWORD not in redact_secrets_in_text(message, URL)
