"""Region-scoped Alibaba Model Studio list-price estimates, never invoice cost.

Standard USD prices: https://www.alibabacloud.com/help/en/model-studio/model-pricing
(Singapore, International, retrieved 2026-09-11). The native catalog API does
not declare currency. Its observed Qwen3.8 cache prices are deliberately NOT
used until their currency is confirmed; matching the standard input/output
numbers alone does not prove currency for every other billing item.

Other regions, unknown snapshots, time bands and unverified cache rates remain
unpriced. Do not substitute the same model's native-provider price. Coupons,
account discounts and free quota belong to delayed invoice reconciliation.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from preloop.models import models
from preloop.services.litellm_routing import is_openrouter_model


@dataclass(frozen=True)
class Tariff:
    """Verified USD amounts per million tokens for one serving tariff."""

    input: float
    output: float
    implicit_read: float | None = None
    explicit_read: float | None = None
    creation: float | None = None
    max_input: int | None = None


_SINGAPORE: dict[str, Tariff] = {
    "qwen3.8-max": Tariff(2, 6, max_input=1_000_000),
    # Snapshot standard prices are documented; cache rates are not assumed
    # identical to the moving alias without separate verification.
    "qwen3.8-max-0902": Tariff(2, 6, max_input=1_000_000),
    "deepseek-v4-pro": Tariff(2.4, 4.8),
    "deepseek-v4-flash": Tariff(0.2, 0.4, implicit_read=0.04),
    "glm-5.2": Tariff(1.4, 4.4, implicit_read=0.35),
    "kimi-k2.7-code": Tariff(0.95, 4),
    "kimi-k3": Tariff(3, 15),
}
# Cache ratios for DeepSeek Flash (20%) and GLM5.2 (25%):
# https://www.alibabacloud.com/help/en/model-studio/context-cache (2026-09-11).


def _host(ai_model: models.AIModel) -> str:
    endpoint = getattr(ai_model, "api_endpoint", None)
    if not isinstance(endpoint, str):
        return ""
    try:
        return (urlparse(endpoint.strip()).hostname or "").lower()
    except ValueError:
        return ""


def is_alibaba(ai_model: models.AIModel) -> bool:
    """Recognize provider identity or an actual Alibaba serving hostname."""
    if is_openrouter_model(ai_model):
        return False
    host = _host(ai_model)
    return (ai_model.provider_name or "").strip().lower() in {"qwen", "dashscope"} or (
        host
        in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
            "cn-hongkong.dashscope.aliyuncs.com",
        }
        or host.endswith(".maas.aliyuncs.com")
    )


def tariff_for(ai_model: models.AIModel) -> Tariff | None:
    """Resolve only an exact SKU on a verified Singapore International host."""
    host = _host(ai_model)
    if host != "dashscope-intl.aliyuncs.com" and not host.endswith(
        ".ap-southeast-1.maas.aliyuncs.com"
    ):
        return None
    return _SINGAPORE.get((ai_model.model_identifier or "").strip())


def catalog_entry(ai_model: models.AIModel) -> tuple[str, dict[str, Any]] | None:
    """Expose the same scoped list tariff in the model pricing view."""
    tariff = tariff_for(ai_model)
    if tariff is None:
        return None
    entry: dict[str, Any] = {
        "input_cost_per_token": tariff.input / 1_000_000,
        "output_cost_per_token": tariff.output / 1_000_000,
    }
    # The UI has one cached-input column. Do not collapse explicit and implicit
    # prices into one misleading number when they differ.
    if tariff.implicit_read is not None and tariff.explicit_read in (
        None,
        tariff.implicit_read,
    ):
        entry["cache_read_input_token_cost"] = tariff.implicit_read / 1_000_000
    return f"alibaba/singapore-international/{ai_model.model_identifier}", entry


def estimate(
    ai_model: models.AIModel,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    usage_details: dict[str, Any] | None,
) -> float | None:
    """Estimate known token classes; unknown tariff/mode yields no estimate.

    Prompt totals already include cached and creation tokens. Completion totals
    already include reasoning tokens, which must never be added a second time.
    The internal cache-mode tag comes from the forwarded request, not the model.
    """
    tariff = tariff_for(ai_model)
    if tariff is None or (tariff.max_input and prompt_tokens > tariff.max_input):
        return None
    usage = usage_details or {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return None
    creation_details = details.get("cache_creation") or {}
    if not isinstance(creation_details, dict):
        return None
    try:
        cached = int(details.get("cached_tokens") or 0)
        created = int(
            details.get("cache_creation_input_tokens")
            or details.get("cache_creation_tokens")
            or creation_details.get("ephemeral_5m_input_tokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if min(prompt_tokens, completion_tokens, cached, created) < 0:
        return None
    if cached + created > prompt_tokens:
        return None
    mode = usage.get("_preloop_cache_mode")
    read_rate = tariff.explicit_read if mode == "explicit" else tariff.implicit_read
    if cached and (mode not in {"implicit", "explicit"} or read_rate is None):
        return None
    if created and tariff.creation is None:
        return None
    return round(
        (
            (prompt_tokens - cached - created) * tariff.input
            + cached * (read_rate or 0)
            + created * (tariff.creation or 0)
            + completion_tokens * tariff.output
        )
        / 1_000_000,
        6,
    )
