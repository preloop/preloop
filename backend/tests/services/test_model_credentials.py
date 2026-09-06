"""Tests for auxiliary model credential resolution and fallback orchestration."""

import pytest
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from preloop.services.model_credentials import (
    resolve_model_call_credentials,
    call_with_default_model_fallback,
    get_aux_fallback_daily_cap,
    _fallback_counters,
    _fallback_warnings,
    DEFAULT_AUX_FALLBACK_DAILY_CAP,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_model():
    """Create a mock AIModel with typical attributes."""
    model = SimpleNamespace(
        id="model-1",
        model_identifier="gpt-4o-mini",
        provider_name="openai",
        api_endpoint=None,
    )
    return model


@pytest.fixture(autouse=True)
def reset_counters():
    """Reset fallback counters and warnings between tests."""
    _fallback_counters.clear()
    _fallback_warnings.clear()
    yield
    _fallback_counters.clear()
    _fallback_warnings.clear()


class TestResolveModelCallCredentials:
    """Tests for credential resolution helper."""

    def test_resolves_api_key_credential(self, mock_model, mock_db):
        """Model with api_key credential type returns api_key in kwargs."""
        resolved = SimpleNamespace(
            credential_type="api_key",
            backend_type="vault",
            value="sk-test-123",
        )
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = (
                resolved
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert result == {"api_key": "sk-test-123"}

    def test_resolves_api_key_with_custom_endpoint(self, mock_model, mock_db):
        """Model with api_key and custom endpoint returns both."""
        mock_model.api_endpoint = "https://custom.endpoint.com"
        resolved = SimpleNamespace(
            credential_type="api_key",
            backend_type="vault",
            value="sk-test-123",
        )
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = (
                resolved
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert result == {
            "api_key": "sk-test-123",
            "api_base": "https://custom.endpoint.com",
        }

    def test_oauth_credential_does_not_inject_api_key(self, mock_model, mock_db):
        """OAuth credential type does not inject api_key into kwargs."""
        resolved = SimpleNamespace(
            credential_type="openai_codex_oauth",
            backend_type="vault",
            value=None,
            payload={"access_token": "oauth-token"},
        )
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = (
                resolved
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        # OAuth credentials don't inject api_key
        assert "api_key" not in result
        assert result == {}

    def test_ambient_credential_does_not_inject_api_key(self, mock_model, mock_db):
        """Ambient credential type does not inject api_key into kwargs."""
        resolved = SimpleNamespace(
            credential_type="ambient",
            backend_type="vault",
            value=None,
        )
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = (
                resolved
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert "api_key" not in result
        assert result == {}

    def test_no_credentials_returns_empty_dict(self, mock_model, mock_db):
        """Model with no credentials returns empty dict."""
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = None

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert result == {}

    def test_resolution_failure_returns_empty_dict(self, mock_model, mock_db):
        """Resolution exception is caught and returns empty dict."""
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.side_effect = (
                RuntimeError("Vault unavailable")
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert result == {}

    def test_api_key_without_value_not_injected(self, mock_model, mock_db):
        """API key credential with no value does not inject api_key."""
        resolved = SimpleNamespace(
            credential_type="api_key",
            backend_type="vault",
            value=None,
        )
        with patch(
            "preloop.services.model_credentials.get_secret_service"
        ) as mock_service:
            mock_service.return_value.resolve_ai_model_credentials.return_value = (
                resolved
            )

            result = resolve_model_call_credentials(mock_model, db=mock_db)

        assert "api_key" not in result


class TestCallWithDefaultModelFallback:
    """Tests for fallback orchestration."""

    async def test_primary_model_success_no_fallback(self, mock_model, mock_db):
        """Primary model succeeds, no fallback attempted."""
        caller = MagicMock(return_value="success")

        result = await call_with_default_model_fallback(
            db=mock_db,
            account_id="acct-1",
            primary_model=mock_model,
            caller=caller,
            operation_name="test_op",
        )

        assert result == "success"
        # Caller should be invoked with primary model
        caller.assert_called_once()
        args = caller.call_args[0]
        assert args[0] == mock_model  # first arg is the model
        assert isinstance(args[1], dict)  # second arg is creds_kwargs

    async def test_primary_fails_fallback_succeeds(self, mock_model, mock_db):
        """Primary model fails, fallback to system default succeeds."""
        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        def caller_side_effect(model, creds):
            if model.id == "model-1":
                raise RuntimeError("Primary failed")
            return "fallback_success"

        caller = MagicMock(side_effect=caller_side_effect)

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result == "fallback_success"
        assert caller.call_count == 2

    async def test_no_system_default_returns_none(self, mock_model, mock_db):
        """No system default configured, returns None after primary failure."""
        caller = MagicMock(side_effect=RuntimeError("Primary failed"))

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=None,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result is None
        caller.assert_called_once()

    async def test_fallback_also_fails_returns_none(self, mock_model, mock_db):
        """Both primary and fallback fail, returns None."""
        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        caller = MagicMock(side_effect=RuntimeError("Always fails"))

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result is None
        assert caller.call_count == 2

    async def test_system_default_same_as_primary_no_retry(self, mock_model, mock_db):
        """System default is same model as failed primary, no retry."""
        # System default has the same ID as primary
        system_default = mock_model

        caller = MagicMock(side_effect=RuntimeError("Primary failed"))

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result is None
        # Only one call (primary), no fallback
        caller.assert_called_once()

    async def test_daily_cap_enforced(self, mock_model, mock_db):
        """Fallback cap exceeded, no fallback attempt."""
        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        # Pre-populate the counter to exceed the cap
        today = datetime.now(UTC).date()
        cap_key = ("acct-1", today)
        _fallback_counters[cap_key] = DEFAULT_AUX_FALLBACK_DAILY_CAP

        caller = MagicMock(side_effect=RuntimeError("Primary failed"))

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result is None
        # Only one call (primary), fallback skipped due to cap
        caller.assert_called_once()

    async def test_fallback_increments_counter(self, mock_model, mock_db):
        """Fallback attempt increments the daily counter."""
        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        def caller_side_effect(model, creds):
            if model.id == "model-1":
                raise RuntimeError("Primary failed")
            return "fallback_success"

        caller = MagicMock(side_effect=caller_side_effect)

        today = datetime.now(UTC).date()
        cap_key = ("acct-1", today)
        assert _fallback_counters[cap_key] == 0

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert _fallback_counters[cap_key] == 1

    async def test_deduped_warning_per_account(self, mock_model, mock_db):
        """Warning is deduped per account per day."""
        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        def caller_side_effect(model, creds):
            if model.id == "model-1":
                raise RuntimeError("Primary failed")
            return "fallback_success"

        caller = MagicMock(side_effect=caller_side_effect)

        today = datetime.now(UTC).date()
        cap_key = ("acct-1", today)

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            # First fallback
            await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

            assert _fallback_warnings[cap_key] is True

            # Second fallback for same account/day
            # (would normally log, but we can't easily assert on log calls here)
            await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-1",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

            # Still marked as warned
            assert _fallback_warnings[cap_key] is True


class TestGatewayNoFallback:
    """Ensure gateway path does not use fallback helpers."""

    def test_gateway_module_does_not_reference_fallback(self):
        """The gateway module never imports or calls the aux fallback helpers.

        Imports the real module under its canonical name (preloop.*), not a
        second copy under backend.preloop.*, so the assertion is meaningful.
        """
        import preloop.services.openai_gateway as gateway_module

        assert not hasattr(gateway_module, "call_with_default_model_fallback")
        assert not hasattr(gateway_module, "resolve_model_call_credentials")

    def test_gateway_source_has_no_fallback_call(self):
        """Source-level guard: gateway traffic must never fall back.

        A name-based check alone would pass if someone imported the module and
        called it via an attribute, so assert on the source text too.
        """
        import inspect

        import preloop.services.openai_gateway as gateway_module

        source = inspect.getsource(gateway_module)
        assert "call_with_default_model_fallback" not in source
        # Match the import specifically. A bare "model_credentials" substring
        # would also match the gateway's own resolve_ai_model_credentials call.
        assert "services.model_credentials" not in source
        assert "from preloop.services import model_credentials" not in source

    @pytest.mark.asyncio
    async def test_gateway_credential_resolution_is_untouched(self):
        """The gateway still resolves credentials via the secret service directly."""
        import inspect

        import preloop.services.openai_gateway as gateway_module

        source = inspect.getsource(gateway_module)
        # The gateway's own resolution path must remain in place.
        assert "resolve_ai_model_credentials" in source


class TestCapConfiguration:
    """The daily cap is read at call time, not frozen at import."""

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("PRELOOP_AUX_FALLBACK_DAILY_CAP", raising=False)
        assert get_aux_fallback_daily_cap() == DEFAULT_AUX_FALLBACK_DAILY_CAP

    def test_env_override_is_picked_up(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_AUX_FALLBACK_DAILY_CAP", "7")
        assert get_aux_fallback_daily_cap() == 7

    def test_zero_cap_disables_fallback(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_AUX_FALLBACK_DAILY_CAP", "0")
        assert get_aux_fallback_daily_cap() == 0

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_AUX_FALLBACK_DAILY_CAP", "not-a-number")
        assert get_aux_fallback_daily_cap() == DEFAULT_AUX_FALLBACK_DAILY_CAP


class TestCapEnvEnforcement:
    """A cap of zero must block the fallback entirely."""

    async def test_zero_cap_blocks_fallback(self, mock_model, mock_db, monkeypatch):
        monkeypatch.setenv("PRELOOP_AUX_FALLBACK_DAILY_CAP", "0")
        _fallback_counters.clear()
        _fallback_warnings.clear()

        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )
        caller = MagicMock(side_effect=RuntimeError("Primary failed"))

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-zero-cap",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result is None
        # Primary attempted once; fallback never attempted.
        caller.assert_called_once()


class TestAttemptTimeout:
    """Attempt deadlines trigger fallback after shared-session work drains."""

    async def test_primary_timeout_triggers_fallback(self, mock_model, mock_db):
        _fallback_counters.clear()
        _fallback_warnings.clear()

        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        def caller(model, creds):
            if model.id == mock_model.id:
                # Simulate a hanging primary call.
                import time

                time.sleep(2)
                return "primary"
            return "fallback-summary"

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-timeout",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
                attempt_timeout=0.1,
            )

        assert result == "fallback-summary"

    async def test_credentials_resolved_off_the_event_loop(self, mock_model, mock_db):
        """Credential resolution must happen in the worker thread."""
        _fallback_counters.clear()
        _fallback_warnings.clear()

        import threading

        main_thread_id = threading.get_ident()
        resolution_threads = []

        def fake_resolve(model, *, db=None):
            resolution_threads.append(threading.get_ident())
            return {"api_key": "sk-test"}

        def caller(model, creds):
            return "ok"

        with patch(
            "preloop.services.model_credentials.resolve_model_call_credentials",
            side_effect=fake_resolve,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-thread",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result == "ok"
        assert resolution_threads, "credential resolution never ran"
        assert all(tid != main_thread_id for tid in resolution_threads)


class TestCounterPruning:
    """Per-day maps must not grow without bound."""

    async def test_previous_day_entries_are_pruned(self, mock_model, mock_db):
        from datetime import timedelta

        _fallback_counters.clear()
        _fallback_warnings.clear()

        today = datetime.now(UTC).date()
        stale_key = ("acct-old", today - timedelta(days=3))
        _fallback_counters[stale_key] = 12
        _fallback_warnings[stale_key] = True

        system_default = SimpleNamespace(
            id="system-model",
            model_identifier="gpt-3.5-turbo",
            provider_name="openai",
            api_endpoint=None,
        )

        def caller(model, creds):
            if model.id == mock_model.id:
                raise RuntimeError("primary failed")
            return "fallback-summary"

        with patch(
            "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
            return_value=system_default,
        ):
            result = await call_with_default_model_fallback(
                db=mock_db,
                account_id="acct-prune",
                primary_model=mock_model,
                caller=caller,
                operation_name="test_op",
            )

        assert result == "fallback-summary"
        assert stale_key not in _fallback_counters
        assert stale_key not in _fallback_warnings
        assert ("acct-prune", today) in _fallback_counters
