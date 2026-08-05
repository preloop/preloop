"""Tests for reasoning-model support in auxiliary generation paths.

Covers:
- kwargs merge precedence (call-site wins over model_parameters)
- empty-content guard triggers fallback for reasoning models
- model_parameters thinking-disable knob produces content
- gateway path remains unaffected
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_credentials import (
    ReasoningModelEmptyContentError,
    build_aux_kwargs,
    check_reasoning_model_empty_content,
    get_aux_openai_sdk_extra_kwargs,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_model(
    *,
    model_id="model-1",
    model_identifier="deepseek/deepseek-v4-flash",
    provider_name="deepseek",
    api_endpoint=None,
    model_parameters=None,
):
    return SimpleNamespace(
        id=model_id,
        model_identifier=model_identifier,
        provider_name=provider_name,
        api_endpoint=api_endpoint,
        model_parameters=model_parameters,
    )


# ---------------------------------------------------------------------------
# 1. kwargs merge precedence: call-site arg beats model_parameters
# ---------------------------------------------------------------------------


class TestBuildAuxKwargsPrecedence:
    """Call-site explicit args MUST win over model_parameters on the same key."""

    def test_call_site_beats_model_parameters(self):
        """max_tokens from call site overrides model_parameters."""
        model = _make_model(model_parameters={"max_tokens": 9999, "temperature": 0.9})
        creds = {"api_key": "sk-test"}
        call_site = {"model": "deepseek/test", "max_tokens": 150, "messages": []}

        result = build_aux_kwargs(model, creds, call_site_kwargs=call_site)

        assert result["max_tokens"] == 150, (
            "call-site max_tokens must beat model_parameters"
        )
        assert result["messages"] == [], "call-site messages must survive"
        assert result["api_key"] == "sk-test", "creds must be present"

    def test_model_parameters_fills_absent_keys(self):
        """Keys in model_parameters not set by the call site are included."""
        model = _make_model(model_parameters={"seed": 42})
        creds = {}
        call_site = {"model": "deepseek/test", "messages": []}

        result = build_aux_kwargs(model, creds, call_site_kwargs=call_site)

        assert result["seed"] == 42

    def test_creds_beat_model_parameters(self):
        """Credential kwargs override model_parameters."""
        model = _make_model(model_parameters={"api_key": "from-model-row"})
        creds = {"api_key": "from-vault"}
        call_site = {"model": "test"}

        result = build_aux_kwargs(model, creds, call_site_kwargs=call_site)

        assert result["api_key"] == "from-vault"

    def test_call_site_beats_creds(self):
        """Call-site kwargs override credential kwargs too."""
        model = _make_model()
        creds = {"api_base": "https://wrong.example.com"}
        call_site = {"model": "test", "api_base": "https://correct.example.com"}

        result = build_aux_kwargs(model, creds, call_site_kwargs=call_site)

        assert result["api_base"] == "https://correct.example.com"

    def test_deepseek_gets_thinking_disabled_default(self):
        """DeepSeek models get extra_body thinking disabled, not reasoning_effort."""
        model = _make_model()
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert "reasoning_effort" not in result
        assert result.get("extra_body") == {"thinking": {"type": "disabled"}}

    def test_openai_non_reasoning_gets_no_reasoning_knob(self):
        """A non-reasoning OpenAI model (gpt-4o) gets no reasoning knob at all."""
        model = _make_model(model_identifier="gpt-4o", provider_name="openai")
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert "reasoning_effort" not in result

    def test_model_parameters_reasoning_effort_wins_over_default(self):
        """model_parameters reasoning_effort overrides any capability default."""
        model = _make_model(model_parameters={"reasoning_effort": "high"})
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result["reasoning_effort"] == "high"

    def test_drop_params_is_set(self):
        """drop_params=True is always present for litellm unsupported-param safety."""
        model = _make_model()
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result["drop_params"] is True

    def test_none_model_parameters_handled(self):
        """model_parameters=None does not crash."""
        model = _make_model(model_parameters=None)
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert "model" in result

    def test_non_dict_model_parameters_ignored(self):
        """model_parameters that is not a dict is silently ignored."""
        model = _make_model(model_parameters="not-a-dict")
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result["model"] == "test"

    def test_extra_body_merge_not_clobber(self):
        """model_parameters extra_body merges with the default, not replaces it."""
        model = _make_model(
            model_parameters={"extra_body": {"custom_key": "custom_val"}}
        )
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        eb = result.get("extra_body", {})
        # Default thinking-disable from DeepSeek detection must survive.
        assert eb.get("thinking") == {"type": "disabled"}
        # model_parameters custom key must also be present.
        assert eb.get("custom_key") == "custom_val"

    def test_model_parameters_extra_body_thinking_wins(self):
        """Operator-set thinking in extra_body overrides the default."""
        model = _make_model(
            model_parameters={
                "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 5000}}
            }
        )
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        eb = result.get("extra_body", {})
        assert eb["thinking"] == {"type": "enabled", "budget_tokens": 5000}


# ---------------------------------------------------------------------------
# 2. OpenAI SDK extra kwargs helper
# ---------------------------------------------------------------------------


class TestGetAuxOpenaiSdkExtraKwargs:
    """Tests for the OpenAI SDK call-site helper."""

    def test_no_reasoning_effort_for_deepseek(self):
        """DeepSeek model on OpenAI SDK path gets no reasoning_effort."""
        model = _make_model()  # deepseek by default
        result = get_aux_openai_sdk_extra_kwargs(model)
        assert "reasoning_effort" not in result

    def test_no_reasoning_effort_for_non_reasoning_openai(self):
        """gpt-4o (non-reasoning) gets no reasoning_effort."""
        model = _make_model(model_identifier="gpt-4o", provider_name="openai")
        result = get_aux_openai_sdk_extra_kwargs(model)
        assert "reasoning_effort" not in result

    def test_model_parameters_override_default(self):
        model = _make_model(model_parameters={"reasoning_effort": "medium"})
        result = get_aux_openai_sdk_extra_kwargs(model)
        assert result["reasoning_effort"] == "medium"

    def test_call_site_keys_excluded(self):
        model = _make_model(model_parameters={"temperature": 0.5})
        result = get_aux_openai_sdk_extra_kwargs(
            model, call_site_kwargs={"temperature": 0.1}
        )
        assert "temperature" not in result

    def test_unsafe_keys_excluded(self):
        """Keys not in the safe list are excluded."""
        model = _make_model(
            model_parameters={"drop_params": True, "thinking": {"type": "disabled"}}
        )
        result = get_aux_openai_sdk_extra_kwargs(model)
        assert "drop_params" not in result
        assert "thinking" not in result


# ---------------------------------------------------------------------------
# 3. Empty-content guard
# ---------------------------------------------------------------------------


class TestCheckReasoningModelEmptyContent:
    """A reasoning model returning empty content + length must raise."""

    def test_raises_on_empty_content_with_reasoning(self):
        """Empty content, finish_reason=length, reasoning_content present."""
        message = SimpleNamespace(
            content="", reasoning_content="<think>deep thoughts</think>"
        )
        choice = SimpleNamespace(finish_reason="length", message=message)
        response = SimpleNamespace(choices=[choice])

        with pytest.raises(ReasoningModelEmptyContentError):
            check_reasoning_model_empty_content(response)

    def test_no_raise_when_content_present(self):
        """Non-empty content, even with reasoning, is fine."""
        message = SimpleNamespace(
            content="Allow force-push?", reasoning_content="thinking..."
        )
        choice = SimpleNamespace(finish_reason="length", message=message)
        response = SimpleNamespace(choices=[choice])

        check_reasoning_model_empty_content(response)  # should not raise

    def test_no_raise_on_stop_finish(self):
        """finish_reason=stop with empty content is not the reasoning failure mode."""
        message = SimpleNamespace(content="", reasoning_content="thinking...")
        choice = SimpleNamespace(finish_reason="stop", message=message)
        response = SimpleNamespace(choices=[choice])

        check_reasoning_model_empty_content(response)  # should not raise

    def test_no_raise_when_no_reasoning_content(self):
        """Empty content + length but no reasoning_content is not this failure mode."""
        message = SimpleNamespace(content="")
        choice = SimpleNamespace(finish_reason="length", message=message)
        response = SimpleNamespace(choices=[choice])

        check_reasoning_model_empty_content(response)  # should not raise

    def test_no_raise_on_none_response(self):
        """Gracefully handles a None or malformed response."""
        check_reasoning_model_empty_content(None)  # should not raise

    def test_no_raise_on_empty_choices(self):
        """Response with empty choices list."""
        response = SimpleNamespace(choices=[])
        check_reasoning_model_empty_content(response)  # should not raise


# ---------------------------------------------------------------------------
# 4. Reasoning model empty content triggers fallback
# ---------------------------------------------------------------------------


class TestApprovalSummaryReasoningFallback:
    """A reasoning model returning empty content triggers the fallback path."""

    async def test_reasoning_model_empty_content_triggers_fallback(self):
        """When the primary model returns empty content due to reasoning, the
        fallback model is called and its result is returned."""
        from preloop.services.approval_summary import generate_approval_summary
        from preloop.services.model_credentials import (
            _fallback_counters,
            _fallback_warnings,
        )

        _fallback_counters.clear()
        _fallback_warnings.clear()

        primary_model = SimpleNamespace(
            id="reasoning-model",
            provider_name="deepseek",
            model_identifier="deepseek/deepseek-v4-flash",
            api_endpoint=None,
            model_parameters=None,
        )
        fallback_model = SimpleNamespace(
            id="fallback-model",
            provider_name="openai",
            model_identifier="o4-mini",
            api_endpoint=None,
            model_parameters=None,
        )

        # Primary returns empty content with reasoning (the failure mode).
        primary_message = MagicMock()
        primary_message.content = ""
        primary_message.reasoning_content = "<think>long reasoning</think>"
        primary_choice = MagicMock()
        primary_choice.finish_reason = "length"
        primary_choice.message = primary_message
        primary_response = MagicMock()
        primary_response.choices = [primary_choice]

        # Fallback returns actual content.
        fallback_message = MagicMock()
        fallback_message.content = "Allow the agent to deploy?"
        fallback_message.reasoning_content = None
        fallback_choice = MagicMock()
        fallback_choice.finish_reason = "stop"
        fallback_choice.message = fallback_message
        fallback_response = MagicMock()
        fallback_response.choices = [fallback_choice]

        call_count = {"n": 0}

        def fake_completion(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return primary_response
            return fallback_response

        # Both approval_summary and model_credentials import the same
        # crud_ai_model object. A single side_effect discriminates by
        # the account_id argument: non-None is the per-account lookup
        # in generate_approval_summary; None is the system-wide
        # fallback lookup in call_with_default_model_fallback.
        def get_model_side_effect(db, account_id=None, model_kind="llm"):
            if account_id is not None:
                return primary_model
            return fallback_model

        with (
            patch(
                "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
                side_effect=get_model_side_effect,
            ),
            patch("preloop.services.model_credentials.get_secret_service") as mock_ss,
            patch("litellm.completion", side_effect=fake_completion),
        ):
            mock_ss.return_value.resolve_ai_model_credentials.return_value = None

            summary = await generate_approval_summary(
                MagicMock(),
                account_id="acct-1",
                tool_name="deploy",
                tool_args={"env": "prod"},
            )

        assert summary == "Allow the agent to deploy?"
        assert call_count["n"] == 2, "Expected primary + fallback calls"


# ---------------------------------------------------------------------------
# 5. model_parameters thinking-disable knob produces content
# ---------------------------------------------------------------------------


class TestModelParametersThinkingDisable:
    """model_parameters with a thinking-disable knob is passed through to litellm."""

    async def test_thinking_disabled_via_model_parameters(self):
        """When model_parameters carries thinking={'type': 'disabled'},
        that value is merged into the litellm kwargs."""
        from preloop.services.approval_summary import generate_approval_summary
        from preloop.services.model_credentials import (
            _fallback_counters,
            _fallback_warnings,
        )

        _fallback_counters.clear()
        _fallback_warnings.clear()

        model = SimpleNamespace(
            id="model-with-thinking-disabled",
            provider_name="deepseek",
            model_identifier="deepseek/deepseek-v4-flash",
            api_endpoint=None,
            model_parameters={"thinking": {"type": "disabled"}},
        )

        mock_message = MagicMock()
        mock_message.content = "Allow the agent to push?"
        mock_message.reasoning_content = None
        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        captured_kwargs = {}

        def fake_completion(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        with (
            patch(
                "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
                return_value=model,
            ),
            patch("preloop.services.model_credentials.get_secret_service") as mock_ss,
            patch("litellm.completion", side_effect=fake_completion),
        ):
            mock_ss.return_value.resolve_ai_model_credentials.return_value = None

            summary = await generate_approval_summary(
                MagicMock(),
                account_id="acct-1",
                tool_name="git_push",
                tool_args={"branch": "main"},
            )

        assert summary == "Allow the agent to push?"
        # The thinking knob must have been passed through to litellm.
        assert captured_kwargs.get("thinking") == {"type": "disabled"}


# ---------------------------------------------------------------------------
# 6. Gateway path is unaffected
# ---------------------------------------------------------------------------


class TestGatewayUnaffected:
    """The gateway module must not import or use aux helpers."""

    def test_gateway_does_not_import_build_aux_kwargs(self):
        import inspect
        import preloop.services.openai_gateway as gateway_module

        source = inspect.getsource(gateway_module)
        assert "build_aux_kwargs" not in source
        assert "check_reasoning_model_empty_content" not in source
        assert "get_aux_openai_sdk_extra_kwargs" not in source

    def test_gateway_does_not_import_model_credentials_module(self):
        import inspect
        import preloop.services.openai_gateway as gateway_module

        source = inspect.getsource(gateway_module)
        assert "services.model_credentials" not in source
        assert "from preloop.services import model_credentials" not in source

    def test_gateway_still_uses_direct_secret_resolution(self):
        import inspect
        import preloop.services.openai_gateway as gateway_module

        source = inspect.getsource(gateway_module)
        assert "resolve_ai_model_credentials" in source


# ---------------------------------------------------------------------------
# 7. Real litellm get_optional_params integration tests (no mocks, no network)
# ---------------------------------------------------------------------------


class TestLitellmRealGetOptionalParams:
    """Call litellm's REAL get_optional_params to verify the reasoning-disable
    knobs actually reach the wire (or stay absent) as intended.

    These tests do not mock litellm. They exercise the pure parameter-mapping
    logic that litellm provides, which runs locally with no network calls.
    """

    def test_deepseek_thinking_disabled_survives(self):
        """extra_body thinking disabled for DeepSeek IS present in the output.

        This test must FAIL against c3f8c85e, which produces extra_body={}.
        """
        import litellm.utils

        # Build kwargs the way build_aux_kwargs does for DeepSeek
        model = _make_model()
        result = build_aux_kwargs(
            model,
            {},
            call_site_kwargs={
                "model": "deepseek/deepseek-v4-flash",
                "max_tokens": 150,
            },
        )

        # Simulate what litellm.completion does internally
        params = litellm.utils.get_optional_params(
            model="deepseek/deepseek-v4-flash",
            custom_llm_provider="deepseek",
            max_tokens=result.get("max_tokens", 150),
            drop_params=result.get("drop_params", True),
            extra_body=result.get("extra_body"),
            **(
                {"reasoning_effort": result["reasoning_effort"]}
                if "reasoning_effort" in result
                else {}
            ),
        )

        eb = params.get("extra_body", {})
        assert eb.get("thinking") == {"type": "disabled"}, (
            f"thinking disable knob must survive in extra_body, got {params}"
        )

    def test_o4_mini_no_reasoning_effort(self):
        """o4-mini must NOT receive reasoning_effort in the output.

        This test must FAIL against c3f8c85e, which sends reasoning_effort="none".
        """
        import litellm.utils

        model = _make_model(model_identifier="o4-mini", provider_name="openai")
        result = build_aux_kwargs(
            model,
            {},
            call_site_kwargs={
                "model": "o4-mini",
                "max_tokens": 150,
            },
        )

        params = litellm.utils.get_optional_params(
            model="o4-mini",
            custom_llm_provider="openai",
            max_tokens=result.get("max_tokens", 150),
            drop_params=result.get("drop_params", True),
            **(
                {"reasoning_effort": result["reasoning_effort"]}
                if "reasoning_effort" in result
                else {}
            ),
        )

        assert "reasoning_effort" not in params, (
            f"o4-mini must not receive reasoning_effort, got {params}"
        )

    def test_gpt4o_no_reasoning_knob(self):
        """gpt-4o (non-reasoning model) must not have any reasoning knob."""
        import litellm.utils

        model = _make_model(model_identifier="gpt-4o", provider_name="openai")
        result = build_aux_kwargs(
            model,
            {},
            call_site_kwargs={
                "model": "gpt-4o",
                "max_tokens": 150,
            },
        )

        params = litellm.utils.get_optional_params(
            model="gpt-4o",
            custom_llm_provider="openai",
            max_tokens=result.get("max_tokens", 150),
            drop_params=result.get("drop_params", True),
            **(
                {"reasoning_effort": result["reasoning_effort"]}
                if "reasoning_effort" in result
                else {}
            ),
        )

        assert "reasoning_effort" not in params, (
            f"gpt-4o must not receive reasoning_effort, got {params}"
        )

    def test_gpt51_gets_reasoning_effort_none(self):
        """gpt-5.1 supports reasoning_effort='none' and should receive it."""
        import litellm.utils

        model = _make_model(model_identifier="gpt-5.1", provider_name="openai")
        result = build_aux_kwargs(
            model,
            {},
            call_site_kwargs={
                "model": "gpt-5.1",
                "max_tokens": 150,
            },
        )

        params = litellm.utils.get_optional_params(
            model="gpt-5.1",
            custom_llm_provider="openai",
            max_tokens=result.get("max_tokens", 150),
            drop_params=result.get("drop_params", True),
            **(
                {"reasoning_effort": result["reasoning_effort"]}
                if "reasoning_effort" in result
                else {}
            ),
        )

        assert params.get("reasoning_effort") == "none", (
            f"gpt-5.1 should receive reasoning_effort='none', got {params}"
        )

    def test_model_parameters_wins_over_capability_default(self):
        """model_parameters reasoning_effort overrides the capability default.

        Even for DeepSeek (where the default is extra_body thinking), an
        operator-set reasoning_effort must appear in the final kwargs.
        """
        model = _make_model(
            model_parameters={"reasoning_effort": "high"},
        )
        result = build_aux_kwargs(
            model,
            {},
            call_site_kwargs={
                "model": "deepseek/deepseek-v4-flash",
                "max_tokens": 150,
            },
        )

        assert result["reasoning_effort"] == "high", (
            "model_parameters must win over any capability default"
        )
