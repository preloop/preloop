"""Refresh current estimates from an operator-controlled, reviewed price feed.

This service never discovers providers, changes account overrides, or writes usage
rows. Every serving process polls independently; one dictionary replacement makes
the validated price set visible together. Published pages are evidence, not an
executable scraping policy. Only flat token rates and the known native DeepSeek UTC-band policy are
supported; new policy structures need estimator support before publication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)
MAX_FEED_BYTES = 2_000_000
DEEPSEEK_MODELS = frozenset(
    {
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-flash",
    }
)
PRICE_FIELDS = frozenset(
    {
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
    }
)


class PriceRefreshCompatibilityError(RuntimeError):
    """LiteLLM cannot safely invalidate estimates after a price-map change."""


def validate_https_url(value: str) -> str:
    """Require HTTPS without embedded credentials or fragments."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Expected an HTTPS URL without credentials or fragment")
    return value


class DeepSeekBand(BaseModel):
    """One bounded USD-per-million rate band."""

    model_config = ConfigDict(extra="forbid", strict=True)
    input_per_1m: float
    output_per_1m: float
    cached_input_per_1m: float

    @field_validator("input_per_1m", "output_per_1m", "cached_input_per_1m")
    @classmethod
    def valid_rate(cls, value: float) -> float:
        """Reject invalid or impractically large published rate numbers."""
        if not math.isfinite(value) or not 0 <= value <= 1_000_000:
            raise ValueError("Rates must be finite nonnegative USD values")
        return value


class DeepSeekPolicy(BaseModel):
    """Only the already-supported native DeepSeek UTC rate-band structure."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["deepseek_utc_bands"]
    effective_from: datetime
    peak: DeepSeekBand
    off_peak: DeepSeekBand
    peak_hours_utc: list[tuple[int, int]]
    peak_weekdays: list[int]
    public_holidays: Literal["unspecified"]

    @field_validator("effective_from")
    @classmethod
    def valid_effective_date(cls, value: datetime) -> datetime:
        """Normalize instant identity and reject schedules predating support."""
        if value.tzinfo is None:
            raise ValueError("DeepSeek effective date needs a timezone")
        value = value.astimezone(timezone.utc)
        if value < datetime(2026, 9, 10, 4, tzinfo=timezone.utc):
            raise ValueError("DeepSeek weekday policy predates supported schedule")
        return value

    @field_validator("peak_hours_utc")
    @classmethod
    def valid_hours(cls, value: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Accept bounded, non-overlapping UTC hour intervals."""
        if not value or any(not 0 <= start < end <= 24 for start, end in value):
            raise ValueError("Invalid UTC hour interval")
        if value != [(1, 4), (6, 10)]:
            raise ValueError("Unsupported DeepSeek UTC peak schedule")
        return value

    @field_validator("peak_weekdays")
    @classmethod
    def valid_weekdays(cls, value: list[int]) -> list[int]:
        """Weekday zero is Monday, six is Sunday."""
        if value != [0, 1, 2, 3, 4]:
            raise ValueError("Unsupported DeepSeek weekday schedule")
        return value


class HistoricalPolicy(BaseModel):
    """A previously reviewed revision carried with a newer feed."""

    model_config = ConfigDict(extra="forbid")
    policy: DeepSeekPolicy
    provenance: dict[str, Any]


class ReviewedPrice(BaseModel):
    """An explicitly reviewed USD price with first-party evidence."""

    model_config = ConfigDict(extra="forbid")
    policy: Literal["flat_per_token", "deepseek_utc_bands"]
    source_url: str
    verified_at: datetime
    effective_from: datetime
    prices: dict[str, float] | None = None
    price_policy: DeepSeekPolicy | None = None
    price_policy_history: list[HistoricalPolicy] = Field(
        default_factory=list, max_length=100
    )

    _source_url = field_validator("source_url")(validate_https_url)

    @field_validator("prices", mode="before")
    @classmethod
    def valid_prices(cls, value: Any) -> Any:
        """Reject incomplete, non-finite, negative, and unsupported prices."""
        if value is None:
            return value
        if not isinstance(value, dict) or not {
            "input_cost_per_token",
            "output_cost_per_token",
        }.issubset(value):
            raise ValueError("Input and output prices are required")
        if set(value) - PRICE_FIELDS:
            raise ValueError("Unsupported price policy or field")
        for price in value.values():
            if (
                isinstance(price, bool)
                or not isinstance(price, (float, int))
                or price < 0
                or price > 1e6
                or not math.isfinite(price)
            ):
                raise ValueError("Prices must be finite nonnegative numbers")
        return value


class ReviewedPriceFeed(BaseModel):
    """The published artifact accepted by the runtime and build command."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    currency: Literal["USD"]
    revision: str
    published_at: datetime
    expires_at: datetime
    models: dict[str, ReviewedPrice]


def validate_feed(payload: Any, *, now: datetime | None = None) -> ReviewedPriceFeed:
    """Validate provenance and dates without mutating any runtime prices."""
    feed = ReviewedPriceFeed.model_validate(payload)
    clock = now or datetime.now(timezone.utc)
    if not feed.revision.strip() or not feed.models:
        raise ValueError("A revision and at least one model are required")
    stamps = [feed.published_at, feed.expires_at]
    for entry in feed.models.values():
        stamps.extend([entry.verified_at, entry.effective_from])
    if any(stamp.tzinfo is None for stamp in stamps):
        raise ValueError("All timestamps must include a timezone")
    if not feed.published_at <= clock < feed.expires_at:
        raise ValueError("Feed is expired or not yet published")
    if (feed.expires_at - feed.published_at).total_seconds() > 31 * 86400:
        raise ValueError("Feed validity must not exceed 31 days")
    for entry in feed.models.values():
        if entry.verified_at > feed.published_at:
            raise ValueError("Evidence cannot postdate publication")
        if entry.policy == "flat_per_token":
            if entry.effective_from > entry.verified_at:
                raise ValueError("Flat prices must already be effective when verified")
            if (
                entry.prices is None
                or entry.price_policy is not None
                or entry.price_policy_history
            ):
                raise ValueError("Flat policy requires prices only")
        elif (
            entry.prices is not None
            or entry.price_policy is None
            or entry.price_policy.effective_from != entry.effective_from
        ):
            raise ValueError("DeepSeek policy requires matching dated policy data only")
        if (feed.published_at - entry.verified_at).total_seconds() > 14 * 86400:
            raise ValueError("Provider evidence is more than 14 days old")
        for historical in entry.price_policy_history:
            provenance = historical.provenance
            validate_https_url(provenance.get("source_url", ""))
            if not provenance.get("revision"):
                raise ValueError("Historical policy revision is required")
            verified = datetime.fromisoformat(provenance.get("verified_at", ""))
            effective = datetime.fromisoformat(provenance.get("effective_from", ""))
            if (
                verified.tzinfo is None
                or effective.tzinfo is None
                or verified > feed.published_at
                or effective != historical.policy.effective_from
                or effective >= entry.effective_from
            ):
                raise ValueError("Invalid historical policy provenance or dates")
    return feed


def _policy_history(
    existing: dict[str, Any], entry: ReviewedPrice
) -> list[dict[str, Any]]:
    """Preserve dated revisions and reject silent changes to a prior tariff."""
    assert entry.price_policy is not None
    current = entry.price_policy.model_dump(mode="json")
    current_date = entry.price_policy.effective_from.isoformat()
    history = list(existing.get("preloop_price_policy_history", []))
    if existing.get("preloop_price_policy") and isinstance(
        existing.get("preloop_price_provenance"), dict
    ):
        history.append(
            {
                "policy": existing["preloop_price_policy"],
                "provenance": existing["preloop_price_provenance"],
            }
        )
    history.extend(item.model_dump(mode="json") for item in entry.price_policy_history)
    policies = {current_date: current}
    retained: dict[str, dict[str, Any]] = {}
    for item in history:
        policy = DeepSeekPolicy.model_validate(item["policy"])
        normalized = policy.model_dump(mode="json")
        date = policy.effective_from.isoformat()
        if date in policies and policies[date] != normalized:
            raise ValueError("Cannot mutate a previously reviewed tariff")
        policies[date] = normalized
        if date != current_date:
            retained[date] = {**item, "policy": normalized}
    if len(retained) > 100:
        raise ValueError("Historical policy limit exceeded")
    return [retained[key] for key in sorted(retained)]


class ReviewedPriceRefresher:
    """One lifecycle-owned polling task for a process's LiteLLM price map."""

    def __init__(
        self, *, url: str, allowed_models: list[str], interval_seconds: int
    ) -> None:
        self.url = validate_https_url(url)
        self.allowed_models = frozenset(allowed_models)
        if not self.allowed_models:
            raise ValueError("Reviewed price refresh needs an explicit model allowlist")
        self.interval_seconds = max(60, interval_seconds)
        self.task: asyncio.Task[None] | None = None
        self.applied_at: datetime | None = None
        self.digest: str | None = None

    def apply(self, payload: Any) -> int:
        """Atomically replace supported existing prices after full validation."""
        import hashlib

        import litellm

        feed = validate_feed(payload)
        digest = hashlib.sha256(feed.model_dump_json().encode("utf-8")).hexdigest()
        if (
            self.applied_at is not None
            and feed.published_at <= self.applied_at
            and self.digest != digest
        ):
            raise ValueError("Refusing older or mutated published price revision")
        if set(feed.models) - self.allowed_models:
            raise ValueError("Feed contains models outside the operator allowlist")
        from litellm import utils as litellm_utils

        # This private helper clears LiteLLM's derived model-info caches. A
        # map-only or register_model fallback could leave warmed prices stale.
        invalidate = getattr(
            litellm_utils, "_invalidate_model_cost_lowercase_map", None
        )
        if not callable(invalidate):
            raise PriceRefreshCompatibilityError(
                "LiteLLM price caches are incompatible"
            )
        # Capture supported LRU clearers for rollback if a callable helper
        # fails after the map swap. Names differ between LiteLLM versions.
        rollback_clearers = [
            clear
            for name in (
                "get_model_info",
                "_cached_get_model_info",
                "_cached_get_model_info_helper",
            )
            if callable(
                clear := getattr(
                    getattr(litellm_utils, name, None), "cache_clear", None
                )
            )
        ]

        from preloop.services.model_price_catalog import _lock as catalog_lock

        with catalog_lock:
            changed = 0
            updated = dict(litellm.model_cost)
            for model, entry in feed.models.items():
                existing = updated.get(model)
                if not isinstance(existing, dict):
                    raise ValueError("Feed may only refresh existing catalog models")
                native_key = (
                    existing.get("litellm_provider") == "deepseek"
                    and model.removeprefix("deepseek/") in DEEPSEEK_MODELS
                )
                if entry.policy == "flat_per_token" and any(
                    "cost" in key and key not in PRICE_FIELDS for key in existing
                ):
                    raise ValueError(
                        "Existing model has a policy requiring dedicated support"
                    )
                if entry.policy == "flat_per_token":
                    if native_key or existing.get("preloop_price_policy"):
                        raise ValueError(
                            "Cannot replace a native policy with flat rates"
                        )
                    replacement = {
                        key: value
                        for key, value in existing.items()
                        if key not in PRICE_FIELDS
                    }
                    replacement.update(entry.prices or {})
                else:
                    if not native_key:
                        raise ValueError(
                            "Native DeepSeek policy requires a direct model"
                        )
                    replacement = dict(existing)
                    assert entry.price_policy is not None
                    replacement["preloop_price_policy"] = entry.price_policy.model_dump(
                        mode="json"
                    )
                    replacement["preloop_price_policy_history"] = _policy_history(
                        existing, entry
                    )
                replacement["preloop_price_provenance"] = {
                    "revision": feed.revision,
                    "published_at": feed.published_at.isoformat(),
                    "expires_at": feed.expires_at.isoformat(),
                    "source_url": entry.source_url,
                    "verified_at": entry.verified_at.isoformat(),
                    "effective_from": entry.effective_from.isoformat(),
                }
                changed += replacement != existing
                updated[model] = replacement
            if not changed:
                return 0
            # Probe before publishing: signature/internal API incompatibility
            # normally fails here while all prices still use the old map.
            try:
                invalidate()
            except Exception:  # noqa: BLE001 - dependency callback compatibility
                raise PriceRefreshCompatibilityError(
                    "LiteLLM price caches are incompatible"
                ) from None
            previous = litellm.model_cost
            litellm.model_cost = updated
            try:
                invalidate()
            except Exception:  # noqa: BLE001 - roll back before reporting failure
                litellm.model_cost = previous
                for clear in rollback_clearers:
                    try:
                        clear()
                    except Exception:  # noqa: BLE001 - try remaining compatible caches
                        logger.warning(
                            "Reviewed price rollback could not clear a LiteLLM cache"
                        )
                raise PriceRefreshCompatibilityError(
                    "LiteLLM price caches are incompatible"
                ) from None
        self.applied_at = feed.published_at
        self.digest = digest
        logger.info("Applied reviewed model price feed: %d models", len(feed.models))
        return changed

    async def refresh(self, client: httpx.AsyncClient) -> int:
        """Download a bounded feed; redirects and invalid payloads fail closed."""
        chunks = bytearray()
        async with client.stream("GET", self.url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_FEED_BYTES:
                    raise ValueError("Reviewed price feed exceeds size limit")
        return self.apply(json.loads(chunks))

    async def run(self) -> None:
        """Poll until shutdown, retaining the last good prices on failures."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            while True:
                try:
                    await self.refresh(client)
                except PriceRefreshCompatibilityError:
                    logger.warning(
                        "Reviewed price refresh unavailable: incompatible LiteLLM "
                        "cache API; retaining last good prices"
                    )
                except (httpx.HTTPError, ValueError, TypeError):
                    # Do not log the configured URL or fetched body: operator
                    # endpoints may carry access tokens in query parameters.
                    logger.warning(
                        "Reviewed price refresh failed; retaining last good prices"
                    )
                await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        """Start at most one task for this lifecycle owner."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name="reviewed-model-prices")

    async def stop(self) -> None:
        """Cancel polling and close its HTTP client before lifecycle exit."""
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None


def start_reviewed_price_refresh() -> ReviewedPriceRefresher | None:
    """Start only when the deployment explicitly configures a reviewed feed."""
    from preloop.config import settings

    if not settings.model_price_refresh_url:
        return None
    try:
        refresher = ReviewedPriceRefresher(
            url=settings.model_price_refresh_url,
            allowed_models=settings.model_price_refresh_allowed_models,
            interval_seconds=settings.model_price_refresh_interval_seconds,
        )
    except (ValueError, TypeError):
        # Never include the URL or exception text: configuration can contain
        # query-string tokens, embedded credentials, or private hostnames.
        logger.warning("Reviewed price refresh disabled: invalid configuration")
        return None
    refresher.start()
    return refresher
