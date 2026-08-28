"""TLS verify overrides for provider HTTP clients (private PKI)."""

from __future__ import annotations

from preloop.services.tls_verify import ssl_verify_setting


def test_ssl_verify_default_is_none(monkeypatch) -> None:
    for key in (
        "PRELOOP_SSL_VERIFY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(key, raising=False)
    assert ssl_verify_setting() is None


def test_ssl_cert_file_wins_over_requests_bundle(monkeypatch) -> None:
    monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/private-ca/ca.crt")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/unused.pem")
    assert ssl_verify_setting() == "/etc/ssl/private-ca/ca.crt"


def test_requests_ca_bundle_used_when_ssl_cert_file_missing(monkeypatch) -> None:
    monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/private-ca/ca.crt")
    assert ssl_verify_setting() == "/etc/ssl/private-ca/ca.crt"


def test_preloop_ssl_verify_false_skips_verification(monkeypatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/private-ca/ca.crt")
    monkeypatch.setenv("PRELOOP_SSL_VERIFY", "false")
    assert ssl_verify_setting() is False
