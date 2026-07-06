"""Tests for DynamicFastMCP HTTP transport setup helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from preloop.services.dynamic_fastmcp_http import (
    mcp_http_allowed_hosts,
    mcp_http_allowed_origins,
    setup_dynamic_mcp_http,
)


def test_mcp_http_allowed_hosts_from_review_url() -> None:
    """Review-app hostnames should allow the branch host and wildcard pattern."""
    url = "https://oss-session-optimization.test.preloop.ai"
    assert mcp_http_allowed_hosts(url) == [
        "oss-session-optimization.test.preloop.ai",
        "*.test.preloop.ai",
    ]


def test_mcp_http_allowed_hosts_from_production_url() -> None:
    """Production hostnames should only allow the configured public host."""
    url = "https://app.preloop.ai"
    assert mcp_http_allowed_hosts(url) == ["app.preloop.ai"]


def test_mcp_http_allowed_hosts_empty_when_url_missing() -> None:
    """Missing PRELOOP_URL should not inject additional host patterns."""
    assert mcp_http_allowed_hosts("") == []


def test_mcp_http_allowed_origins() -> None:
    """Allowed origins should mirror the configured public base URL."""
    url = "https://oss-session-optimization.test.preloop.ai/"
    assert mcp_http_allowed_origins(url) == [
        "https://oss-session-optimization.test.preloop.ai"
    ]


def test_setup_dynamic_mcp_http_passes_allowed_hosts_when_supported() -> None:
    """FastMCP 3.4+ should receive public host allowlisting from PRELOOP_URL."""
    mcp = MagicMock()
    mcp.http_app.return_value = MagicMock()
    mcp.http_app.__signature__ = None

    with patch(
        "preloop.services.dynamic_fastmcp_http.inspect.signature",
        return_value=MagicMock(
            parameters={
                "allowed_hosts": MagicMock(),
                "allowed_origins": MagicMock(),
            }
        ),
    ):
        with patch.dict(
            "os.environ",
            {"PRELOOP_URL": "https://oss-session-optimization.test.preloop.ai"},
            clear=False,
        ):
            setup_dynamic_mcp_http(mcp)

    mcp.http_app.assert_called_once()
    kwargs = mcp.http_app.call_args.kwargs
    assert kwargs["allowed_hosts"] == [
        "oss-session-optimization.test.preloop.ai",
        "*.test.preloop.ai",
    ]
    assert kwargs["allowed_origins"] == [
        "https://oss-session-optimization.test.preloop.ai"
    ]
