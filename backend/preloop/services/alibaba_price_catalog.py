"""Live Alibaba Model Studio native-catalog prices, cached in-process.

The documented native ``GET /api/v1/models`` response includes per-SKU
tariffs. Chat completions do not. This module is the keep-current path:
Fetch Models, Fetch price, and unpriced-row lookup refresh the overlay.
Estimates remain list prices, never invoices.

Currency is asserted from the serving region (Singapore International and
US workspace native catalogs are USD). Beijing is not ingested into USD
accounting. Time-banded rows are skipped until a dedicated adapter exists.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from enum import Enum
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from preloop.models import models
from preloop.services.alibaba_pricing import (
    Tariff,
    _host,
    is_alibaba,
)

logger = logging.getLogger(__name__)

SINGAPORE_NATIVE_URL = "https://dashscope-intl.aliyuncs.com/api/v1/models"
NATIVE_TIMEOUT_SECONDS = 15.0
_PAGE_SIZE = 100
_MAX_PAGES = 10

_TOKEN_UNITS = {
    "per 1m tokens",
    "per million tokens",
    "per 1 million tokens",
}

_lock = threading.Lock()
# region -> {model_id: Tariff}
_live: dict[str, dict[str, Tariff]] = {}


class CatalogRefreshStatus(str, Enum):
    """Outcome of a native-catalog overlay refresh."""

    ingested = "ingested"
    no_target = "no_target"
    no_credentials = "no_credentials"
    host_mismatch = "host_mismatch"
    unreachable = "unreachable"
    empty = "empty"


def reset_live_state_for_tests() -> None:
    """Drop the in-process overlay (test isolation only)."""
    with _lock:
        _live.clear()


def native_catalog_target(ai_model: models.AIModel) -> tuple[str, str] | None:
    """Return the documented native catalog URL and service_site, or none.

    Singapore classic keys use the documented classic Singapore native host.
    Singapore workspace hosts still name that documented catalog URL so
    Fetch price can explain a host mismatch; refresh will not send a
    workspace key there. US workspace native is the workspace host itself.
    Classic ``dashscope-us`` has no documented native catalog URL and is not
    guessed.
    """
    if not is_alibaba(ai_model):
        return None
    host = _host(ai_model)
    if host == "dashscope-intl.aliyuncs.com" or host.endswith(
        ".ap-southeast-1.maas.aliyuncs.com"
    ):
        return SINGAPORE_NATIVE_URL, "international"
    if host.endswith(".us-east-1.maas.aliyuncs.com"):
        return f"https://{host}/api/v1/models", "united-states"
    return None


def region_key(service_site: str) -> str:
    """Stable overlay key for a USD service site."""
    if service_site == "united-states":
        return "united-states"
    return "singapore-international"


def live_tariff(ai_model: models.AIModel) -> Tariff | None:
    """Return a live overlay tariff for this exact model id, if present."""
    target = native_catalog_target(ai_model)
    if target is None:
        return None
    ident = (ai_model.model_identifier or "").strip()
    if not ident:
        return None
    key = region_key(target[1])
    with _lock:
        return _live.get(key, {}).get(ident)


def ingest_native_models(
    entries: Iterable[Any],
    *,
    region: str = "singapore-international",
    replace: bool = False,
) -> int:
    """Merge or replace native catalog rows in the in-process overlay.

    Partial discovery ingest (Fetch Models) merges so a later page can
    add SKUs without dropping earlier ones. A complete native download
    passes ``replace=True`` to wholesale-replace the region bucket so SKUs
    that left the catalog drop.

    Args:
        entries: ``output.models`` objects from one or more pages.
        region: Overlay key, normally ``singapore-international``.
        replace: When True, the region bucket becomes exactly these tariffs.

    Returns:
        Count of models with a usable USD token tariff.
    """
    incoming: dict[str, Tariff] = {}
    for entry in entries:
        tariff = parse_native_model(entry)
        if tariff is None:
            continue
        ident = str(entry.get("model") or "").strip()
        if not ident:
            continue
        incoming[ident] = tariff
    accepted = len(incoming)
    with _lock:
        if replace:
            _live[region] = incoming
        else:
            _live.setdefault(region, {}).update(incoming)
    return accepted


def parse_native_model(entry: Any) -> Tariff | None:
    """Parse one native catalog model into a USD token tariff.

    Image, audio, and other non-token units are ignored. A time_band on an
    input or output row drops that row; if no unbanded input/output remain,
    the model stays off the overlay.
    """
    if not isinstance(entry, dict):
        return None
    groups = entry.get("prices")
    if not isinstance(groups, list) or not groups:
        return None
    tiers: list[Tariff] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        tariff = _tariff_from_price_group(group)
        if tariff is not None:
            tiers.append(tariff)
    if not tiers:
        return None
    if len(tiers) == 1:
        return tiers[0]
    return Tariff(
        input=tiers[0].input,
        output=tiers[0].output,
        implicit_read=tiers[0].implicit_read,
        explicit_read=tiers[0].explicit_read,
        creation=tiers[0].creation,
        max_input=tiers[0].max_input,
        tiers=tuple(tiers),
    )


def _tariff_from_price_group(group: dict[str, Any]) -> Tariff | None:
    items = group.get("prices")
    if not isinstance(items, list):
        return None
    parsed: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("time_band") not in (None, "", "Default"):
            continue
        if not _is_token_unit(str(item.get("price_unit") or "")):
            continue
        kind = _price_type(str(item.get("type") or ""))
        if kind is None:
            continue
        try:
            amount = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if amount < 0 or math.isnan(amount):
            continue
        parsed[kind] = amount
    if "input" not in parsed or "output" not in parsed:
        return None
    return Tariff(
        input=parsed["input"],
        output=parsed["output"],
        implicit_read=parsed.get("implicit_read"),
        explicit_read=parsed.get("explicit_read"),
        creation=parsed.get("creation"),
        max_input=_parse_range_upper(str(group.get("range_name") or "")),
    )


def _is_token_unit(unit: str) -> bool:
    compact = " ".join(unit.strip().lower().replace("-", " ").split())
    return compact in _TOKEN_UNITS


def _price_type(raw: str) -> str | None:
    kind = raw.strip().lower()
    if kind == "input_token":
        return "input"
    if kind == "output_token":
        return "output"
    if kind in {"input_token_cache", "input_token_cache_implicit"}:
        return "implicit_read"
    if kind in {"input_token_cache_read", "input_token_cache_explicit"}:
        return "explicit_read"
    if kind in {
        "input_token_cache_creation_5m",
        "input_token_cache_creation",
    }:
        return "creation"
    return None


def _parse_range_upper(range_name: str) -> int | None:
    label = range_name.strip()
    if not label or label.lower() == "default":
        return None
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([KMkm]?)", label.replace(",", ""))
    if not parts:
        return None
    number, unit = parts[-1]
    value = float(number)
    suffix = unit.upper()
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    return int(value)


def _credential_host_matches_catalog(
    ai_model: models.AIModel, catalog_url: str
) -> bool:
    """True when this model's configured host is the catalog we would call."""
    catalog_host = (urlparse(catalog_url).hostname or "").lower()
    return bool(catalog_host) and _host(ai_model) == catalog_host


def refresh_from_model(ai_model: models.AIModel) -> CatalogRefreshStatus:
    """Fetch the native catalog with this model's key and refresh the overlay.

    Credentials are sent only when the model's configured host matches the
    catalog URL. A complete download wholesale-replaces that region's
    overlay; a failed or truncated download leaves existing SKUs in place
    (discovery still merges).

    Returns:
        Why the refresh ingested, failed, or stayed empty.
    """
    target = native_catalog_target(ai_model)
    if target is None:
        return CatalogRefreshStatus.no_target
    url, service_site = target
    if not _credential_host_matches_catalog(ai_model, url):
        logger.debug(
            "Alibaba native catalog refresh skipped: credentials belong to "
            "a different host than %s",
            urlparse(url).hostname,
        )
        return CatalogRefreshStatus.host_mismatch
    api_key = _api_key(ai_model)
    if not api_key:
        return CatalogRefreshStatus.no_credentials
    try:
        entries, complete = _download_catalog(url, api_key, service_site)
    except Exception:  # noqa: BLE001 - overlay refresh is best-effort
        logger.debug("Alibaba native catalog refresh failed", exc_info=True)
        return CatalogRefreshStatus.unreachable
    accepted = ingest_native_models(
        entries,
        region=region_key(service_site),
        replace=complete,
    )
    if accepted > 0:
        return CatalogRefreshStatus.ingested
    if complete:
        return CatalogRefreshStatus.empty
    return CatalogRefreshStatus.unreachable


def install_live_tariff(region: str, model_id: str, tariff: Tariff) -> None:
    """Install one overlay tariff (tests and Fetch Models)."""
    with _lock:
        _live.setdefault(region, {})[model_id] = tariff


def _download_catalog(
    url: str,
    api_key: str,
    service_site: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Download native catalog pages.

    Returns:
        ``(entries, complete)``. ``complete`` is True only when pagination
        reached a documented end. Auth, ``success: false``, and safety
        stops yield no entries and ``complete=False`` so the overlay is
        not wiped.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return [], False
    models_out: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, ...]] = set()
    complete = False
    with httpx.Client(timeout=NATIVE_TIMEOUT_SECONDS, follow_redirects=False) as client:
        for page_no in range(1, _MAX_PAGES + 1):
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={
                    "capabilities": "TG",
                    "service_site": service_site,
                    "language": "en-US",
                    "page_no": page_no,
                    "page_size": _PAGE_SIZE,
                },
            )
            if response.status_code in {401, 403}:
                return [], False
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                return [], False
            output = body.get("output")
            if not isinstance(output, dict) or not isinstance(
                output.get("models"), list
            ):
                return [], False
            entries = [row for row in output["models"] if isinstance(row, dict)]
            page_ids = tuple(str(row.get("model") or "") for row in entries)
            if page_ids in seen_pages:
                break
            seen_pages.add(page_ids)
            models_out.extend(entries)
            total = output.get("total")
            if not entries or (type(total) is int and page_no * _PAGE_SIZE >= total):
                complete = True
                break
            if len(entries) < _PAGE_SIZE and type(total) is not int:
                complete = True
                break
    return models_out, complete


def _legacy_api_key(ai_model: models.AIModel) -> str | None:
    """Return a non-empty legacy ``ai_model.api_key``, if present."""
    legacy = getattr(ai_model, "api_key", None)
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return None


def _api_key(ai_model: models.AIModel) -> str | None:
    try:
        from preloop.services.secret_service import get_secret_service

        resolved = get_secret_service().resolve_ai_model_credentials(
            ai_model, allow_refresh=False
        )
        if (
            resolved is not None
            and resolved.credential_type == "api_key"
            and isinstance(resolved.value, str)
            and resolved.value.strip()
        ):
            return resolved.value.strip()
    except Exception:  # noqa: BLE001 - optional secret backends
        logger.debug("Alibaba catalog could not resolve credentials", exc_info=True)
        return _legacy_api_key(ai_model)
    return _legacy_api_key(ai_model)
