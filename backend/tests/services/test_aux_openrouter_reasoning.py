"""Aux-path reasoning-disable knobs for models routed through OpenRouter.

Prod incident (2026-08-06): an account-scoped model configured as
provider ``openai-compatible``, endpoint ``https://openrouter.ai/api/v1``,
identifier ``deepseek/deepseek-v4-flash-0731`` had every auxiliary call
(approval summaries) time out and silently ride the system-default fallback.
Interactive gateway traffic on the same config worked.

Root cause: ``_get_reasoning_disable_defaults`` matched the ``deepseek/``
vendor prefix and emitted DeepSeek's native knob
``extra_body={"thinking": {"type": "disabled"}}``. The request, however, is
routed to OpenRouter (``to_litellm_model`` -> ``openrouter/deepseek/...``),
where that knob is unknown and ignored, so the model reasoned anyway
(~600 reasoning tokens per call in prod api_usage) and blew the aux
per-attempt timeout. OpenRouter's own disable knob is
``extra_body={"reasoning": {"enabled": false}}``.

These tests pin the routing-aware knob selection, mirroring the precedence
rule in ``litellm_routing.to_litellm_model``: explicit OpenRouter routing
outranks the vendor prefix inside the model id.
"""

from types import SimpleNamespace

from preloop.services.model_credentials import (
    build_aux_kwargs,
    get_aux_openai_sdk_extra_kwargs,
)

OPENROUTER_REASONING_DISABLE = {"reasoning": {"enabled": False}}


def _make_model(
    *,
    model_id="model-1",
    model_identifier="deepseek/deepseek-v4-flash-0731",
    provider_name="openai-compatible",
    api_endpoint="https://openrouter.ai/api/v1",
    model_parameters=None,
):
    return SimpleNamespace(
        id=model_id,
        model_identifier=model_identifier,
        provider_name=provider_name,
        api_endpoint=api_endpoint,
        model_parameters=model_parameters,
    )


class TestOpenRouterReasoningDisable:
    """OpenRouter-routed models must get OpenRouter's knob, not the vendor's."""

    def test_openrouter_endpoint_beats_deepseek_prefix(self):
        """A real-world prod config: openai-compatible + openrouter.ai + deepseek/ id.

        Must produce the OpenRouter reasoning-disable form. The DeepSeek
        ``thinking`` knob is ignored by OpenRouter and lets reasoning eat the
        entire aux token budget (the 2026-08-06 timeout incident).
        """
        model = _make_model()
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result.get("extra_body") == OPENROUTER_REASONING_DISABLE
        assert "thinking" not in result.get("extra_body", {})
        assert "reasoning_effort" not in result

    def test_openrouter_provider_name(self):
        """provider_name=openrouter routes the same way regardless of endpoint."""
        model = _make_model(provider_name="openrouter", api_endpoint=None)
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result.get("extra_body") == OPENROUTER_REASONING_DISABLE

    def test_openrouter_subdomain_endpoint(self):
        """Host matching covers subdomains of openrouter.ai."""
        model = _make_model(api_endpoint="https://gateway.openrouter.ai/api/v1")
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result.get("extra_body") == OPENROUTER_REASONING_DISABLE

    def test_non_openrouter_lookalike_host_not_matched(self):
        """A host merely containing the string must not be treated as OpenRouter."""
        model = _make_model(
            provider_name="deepseek",
            model_identifier="deepseek-v4-flash",
            api_endpoint="https://openrouter.ai.evil.test/api/v1",
        )
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        # Falls through to the DeepSeek branch (provider_name=deepseek).
        assert result.get("extra_body") == {"thinking": {"type": "disabled"}}

    def test_direct_deepseek_unchanged(self):
        """Direct DeepSeek configs keep the wire-surviving thinking knob."""
        model = _make_model(
            provider_name="deepseek",
            model_identifier="deepseek-v4-flash",
            api_endpoint="https://api.deepseek.com/v1",
        )
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result.get("extra_body") == {"thinking": {"type": "disabled"}}

    def test_model_parameters_can_override_disable(self):
        """An operator-set reasoning knob on the model row wins over the default."""
        model = _make_model(
            model_parameters={"extra_body": {"reasoning": {"enabled": True}}}
        )
        result = build_aux_kwargs(model, {}, call_site_kwargs={"model": "test"})

        assert result["extra_body"]["reasoning"] == {"enabled": True}

    def test_survives_litellm_openrouter_param_mapping(self):
        """The knob must reach the wire through litellm's REAL param mapping.

        Uses litellm's actual get_optional_params (pure local logic, no
        network). The old DeepSeek ``thinking`` form also passed through
        OpenrouterConfig verbatim, but OpenRouter upstream ignores it; the
        point pinned here is that the ``reasoning`` form we now emit is
        preserved end to end on the openrouter provider path.
        """
        import litellm.utils

        model = _make_model()
        result = build_aux_kwargs(
            model,
            {"api_base": "https://openrouter.ai/api/v1"},
            call_site_kwargs={
                "model": "openrouter/deepseek/deepseek-v4-flash-0731",
                "max_tokens": 150,
            },
        )

        params = litellm.utils.get_optional_params(
            model="deepseek/deepseek-v4-flash-0731",
            custom_llm_provider="openrouter",
            max_tokens=result["max_tokens"],
            drop_params=result["drop_params"],
            extra_body=result.get("extra_body"),
        )

        assert params.get("extra_body", {}).get("reasoning") == {"enabled": False}, (
            f"OpenRouter reasoning-disable knob must survive litellm mapping, got {params}"
        )


class TestOpenRouterSdkExtras:
    """The OpenAI-SDK aux helper mirrors the same routing-aware knob."""

    def test_openrouter_model_gets_reasoning_disable_extra_body(self):
        model = _make_model()
        extras = get_aux_openai_sdk_extra_kwargs(model)

        assert extras.get("extra_body") == OPENROUTER_REASONING_DISABLE
        assert "reasoning_effort" not in extras

    def test_call_site_extra_body_excluded(self):
        """A call site already passing extra_body keeps its own value."""
        model = _make_model()
        extras = get_aux_openai_sdk_extra_kwargs(
            model, call_site_kwargs={"extra_body": {"reasoning": {"enabled": True}}}
        )

        assert "extra_body" not in extras

    def test_non_openrouter_model_gets_no_extra_body(self):
        model = _make_model(
            provider_name="openai",
            model_identifier="gpt-4o",
            api_endpoint=None,
        )
        extras = get_aux_openai_sdk_extra_kwargs(model)

        assert "extra_body" not in extras
