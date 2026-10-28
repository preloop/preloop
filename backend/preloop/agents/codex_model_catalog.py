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
        "truncation_policy": {"mode": "bytes", "limit": 10_000},
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
