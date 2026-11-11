"""Unit tests for the API Strict-Transport-Security middleware (#2279)."""

from __future__ import annotations

from typing import Dict, List

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from preloop.api.middleware.hsts import HSTSMiddleware, HSTSSettings


def _client(settings: HSTSSettings, *, upstream_hsts: bool = False) -> TestClient:
    async def health(_request: Request) -> PlainTextResponse:
        headers = {"Strict-Transport-Security": "max-age=5"} if upstream_hsts else {}
        return PlainTextResponse("ok", headers=headers)

    app = Starlette(routes=[Route("/api/v1/health", health)])
    app.add_middleware(HSTSMiddleware, settings=settings)
    return TestClient(app)


def _hsts(response) -> List[str]:
    return response.headers.get_list("strict-transport-security")


DEFAULT = HSTSSettings.from_env({})


def test_https_request_gets_default_header() -> None:
    """protects=criterion 1/6: one default HSTS header on a direct HTTPS hit;
    fails_when=missing, duplicated or a non-default value; no_claim=Starlette
    TestClient stands in for uvicorn behind an ingress."""
    response = _client(DEFAULT).get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert _hsts(response) == ["max-age=31536000"]


def test_https_scheme_without_forwarded_header_gets_header() -> None:
    """protects=HTTPS detection from the connection scheme;
    fails_when=only X-Forwarded-Proto is honoured; no_claim=none beyond protects."""
    client = TestClient(_client(DEFAULT).app, base_url="https://testserver")
    assert _hsts(client.get("/api/v1/health")) == ["max-age=31536000"]


def test_plain_http_gets_no_header() -> None:
    """protects=criterion 2: no HSTS over plain HTTP; fails_when=the header is
    sent regardless of scheme; no_claim=none beyond protects."""
    response = _client(DEFAULT).get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "http"}
    )
    assert _hsts(response) == []
    assert _hsts(_client(DEFAULT).get("/api/v1/health")) == []


def test_disabled_sends_nothing() -> None:
    """protects=criterion 3 opt-out; fails_when=HSTS_ENABLED=false still sends;
    no_claim=none beyond protects."""
    settings = HSTSSettings.from_env({"HSTS_ENABLED": "false"})
    response = _client(settings).get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert _hsts(response) == []


def test_custom_values() -> None:
    """protects=criterion 4; fails_when=max-age or includeSubDomains ignored;
    no_claim=none beyond protects."""
    settings = HSTSSettings.from_env(
        {"HSTS_MAX_AGE": "63072000", "HSTS_INCLUDE_SUBDOMAINS": "true"}
    )
    response = _client(settings).get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert _hsts(response) == ["max-age=63072000; includeSubDomains"]


def test_existing_header_is_left_alone() -> None:
    """protects=criterion 6: exactly one header when the app already set it;
    fails_when=a second header is appended or the value is overwritten;
    no_claim=a header set by a proxy after the API (nginx hides it)."""
    response = _client(DEFAULT, upstream_hsts=True).get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert _hsts(response) == ["max-age=5"]


@pytest.mark.parametrize(
    "env",
    [
        {"HSTS_PRELOAD": "true"},
        {
            "HSTS_PRELOAD": "true",
            "HSTS_INCLUDE_SUBDOMAINS": "true",
            "HSTS_MAX_AGE": "600",
        },
    ],
)
def test_invalid_preload_refuses_to_start(env: Dict[str, str]) -> None:
    """protects=criterion 5: startup fails naming both settings;
    fails_when=an invalid preload policy is accepted; no_claim=that create_app
    calls from_env (see test_create_app_validates_hsts)."""
    with pytest.raises(ValueError) as excinfo:
        HSTSSettings.from_env(env)
    message = str(excinfo.value)
    assert "HSTS_PRELOAD" in message
    assert "HSTS_INCLUDE_SUBDOMAINS" in message
    assert "HSTS_MAX_AGE" in message


def test_valid_preload_value() -> None:
    """protects=a valid preload policy is rendered in full;
    fails_when=preload is dropped; no_claim=none beyond protects."""
    settings = HSTSSettings.from_env(
        {"HSTS_PRELOAD": "true", "HSTS_INCLUDE_SUBDOMAINS": "true"}
    )
    assert settings.header_value == "max-age=31536000; includeSubDomains; preload"


def test_invalid_max_age_is_rejected() -> None:
    """protects=startup refuses a malformed max-age; fails_when=a garbage
    header value would ship; no_claim=none beyond protects."""
    with pytest.raises(ValueError, match="HSTS_MAX_AGE"):
        HSTSSettings.from_env({"HSTS_MAX_AGE": "a year"})


def test_create_app_validates_hsts(monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=the API refuses to start on an invalid preload combination;
    fails_when=create_app does not read the HSTS settings;
    no_claim=gateway-only deployments beyond the shared create_app."""
    from preloop.api.app import create_app

    monkeypatch.setenv("HSTS_PRELOAD", "true")
    monkeypatch.setenv("HSTS_INCLUDE_SUBDOMAINS", "false")
    with pytest.raises(ValueError, match="HSTS_INCLUDE_SUBDOMAINS"):
        create_app()


def test_hsts_middleware_is_outermost(monkeypatch: pytest.MonkeyPatch) -> None:
    """protects=HSTS on responses short-circuited by any other middleware,
    including plugin ones registered in create_app; fails_when=another
    middleware is added after HSTSMiddleware; no_claim=plugins loaded at
    runtime in EE (none are loaded here)."""
    from preloop.api.app import create_app

    for name in ("HSTS_PRELOAD", "HSTS_INCLUDE_SUBDOMAINS"):
        monkeypatch.delenv(name, raising=False)
    app = create_app()
    assert app.user_middleware[0].cls is HSTSMiddleware
