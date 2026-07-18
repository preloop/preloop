"""Tests for push notification service initialization."""

from unittest.mock import mock_open, patch

import pytest

from preloop.services.push_notifications import get_apns_service


class TestGetAPNsService:
    """Test get_apns_service singleton initialization."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self, monkeypatch):
        """Reset the singleton between tests."""
        monkeypatch.setattr("preloop.services.push_notifications._apns_service", None)

    def test_get_apns_service_initializes_singleton(self):
        """Test that get_apns_service initializes singleton on first call."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
            "APNS_USE_SANDBOX": "true",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service = get_apns_service()

                assert service is not None
                assert service.team_id == "TESTTEAM12"
                assert service.key_id == "TESTKEY123"
                assert service.bundle_id == "com.test.app"

    def test_get_apns_service_returns_cached_instance(self):
        """Test that subsequent calls return the same cached instance."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
            "APNS_USE_SANDBOX": "true",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service1 = get_apns_service()
                service2 = get_apns_service()

                # Should return the same instance
                assert service1 is service2

    def test_get_apns_service_missing_team_id(self):
        """Test that missing APNS_TEAM_ID returns None."""
        env_vars = {
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            service = get_apns_service()
            assert service is None

    def test_get_apns_service_missing_key_id(self):
        """Test that missing APNS_KEY_ID returns None."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            service = get_apns_service()
            assert service is None

    def test_get_apns_service_missing_auth_key_path(self):
        """Test that missing APNS_AUTH_KEY_PATH returns None."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_BUNDLE_ID": "com.test.app",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            service = get_apns_service()
            assert service is None

    def test_get_apns_service_invalid_auth_key_path(self):
        """Test that invalid auth key path is handled gracefully."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/nonexistent/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            # open() will raise FileNotFoundError
            service = get_apns_service()
            assert service is None

    def test_get_apns_service_sandbox_mode(self):
        """Test that sandbox mode is enabled when APNS_USE_SANDBOX=true."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
            "APNS_USE_SANDBOX": "true",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service = get_apns_service()

                assert service is not None
                assert service.apns_server == "https://api.sandbox.push.apple.com"

    def test_get_apns_service_production_mode(self):
        """Test that production mode is enabled when APNS_USE_SANDBOX=false."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            "APNS_BUNDLE_ID": "com.test.app",
            "APNS_USE_SANDBOX": "false",
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service = get_apns_service()

                assert service is not None
                assert service.apns_server == "https://api.push.apple.com"

    def test_get_apns_service_default_bundle_id(self):
        """Test that default bundle ID is used if not specified."""
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            # APNS_BUNDLE_ID not specified
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service = get_apns_service()

                assert service is not None
                assert service.bundle_id == "spacecode.ai.Preloop"

    def test_get_apns_service_default_production_mode(self):
        """Test that production mode is default if APNS_USE_SANDBOX not specified.

        Production is the default because:
        1. Most deployments are production environments
        2. App Store/Google Play apps use production push tokens
        3. Sandbox mode should be explicitly enabled for development
        """
        env_vars = {
            "APNS_TEAM_ID": "TESTTEAM12",
            "APNS_KEY_ID": "TESTKEY123",
            "APNS_AUTH_KEY_PATH": "/fake/path.p8",
            # APNS_USE_SANDBOX not specified - defaults to production
        }

        with patch(
            "os.getenv", side_effect=lambda k, default=None: env_vars.get(k, default)
        ):
            with patch("builtins.open", mock_open(read_data="fake_key")):
                service = get_apns_service()

                assert service is not None
                assert service.apns_server == "https://api.push.apple.com"
