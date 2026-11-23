"""Explicit custom-model metadata for the version-pinned hosted Codex CLI."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from preloop.services.model_context_limits import ModelContextLimits

from .codex_0_153_4_defaults import BASE_INSTRUCTIONS

logger = logging.getLogger(__name__)

# Standard variants in rust-v0.153.4/codex-rs/protocol/src/openai_models.rs.
# Preserve role overrides as pass-through inputs without inferring provider support.
CODEX_CLI_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "persistent",
)

CODEX_CATALOG_VERSION = "0.153.4"
CODEX_FALLBACK_CONTEXT_WINDOW = 272_000
#: Share of the context window one tool result may take, and its ceiling.
#: Codex 0.153.4 cuts each result to the smaller of this catalog policy and
#: the call's own ``max_output_tokens`` (default 10,000), keeping the start
#: and end (codex-rs/core/src/tools/context.rs, ``model_output_policy``).
TOOL_OUTPUT_WINDOW_DIVISOR = 10
TOOL_OUTPUT_TOKEN_CEILING = 64_000
# Exact slugs in rust-v0.153.4/codex-rs/models-manager/models.json. Codex
# longest-prefix and namespaced-suffix resolution must keep these profiles.
BUNDLED_MODEL_PREFIXES = (
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-daybreak-blue-latest",
    "gpt-daybreak-red-latest",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.2",
    "codex-auto-review",
)


def tool_output_token_limit(context_window: int) -> int:
    """Per-result token limit for a custom model's catalog entry.

    One tenth of the window, at most :data:`TOOL_OUTPUT_TOKEN_CEILING`:
    3,200 tokens for a 32,000 window, 12,800 for 128,000, 20,000 for 200,000
    and 64,000 for 1,000,000.

    Args:
        context_window: The window written into the catalog entry.

    Returns:
        The ``truncation_policy`` limit, in tokens.
    """
    share = max(context_window, 0) // TOOL_OUTPUT_WINDOW_DIVISOR
    return max(1, min(share, TOOL_OUTPUT_TOKEN_CEILING))


def custom_model_catalog(
    model: str,
    limits: ModelContextLimits | None,
    *,
    cli_version: str = CODEX_CATALOG_VERSION,
) -> dict[str, Any] | None:
    """Describe an unknown model without changing Codex's fallback behavior.

    Known models keep the CLI's own profiles. Unknown limits retain the pinned
    CLI's fallback window, explicitly logged as a fallback rather than provider
    capacity. A known window replaces both context fields: Codex clamps the
    scalar override to ``max_context_window`` otherwise.
    """
    if cli_version != CODEX_CATALOG_VERSION:
        logger.warning(
            "Custom Codex catalog not validated for CLI %s; keeping CLI metadata",
            cli_version,
        )
        return None
    # These legacy slugs use a separate personality template in the fallback.
    if model in {"gpt-5.2-codex", "exp-codex-personality"}:
        return None
    candidates = [model]
    namespace, separator, suffix = model.partition("/")
    if separator and "/" not in suffix and re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
        candidates.append(suffix)
    if any(
        candidate.startswith(prefix)
        for candidate in candidates
        for prefix in BUNDLED_MODEL_PREFIXES
    ):
        return None
    limits = limits or ModelContextLimits()
    context_window = limits.context_window or CODEX_FALLBACK_CONTEXT_WINDOW
    logger.info(
        "Codex custom model metadata: model=%s context_window=%s source=%s "
        "CLI=%s; output allowance is separate (gateway when enabled, otherwise provider defaults)",
        model,
        context_window,
        limits.context_window_source or "codex_fallback_not_provider_capacity",
        cli_version,
    )
    # Match model_info_from_slug in the supported release, including generic
    # instructions, shell/tool behavior and reasoning defaults. These are CLI
    # defaults, not assertions that an unknown upstream has those capabilities.
    entry: dict[str, Any] = {
        "slug": model,
        "display_name": model,
        "description": None,
        "default_reasoning_level": None,
        # Codex validates role/subagent overrides for known catalog entries.
        # These are pinned CLI pass-through inputs, not inferred
        # provider capabilities. Keep the inherited default unset.
        "supported_reasoning_levels": [
            {
                "effort": effort,
                "description": "Pass through explicit effort to provider",
            }
            for effort in CODEX_CLI_REASONING_EFFORTS
        ],
        "shell_type": "unified_exec",
        "visibility": "none",
        "supported_in_api": True,
        "priority": 99,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": hosted_fallback_instructions(),
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": True,
        "default_reasoning_summary": "auto",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "web_search_tool_type": "text",
        "truncation_policy": {
            "mode": "tokens",
            "limit": tool_output_token_limit(context_window),
        },
        "supports_image_detail_original": False,
        "context_window": context_window,
        "max_context_window": context_window,
        "auto_compact_token_limit": None,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
    }
    return {"models": [entry]}


def catalog_materialization_shell(
    catalog: dict[str, Any], limits: ModelContextLimits | None = None
) -> str:
    """Write only trusted metadata, with no shell interpolation of its values."""
    document = json.dumps(catalog, ensure_ascii=True)
    limits = limits or ModelContextLimits()
    known_context = limits.context_window is not None
    source = limits.context_window_source if known_context else "codex_fallback"
    if source not in {"model_row", "catalog", "codex_fallback"}:
        source = "resolved"
    entry = catalog["models"][0]
    startup = json.dumps(
        {
            "cli_version": CODEX_CATALOG_VERSION,
            "model": entry["slug"],
            "cli_context_window": entry["context_window"],
            "context_source": source,
            "provider_context_known": known_context,
            "warning": None
            if known_context
            else (
                "No context window is registered for this custom model. "
                "Set the actual server limit in model_parameters.context_window "
                "(docs/guide/model-context-limits.md); the CLI fallback is not "
                "the server's capacity."
            ),
            "instruction_and_tool_profile": "codex_cli_fallback",
            "upstream_output_allowance": "gateway_when_enabled_otherwise_provider_defaults",
        },
        ensure_ascii=True,
    )
    return (
        "cat > ~/.codex/preloop-models.json <<'PRELOOP_CODEX_MODEL_CATALOG'\n"
        f"{document}\n"
        "PRELOOP_CODEX_MODEL_CATALOG\n"
        "cat <<'PRELOOP_CODEX_METADATA_MARKER'\n"
        f"PRELOOP_CODEX_MODEL_METADATA {startup}\n"
        "PRELOOP_CODEX_METADATA_MARKER\n"
    )


def hosted_fallback_instructions() -> str:
    """Mirror 0.153.4's disabled-update-plan rendering of the generic prompt.

    The hosted CLI leaves update_plan_enabled at its false default. Codex skips
    its own checklist-section removal for a local catalog, so apply that exact
    removal here. Keep the original upstream blob separately for upgrade audits.
    Source: codex-rs/core/src/context/update_plan_instructions.rs.
    """
    instructions = BASE_INSTRUCTIONS
    start = instructions.index("## Planning\n")
    end = instructions.index("## Task execution\n", start)
    instructions = instructions[:start] + instructions[end:]
    return instructions[: instructions.index("## `update_plan`\n")]


#: Codex's per-call default when the model sends no ``max_output_tokens``
#: (``DEFAULT_MAX_OUTPUT_TOKENS``, codex-rs/core/src/unified_exec/mod.rs).
CODEX_DEFAULT_CALL_OUTPUT_TOKENS = 10_000
#: Codex's own fallback for a model it does not know, ``bytes`` 10,000
#: (about 2,500 tokens), and the bundled ``gpt-5.2`` profile.
CODEX_FALLBACK_TOOL_OUTPUT_TOKENS = 2_500


def tool_output_limit_for_context(context: dict[str, Any]) -> int:
    """The per-result token limit hosted Codex applies to this execution.

    Mirrors how :class:`preloop.agents.codex.CodexAgent` picks the model and
    provider. Bundled models keep their own 10,000-token profile.

    Args:
        context: The execution context at launch.

    Returns:
        The catalog limit in tokens, before the per-call ``max_output_tokens``.
    """
    from preloop.services.model_context_limits import limits_for_execution

    gateway = bool(context.get("model_gateway_enabled"))
    model = (context.get("model_gateway_model_alias") if gateway else None) or (
        context.get("codex_model") or context.get("model_identifier") or "gpt-5.4"
    )
    provider = (
        context.get("model_gateway_provider")
        if gateway
        else context.get("model_provider", "openai")
    )
    model = str(model)
    if (provider or "openai").lower() != "openai":
        catalog = custom_model_catalog(model, limits_for_execution(context))
        if catalog is not None:
            return int(catalog["models"][0]["truncation_policy"]["limit"])
    bundled = model.rpartition("/")[2]
    if bundled == "gpt-5.2" or not bundled.startswith(BUNDLED_MODEL_PREFIXES):
        return CODEX_FALLBACK_TOOL_OUTPUT_TOKENS
    return CODEX_DEFAULT_CALL_OUTPUT_TOKENS
