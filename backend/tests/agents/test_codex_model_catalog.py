"""Custom Codex metadata is explicit without impersonating a known model."""

import hashlib
import json
from unittest.mock import patch

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.codex_0_153_4_defaults import BASE_INSTRUCTIONS
from preloop.agents.codex_model_catalog import (
    CODEX_FALLBACK_CONTEXT_WINDOW,
    catalog_materialization_shell,
    custom_model_catalog,
    hosted_fallback_instructions,
)
from preloop.services.model_context_limits import ModelContextLimits


def test_custom_auth_installs_exact_slug_catalog_with_uncapped_known_context() -> None:
    with patch.object(CodexAgent, "_detect_kubernetes_environment", return_value=False):
        agent = CodexAgent({})
    script = agent._build_codex_auth_config(
        "example/reasoning-model",
        "preloop",
        "https://example.com/openai/v1",
        ModelContextLimits(
            context_window=1_000_000,
            max_output_tokens=8192,
            context_window_source="model_row",
        ),
        reasoning_effort="low",
        attach_mcp=False,
    )
    assert 'model_catalog_json = "$HOME/.codex/preloop-models.json"' in script
    assert "tools.update_plan.enabled = false" in script
    assert 'model_reasoning_effort = "low"' in script
    assert "model_max_output_tokens =" not in script
    document = script.split("<<'PRELOOP_CODEX_MODEL_CATALOG'\n", 1)[1].split(
        "\nPRELOOP_CODEX_MODEL_CATALOG", 1
    )[0]
    entry = json.loads(document)["models"][0]
    assert entry["slug"] == "example/reasoning-model"
    assert entry["context_window"] == entry["max_context_window"] == 1_000_000
    assert entry["base_instructions"] == hosted_fallback_instructions()


def test_unknown_context_is_named_cli_fallback_not_upstream_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        catalog = custom_model_catalog("example/unknown", ModelContextLimits())
    assert catalog is not None
    assert catalog["models"][0]["context_window"] == CODEX_FALLBACK_CONTEXT_WINDOW
    assert "codex_fallback_not_provider_capacity" in caplog.text
    assert "max_output_tokens" not in catalog["models"][0]


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-2026", "provider/gpt-5.4"])
def test_native_model_profiles_are_not_replaced(model: str) -> None:
    assert (
        custom_model_catalog(model, ModelContextLimits(context_window=100_000)) is None
    )


def test_multiple_namespaces_do_not_accidentally_borrow_a_native_profile() -> None:
    catalog = custom_model_catalog("provider/namespace/gpt-5.4", None)
    assert catalog is not None
    assert catalog["models"][0]["base_instructions"] == hosted_fallback_instructions()


def test_unvalidated_cli_version_preserves_existing_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert custom_model_catalog("example/unknown", None, cli_version="0.99.0") is None
    assert "not validated" in caplog.text


def test_fallback_instructions_are_exact_versioned_upstream_blob() -> None:
    encoded = BASE_INSTRUCTIONS.encode()
    git_blob = b"blob " + str(len(encoded)).encode() + b"\0" + encoded
    assert (
        hashlib.sha1(git_blob).hexdigest() == "907ff8b877026871b088f01f4366cea36e1f02cd"
    )


def test_catalog_heredoc_does_not_expand_model_or_instructions() -> None:
    catalog = custom_model_catalog('example/$(touch /tmp/unsafe)"model', None)
    assert catalog is not None
    script = catalog_materialization_shell(catalog)
    assert "<<'PRELOOP_CODEX_MODEL_CATALOG'" in script
    document = script.split("\n", 1)[1].split("\nPRELOOP_CODEX_MODEL_CATALOG", 1)[0]
    assert (
        json.loads(document)["models"][0]["slug"]
        == 'example/$(touch /tmp/unsafe)"model'
    )


def test_startup_marker_labels_unknown_context_and_gateway_output_owner() -> None:
    catalog = custom_model_catalog("example/custom", None)
    assert catalog is not None
    script = catalog_materialization_shell(catalog)
    line = next(
        line
        for line in script.splitlines()
        if line.startswith("PRELOOP_CODEX_MODEL_METADATA ")
    )
    marker = json.loads(line.removeprefix("PRELOOP_CODEX_MODEL_METADATA "))
    assert marker["context_source"] == "codex_fallback"
    assert marker["provider_context_known"] is False
    assert marker["instruction_and_tool_profile"] == "codex_cli_fallback"
    assert marker["upstream_output_allowance"] == "controlled_separately_by_gateway"


def test_hosted_prompt_matches_pinned_cli_default_request() -> None:
    # Captured from Codex 0.153.4 against a localhost fake Responses server,
    # without a catalog, using an unknown slug and the default disabled plan tool.
    assert hashlib.sha256(hosted_fallback_instructions().encode()).hexdigest() == (
        "3b08633fa672906666659d764864dfda1d7af5b5111ea5817c8f46e5de4e1a8d"
    )


def test_catalog_advertises_only_selected_route() -> None:
    catalog = custom_model_catalog("example/custom", None)
    assert catalog is not None
    assert [entry["slug"] for entry in catalog["models"]] == ["example/custom"]


def test_auth_does_not_install_unvalidated_version_catalog() -> None:
    with patch.object(CodexAgent, "_detect_kubernetes_environment", return_value=False):
        agent = CodexAgent({})
    script = agent._build_codex_auth_config(
        "example/custom",
        "example",
        "https://example.com/v1",
        cli_version="0.99.0",
        attach_mcp=False,
    )
    assert "model_catalog_json" not in script
    assert "PRELOOP_CODEX_MODEL_METADATA" not in script


def test_startup_marker_reports_registered_context_source() -> None:
    limits = ModelContextLimits(
        context_window=1_000_000, context_window_source="model_row"
    )
    catalog = custom_model_catalog("example/custom", limits)
    assert catalog is not None
    script = catalog_materialization_shell(catalog, limits)
    line = next(
        line
        for line in script.splitlines()
        if line.startswith("PRELOOP_CODEX_MODEL_METADATA ")
    )
    marker = json.loads(line.removeprefix("PRELOOP_CODEX_MODEL_METADATA "))
    assert marker["context_source"] == "model_row"
    assert marker["provider_context_known"] is True
    assert marker["cli_context_window"] == 1_000_000
