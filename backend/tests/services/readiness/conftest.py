"""Readiness fixtures must never contact a provider or inherited HTTP proxy."""

import socket
from typing import Any
from unittest.mock import patch

import pytest
import requests


@pytest.fixture(autouse=True)
def local_readiness_fixtures_only(monkeypatch: Any) -> Any:
    original = socket.getaddrinfo

    def local_dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in {"localhost", "127.0.0.1", "::1", None}:
            raise AssertionError("Readiness fixtures cannot resolve external hosts")
        return original(host, *args, **kwargs)

    def denied_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Readiness fixtures cannot make synchronous HTTP requests")

    monkeypatch.setattr(socket, "getaddrinfo", local_dns)
    monkeypatch.setattr(requests.Session, "request", denied_request)
    with patch("preloop.sync.trackers.jira.JIRA"):
        yield
