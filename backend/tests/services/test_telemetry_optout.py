"""Tests for telemetry opt-out functionality.

Regression tests to ensure both PRELOOP_DISABLE_TELEMETRY and DISABLE_VERSION_CHECK
environment variables properly disable telemetry.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestTelemetryOptOut:
    """Test that telemetry can be disabled via environment variables."""

    def test_is_telemetry_disabled_with_preloop_env(self):
        """PRELOOP_DISABLE_TELEMETRY=true should disable telemetry."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "true"}):
            # Need to reimport to pick up the patched env
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_legacy_env(self):
        """DISABLE_VERSION_CHECK=true should disable telemetry (backwards compat)."""
        with patch.dict("os.environ", {"DISABLE_VERSION_CHECK": "true"}, clear=False):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_yes_value(self):
        """Telemetry should be disabled with 'yes' value."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "yes"}):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_1_value(self):
        """Telemetry should be disabled with '1' value."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "1"}):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_t_value(self):
        """Telemetry should be disabled with 't' (aligned with the Go CLI)."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "t"}):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_uppercase_t_value(self):
        """Telemetry should be disabled with 'T' (case-insensitive)."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "T"}):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_with_whitespace_padded_value(self):
        """Telemetry should be disabled with a whitespace-padded value."""
        with patch.dict("os.environ", {"PRELOOP_DISABLE_TELEMETRY": "  true  "}):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_is_telemetry_disabled_legacy_env_with_t_value(self):
        """DISABLE_VERSION_CHECK should accept the same truthy set."""
        with patch.dict("os.environ", {"DISABLE_VERSION_CHECK": " T "}, clear=False):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            assert instance_service.is_telemetry_disabled() is True

    def test_telemetry_enabled_by_default(self):
        """Telemetry should be enabled when env vars are not set."""
        with patch.dict("os.environ", {}, clear=True):
            import importlib
            import preloop.services.instance_service as instance_service

            importlib.reload(instance_service)

            # Remove the env vars if they exist
            import os

            os.environ.pop("PRELOOP_DISABLE_TELEMETRY", None)
            os.environ.pop("DISABLE_VERSION_CHECK", None)

            assert instance_service.is_telemetry_disabled() is False


class TestSendVersionCheckOptOut:
    """Test that send_version_check respects the opt-out."""

    @pytest.mark.asyncio
    async def test_send_version_check_returns_false_when_disabled(self):
        """send_version_check should return False immediately when disabled."""
        with patch(
            "preloop.services.instance_service.is_telemetry_disabled",
            return_value=True,
        ):
            from preloop.services.instance_service import send_version_check

            mock_instance = MagicMock()
            result = await send_version_check(mock_instance)

            assert result is False

    @pytest.mark.asyncio
    async def test_send_version_check_makes_request_when_enabled(self):
        """send_version_check should make HTTP request when enabled."""
        with patch(
            "preloop.services.instance_service.is_telemetry_disabled",
            return_value=False,
        ):
            with patch(
                "preloop.services.instance_service.httpx.AsyncClient"
            ) as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"update_available": False}

                mock_client_instance = AsyncMock()
                mock_client_instance.post = AsyncMock(return_value=mock_response)
                mock_client_instance.__aenter__ = AsyncMock(
                    return_value=mock_client_instance
                )
                mock_client_instance.__aexit__ = AsyncMock()
                mock_client.return_value = mock_client_instance

                from preloop.services.instance_service import send_version_check

                mock_instance = MagicMock()
                mock_instance.instance_uuid = "test-uuid"
                mock_instance.version = "1.0.0"
                mock_instance.edition = "community"
                mock_instance.metadata_ = {}

                result = await send_version_check(mock_instance)

                assert result is True
                mock_client_instance.post.assert_called_once()
