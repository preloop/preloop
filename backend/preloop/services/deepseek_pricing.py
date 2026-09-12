"""Dated native DeepSeek USD tariffs, with explicit estimation provenance.

These rates apply only to api.deepseek.com. Marketplace and cloud sellers
set their own tariffs. Public-holiday exemptions are not precisely specified
by the provider, so weekday peak estimates conservatively omit that exemption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any
from urllib.parse import urlsplit

from preloop.models import models

PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
AUGUST_START = datetime(2026, 8, 16, 16, tzinfo=timezone.utc)
SEPTEMBER_START = datetime(2026, 9, 10, 4, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DeepSeekTariff:
    """The selected dated tariff; prices are USD per million tokens."""

    model: str
    observed_at: datetime
    effective_from: datetime
    band: str
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float
    weekday_schedule: bool
    reviewed_provenance: dict[str, Any] | None = None

    def metadata(self) -> dict[str, Any]:
        """Return immutable calculation inputs for a usage-row snapshot."""
        snapshot = {
            "provider": "deepseek",
            "model": self.model,
            "currency": "USD",
            "policy_version": "deepseek-native-2026-09-12",
            "source_url": (
                "https://api-docs.deepseek.com/img/v4_260813_price_en.png"
                if self.effective_from < SEPTEMBER_START
                else PRICING_URL
            ),
            "source_verified_at": "2026-09-12T17:12:53+00:00",
            "effective_from": self.effective_from.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "rate_band": self.band,
            "input_per_1m": self.input_per_1m,
            "output_per_1m": self.output_per_1m,
            "cached_input_per_1m": self.cached_input_per_1m,
            "peak_hours_utc": [[1, 4], [6, 10]],
            "peak_weekdays": [0, 1, 2, 3, 4]
            if self.weekday_schedule
            else [0, 1, 2, 3, 4, 5, 6],
            "estimate_limitations": [
                "Provider public-holiday calendar is unspecified; weekday peak "
                "rates may overestimate exempt holidays."
            ]
            if self.weekday_schedule
            else [],
        }
        if self.model == "deepseek-v4-pro" and self.weekday_schedule:
            snapshot["estimate_limitations"].append(
                "The current shared pricing page specifies weekday peaks; its "
                "historical effective instant for Pro is not explicitly stated. "
                "This estimate applies the September 10 schedule transition."
            )
        if self.reviewed_provenance:
            snapshot.update(
                source_url=self.reviewed_provenance.get("source_url"),
                source_verified_at=self.reviewed_provenance.get("verified_at"),
                policy_version=self.reviewed_provenance.get("revision"),
                reviewed_feed=self.reviewed_provenance,
            )
        return snapshot

    def estimate(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        usage_details: dict[str, Any] | None,
    ) -> float:
        """Price reported cache reads separately, conservatively if absent."""
        details = usage_details or {}
        nested = details.get("prompt_tokens_details")
        nested = nested if isinstance(nested, dict) else {}
        cached = nested.get("cached_tokens")
        if cached is None:
            cached = details.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = details.get("cache_read_input_tokens")
        try:
            reads = min(max(int(cached or 0), 0), max(prompt_tokens, 0))
        except (TypeError, ValueError, OverflowError):
            reads = 0
        return round(
            (
                (max(prompt_tokens, 0) - reads) * self.input_per_1m
                + reads * self.cached_input_per_1m
                + max(completion_tokens, 0) * self.output_per_1m
            )
            / 1_000_000,
            12,
        )


def native_tariff(
    ai_model: models.AIModel, *, observed_at: datetime | None = None
) -> DeepSeekTariff | None:
    """Select a native tariff by actual endpoint, model and request time.

    Earlier periods and unknown model identifiers retain the existing catalog
    fallback. The September Pro retirement was rescinded; Pro stays Pro.
    """
    endpoint = (ai_model.api_endpoint or "").strip()
    provider = (ai_model.provider_name or "").strip().lower()
    if endpoint:
        try:
            host = urlsplit(endpoint).hostname
        except ValueError:
            return None
        if host != "api.deepseek.com":
            return None
    elif provider != "deepseek":
        return None
    identifier = (ai_model.model_identifier or "").strip().lower()
    for prefix in ("preloop/", "deepseek/"):
        if identifier.startswith(prefix):
            identifier = identifier[len(prefix) :]
    moment = observed_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment < AUGUST_START:
        return None
    september = moment >= SEPTEMBER_START
    if identifier == "deepseek-v4-pro":
        canonical, rates = "deepseek-v4-pro", (0.044, 1.32, 3.96)
    elif identifier in {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}:
        canonical = "deepseek-flash" if september else identifier
        rates = (0.006, 0.30, 1.20) if september else (0.014, 0.44, 1.32)
    elif identifier == "deepseek-flash" and september:
        canonical, rates = "deepseek-flash", (0.006, 0.30, 1.20)
    else:
        return None
    peak_hour = 1 <= moment.hour < 4 or 6 <= moment.hour < 10
    peak = peak_hour and (not september or moment.weekday() < 5)
    multiplier = 1 if peak else 0.5
    baseline = DeepSeekTariff(
        model=canonical,
        observed_at=moment,
        effective_from=SEPTEMBER_START if september else AUGUST_START,
        band="peak" if peak else "off_peak",
        input_per_1m=rates[1] * multiplier,
        output_per_1m=rates[2] * multiplier,
        cached_input_per_1m=rates[0] * multiplier,
        weekday_schedule=september,
    )

    return _reviewed_tariff(identifier, baseline) or baseline


def _reviewed_tariff(
    identifier: str, baseline: DeepSeekTariff
) -> DeepSeekTariff | None:
    """Read a validated reviewed policy while keeping historical built-in rates.

    Validation is repeated defensively for the small supported policy shape;
    arbitrary LiteLLM/source fields must not silently become billing policies.
    """
    import litellm

    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    keys = dict.fromkeys(
        (
            f"deepseek/{baseline.model}",
            baseline.model,
            f"deepseek/{identifier}",
            identifier,
        )
    )
    if baseline.model == "deepseek-flash":
        # Existing catalogs still use the retired native Flash identifiers.
        # They are one serving tariff after the September alias transition.
        for alias in ("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"):
            keys.setdefault(f"deepseek/{alias}", None)
            keys.setdefault(alias, None)
    for key in keys:
        entry = litellm.model_cost.get(key)
        if not isinstance(entry, dict):
            continue
        policy = entry.get("preloop_price_policy")
        provenance = entry.get("preloop_price_provenance")
        if isinstance(policy, dict) and isinstance(provenance, dict):
            candidates.append((policy, provenance))
        history = entry.get("preloop_price_policy_history")
        if isinstance(history, list):
            for version in history:
                if (
                    isinstance(version, dict)
                    and isinstance(version.get("policy"), dict)
                    and isinstance(version.get("provenance"), dict)
                ):
                    candidates.append((version["policy"], version["provenance"]))
    selected: DeepSeekTariff | None = None
    for policy, provenance in candidates:
        if (
            policy.get("kind") != "deepseek_utc_bands"
            or policy.get("peak_hours_utc") != [[1, 4], [6, 10]]
            or policy.get("peak_weekdays") != [0, 1, 2, 3, 4]
            or policy.get("public_holidays") != "unspecified"
        ):
            continue
        try:
            effective = datetime.fromisoformat(policy["effective_from"])
            if effective.tzinfo is None:
                continue
            effective = effective.astimezone(timezone.utc)
            if effective < SEPTEMBER_START or baseline.observed_at < effective:
                continue
            band = policy[baseline.band]
            rates = [
                band[field]
                for field in ("cached_input_per_1m", "input_per_1m", "output_per_1m")
            ]
            if not all(
                isinstance(rate, (float, int))
                and not isinstance(rate, bool)
                and math.isfinite(rate)
                and rate >= 0
                for rate in rates
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if selected is not None and selected.effective_from >= effective:
            continue
        selected = DeepSeekTariff(
            model=baseline.model,
            observed_at=baseline.observed_at,
            effective_from=effective,
            band=baseline.band,
            cached_input_per_1m=rates[0],
            input_per_1m=rates[1],
            output_per_1m=rates[2],
            weekday_schedule=True,
            reviewed_provenance=dict(provenance),
        )
    return selected
