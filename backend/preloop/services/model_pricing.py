"""Shared model pricing resolution and cost estimation helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import litellm

from preloop.models.models.ai_model import AIModel

logger = logging.getLogger(__name__)

_PROVIDER_PREFIX: Dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "gemini": "gemini",
    "qwen": "openai",
    "deepseek": "deepseek",
    "aws": "bedrock",
    "bedrock": "bedrock",
    "amazon-bedrock": "bedrock",
    "azure": "azure",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
}

# Bedrock cross-region inference profiles prefix the model id with a region
# marker that litellm's price map does not include.
_BEDROCK_REGION_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "global.")

# Trailing date/version stamps that often differ from the price-map key,
# e.g. "claude-3-5-sonnet-20241022", "gpt-4o-2024-08-06", "gemini-pro@001".
_DATE_SUFFIX_PATTERNS = (
    re.compile(r"-\d{8}$"),
    re.compile(r"-\d{4}-\d{2}-\d{2}$"),
    re.compile(r"@\d+$"),
)


def _expand_candidate(candidate: str, provider: str) -> Iterable[str]:
    """Yield normalized fallback forms of one candidate model name.

    Ordered from most to least specific: the raw name first, then with the
    Bedrock region prefix stripped, then with trailing date/version stamps
    removed (litellm aliases the undated name for most models).
    """
    yield candidate

    stripped = candidate
    for region_prefix in _BEDROCK_REGION_PREFIXES:
        if stripped.startswith(region_prefix):
            stripped = stripped[len(region_prefix) :]
            yield stripped
            if "/" not in stripped:
                yield f"bedrock/{stripped}"
            break

    for pattern in _DATE_SUFFIX_PATTERNS:
        undated = pattern.sub("", stripped)
        if undated != stripped and undated:
            yield undated
            prefix = _PROVIDER_PREFIX.get(provider, provider)
            if "/" not in undated:
                yield f"{prefix}/{undated}"
            break


@dataclass(frozen=True)
class CostEstimate:
    """A cost estimate with its pricing provenance.

    ``source`` values: ``override`` (account price override), ``model_config``
    (pricing stored on the AIModel), ``catalog`` (vendored/litellm list
    price), ``unpriced`` (no price could be resolved; ``cost`` is None).
    """

    cost: Optional[float]
    source: str


def estimate_ai_model_usage_cost(
    ai_model: AIModel,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    usage_details: Optional[Dict[str, Any]] = None,
    pricing_override: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Estimate usage cost using manual pricing overrides or LiteLLM pricing."""
    return estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        usage_details=usage_details,
        pricing_override=pricing_override,
    ).cost


def estimate_ai_model_usage_cost_detailed(
    ai_model: AIModel,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    usage_details: Optional[Dict[str, Any]] = None,
    pricing_override: Optional[Dict[str, Any]] = None,
) -> CostEstimate:
    """Estimate usage cost and report which pricing source produced it."""
    configured_pricing = pricing_override or _get_configured_pricing(ai_model)
    if configured_pricing:
        configured_cost = _estimate_cost_from_pricing(
            configured_pricing,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_details=usage_details,
        )
        if configured_cost is not None:
            return CostEstimate(
                cost=configured_cost,
                source="override" if pricing_override else "model_config",
            )

    if prompt_tokens <= 0 and completion_tokens <= 0:
        return CostEstimate(cost=None, source="unpriced")

    list_cost = _estimate_litellm_cost(
        ai_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        usage_details=usage_details,
    )
    if list_cost is None:
        return CostEstimate(cost=None, source="unpriced")
    if pricing_override:
        # Override carries only adjustments (discount/prepaid) — apply them
        # on top of the list price.
        return CostEstimate(
            cost=_apply_pricing_adjustments(
                list_cost,
                pricing_override,
                total_tokens=total_tokens,
            ),
            source="override",
        )
    return CostEstimate(cost=list_cost, source="catalog")


def _estimate_litellm_cost(
    ai_model: AIModel,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    usage_details: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Estimate list-price model cost using LiteLLM metadata."""
    for candidate in _iter_litellm_model_candidates(ai_model):
        if usage_details:
            try:
                return round(
                    float(
                        litellm.completion_cost(
                            model=candidate,
                            completion_response={"usage": usage_details},
                        )
                    ),
                    6,
                )
            except Exception:
                logger.debug(
                    "LiteLLM detailed pricing unavailable for model candidate %s",
                    candidate,
                    exc_info=True,
                )
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=candidate,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return round(float(prompt_cost or 0.0) + float(completion_cost or 0.0), 6)
        except Exception:
            logger.debug(
                "LiteLLM pricing unavailable for model candidate %s",
                candidate,
                exc_info=True,
            )

    return None


def _get_configured_pricing(ai_model: AIModel) -> Optional[Dict[str, Any]]:
    """Return manually configured pricing metadata when present."""
    pricing = None
    if ai_model.meta_data and isinstance(ai_model.meta_data, dict):
        pricing = ai_model.meta_data.get("pricing")
    if (
        not pricing
        and ai_model.model_parameters
        and isinstance(ai_model.model_parameters, dict)
    ):
        pricing = ai_model.model_parameters.get("pricing")
    return pricing if isinstance(pricing, dict) else None


def _estimate_cost_from_pricing(
    pricing: Dict[str, Any],
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    usage_details: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Estimate cost from a normalized pricing configuration."""
    usage_details = usage_details or {}
    prompt_tokens_details = usage_details.get("prompt_tokens_details") or {}
    # Anthropic reports cache reads at the top level; OpenAI nests them under
    # prompt_tokens_details.cached_tokens. Without the top-level fallback,
    # Anthropic cache reads were billed at the full input price.
    cached_tokens = int(
        prompt_tokens_details.get("cached_tokens")
        or usage_details.get("cache_read_input_tokens")
        or 0
    )
    cache_creation_tokens = int(
        prompt_tokens_details.get("cache_creation_tokens")
        or usage_details.get("cache_creation_input_tokens")
        or 0
    )
    uncached_prompt_tokens = max(
        prompt_tokens - cached_tokens - cache_creation_tokens, 0
    )

    input_price_per_1k = pricing.get("input_price_per_1k")
    output_price_per_1k = pricing.get("output_price_per_1k")
    if input_price_per_1k is not None or output_price_per_1k is not None:
        input_cost = (uncached_prompt_tokens / 1000.0) * float(input_price_per_1k or 0)
        input_cost += (cached_tokens / 1000.0) * float(
            pricing.get("cache_read_input_price_per_1k") or input_price_per_1k or 0
        )
        input_cost += (cache_creation_tokens / 1000.0) * float(
            pricing.get("cache_creation_input_price_per_1k") or input_price_per_1k or 0
        )
        output_cost = (completion_tokens / 1000.0) * float(output_price_per_1k or 0)
        request_cost = float(pricing.get("request_price") or 0)
        return _apply_pricing_adjustments(
            input_cost + output_cost + request_cost,
            pricing,
            total_tokens=total_tokens,
        )

    price_per_1k = pricing.get("price_per_1k")
    if price_per_1k is not None:
        request_cost = float(pricing.get("request_price") or 0)
        return _apply_pricing_adjustments(
            (total_tokens / 1000.0) * float(price_per_1k) + request_cost,
            pricing,
            total_tokens=total_tokens,
        )

    request_price = pricing.get("request_price")
    if request_price is not None:
        return _apply_pricing_adjustments(
            float(request_price),
            pricing,
            total_tokens=total_tokens,
        )

    return None


def _apply_pricing_adjustments(
    cost: float,
    pricing: Dict[str, Any],
    *,
    total_tokens: int,
) -> float:
    """Apply discounts and prepaid balances to an estimated request cost."""
    adjusted_cost = max(float(cost), 0.0)
    discount_percent = pricing.get("discount_percent")
    if discount_percent is not None:
        discount_ratio = min(max(float(discount_percent), 0.0), 100.0) / 100.0
        adjusted_cost *= 1.0 - discount_ratio

    prepaid_token_balance = pricing.get("prepaid_token_balance")
    if prepaid_token_balance is not None and total_tokens > 0:
        covered_ratio = min(
            max(float(prepaid_token_balance), 0.0), total_tokens
        ) / float(total_tokens)
        adjusted_cost *= 1.0 - covered_ratio

    prepaid_credit_balance_usd = pricing.get("prepaid_credit_balance_usd")
    if prepaid_credit_balance_usd is not None:
        adjusted_cost = max(adjusted_cost - float(prepaid_credit_balance_usd), 0.0)

    return round(adjusted_cost, 6)


def _iter_litellm_model_candidates(ai_model: AIModel) -> Iterable[str]:
    """Yield likely LiteLLM model names for the configured AI model."""
    provider = (ai_model.provider_name or "openai").strip().lower()
    model_identifier = (ai_model.model_identifier or "").strip()
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    gateway_config = (
        meta_data.get("gateway") if isinstance(meta_data.get("gateway"), dict) else {}
    )

    candidates = []
    gateway_alias = gateway_config.get("model_alias")
    if isinstance(gateway_alias, str) and gateway_alias.strip():
        candidates.append(gateway_alias.strip())

    if model_identifier:
        candidates.append(model_identifier)
        prefix = _PROVIDER_PREFIX.get(provider, provider)
        if "/" not in model_identifier:
            candidates.append(f"{prefix}/{model_identifier}")

    seen = set()
    for candidate in candidates:
        for expanded in _expand_candidate(candidate.strip(), provider):
            normalized = expanded.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            yield normalized
