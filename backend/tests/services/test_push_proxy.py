"""Unit tests for push notification proxy service."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services import push_proxy
from preloop.services.push_proxy import (
    is_push_proxy_configured,
    send_push_via_proxy,
    send_bulk_push_via_proxy,
)


class TestIsPushProxyConfigured:
    """Tests for is_push_proxy_configured."""

    def test_not_configured_when_both_none(self):
        """Returns False when PUSH_PROXY_URL and PUSH_PROXY_API_KEY are None."""
        with (
            patch.object(push_proxy, "PUSH_PROXY_URL", None),
            patch.object(push_proxy, "PUSH_PROXY_API_KEY", None),
        ):
            assert is_push_proxy_configured() is False

    def test_not_configured_when_only_url(self):
        """Returns False when only PUSH_PROXY_URL is set."""
        with (
            patch.object(push_proxy, "PUSH_PROXY_URL", "https://proxy.example.com"),
            patch.object(push_proxy, "PUSH_PROXY_API_KEY", None),
        ):
            assert is_push_proxy_configured() is False

    def test_configured_when_both_set(self):
        """Returns True when both PUSH_PROXY_URL and PUSH_PROXY_API_KEY are set."""
        with (
            patch.object(push_proxy, "PUSH_PROXY_URL", "https://proxy.example.com"),
            patch.object(push_proxy, "PUSH_PROXY_API_KEY", "test-key"),
        ):
            assert is_push_proxy_configured() is True


@pytest.mark.asyncio
class TestSendPushViaProxy:
    """Tests for send_push_via_proxy."""

    async def test_returns_error_when_not_configured(self):
        """Returns error dict when push proxy is not configured."""
        with patch(
            "preloop.services.push_proxy.is_push_proxy_configured",
            return_value=False,
        ):
            result = await send_push_via_proxy(
                platform="ios",
                device_token="token123",
                title="Test",
                body="Body",
            )
            assert result["success"] is False
            assert "not configured" in result["error"].lower()

    @patch("preloop.services.push_proxy.is_push_proxy_configured", return_value=True)
    @patch("preloop.services.push_proxy.PUSH_PROXY_URL", "https://proxy.example.com")
    @patch("preloop.services.push_proxy.PUSH_PROXY_API_KEY", "test-key")
    async def test_success_on_200(self, mock_configured):
        """Returns success when proxy returns 200 with success=True."""

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "details": {}}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await send_push_via_proxy(
                platform="android",
                device_token="token456",
                title="Title",
                body="Body",
            )
            assert result["success"] is True
            assert "result" in result

    @patch("preloop.services.push_proxy.is_push_proxy_configured", return_value=True)
    @patch("preloop.services.push_proxy.PUSH_PROXY_URL", "https://proxy.example.com")
    @patch("preloop.services.push_proxy.PUSH_PROXY_API_KEY", "test-key")
    async def test_success_log_omits_the_title(self, mock_configured):
        """The success log must not echo the caller-supplied title.

        Regression for py/clear-text-logging-sensitive-data in the flow-failure
        alert path, which derives the push title from the flow name.
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "details": {}}

        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Handler(level=logging.DEBUG)
        push_logger = logging.getLogger("preloop.services.push_proxy")
        previous_level = push_logger.level
        push_logger.addHandler(handler)
        push_logger.setLevel(logging.DEBUG)
        try:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_class.return_value = mock_client

                await send_push_via_proxy(
                    platform="ios",
                    device_token="token456",
                    title="TITLE-SENTINEL-should-not-be-logged",
                    body="Body",
                )
        finally:
            push_logger.removeHandler(handler)
            push_logger.setLevel(previous_level)

        assert records, "no log records captured; assertion would be vacuous"
        assert "TITLE-SENTINEL-should-not-be-logged" not in "\n".join(records)

    @patch("preloop.services.push_proxy.is_push_proxy_configured", return_value=True)
    @patch("preloop.services.push_proxy.PUSH_PROXY_URL", "https://proxy.example.com")
    @patch("preloop.services.push_proxy.PUSH_PROXY_API_KEY", "test-key")
    async def test_failure_on_401(self, mock_configured):
        """Returns error on 401 invalid API key."""
        mock_response = AsyncMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await send_push_via_proxy(
                platform="ios",
                device_token="token",
                title="T",
                body="B",
            )
            assert result["success"] is False
            assert "Invalid API key" in result["error"]

    @patch("preloop.services.push_proxy.is_push_proxy_configured", return_value=True)
    @patch("preloop.services.push_proxy.PUSH_PROXY_URL", "https://proxy.example.com")
    @patch("preloop.services.push_proxy.PUSH_PROXY_API_KEY", "test-key")
    async def test_timeout_returns_error(self, mock_configured):
        import httpx

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.TimeoutException("timeout")
            )
            mock_client.__aexit__.return_value = None
            mock_client_class.return_value = mock_client

            result = await send_push_via_proxy(
                platform="ios",
                device_token="token",
                title="T",
                body="B",
            )
            assert result["success"] is False
            assert (
                "timeout" in result["error"].lower()
                or "timed out" in result["error"].lower()
            )


@pytest.mark.asyncio
class TestSendBulkPushViaProxy:
    """Tests for send_bulk_push_via_proxy."""

    async def test_returns_errors_when_not_configured(self):
        """Returns list of errors when push proxy is not configured."""
        with patch(
            "preloop.services.push_proxy.is_push_proxy_configured",
            return_value=False,
        ):
            notifications = [
                {"platform": "ios", "device_token": "t1", "title": "T", "body": "B"},
            ]
            result = await send_bulk_push_via_proxy(notifications)
            assert len(result) == 1
            assert result[0]["success"] is False
            assert "not configured" in result[0]["error"].lower()

    @patch("preloop.services.push_proxy.is_push_proxy_configured", return_value=True)
    @patch("preloop.services.push_proxy.PUSH_PROXY_URL", "https://proxy.example.com/")
    @patch("preloop.services.push_proxy.PUSH_PROXY_API_KEY", "test-key")
    async def test_success_on_200(self, mock_configured):
        """Returns results when bulk proxy returns 200."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"success": True},
            {"success": True},
        ]

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            notifications = [
                {"platform": "ios", "device_token": "t1", "title": "T1", "body": "B1"},
                {
                    "platform": "android",
                    "device_token": "t2",
                    "title": "T2",
                    "body": "B2",
                },
            ]
            result = await send_bulk_push_via_proxy(notifications)
            assert len(result) == 2
            assert result[0]["success"] is True
            assert result[1]["success"] is True
