"""Shared model pricing resolution and cost estimation helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import litellm

from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import PROVIDER_PREFIX as _PROVIDER_PREFIX

logger = logging.getLogger(__name__)

# _PROVIDER_PREFIX is the shared gateway-routing map (imported above). Pricing
# only uses it to GENERATE candidate catalog keys alongside the unprefixed
# identifier, so routing-oriented entries are harmless here.

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

# Provider labels that describe HOW Preloop reaches an endpoint rather than a
# namespace in any price map. Left on a candidate they guarantee a miss, so
# they are stripped before catalog lookup (issue: OpenRouter usage priced $0).
_SYNTHETIC_PROVIDER_PREFIXES = ("openai-compatible/", "custom/", "preloop/")

# Hosts that front a model marketplace whose catalog keys litellm namespaces
# under a provider prefix (e.g. ``openrouter/deepseek/deepseek-chat``).
_ENDPOINT_HOST_PREFIXES = (
    ("openrouter.ai", "openrouter"),
    ("dashscope.aliyuncs.com", "dashscope"),
    ("dashscope-intl.aliyuncs.com", "dashscope"),
    ("dashscope-us.aliyuncs.com", "dashscope"),
    ("maas.aliyuncs.com", "dashscope"),
)


def _strip_synthetic_prefix(candidate: str) -> str:
    """Remove Preloop's routing-only provider prefixes from a model name.

    ``openai-compatible``/``custom`` are transport labels, not price-map
    namespaces, so they must not participate in catalog matching.

    Args:
        candidate: A raw model alias or identifier.

    Returns:
        The candidate without a leading synthetic provider prefix.
    """
    lowered = candidate.lower()
    for prefix in _SYNTHETIC_PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return candidate[len(prefix) :].strip()
    return candidate


def _pricing_provider_prefix(provider: str) -> str:
    """Return the price-catalog namespace for a configured provider.

    Gateway routing maps ``qwen`` to ``openai`` (DashScope compatible-mode).
    Vendored prices live under ``dashscope/<id>``, so pricing lookup must not
    reuse the routing prefix.
    """
    if provider == "qwen":
        return "dashscope"
    return _PROVIDER_PREFIX.get(provider, provider)


def _endpoint_prefix(api_endpoint: Optional[str]) -> Optional[str]:
    """Return the price-map provider prefix implied by a model's endpoint.

    Args:
        api_endpoint: The configured base URL for the model, if any.

    Returns:
        The provider prefix (e.g. ``openrouter``) or None when unknown.
    """
    if not isinstance(api_endpoint, str) or not api_endpoint.strip():
        return None
    lowered = api_endpoint.lower()
    for host, prefix in _ENDPOINT_HOST_PREFIXES:
        if host in lowered:
            return prefix
    return None


def normalize_gateway_model_alias(alias: Optional[str]) -> Optional[str]:
    """Normalize a gateway/client model alias for pricing and matching.

    Strips whitespace and a leading ``preloop/`` gateway prefix so recording
    and pricing lookup paths share one identity for the same model.
    """
    if not isinstance(alias, str):
        return None
    trimmed = alias.strip()
    if not trimmed:
        return None
    if trimmed.lower().startswith("preloop/"):
        trimmed = trimmed.split("/", 1)[1].strip()
    return trimmed or None


def _expand_candidate(
    candidate: str, provider: str, endpoint_prefix: Optional[str] = None
) -> Iterable[str]:
    """Yield normalized fallback forms of one candidate model name.

    Ordered from most to least specific. Preloop's routing-only provider
    prefixes are stripped first, then the marketplace form implied by the
    model's endpoint is offered (``openrouter/vendor/model``, which is how
    litellm prices marketplace-routed traffic), then the bare name, then the
    Bedrock region-stripped form, then the undated form.

    Date suffixes are deliberately NOT stripped from ``vendor/model`` names:
    on model marketplaces a dated snapshot is a separately priced product
    (``deepseek-v4-flash-0731`` is $0.09/$0.18 per Mtok while the undated
    ``deepseek-v4-flash`` is $0.14/$0.28), so aliasing them would invent a
    price that is wrong by ~55%.

    Args:
        candidate: Raw model alias or identifier.
        provider: Configured provider name for the model.
        endpoint_prefix: Price-map prefix implied by the model's endpoint.

    Yields:
        Candidate keys to try against the price catalog, most specific first.
    """
    normalized = normalize_gateway_model_alias(candidate) or candidate
    normalized = _strip_synthetic_prefix(normalized) or normalized

    if endpoint_prefix and not normalized.lower().startswith(f"{endpoint_prefix}/"):
        yield f"{endpoint_prefix}/{normalized}"
    yield normalized

    stripped = normalized
    for region_prefix in _BEDROCK_REGION_PREFIXES:
        if stripped.startswith(region_prefix):
            stripped = stripped[len(region_prefix) :]
            yield stripped
            if "/" not in stripped:
                yield f"bedrock/{stripped}"
            break

    # Marketplace ids carry the vendor in the name (``deepseek/...``). For
    # those, a date stamp identifies a distinct SKU with its own price, so
    # falling back to the undated entry would report a confidently wrong
    # number. Only undate flat, single-segment vendor model names.
    if "/" in stripped:
        return

    for pattern in _DATE_SUFFIX_PATTERNS:
        undated = pattern.sub("", stripped)
        if undated != stripped and undated:
            yield undated
            prefix = _pricing_provider_prefix(provider)
            if "/" not in undated:
                yield f"{prefix}/{undated}"
            break


@dataclass(frozen=True)
class CostEstimate:
    """A cost estimate with its pricing provenance.

    ``source`` values: ``override`` (account price override), ``model_config``
    (pricing stored on the AIModel), ``provider`` (the upstream reported the
    request's actual cost in its usage payload; authoritative over any
    catalog estimate), ``catalog`` (Preloop's vendored price snapshot, which
    may fall back to litellm's bundled map for models absent from the
    snapshot), ``unpriced`` (no price could be resolved; ``cost`` is None).
    """

    cost: Optional[float]
    source: str


def provider_reported_cost(
    usage_details: Optional[Dict[str, Any]],
) -> Optional[float]:
    """Extract the upstream-reported request cost from a usage payload.

    OpenRouter (with usage accounting enabled) returns the request's actual
    charge inside the response ``usage`` object:

    - ``usage.cost``: credits charged by OpenRouter for the request. On BYOK
      requests this is only OpenRouter's fee.
    - ``usage.cost_details.upstream_inference_cost``: what the upstream vendor
      charged. On BYOK requests ``cost`` excludes it, so the customer pays
      both and their sum is the authoritative total. On credits-based
      requests it is informational and duplicates ``cost`` (live-verified,
      issue #224), so summing would double-count.

    Shape discrimination: when the payload carries an explicit ``is_byok``
    flag (top-level or inside ``cost_details``) it is authoritative — sum
    for BYOK, ``cost`` alone otherwise. Without the flag, fall back to the
    magnitude heuristic: sum only when ``cost < upstream_inference_cost``
    (BYOK: a small fee next to the vendor charge); otherwise ``cost`` alone
    is the total. Zero and negative values mean "not accounted" (the Auto
    Router's catalog price is literally ``-1``), never a real charge, so
    they are treated as absent.

    Args:
        usage_details: Raw provider usage dict from the response, if any.

    Returns:
        The provider-reported USD cost, or None when the payload carries no
        usable cost information.
    """
    if not isinstance(usage_details, dict):
        return None

    def _positive_number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if value > 0 else None

    cost_details = usage_details.get("cost_details")
    upstream_cost = (
        _positive_number(cost_details.get("upstream_inference_cost"))
        if isinstance(cost_details, dict)
        else None
    )
    gateway_cost = _positive_number(usage_details.get("cost"))

    if upstream_cost is None and gateway_cost is None:
        return None
    if gateway_cost is not None and upstream_cost is not None:
        # BYOK shape: cost is OpenRouter's fee, strictly below the vendor
        # charge it excludes -> the customer pays the sum. Credits shape:
        # cost already includes the upstream charge (cost_details merely
        # echoes it) -> cost alone is the total (#224). An explicit
        # is_byok flag from the provider is authoritative over the
        # magnitude heuristic (covers the rare BYOK request whose fee
        # meets or exceeds the vendor charge).
        is_byok = usage_details.get("is_byok")
        if not isinstance(is_byok, bool):
            is_byok = (
                cost_details.get("is_byok") if isinstance(cost_details, dict) else None
            )
        if isinstance(is_byok, bool):
            total = gateway_cost + upstream_cost if is_byok else gateway_cost
        else:
            total = (
                gateway_cost + upstream_cost
                if gateway_cost < upstream_cost
                else gateway_cost
            )
    else:
        total = gateway_cost if gateway_cost is not None else upstream_cost
    # Keep more precision than the catalog's round(6): provider-reported
    # micro-costs (e.g. 1.946e-05) would otherwise collapse to one digit,
    # and live OpenRouter charges carry 12 decimal places (#224:
    # 0.000001979964 must round-trip, not flatten to 0.00000198).
    return round(total, 12)


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
    """Estimate usage cost and report which pricing source produced it.

    Resolution order: an operator's explicit pricing (account override or
    pricing configured on the model) wins first — it is a deliberate human
    decision (e.g. amortizing a subscription). Next the provider-reported
    cost from the response usage payload wins over any catalog estimate: it
    is the upstream's own ledger figure, exact where the catalog can only
    approximate (and the Auto Router has no catalog price at all). The
    catalog is the fallback, and ``unpriced`` means nothing could price the
    request.
    """
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

    reported_cost = provider_reported_cost(usage_details)
    if reported_cost is not None:
        return CostEstimate(cost=reported_cost, source="provider")

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
            except Exception:  # noqa: BLE001 - litellm cost functions raise various types
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
        except Exception:  # noqa: BLE001 - litellm cost functions raise various types
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
    endpoint_prefix = _endpoint_prefix(getattr(ai_model, "api_endpoint", None))

    candidates = []
    gateway_alias = gateway_config.get("model_alias")
    if isinstance(gateway_alias, str) and gateway_alias.strip():
        candidates.append(gateway_alias.strip())

    if model_identifier:
        candidates.append(model_identifier)
        # Routing maps qwen -> openai (DashScope compatible-mode). Pricing
        # keys live under dashscope/<id> in the vendored catalog.
        prefix = _pricing_provider_prefix(provider)
        bare_identifier = _strip_synthetic_prefix(model_identifier)
        if "/" not in bare_identifier and prefix not in _SYNTHETIC_PROVIDER_PREFIXES:
            candidates.append(f"{prefix}/{bare_identifier}")

    seen = set()
    for candidate in candidates:
        for expanded in _expand_candidate(candidate.strip(), provider, endpoint_prefix):
            normalized = normalize_gateway_model_alias(expanded) or expanded.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            yield normalized
