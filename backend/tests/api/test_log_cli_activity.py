"""Unit tests for version endpoint CLI activity audit logging."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from preloop.api.endpoints import version as version_module


@pytest.fixture
def cli_request() -> MagicMock:
    request = MagicMock()
    request.headers = {
        "user-agent": "preloop-cli/0.10.0 (darwin; arm64)",
    }
    return request


def test_log_cli_activity_uses_separate_session(monkeypatch, cli_request):
    """Audit logging must not reuse/commit the request-scoped session."""
    audit_session = MagicMock(name="audit_session")
    request_session = MagicMock(name="request_session")
    recorded = {}

    def fake_log_action(db, **kwargs):
        recorded["db"] = db
        recorded.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        version_module.settings, "installer_audit_account_id", "acct-123"
    )
    monkeypatch.setattr(
        version_module, "get_session_factory", lambda: lambda: audit_session
    )
    monkeypatch.setattr(version_module.crud_audit_log, "log_action", fake_log_action)

    version_module._log_cli_activity(
        request=cli_request,
        client_ip="1.2.3.4",
        client_version_header="0.10.0",
    )

    assert recorded["db"] is audit_session
    assert recorded["db"] is not request_session
    assert recorded["action"] == "cli_activity"
    request_session.commit.assert_not_called()
    audit_session.close.assert_called_once()


def test_log_cli_activity_skips_without_audit_account(monkeypatch, cli_request):
    factory = MagicMock()
    monkeypatch.setattr(version_module.settings, "installer_audit_account_id", None)
    monkeypatch.setattr(version_module, "get_session_factory", factory)

    version_module._log_cli_activity(
        request=cli_request,
        client_ip="1.2.3.4",
        client_version_header=None,
    )

    factory.assert_not_called()


def test_log_cli_activity_sqlalchemy_error_is_swallowed(monkeypatch, cli_request):
    """DB failures must not raise; session is rolled back and closed."""
    audit_session = MagicMock(name="audit_session")

    def boom(db, **kwargs):
        raise SQLAlchemyError("commit failed")

    monkeypatch.setattr(
        version_module.settings, "installer_audit_account_id", "acct-123"
    )
    monkeypatch.setattr(
        version_module, "get_session_factory", lambda: lambda: audit_session
    )
    monkeypatch.setattr(version_module.crud_audit_log, "log_action", boom)

    version_module._log_cli_activity(
        request=cli_request,
        client_ip="1.2.3.4",
        client_version_header=None,
    )

    audit_session.rollback.assert_called_once()
    audit_session.close.assert_called_once()


def test_log_cli_activity_rollback_failure_is_logged(monkeypatch, cli_request, caplog):
    """Rollback failures are debug-logged; version path still does not raise."""
    import logging

    # configure_logging() sets propagate=False on the "preloop" logger, which
    # keeps records from reaching caplog's root handler once any import has
    # configured logging. Restore propagation for this assertion only.
    monkeypatch.setattr(logging.getLogger("preloop"), "propagate", True)

    audit_session = MagicMock(name="audit_session")
    audit_session.rollback.side_effect = SQLAlchemyError("rollback failed")

    def boom(db, **kwargs):
        raise SQLAlchemyError("commit failed")

    monkeypatch.setattr(
        version_module.settings, "installer_audit_account_id", "acct-123"
    )
    monkeypatch.setattr(
        version_module, "get_session_factory", lambda: lambda: audit_session
    )
    monkeypatch.setattr(version_module.crud_audit_log, "log_action", boom)

    with caplog.at_level(logging.DEBUG, logger=version_module.logger.name):
        version_module._log_cli_activity(
            request=cli_request,
            client_ip="1.2.3.4",
            client_version_header=None,
        )

    assert any(
        "Failed to roll back CLI activity audit session" in record.message
        for record in caplog.records
    )
    audit_session.close.assert_called_once()
