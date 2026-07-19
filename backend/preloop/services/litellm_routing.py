"""Shared litellm model-string routing for AIModel rows.

Every service that hands an ``AIModel`` to litellm must build the
``provider/model`` string the same way — this module is the single
implementation (the gateway, policy generation, approval summaries, and
session-explorer analysis previously carried divergent copies).

Resolution order:

1. An identifier that already carries a routable prefix (``azure/gpt-5.4``)
   passes through untouched.
2. The local prefix map translates Preloop provider names to litellm ones
   (``google`` -> ``gemini``, ``qwen`` -> ``openai``).
3. Providers litellm routes natively (``mistral``, ``groq``, ...) pass
   through by name.
4. Unknown provider names with their own endpoint — arbitrary
   OpenAI-compatible providers imported from agent configs, e.g. Hermes
   ``model.provider: custom`` entries like ``kimi-for-coding`` — dispatch via
   litellm's generic OpenAI-compatible adapter (``openai/<model>``); callers
   pass the model's ``api_endpoint`` as ``api_base``. litellm rejects unknown
   prefixes outright ("LLM Provider NOT provided") even when ``api_base`` is
   set, so the name must not be forwarded verbatim.
5. Unknown provider names without an endpoint keep the name so the litellm
   error stays attributable instead of silently hitting api.openai.com.
"""

from functools import lru_cache
from typing import Dict

import litellm

from preloop.models.models.ai_model import AIModel

PROVIDER_PREFIX: Dict[str, str] = {
    "openai": "openai",
    "openai-codex": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "gemini": "gemini",
    "bedrock": "bedrock",
    "amazon-bedrock": "bedrock",
    "qwen": "openai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "openrouter": "openrouter",
    "azure": "azure",
    "aws": "bedrock",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
}


@lru_cache(maxsize=1)
def known_litellm_providers() -> frozenset:
    """Provider names litellm can route natively (mistral, groq, ...)."""
    try:
        return frozenset(
            str(getattr(entry, "value", entry)) for entry in litellm.provider_list
        )
    except Exception:  # pragma: no cover - defensive against litellm changes
        return frozenset()


def to_litellm_model(ai_model: AIModel) -> str:
    """Build the litellm model string for an AI model row."""
    provider = (ai_model.provider_name or "openai").strip().lower()
    identifier = (ai_model.model_identifier or "").strip()

    if "/" in identifier:
        head = identifier.split("/", 1)[0].strip().lower()
        if head in known_litellm_providers() or head in set(PROVIDER_PREFIX.values()):
            return identifier

    prefix = PROVIDER_PREFIX.get(provider)
    if prefix is None:
        if provider in known_litellm_providers():
            prefix = provider
        elif ai_model.api_endpoint:
            prefix = "openai"
        else:
            prefix = provider
    return f"{prefix}/{identifier}"
