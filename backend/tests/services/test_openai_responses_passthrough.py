"""Native OpenAI Responses passthrough (issue #159).

``POST /openai/v1/responses`` used to have exactly one non-Codex
implementation: flatten the Responses payload into chat messages and send it
to the upstream's **chat completions** endpoint. A request that entered
Preloop through the Responses API therefore left Preloop through a different
API, which is lossy (``instructions``, ``reasoning``, ``include``, ``store``
and typed tool items have no chat-completions representation) and, for an
upstream that implements Responses but not chat completions, impossible.

The reported case is OpenCode Zen (``https://opencode.ai/zen/v1``): ``POST
/responses`` answers 200 and ``POST /chat/completions`` answers 500, so every
Responses request through Preloop came back 502 ``InternalServerError -
Internal server error``.

These tests pin both halves of the fix:

* the routing decision (which upstreams get a native call, and how an
  upstream with no ``/responses`` endpoint is detected once and remembered),
  which lives in :mod:`preloop.services.openai_responses_passthrough`;
* the gateway behaviour: verbatim forward, verbatim SSE relay, usage
  accounting on both, governance tool-stripping still applied, upstream
  errors classified, and a clean fall back to the transcode.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.models.crud import crud_ai_model, crud_api_key
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.openai_responses_passthrough import (
    CAPABILITY_CACHE_TTL_SECONDS,
    DEFAULT_OPENAI_BASE_URL,
    PASSTHROUGH_MODE_AUTO,
    PASSTHROUGH_MODE_NATIVE,
    PASSTHROUGH_MODE_TRANSCODE,
    build_passthrough_body,
    capability_cache_key,
    is_openai_shaped_upstream,
    is_responses_api_absent,
    mark_responses_api_absent,
    reset_capability_cache,
    responses_passthrough_mode,
    responses_passthrough_url,
    responses_tool_choice_named_tool,
    should_use_responses_passthrough,
)

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_MODEL_ID = "muse-spark-1.3-contributor-free"


@pytest.fixture(autouse=True)
def _clean_capability_cache():
    """The negative probe cache is process-level; never leak it across tests."""
    reset_capability_cache()
    yield
    reset_capability_cache()


# ----------------------------------------------------------------------
# Routing rules
# ----------------------------------------------------------------------
def _model(**kwargs):
    """A minimal stand-in for an AIModel row."""
    defaults = {
        "provider_name": "openai-compatible",
        "model_identifier": ZEN_MODEL_ID,
        "api_endpoint": ZEN_BASE_URL,
        "meta_data": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestRoutingMode:
    """``meta_data.gateway.responses_api`` selects the Responses ingress."""

    def test_default_is_auto(self):
        assert responses_passthrough_mode(_model()) == PASSTHROUGH_MODE_AUTO

    def test_reads_gateway_meta_data(self):
        model = _model(meta_data={"gateway": {"responses_api": "native"}})
        assert responses_passthrough_mode(model) == PASSTHROUGH_MODE_NATIVE

    def test_reads_flat_meta_data_fallback(self):
        model = _model(meta_data={"responses_api": "transcode"})
        assert responses_passthrough_mode(model) == PASSTHROUGH_MODE_TRANSCODE

    def test_value_is_case_and_whitespace_insensitive(self):
        model = _model(meta_data={"gateway": {"responses_api": "  Native "}})
        assert responses_passthrough_mode(model) == PASSTHROUGH_MODE_NATIVE

    def test_unknown_value_degrades_to_auto_not_to_a_pinned_protocol(self):
        """A typo must not silently pin an account's traffic to one protocol."""
        model = _model(meta_data={"gateway": {"responses_api": "yes-please"}})
        assert responses_passthrough_mode(model) == PASSTHROUGH_MODE_AUTO

    def test_non_dict_meta_data_is_tolerated(self):
        assert responses_passthrough_mode(_model(meta_data=None)) == (
            PASSTHROUGH_MODE_AUTO
        )
        assert responses_passthrough_mode(SimpleNamespace()) == PASSTHROUGH_MODE_AUTO


class TestUpstreamShape:
    """Only upstreams LiteLLM would drive as ``openai/<id>`` can go native."""

    def test_openai_byok_is_openai_shaped(self):
        model = _model(
            provider_name="openai", model_identifier="gpt-4o", api_endpoint=None
        )
        assert is_openai_shaped_upstream(model) is True

    def test_openai_compatible_endpoint_is_openai_shaped(self):
        assert is_openai_shaped_upstream(_model()) is True

    def test_anthropic_is_not_openai_shaped(self):
        model = _model(
            provider_name="anthropic",
            model_identifier="claude-sonnet-4-5",
            api_endpoint=None,
        )
        assert is_openai_shaped_upstream(model) is False

    def test_gemini_is_not_openai_shaped(self):
        model = _model(
            provider_name="google",
            model_identifier="gemini-2.5-pro",
            api_endpoint=None,
        )
        assert is_openai_shaped_upstream(model) is False

    def test_openrouter_is_excluded_on_purpose(self):
        """OpenRouter's LiteLLM adapter carries usage/attribution (#219)."""
        model = _model(
            provider_name="openrouter",
            model_identifier="anthropic/claude-sonnet-4",
            api_endpoint="https://openrouter.ai/api/v1",
        )
        assert is_openai_shaped_upstream(model) is False


class TestPassthroughUrl:
    def test_defaults_to_openai_when_no_endpoint_is_configured(self):
        model = _model(api_endpoint=None)
        assert (
            responses_passthrough_url(model) == f"{DEFAULT_OPENAI_BASE_URL}/responses"
        )

    def test_appends_responses_to_a_configured_base_url(self):
        assert responses_passthrough_url(_model()) == f"{ZEN_BASE_URL}/responses"

    def test_tolerates_a_trailing_slash(self):
        model = _model(api_endpoint=f"{ZEN_BASE_URL}/")
        assert responses_passthrough_url(model) == f"{ZEN_BASE_URL}/responses"

    def test_does_not_double_up_an_exact_responses_endpoint(self):
        """An operator who configured the full URL gets what they configured."""
        model = _model(api_endpoint=f"{ZEN_BASE_URL}/responses")
        assert responses_passthrough_url(model) == f"{ZEN_BASE_URL}/responses"

    def test_cache_key_is_the_deployment_not_the_model(self):
        """One probe per upstream: two models on one base URL share a key."""
        assert capability_cache_key(
            _model(model_identifier="a")
        ) == capability_cache_key(_model(model_identifier="b"))


class TestCapabilityCache:
    def test_absent_is_remembered_then_expires(self):
        key = "https://example.test/v1"
        assert is_responses_api_absent(key, now=100.0) is False
        mark_responses_api_absent(key, now=100.0)
        assert is_responses_api_absent(key, now=100.0) is True
        assert (
            is_responses_api_absent(key, now=100.0 + CAPABILITY_CACHE_TTL_SECONDS - 1)
            is True
        )
        # After the TTL the upstream is probed again, so an upstream that ships
        # Responses support is picked up without a restart.
        assert (
            is_responses_api_absent(key, now=100.0 + CAPABILITY_CACHE_TTL_SECONDS + 1)
            is False
        )

    def test_reset_forgets_everything(self):
        mark_responses_api_absent("https://example.test/v1")
        reset_capability_cache()
        assert is_responses_api_absent("https://example.test/v1") is False


class TestShouldUsePassthrough:
    def test_auto_uses_native_for_an_openai_shaped_upstream(self):
        assert should_use_responses_passthrough(_model()) is True

    def test_transcode_mode_pins_the_old_behaviour(self):
        model = _model(meta_data={"gateway": {"responses_api": "transcode"}})
        assert should_use_responses_passthrough(model) is False

    def test_auto_falls_back_once_the_upstream_said_no_such_endpoint(self):
        model = _model()
        mark_responses_api_absent(capability_cache_key(model))
        assert should_use_responses_passthrough(model) is False

    def test_native_mode_skips_the_probe_cache_entirely(self):
        """An explicit ``native`` pin is an operator statement of fact."""
        model = _model(meta_data={"gateway": {"responses_api": "native"}})
        mark_responses_api_absent(capability_cache_key(model))
        assert should_use_responses_passthrough(model) is True

    def test_non_openai_upstreams_never_go_native_even_when_pinned(self):
        model = _model(
            provider_name="anthropic",
            model_identifier="claude-sonnet-4-5",
            api_endpoint=None,
            meta_data={"gateway": {"responses_api": "native"}},
        )
        assert should_use_responses_passthrough(model) is False


class TestResponsesToolChoice:
    """A forced tool_choice must be recognised in its Responses shape."""

    def test_responses_shape_names_the_tool_at_the_top_level(self):
        assert (
            responses_tool_choice_named_tool({"type": "function", "name": "shell"})
            == "shell"
        )

    def test_chat_completions_shape_still_works(self):
        assert (
            responses_tool_choice_named_tool(
                {"type": "function", "function": {"name": "shell"}}
            )
            == "shell"
        )

    def test_custom_tool_shape_is_recognised(self):
        assert (
            responses_tool_choice_named_tool({"type": "custom", "name": "apply_patch"})
            == "apply_patch"
        )

    def test_string_choices_name_no_tool(self):
        for choice in ("auto", "none", "required"):
            assert responses_tool_choice_named_tool(choice) is None

    def test_allowed_tools_container_names_no_single_tool(self):
        assert (
            responses_tool_choice_named_tool(
                {"type": "allowed_tools", "mode": "auto", "tools": []}
            )
            is None
        )


class TestPassthroughBody:
    def test_forwards_every_responses_only_field_the_transcode_dropped(self):
        """These are precisely the fields chat completions has no slot for."""
        payload = {
            "model": "zen-alias",
            "input": [{"role": "user", "content": "hi"}],
            "instructions": "You are terse.",
            "reasoning": {"effort": "high", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "prompt_cache_key": "session-42",
            "text": {"format": {"type": "text"}},
            "truncation": "auto",
            "tools": [{"type": "function", "name": "read_file"}],
        }
        body = build_passthrough_body(_model(), payload, stream=False)
        for field in (
            "instructions",
            "reasoning",
            "include",
            "store",
            "prompt_cache_key",
            "text",
            "truncation",
            "input",
            "tools",
        ):
            assert body[field] == payload[field], f"{field} must survive untouched"

    def test_rewrites_only_the_model_alias(self):
        body = build_passthrough_body(_model(), {"model": "zen-alias"}, stream=False)
        assert body["model"] == ZEN_MODEL_ID

    def test_does_not_mutate_the_caller_payload(self):
        payload = {"model": "zen-alias", "stream": True}
        build_passthrough_body(_model(), payload, stream=False)
        assert payload == {"model": "zen-alias", "stream": True}

    def test_stream_matches_the_ingress_the_client_actually_used(self):
        """A non-streaming ingress must not be answered with an SSE body."""
        assert (
            build_passthrough_body(
                _model(), {"model": "m", "stream": True}, stream=False
            ).get("stream")
            is None
        )
        assert (
            build_passthrough_body(_model(), {"model": "m"}, stream=True)["stream"]
            is True
        )


# ----------------------------------------------------------------------
# Gateway behaviour
# ----------------------------------------------------------------------
def _create_zen_model(db_session, test_user, *, responses_api=None, alias="zen-spark"):
    """A gateway model shaped like the OpenCode Zen row from issue #159."""
    gateway_meta = {
        "enabled": True,
        "model_alias": alias,
        "provider_adapter": "preloop",
    }
    if responses_api is not None:
        gateway_meta["responses_api"] = responses_api
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Zen Spark",
            "provider_name": "openai-compatible",
            "model_identifier": ZEN_MODEL_ID,
            "api_endpoint": ZEN_BASE_URL,
            "api_key": "zen-secret",
            "meta_data": {
                "gateway": gateway_meta,
                "pricing": {
                    "input_price_per_1k": 0.01,
                    "output_price_per_1k": 0.02,
                },
            },
            "is_default": True,
        },
        account_id=test_user.account_id,
    )


def _service(db_session, test_user, api_key=None) -> OpenAIGatewayService:
    return OpenAIGatewayService(
        db_session,
        ModelGatewayAuthContext(token="t", user=test_user, api_key=api_key),
    )


def _api_key_credentials():
    return SimpleNamespace(credential_type="api_key", value="zen-secret")


def _patch_passthrough_client(client: MagicMock):
    """The passthrough uses a process-level client, not module-level httpx."""
    return patch(
        "preloop.services.openai_gateway._openai_passthrough_http_client",
        return_value=client,
    )


def _upstream_responses_object():
    """What a real upstream returns: ``output`` items, and no ``output_text``."""
    return {
        "id": "resp_native_1",
        "object": "response",
        "status": "completed",
        "model": ZEN_MODEL_ID,
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Thinking about it."}],
            },
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello from Zen"}],
            },
        ],
        "usage": {
            "input_tokens": 11,
            "output_tokens": 5,
            "total_tokens": 16,
            "output_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _json_response(status_code: int, payload) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    if isinstance(payload, (dict, list)):
        response.json.return_value = payload
        response.text = json.dumps(payload)
        response.read.return_value = json.dumps(payload).encode("utf-8")
    else:
        response.json.side_effect = ValueError("not json")
        response.text = payload
        response.read.return_value = payload.encode("utf-8")
    return response


def _last_usage_row(db_session) -> ApiUsage:
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.endpoint == "/openai/v1/responses")
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


def test_responses_request_leaves_preloop_as_a_responses_request(db_session, test_user):
    """The reported bug: a Responses request must not become a chat call.

    OpenCode Zen answers ``/chat/completions`` with 500, so the transcode
    could only ever produce the 502 Alex reported. Going native is the fix,
    and this asserts the request really is a native one: the right URL, the
    payload forwarded as sent, and LiteLLM never touched.
    """
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)
    request_payload = {
        "model": "zen-spark",
        "input": [{"role": "user", "content": "Say hello"}],
        "instructions": "Be brief.",
        "reasoning": {"effort": "medium", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "prompt_cache_key": "codex-session-1",
    }

    client = MagicMock()
    client.post.return_value = _json_response(200, _upstream_responses_object())

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        response_payload = service.create_response(dict(request_payload))

    mock_completion.assert_not_called()
    client.post.assert_called_once()
    assert client.post.call_args.args[0] == f"{ZEN_BASE_URL}/responses"

    body = client.post.call_args.kwargs["json"]
    assert body["model"] == ZEN_MODEL_ID
    assert body["input"] == request_payload["input"]
    # Responses-only fields the chat-completions transcode silently dropped.
    assert body["instructions"] == "Be brief."
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["store"] is False
    assert body["prompt_cache_key"] == "codex-session-1"
    assert "stream" not in body

    headers = client.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer zen-secret"

    # The upstream Responses object reaches the client verbatim, reasoning
    # item included: that is the whole point of not translating.
    assert response_payload == _upstream_responses_object()


def test_native_response_is_metered_with_upstream_token_counts(db_session, test_user):
    """Going native must not cost Preloop its accounting."""
    ai_model = _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    client = MagicMock()
    client.post.return_value = _json_response(200, _upstream_responses_object())

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        service.create_response({"model": "zen-spark", "input": "Say hello"})

    row = _last_usage_row(db_session)
    assert row is not None
    assert row.ai_model_id == ai_model.id
    assert row.status_code == 200
    assert row.prompt_tokens == 11
    assert row.completion_tokens == 5
    assert float(row.estimated_cost or 0.0) > 0.0
    assert row.meta_data["upstream_credential_type"] == "api_key"


def test_upstream_without_responses_endpoint_falls_back_to_the_transcode(
    db_session, test_user
):
    """Chat-completions-only upstreams keep working with zero configuration.

    A 404 means "no such endpoint", not "your request was wrong", so the
    request is completed on the transcode instead of failing.
    """
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    client = MagicMock()
    client.post.return_value = _json_response(
        404, {"error": {"message": "Unknown request URL", "type": "invalid_request"}}
    )
    litellm_response = {
        "id": "chatcmpl-1",
        "model": ZEN_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello from the transcode"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=litellm_response,
        ) as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        response_payload = service.create_response(
            {"model": "zen-spark", "input": "Say hello"}
        )

    mock_completion.assert_called_once()
    assert response_payload["output_text"] == "Hello from the transcode"
    row = _last_usage_row(db_session)
    assert row.status_code == 200
    assert row.prompt_tokens == 7


def test_missing_responses_endpoint_is_probed_once_per_upstream(db_session, test_user):
    """The probe costs one round trip per upstream per TTL, and no tokens."""
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    client = MagicMock()
    client.post.return_value = _json_response(404, {"error": {"message": "nope"}})
    litellm_response = {
        "id": "chatcmpl-1",
        "model": ZEN_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=litellm_response,
        ) as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        service.create_response({"model": "zen-spark", "input": "one"})
        service.create_response({"model": "zen-spark", "input": "two"})
        service.create_response({"model": "zen-spark", "input": "three"})

    assert client.post.call_count == 1, "the negative probe must be cached"
    assert mock_completion.call_count == 3


def test_transcode_pin_never_touches_the_native_endpoint(db_session, test_user):
    """``responses_api: transcode`` is the documented escape hatch."""
    _create_zen_model(db_session, test_user, responses_api="transcode")
    service = _service(db_session, test_user)

    client = MagicMock()
    litellm_response = {
        "id": "chatcmpl-1",
        "model": ZEN_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=litellm_response,
        ) as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        service.create_response({"model": "zen-spark", "input": "one"})

    client.post.assert_not_called()
    mock_completion.assert_called_once()


def test_real_upstream_error_is_surfaced_not_retried_through_another_protocol(
    db_session, test_user
):
    """A 400/429 is a real answer; hiding it behind a second protocol lies."""
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    client = MagicMock()
    client.post.return_value = _json_response(
        429,
        {"error": {"type": "rate_limit_exceeded", "message": "slow down please"}},
    )

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_response({"model": "zen-spark", "input": "hi"})

    assert exc_info.value.status_code == 429
    assert exc_info.value.error_type == "rate_limit_exceeded"
    assert "slow down please" in exc_info.value.message
    mock_completion.assert_not_called()
    # A genuine upstream error must not poison the capability cache.
    assert is_responses_api_absent(f"{ZEN_BASE_URL}".lower()) is False


def test_governance_tool_stripping_still_applies_on_the_native_path(
    db_session, test_user
):
    """Tool disabling is a control, so it must survive the protocol change.

    Everything else about the payload stays byte-faithful: only the tools
    array changes, because rewriting message content would break the
    upstream's prompt cache prefix.
    """
    _create_zen_model(db_session, test_user)
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Managed Gateway Key",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    service = _service(db_session, test_user, api_key=api_key)
    governance_meta = {
        "subject_governance": {
            "api_keys": {
                str(api_key.id): {"tool_enabled_overrides": {"shell": False}},
            },
            "managed_agents": {},
        }
    }
    request_payload = {
        "model": "zen-spark",
        "input": [{"role": "user", "content": "list files"}],
        "instructions": "Be careful.",
        "tools": [
            {"type": "function", "name": "shell", "parameters": {"type": "object"}},
            {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
        ],
        "tool_choice": {"type": "function", "name": "shell"},
    }

    client = MagicMock()
    client.post.return_value = _json_response(200, _upstream_responses_object())

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        patch(
            "preloop.services.openai_gateway.get_cached_account_meta_data",
            return_value=governance_meta,
        ),
        _patch_passthrough_client(client),
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        service.create_response(dict(request_payload))

    body = client.post.call_args.kwargs["json"]
    assert [tool["name"] for tool in body["tools"]] == ["read_file"]
    # A forced tool_choice naming a stripped tool would 400 upstream.
    assert body["tool_choice"] == "auto"
    # Nothing else was rewritten.
    assert body["input"] == request_payload["input"]
    assert body["instructions"] == "Be careful."


def test_native_stream_relays_upstream_sse_verbatim(db_session, test_user):
    """Codex gets the upstream's own event sequence, not a re-synthesis.

    The transcode had to invent Responses events from chat-completion deltas,
    which is where typed tool-call items and reasoning summaries were lost.
    """
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    sse_chunks = [
        'event: response.created\ndata: {"type":"response.created","response":'
        '{"id":"resp_stream_1","status":"in_progress"}}\n\n',
        "event: response.reasoning_summary_text.delta\n"
        'data: {"type":"response.reasoning_summary_text.delta","delta":"Hmm"}\n\n',
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n',
        "event: response.output_text.delta\n"
        'data: {"type":"response.output_text.delta","delta":"from Zen"}\n\n',
        'event: response.completed\ndata: {"type":"response.completed","response":'
        '{"id":"resp_stream_1","status":"completed","usage":'
        '{"input_tokens":9,"output_tokens":4,"total_tokens":13}}}\n\n',
    ]

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = {}
    upstream_response.iter_text.return_value = iter(sse_chunks)
    client = MagicMock()
    client.send.return_value = upstream_response

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch("preloop.services.openai_gateway.litellm.completion") as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        emitted = list(
            service.stream_response(
                {"model": "zen-spark", "input": "Say hello", "stream": True}
            )
        )
        service.flush_deferred_stream_record()

    mock_completion.assert_not_called()
    # Byte-faithful relay: concatenation reproduces the upstream stream, and
    # the reasoning event is still in it.
    assert "".join(emitted) == "".join(sse_chunks)
    assert client.send.call_args.kwargs["stream"] is True
    assert client.build_request.call_args.kwargs["json"]["stream"] is True
    upstream_response.close.assert_called_once()
    client.close.assert_not_called()

    row = _last_usage_row(db_session)
    assert row is not None
    assert row.status_code == 200
    assert row.prompt_tokens == 9
    assert row.completion_tokens == 4
    assert row.meta_data["endpoint_kind"] == "responses_stream"


def test_native_stream_upstream_error_surfaces_before_any_bytes(db_session, test_user):
    """Status is checked eagerly so a 401 is a 401, not an empty 200 (#109)."""
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    upstream_response = MagicMock()
    upstream_response.status_code = 401
    upstream_response.headers = {}
    upstream_response.read.return_value = json.dumps(
        {"error": {"type": "invalid_api_key", "message": "bad key"}}
    ).encode("utf-8")
    client = MagicMock()
    client.send.return_value = upstream_response

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.stream_response(
                {"model": "zen-spark", "input": "hi", "stream": True}
            )

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_type == "invalid_api_key"
    upstream_response.close.assert_called_once()
    client.close.assert_not_called()


def test_native_stream_falls_back_when_upstream_has_no_responses_endpoint(
    db_session, test_user
):
    """Streaming gets the same automatic fallback as the unary path."""
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    upstream_response = MagicMock()
    upstream_response.status_code = 405
    upstream_response.headers = {}
    upstream_response.read.return_value = b"Method Not Allowed"
    client = MagicMock()
    client.send.return_value = upstream_response

    def _chunks():
        yield {"choices": [{"delta": {"content": "Hi"}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=_chunks(),
        ) as mock_completion,
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        emitted = list(
            service.stream_response(
                {"model": "zen-spark", "input": "hi", "stream": True}
            )
        )
        service.flush_deferred_stream_record()

    mock_completion.assert_called_once()
    assert any("response.completed" in frame for frame in emitted)
    upstream_response.close.assert_called_once()


def test_invalid_upstream_json_is_a_gateway_error_not_a_crash(db_session, test_user):
    """A 200 with an HTML error page must not surface as a 500."""
    _create_zen_model(db_session, test_user)
    service = _service(db_session, test_user)

    client = MagicMock()
    client.post.return_value = _json_response(200, "<html>502 Bad Gateway</html>")

    with (
        patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets,
        _patch_passthrough_client(client),
    ):
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = (
            _api_key_credentials()
        )
        with pytest.raises(ModelGatewayAPIError) as exc_info:
            service.create_response({"model": "zen-spark", "input": "hi"})

    assert exc_info.value.status_code == 502


def test_upstream_response_object_without_output_text_still_yields_text(
    db_session, test_user
):
    """``output_text`` is an SDK convenience field, not a wire field.

    Response policy and usage recording both need the assistant text, and a
    real upstream Responses object only carries it inside ``output`` items.
    """
    text = OpenAIGatewayService._responses_payload_output_text(
        _upstream_responses_object()
    )
    assert text == "Hello from Zen"
    assert (
        OpenAIGatewayService._responses_payload_output_text(
            {"output_text": "direct", "output": []}
        )
        == "direct"
    )
    assert OpenAIGatewayService._responses_payload_output_text({}) == ""
