"""Tests for provider-native model gateway error envelopes."""

import pytest

from preloop.services.model_gateway_errors import (
    ModelGatewayAPIError,
    _default_error_type,
)


class TestDefaultErrorType:
    """Tests for the _default_error_type provider/status mapping."""

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (400, "INVALID_ARGUMENT"),
            (401, "UNAUTHENTICATED"),
            (403, "PERMISSION_DENIED"),
            (404, "NOT_FOUND"),
            (429, "RESOURCE_EXHAUSTED"),
            (500, "INTERNAL"),
            (503, "INTERNAL"),
            (418, "UNKNOWN"),
        ],
    )
    def test_gemini_mapping(self, status_code, expected):
        assert _default_error_type("gemini", status_code) == expected

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (400, "invalid_request_error"),
            (401, "authentication_error"),
            (403, "permission_error"),
            (404, "not_found_error"),
            (429, "rate_limit_error"),
            (529, "overloaded_error"),
            (500, "api_error"),
        ],
    )
    def test_anthropic_mapping(self, status_code, expected):
        assert _default_error_type("anthropic", status_code) == expected

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (400, "invalid_request_error"),
            (401, "authentication_error"),
            (403, "permission_error"),
            (404, "not_found_error"),
            (429, "rate_limit_error"),
            (500, "api_error"),
            # OpenAI has no special 529 mapping -> falls through to api_error
            (529, "api_error"),
        ],
    )
    def test_openai_mapping(self, status_code, expected):
        assert _default_error_type("openai", status_code) == expected


class TestModelGatewayAPIError:
    """Tests for the ModelGatewayAPIError dataclass exception."""

    def test_is_exception_with_message(self):
        err = ModelGatewayAPIError(provider="openai", status_code=400, message="boom")
        assert isinstance(err, Exception)
        assert str(err) == "boom"

    def test_default_error_type_populated(self):
        err = ModelGatewayAPIError(
            provider="anthropic", status_code=429, message="slow down"
        )
        assert err.error_type == "rate_limit_error"

    def test_explicit_error_type_preserved(self):
        err = ModelGatewayAPIError(
            provider="openai",
            status_code=400,
            message="bad",
            error_type="custom_type",
        )
        assert err.error_type == "custom_type"

    def test_raisable(self):
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            raise ModelGatewayAPIError(
                provider="gemini", status_code=404, message="missing"
            )
        assert exc_info.value.status_code == 404

    def test_to_payload_gemini(self):
        err = ModelGatewayAPIError(provider="gemini", status_code=403, message="nope")
        payload = err.to_payload()
        assert payload == {
            "error": {
                "code": 403,
                "message": "nope",
                "status": "PERMISSION_DENIED",
            }
        }

    def test_to_payload_anthropic(self):
        err = ModelGatewayAPIError(
            provider="anthropic", status_code=401, message="who?"
        )
        payload = err.to_payload()
        assert payload == {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "who?",
            },
        }

    def test_to_payload_openai_includes_param_and_code(self):
        err = ModelGatewayAPIError(
            provider="openai",
            status_code=400,
            message="bad param",
            param="model",
            code="invalid_model",
        )
        payload = err.to_payload()
        assert payload == {
            "error": {
                "message": "bad param",
                "type": "invalid_request_error",
                "param": "model",
                "code": "invalid_model",
            }
        }

    def test_to_payload_openai_defaults_param_code_none(self):
        err = ModelGatewayAPIError(provider="openai", status_code=500, message="server")
        payload = err.to_payload()
        assert payload["error"]["param"] is None
        assert payload["error"]["code"] is None
        assert payload["error"]["type"] == "api_error"
