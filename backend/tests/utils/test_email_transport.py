"""SMTP transport security and unauthenticated relays (self-hosted mail).

All hosts and addresses are synthetic; smtplib is mocked, nothing is contacted.
"""

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from preloop.utils import email
from preloop.utils.email import EmailError, send_email

SECRET = "relay-password-synthetic"


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Mock both SMTP classes and point the sender at a synthetic relay."""
    monkeypatch.setattr(email, "SMTP_HOST", "relay.example.com")
    monkeypatch.setattr(email, "SMTP_PORT", 25)
    monkeypatch.setattr(email, "SMTP_USERNAME", "")
    monkeypatch.setattr(email, "SMTP_PASSWORD", "")
    monkeypatch.setattr(email, "SMTP_SECURITY", "starttls")
    servers = {}
    for name in ("SMTP", "SMTP_SSL"):
        cls = MagicMock()
        server = MagicMock()
        server.has_extn.return_value = True
        cls.return_value.__enter__.return_value = server
        monkeypatch.setattr(email.smtplib, name, cls)
        servers[name] = cls
    return servers


def _server(cls: MagicMock) -> MagicMock:
    return cls.return_value.__enter__.return_value


def _send() -> bool:
    return send_email("jane.doe@example.com", "Subject", "Body")


def test_relay_without_credentials_sends_without_auth(smtp) -> None:
    """protects=internal relays without AUTH; fails_when=no creds means no mail."""
    assert _send() is True
    server = _server(smtp["SMTP"])
    server.starttls.assert_called_once()
    server.login.assert_not_called()
    server.sendmail.assert_called_once()


def test_no_host_still_skips(smtp) -> None:
    """protects=dev mode without SMTP_HOST; fails_when=connects to empty host."""
    email.SMTP_HOST = ""
    assert _send() is False
    smtp["SMTP"].assert_not_called()


def test_implicit_tls_uses_smtp_ssl(smtp, monkeypatch) -> None:
    """protects=port 465 relays; fails_when=STARTTLS issued on implicit TLS."""
    monkeypatch.setattr(email, "SMTP_SECURITY", "ssl")
    monkeypatch.setattr(email, "SMTP_USERNAME", "jane@example.com")
    monkeypatch.setattr(email, "SMTP_PASSWORD", SECRET)
    assert _send() is True
    smtp["SMTP"].assert_not_called()
    server = _server(smtp["SMTP_SSL"])
    server.starttls.assert_not_called()
    server.login.assert_called_once_with("jane@example.com", SECRET)


def test_none_mode_sends_plain_without_auth(smtp, monkeypatch) -> None:
    """protects=relays without STARTTLS; fails_when=STARTTLS is always called."""
    monkeypatch.setattr(email, "SMTP_SECURITY", "none")
    assert _send() is True
    server = _server(smtp["SMTP"])
    server.starttls.assert_not_called()
    server.login.assert_not_called()


def test_none_mode_refuses_credentials(smtp, monkeypatch) -> None:
    """protects=credentials never cross plaintext; fails_when=login over none."""
    monkeypatch.setattr(email, "SMTP_SECURITY", "none")
    monkeypatch.setattr(email, "SMTP_USERNAME", "jane@example.com")
    monkeypatch.setattr(email, "SMTP_PASSWORD", SECRET)
    with pytest.raises(EmailError) as caught:
        _send()
    smtp["SMTP"].assert_not_called()
    assert "SMTP_SECURITY" in caught.value.reason
    assert SECRET not in caught.value.reason


def test_unknown_mode_is_refused(smtp, monkeypatch) -> None:
    """protects=typos fail loudly; fails_when=unknown mode silently downgrades."""
    monkeypatch.setattr(email, "SMTP_SECURITY", "tls1")
    with pytest.raises(EmailError) as caught:
        _send()
    smtp["SMTP"].assert_not_called()
    assert "SMTP_SECURITY" in caught.value.reason


def test_starttls_unsupported_gives_actionable_reason(smtp) -> None:
    """protects=operator can fix a relay without STARTTLS; fails_when=raw error."""
    _server(smtp["SMTP"]).starttls.side_effect = smtplib.SMTPNotSupportedError(
        "STARTTLS extension not supported by server."
    )
    with pytest.raises(EmailError) as caught:
        _send()
    assert "STARTTLS" in caught.value.reason
    assert "SMTP_SECURITY" in caught.value.reason


def test_auth_failure_reason_has_no_secret(smtp, monkeypatch) -> None:
    """protects=no secret in the caller-facing reason; fails_when=echoes server."""
    monkeypatch.setattr(email, "SMTP_USERNAME", "jane@example.com")
    monkeypatch.setattr(email, "SMTP_PASSWORD", SECRET)
    _server(smtp["SMTP"]).login.side_effect = smtplib.SMTPAuthenticationError(
        535, f"bad credentials {SECRET}".encode()
    )
    with pytest.raises(EmailError) as caught:
        _send()
    assert caught.value.reason == (
        "The mail server rejected the SMTP username or password."
    )
    assert SECRET not in caught.value.reason


@patch("preloop.utils.email.send_email", return_value=False)
def test_invitation_email_returns_send_result(_send_email: MagicMock) -> None:
    """protects=callers learn nothing was sent; fails_when=result is dropped."""
    assert (
        email.send_invitation_email(
            user_email="jane.doe@example.com",
            token="synthetic-token",
            organization_name="Example",
            invited_by="admin@example.com",
        )
        is False
    )
