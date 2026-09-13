"""Region-scoped Alibaba Model Studio list-price estimates, never invoice cost.

Singapore International USD seed: ``data/alibaba_international_prices.json``,
from the public pricing page. The native ``GET /api/v1/models`` overlay
(see ``alibaba_price_catalog``) keeps that map current, including cache rows
from the same USD site response. Chat completions still report tokens only.

Beijing and other CNY sites stay unpriced in USD accounting. Time-banded
SKUs stay unpriced until a dedicated adapter exists. Do not substitute a
native DeepSeek/Z.ai/Moonshot price for an Alibaba-hosted model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from preloop.models import models
from preloop.services.litellm_routing import is_openrouter_model

SEED_PATH = (
    Path(__file__).resolve().parent / "data" / "alibaba_international_prices.json"
)


@dataclass(frozen=True)
class Tariff:
    """USD amounts per million tokens for one serving tariff or tier."""

    input: float
    output: float
    implicit_read: float | None = None
    explicit_read: float | None = None
    creation: float | None = None
    max_input: int | None = None
    tiers: tuple["Tariff", ...] = ()


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


def usd_region(ai_model: models.AIModel) -> str | None:
    """Return the USD overlay/seed region, or none when currency is unverified."""
    host = _host(ai_model)
    if host == "dashscope-intl.aliyuncs.com" or host.endswith(
        ".ap-southeast-1.maas.aliyuncs.com"
    ):
        return "singapore-international"
    if host.endswith(".us-east-1.maas.aliyuncs.com"):
        return "united-states"
    return None


def _tariff_from_seed_entry(entry: dict[str, Any]) -> Tariff | None:
    raw_tiers = entry.get("tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return None
    implicit = _optional_rate(entry.get("implicit_read"))
    explicit = _optional_rate(entry.get("explicit_read"))
    creation = _optional_rate(entry.get("creation"))
    parsed: list[Tariff] = []
    for row in raw_tiers:
        if not isinstance(row, dict):
            continue
        try:
            inp = float(row["input"])
            out = float(row["output"])
        except (KeyError, TypeError, ValueError):
            continue
        if inp < 0 or out < 0:
            continue
        max_input = row.get("max_input")
        parsed.append(
            Tariff(
                input=inp,
                output=out,
                implicit_read=_optional_rate(row.get("implicit_read")) or implicit,
                explicit_read=_optional_rate(row.get("explicit_read")) or explicit,
                creation=_optional_rate(row.get("creation")) or creation,
                max_input=int(max_input) if isinstance(max_input, int) else None,
            )
        )
    if not parsed:
        return None
    first = parsed[0]
    return Tariff(
        input=first.input,
        output=first.output,
        implicit_read=first.implicit_read,
        explicit_read=first.explicit_read,
        creation=first.creation,
        max_input=first.max_input,
        tiers=tuple(parsed) if len(parsed) > 1 else (),
    )


def _optional_rate(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0 or math.isnan(rate):
        return None
    return rate


def _load_seed() -> dict[str, Tariff]:
    try:
        payload = json.loads(SEED_PATH.read_text())
    except (OSError, ValueError):
        return {}
    models_raw = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models_raw, dict):
        return {}
    loaded: dict[str, Tariff] = {}
    for ident, entry in models_raw.items():
        if not isinstance(ident, str) or not isinstance(entry, dict):
            continue
        tariff = _tariff_from_seed_entry(entry)
        if tariff is not None:
            loaded[ident.strip()] = tariff
    return loaded


_SEED = _load_seed()


def tariff_for(ai_model: models.AIModel) -> Tariff | None:
    """Resolve an exact SKU on a USD Alibaba host.

    Live native-catalog overlay wins. The Singapore International seed covers
    models before the first refresh. Other USD regions require a live overlay.
    """
    region = usd_region(ai_model)
    if region is None:
        return None
    ident = (ai_model.model_identifier or "").strip()
    if not ident:
        return None
    from preloop.services.alibaba_price_catalog import live_tariff

    live = live_tariff(ai_model)
    if live is not None:
        return live
    if region == "singapore-international":
        return _SEED.get(ident)
    return None


def catalog_entry(ai_model: models.AIModel) -> tuple[str, dict[str, Any]] | None:
    """Expose the same scoped list tariff in the model pricing view."""
    tariff = tariff_for(ai_model)
    if tariff is None:
        return None
    region = usd_region(ai_model) or "singapore-international"
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
    prefix = (
        "alibaba/native-catalog" if _is_live(ai_model, tariff) else f"alibaba/{region}"
    )
    return f"{prefix}/{ai_model.model_identifier}", entry


def _is_live(ai_model: models.AIModel, tariff: Tariff) -> bool:
    from preloop.services.alibaba_price_catalog import live_tariff

    live = live_tariff(ai_model)
    return live is tariff


def select_tier(tariff: Tariff, prompt_tokens: int) -> Tariff | None:
    """Pick the whole-request input-length tier, or none if out of range."""
    if tariff.tiers:
        matching = [
            tier
            for tier in tariff.tiers
            if tier.max_input is None or prompt_tokens <= tier.max_input
        ]
        if not matching:
            return None
        matching.sort(key=lambda tier: tier.max_input or 10**18)
        return matching[0]
    if tariff.max_input is not None and prompt_tokens > tariff.max_input:
        return None
    return tariff


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
    resolved = tariff_for(ai_model)
    if resolved is None:
        return None
    tariff = select_tier(resolved, prompt_tokens)
    if tariff is None:
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
