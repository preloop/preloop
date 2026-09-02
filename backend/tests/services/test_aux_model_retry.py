"""Tests for bounded retry of aux (non-gateway) model calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.aux_model_retry import (
    AUX_RETRY_AFTER_CAP_SECONDS,
    call_with_aux_retry,
)
from preloop.services.model_credentials import (
    _fallback_counters,
    call_with_default_model_fallback,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.upstream_errors import ERROR_CLASS_UPSTREAM_RATE_LIMITED


class _RateLimitError(Exception):
    """Name-matched stand-in for litellm RateLimitError."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        retry_after: Optional[int] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        if retry_after is not None:
            self.response = SimpleNamespace(headers={"retry-after": str(retry_after)})


class _APIConnectionError(Exception):
    """Name-matched stand-in for litellm.exceptions.APIConnectionError."""


class _AuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _OverloadError(Exception):
    def __init__(self, message: str, *, status_code: int = 529) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _ClientError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def test_transient_429_is_retried_once_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimitError("Error code: 429 - Too Many Requests")
        return "ok"

    slept = []
    assert call_with_aux_retry(fn, sleep=slept.append) == "ok"
    assert calls["n"] == 2
    assert slept == [0.5]


def test_retry_after_hint_raises_backoff():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimitError("Error code: 429 - Too Many Requests", retry_after=2)
        return "ok"

    slept = []
    assert call_with_aux_retry(fn, sleep=slept.append) == "ok"
    assert slept == [2.0]


def test_retry_after_is_capped_for_aux_deadlines():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimitError("Error code: 429 - Too Many Requests", retry_after=30)
        return "ok"

    slept = []
    call_with_aux_retry(fn, sleep=slept.append)
    assert slept == [AUX_RETRY_AFTER_CAP_SECONDS]


def test_quota_exhausted_fails_fast():
    fn = MagicMock(
        side_effect=_RateLimitError(
            "You exceeded your current quota",
            code="insufficient_quota",
        )
    )
    slept = []
    with pytest.raises(_RateLimitError):
        call_with_aux_retry(fn, sleep=slept.append)
    fn.assert_called_once()
    assert slept == []


def test_auth_failure_fails_fast():
    fn = MagicMock(side_effect=_AuthError("Invalid API key"))
    slept = []
    with pytest.raises(_AuthError):
        call_with_aux_retry(fn, sleep=slept.append)
    fn.assert_called_once()
    assert slept == []


def test_network_fault_is_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _APIConnectionError("Connection refused")
        return "ok"

    assert call_with_aux_retry(fn, sleep=lambda _: None) == "ok"
    assert calls["n"] == 2


def test_overload_is_retried():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _OverloadError("overloaded_error: engine is overloaded")
        return "ok"

    assert call_with_aux_retry(fn, sleep=lambda _: None) == "ok"
    assert calls["n"] == 2


def test_exhausted_transient_surfaces_classified_gateway_error():
    exc = _RateLimitError("Error code: 429 - Too Many Requests")
    fn = MagicMock(side_effect=exc)
    with pytest.raises(ModelGatewayAPIError) as caught:
        call_with_aux_retry(fn, sleep=lambda _: None)
    err = caught.value
    assert err.error_class == ERROR_CLASS_UPSTREAM_RATE_LIMITED
    assert err.__cause__ is exc
    assert fn.call_count == 2


def test_client_shaped_errors_are_not_retried():
    fn = MagicMock(side_effect=_ClientError("invalid_request: bad param"))
    slept = []
    with pytest.raises(_ClientError):
        call_with_aux_retry(fn, sleep=slept.append)
    fn.assert_called_once()
    assert slept == []


@pytest.fixture
def mock_model():
    return SimpleNamespace(
        id="model-1",
        model_identifier="gpt-4o-mini",
        provider_name="openai",
        api_endpoint=None,
    )


@pytest.fixture(autouse=True)
def reset_fallback_counters():
    _fallback_counters.clear()
    yield
    _fallback_counters.clear()


@pytest.mark.asyncio
async def test_transient_429_does_not_burn_aux_fallback(mock_model):
    """A retried primary 429 must not consume the one-shot fallback budget."""
    calls = {"n": 0}

    def caller(model, creds):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _RateLimitError("Error code: 429 - Too Many Requests")
        return "ok"

    result = await call_with_default_model_fallback(
        db=MagicMock(),
        account_id="acct-1",
        primary_model=mock_model,
        caller=caller,
        operation_name="approval_summary",
    )
    assert result == "ok"
    assert calls["n"] == 2
    assert _fallback_counters == {}


@pytest.mark.asyncio
async def test_exhausted_429_still_attempts_fallback(mock_model):
    system_default = SimpleNamespace(
        id="system-model",
        model_identifier="gpt-3.5-turbo",
        provider_name="openai",
        api_endpoint=None,
    )

    def caller(model, creds):
        if model.id == "model-1":
            raise _RateLimitError("Error code: 429 - Too Many Requests")
        return "fallback_ok"

    with patch(
        "preloop.services.model_credentials.crud_ai_model.get_default_active_model",
        return_value=system_default,
    ):
        result = await call_with_default_model_fallback(
            db=MagicMock(),
            account_id="acct-1",
            primary_model=mock_model,
            caller=caller,
            operation_name="approval_summary",
        )

    assert result == "fallback_ok"
    assert any(
        key[0] == "acct-1" and count >= 1 for key, count in _fallback_counters.items()
    )


def test_policy_generation_retries_litellm_completion():
    from preloop.services.policy_generation import PolicyGenerationService

    service = PolicyGenerationService(db=MagicMock(), account_id="acct-1")
    model = SimpleNamespace(
        id="model-1",
        model_identifier="gpt-4o-mini",
        provider_name="openai",
        api_endpoint=None,
        kind="llm",
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="key: value\n"))]
    )
    completion = MagicMock(
        side_effect=[
            _RateLimitError("Error code: 429 - Too Many Requests"),
            response,
        ]
    )

    with (
        patch(
            "preloop.services.policy_generation.resolve_model_call_credentials",
            return_value={"api_key": "sk-test"},
        ),
        patch(
            "preloop.services.policy_generation.build_aux_kwargs",
            side_effect=lambda model, creds, call_site_kwargs: call_site_kwargs,
        ),
        patch(
            "preloop.services.policy_generation.to_litellm_model",
            return_value="openai/gpt-4o-mini",
        ),
        patch("preloop.services.policy_generation.check_reasoning_model_empty_content"),
        patch("preloop.services.policy_generation.litellm.completion", completion),
        patch.object(service, "_validate_output"),
        patch.object(service, "_extract_yaml", return_value="key: value\n"),
    ):
        assert service._call_llm(model, "sys", "user") == "key: value\n"

    assert completion.call_count == 2


def test_extract_agent_name_uses_aux_retry():
    from preloop.services.aux_model_retry import call_with_aux_retry as retry_fn

    assert retry_fn.__module__ == "preloop.services.aux_model_retry"
    import preloop.api.endpoints.account as account_mod

    assert account_mod.call_with_aux_retry is retry_fn
