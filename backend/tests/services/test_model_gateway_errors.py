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
        err = ModelGatewayAPIError(
            provider="gemini", status_code=404, message="missing"
        )
        assert err.status_code == 404
        with pytest.raises(ModelGatewayAPIError, match="missing"):
            raise err

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


class TestExtractUpstreamErrorDetail:
    """Tests for lifting the provider's own message out of litellm blobs."""

    def test_lifts_inner_json_message_and_hint(self):
        from preloop.services.model_gateway_errors import (
            extract_upstream_error_detail,
        )

        raw = (
            "litellm.NotFoundError: litellm.NotFoundError: OpenrouterException - "
            '{"error":{"message":"No allowed providers are available for the '
            'selected model.","code":404,"metadata":{"requested_providers":'
            '["alibaba"],"available_providers":["openai","groq"]}},'
            '"user_id":"user_x"}'
        )
        detail = extract_upstream_error_detail(raw)
        assert detail.message == (
            "No allowed providers are available for the selected model."
        )
        assert detail.provider_detail is not None
        assert "openrouter" in detail.provider_detail
        assert "requested_providers: alibaba" in detail.provider_detail
        assert "available_providers" not in detail.provider_detail

    def test_falls_back_to_cleaned_text_without_json(self):
        from preloop.services.model_gateway_errors import (
            extract_upstream_error_detail,
        )

        detail = extract_upstream_error_detail(
            "litellm.BadRequestError: OpenAIException - invalid request"
        )
        assert detail.message == "invalid request"
        assert detail.provider_detail == "openai"

    def test_plain_text_untouched(self):
        from preloop.services.model_gateway_errors import (
            extract_upstream_error_detail,
        )

        detail = extract_upstream_error_detail("model not found")
        assert detail.message == "model not found"
        assert detail.provider_detail is None

    def test_scrubs_secrets(self):
        from preloop.services.model_gateway_errors import (
            extract_upstream_error_detail,
        )

        detail = extract_upstream_error_detail(
            "call to https://u:tok123abcdef@api.example.com failed for "
            "sk-abcdefghijklmnopqrstuvwxyz"
        )
        assert "tok123abcdef" not in detail.message
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in detail.message


class TestProviderDetailPayload:
    """provider_detail rendering across the three wire shapes."""

    def test_openai_payload_includes_provider_detail_when_set(self):
        err = ModelGatewayAPIError(
            provider="openai",
            status_code=404,
            message="No allowed providers are available for the selected model.",
            provider_detail="openrouter (requested_providers: alibaba)",
        )
        payload = err.to_payload()
        assert payload["error"]["provider_detail"] == (
            "openrouter (requested_providers: alibaba)"
        )

    def test_openai_payload_omits_provider_detail_when_unset(self):
        err = ModelGatewayAPIError(provider="openai", status_code=404, message="x")
        assert "provider_detail" not in err.to_payload()["error"]

    def test_anthropic_and_gemini_shapes_stay_provider_native(self):
        for provider in ("anthropic", "gemini"):
            err = ModelGatewayAPIError(
                provider=provider,
                status_code=404,
                message="x",
                provider_detail="openrouter",
            )
            payload = err.to_payload()
            assert "provider_detail" not in payload["error"]
