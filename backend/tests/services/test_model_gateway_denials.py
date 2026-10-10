"""Unit tests for the shared 429 budget denial contract (#1447)."""

from __future__ import annotations

import pytest

from preloop.services.gateway_upstream_identity import (
    is_budget_denial as reexported_is_budget_denial,
)
from preloop.services.model_gateway_denials import (
    BudgetDenialError,
    budget_denial_error,
    is_budget_denial,
    reraise_as_budget_denial,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError

MESSAGE = "Model gateway budget exceeded: account monthly limit reached"


def test_anthropic_shape():
    exc = budget_denial_error("anthropic", None, MESSAGE, 120)
    assert exc.status_code == 429
    assert exc.to_payload() == {
        "type": "error",
        "error": {"type": "billing_error", "message": MESSAGE},
    }
    assert exc.response_headers() == {"retry-after": "120", "x-should-retry": "false"}


def test_openai_shape_keeps_machine_code():
    exc = budget_denial_error("openai", "execution_budget_exceeded", MESSAGE, 5)
    assert exc.status_code == 429
    assert exc.to_payload() == {
        "error": {
            "message": MESSAGE,
            "type": "insufficient_quota",
            "param": None,
            "code": "insufficient_quota",
            "preloop_code": "execution_budget_exceeded",
        }
    }
    assert exc.code == "execution_budget_exceeded"


def test_gemini_shape():
    exc = budget_denial_error("gemini", "budget_limit_exceeded", MESSAGE, None)
    assert exc.to_payload() == {
        "error": {"code": 429, "message": MESSAGE, "status": "RESOURCE_EXHAUSTED"}
    }


@pytest.mark.parametrize("reset", [None, 0, -5])
def test_unknown_reset_defaults_to_one_hour(reset):
    exc = budget_denial_error("openai", None, MESSAGE, reset)
    assert exc.response_headers()["retry-after"] == "3600"
    assert exc.preloop_code == "budget_limit_exceeded"


def test_extra_headers_cannot_override_should_retry():
    exc = budget_denial_error("anthropic", None, MESSAGE, 10)
    exc.extra_response_headers = {"x-should-retry": "true", "x-request-id": "r1"}
    headers = exc.response_headers()
    assert headers["x-should-retry"] == "false"
    assert headers["x-request-id"] == "r1"


@pytest.mark.parametrize(
    ("status", "code", "message", "expected"),
    [
        (403, "budget_limit_exceeded", "x", True),
        (403, None, MESSAGE, True),
        (403, None, "Preloop trial limit for hosted model reached.", True),
        (429, "execution_budget_exceeded", "x", True),
        (429, None, "slow down", False),
        (429, None, MESSAGE, False),
        (403, "model_not_allowed", "Model not allowed", False),
        (401, "budget_limit_exceeded", "x", False),
    ],
)
def test_is_budget_denial_accepts_legacy_and_new_shapes(
    status, code, message, expected
):
    exc = ModelGatewayAPIError(
        provider="openai", status_code=status, message=message, code=code
    )
    assert is_budget_denial(exc) is expected
    assert reexported_is_budget_denial(exc) is expected


def test_is_budget_denial_for_helper_output():
    assert is_budget_denial(budget_denial_error("gemini", None, "anything", 1))


def test_reraise_converts_legacy_403():
    legacy = ModelGatewayAPIError(
        provider="anthropic",
        status_code=403,
        message=MESSAGE,
        code="budget_limit_exceeded",
    )
    legacy.budget_reset_seconds = 42
    converted = reraise_as_budget_denial(legacy, "openai")
    assert isinstance(converted, BudgetDenialError)
    assert converted.provider == "openai"
    assert converted.status_code == 429
    assert converted.response_headers()["retry-after"] == "42"


def test_reraise_leaves_policy_denials():
    policy = ModelGatewayAPIError(
        provider="openai", status_code=403, message="no", code="model_not_allowed"
    )
    assert reraise_as_budget_denial(policy, "openai") is policy
