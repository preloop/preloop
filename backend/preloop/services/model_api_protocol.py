"""Resolve the API that hosted OpenCode should use for a gateway model.

Unknown models keep chat completions. Explicit Responses overrides and known
endpoint-scoped capabilities opt into the native Responses SDK. No network
lookup runs while preparing a flow.
"""

from __future__ import annotations

from preloop.models import models
from preloop.services.openai_responses_passthrough import (
    is_openai_shaped_upstream,
    responses_passthrough_mode,
)

# models.dev (MIT), https://models.dev/api.json, snapshot 2026-09-12.
# OpenCode Zen models whose model.provider.npm is @ai-sdk/openai. Refresh from
# that field, not model-name prefixes: the same endpoint also serves chat-only
# models, and another endpoint can expose the same id over a different API.
OPENCODE_ZEN_RESPONSES_MODELS = frozenset(
    [
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-nano",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5",
        "gpt-5.5-pro",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-6-astra",
        "grok-4.5",
        "grok-4.6",
        "grok-build-0.1",
        "muse-spark-1.2",
        "muse-spark-1.2-contributor-free",
        "muse-spark-1.3",
        "muse-spark-1.3-contributor-free",
    ]
)


def model_api_protocol(ai_model: models.AIModel) -> str:
    """Choose Responses only with an explicit override or scoped capability.

    The existing gateway responses_api=transcode override forces chat. Native
    only applies to upstreams the gateway can actually forward Responses to.
    """
    mode = responses_passthrough_mode(ai_model)
    if mode == "transcode" or not is_openai_shaped_upstream(ai_model):
        return "chat_completions"
    if mode == "native":
        return "responses"
    endpoint = getattr(ai_model, "api_endpoint", None)
    identifier = getattr(ai_model, "model_identifier", None)
    if (
        isinstance(endpoint, str)
        and endpoint.strip().rstrip("/") == "https://opencode.ai/zen/v1"
        and identifier in OPENCODE_ZEN_RESPONSES_MODELS
    ):
        return "responses"
    return "chat_completions"
