"""Tests for models.sentry init_sentry."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.sentry import init_sentry


class TestInitSentry:
    """Test init_sentry function."""

    def test_no_dsn_does_nothing(self):
        """When SENTRY_DSN is not set, init_sentry does nothing."""
        with patch.dict(os.environ, {"SENTRY_DSN": ""}, clear=False):
            if "SENTRY_DSN" in os.environ and os.environ["SENTRY_DSN"]:
                pass
            orig = os.environ.get("SENTRY_DSN")
            try:
                os.environ.pop("SENTRY_DSN", None)
                init_sentry()
            finally:
                if orig is not None:
                    os.environ["SENTRY_DSN"] = orig

    def test_with_dsn_inits_sentry_production(self):
        """When SENTRY_DSN and preloop.ai URL, environment is production."""
        mock_sdk = MagicMock()
        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
                with patch("preloop.config.settings") as mock_settings:
                    mock_settings.preloop_url = "https://preloop.ai"
                    init_sentry()
                    mock_sdk.init.assert_called_once()
                    call_kw = mock_sdk.init.call_args[1]
                    assert call_kw["dsn"] == "https://key@sentry.io/1"
                    assert call_kw["environment"] == "production"
                    assert call_kw["before_send"] is not None
                    assert call_kw["profiles_sample_rate"] == 0.0
                    assert call_kw["traces_sample_rate"] == 1.0

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("0", 0.0),
            ("0.2", 0.2),
            ("1", 1.0),
            ("bad", 0.0),
            ("-1", 0.0),
            ("1.1", 0.0),
            ("nan", 0.0),
            ("inf", 0.0),
        ],
    )
    def test_profiling_requires_valid_explicit_opt_in(
        self, monkeypatch: pytest.MonkeyPatch, configured: str, expected: float
    ) -> None:
        monkeypatch.setenv("SENTRY_DSN", "https://key@example.com/1")
        monkeypatch.setenv("SENTRY_PROFILES_SAMPLE_RATE", configured)
        sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": sdk}):
            with patch("preloop.config.settings") as settings:
                settings.preloop_url = "https://preloop.ai"
                init_sentry()
        assert sdk.init.call_args.kwargs["profiles_sample_rate"] == expected
        assert sdk.init.call_args.kwargs["before_send"] is not None

    def test_staging_env_detected(self):
        """Staging URL sets environment to staging."""
        mock_sdk = MagicMock()
        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
                with patch("preloop.config.settings") as mock_settings:
                    mock_settings.preloop_url = "https://staging.preloop.ai"
                    init_sentry()
                    call_kw = mock_sdk.init.call_args[1]
                    assert call_kw["environment"] == "staging"

    def test_development_env_detected(self):
        """Localhost URL sets environment to development."""
        mock_sdk = MagicMock()
        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
                with patch("preloop.config.settings") as mock_settings:
                    mock_settings.preloop_url = "http://localhost:8000"
                    init_sentry()
                    call_kw = mock_sdk.init.call_args[1]
                    assert call_kw["environment"] == "development"
