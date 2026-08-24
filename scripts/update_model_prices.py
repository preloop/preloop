#!/usr/bin/env python
"""Refresh the vendored model-price catalog from litellm plus first-party overlays.

This is the standardized way to update Preloop's default model prices:

    python scripts/update_model_prices.py          # fetch, diff, rewrite
    python scripts/update_model_prices.py --check  # CI freshness gate

The catalog (``preloop/services/data/model_prices.json``) is a filtered
snapshot of litellm's ``model_prices_and_context_window.json``:

- providers limited to the ones Preloop routes,
- modes limited to chat/responses (no image/audio/video/embedding models:
  the gateway never bills those),
- entries past their ``deprecation_date`` dropped,
- fields stripped to what pricing needs (cost fields + provider/mode/limits;
  capability flags are omitted. ``litellm.register_model`` merges per key,
  so bundled entries keep their flags).

First-party overlays (``moonshot/*``, ``zai/*``) are preserved across a
refresh and are not sourced from litellm. z.ai has no live price API
(probed 2026-08-22: ``/api/paas/v4/models`` has ids only; ``/pricing``,
``/prices``, ``/price`` 404; ``/api/v1/pricing`` is HTTP 200 with
``success=false``). Text-model prices are scraped from the public docs
page and upserted under ``zai/<slug>``. Do not invent a live z.ai fetch.

Models missing from the snapshot are looked up live, once, at runtime (see
``model_price_catalog.schedule_price_lookup``). The snapshot is a warm,
deterministic cache, not an exhaustive registry. It is loaded at startup via
``preloop.services.model_price_catalog.load_catalog`` so runtime pricing does
not depend on the installed litellm version. Review the printed diff and
commit the regenerated file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
ZAI_PRICING_URL = "https://docs.z.ai/guides/overview/pricing.md"
CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "preloop"
    / "services"
    / "data"
    / "model_prices.json"
)
META_KEY = "_preloop_meta"

# litellm_provider values Preloop routes (exact match, or prefix for the
# vertex_ai-* families). moonshot and zai are first-party overlays, not
# this allowlist: a refresh must not drop them when litellm omits the keys.
PROVIDER_ALLOWLIST = {
    "openai",
    "azure",
    "anthropic",
    "bedrock",
    "bedrock_converse",
    "gemini",
    "deepseek",
    "dashscope",  # qwen
}
PROVIDER_PREFIXES = ("vertex_ai",)

OVERLAY_KEY_PREFIXES = ("moonshot/", "zai/")
OVERLAY_PROVIDERS = frozenset({"moonshot", "zai"})

# Only modes the gateway can actually bill (chat completions / responses).
MODE_ALLOWLIST = {"chat", "responses"}

# Fields kept per entry: everything pricing-related plus routing/limit
# metadata. Capability flags (supports_*), sources, and sample specs are
# dropped. register_model merges per key, so litellm's bundled entries keep
# any field we omit.
KEEP_FIELDS_EXACT = {
    "litellm_provider",
    "mode",
    "deprecation_date",
    "max_tokens",
    "max_input_tokens",
    "max_output_tokens",
}
KEEP_FIELD_SUBSTRING = "cost"

PRICE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
    "input_cost_per_token_above_200k_tokens",
    "output_cost_per_token_above_200k_tokens",
    "output_cost_per_reasoning_token",
)

# Official GLM-5.3 limits from https://docs.z.ai/guides/llm/glm-5.3
# ("1M-token context window and a maximum output length of 128K tokens").
# Do not invent max_* for other z.ai rows.
ZAI_GLM_53_LIMITS = {
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 128_000,
}

_TEXT_MODELS_HEADING = re.compile(r"^###\s+Text Models\s*$", re.MULTILINE)
_NEXT_H3 = re.compile(r"^###\s+", re.MULTILINE)
_TABLE_SEP = re.compile(r"^:?-+:?$")


def _provider_allowed(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    provider = entry.get("litellm_provider") or ""
    return provider in PROVIDER_ALLOWLIST or provider.startswith(PROVIDER_PREFIXES)


def is_overlay_entry(key: str, entry: Any) -> bool:
    """True for first-party overlay keys (moonshot/zai), not litellm-sourced."""
    if key.startswith(OVERLAY_KEY_PREFIXES):
        return True
    if isinstance(entry, dict) and entry.get("litellm_provider") in OVERLAY_PROVIDERS:
        return True
    return False


def _ssl_context() -> Any:
    try:
        import ssl

        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # certifi is optional: fall back to the system CA store via
        # urlopen's default SSL context when the package is not installed.
        return None


def _urlopen_bytes(url: str) -> bytes:
    with urllib.request.urlopen(  # noqa: S310
        url, timeout=60, context=_ssl_context()
    ) as response:
        return response.read()


def fetch_remote(url: str = SOURCE_URL) -> Dict[str, Any]:
    """Download and parse the upstream litellm price map."""
    return json.loads(_urlopen_bytes(url).decode("utf-8"))


def fetch_text(url: str) -> str:
    """Download a text document (used for the public z.ai pricing page)."""
    return _urlopen_bytes(url).decode("utf-8")


def _entry_relevant(entry: Any, today: str) -> bool:
    """True for billable, current models on providers Preloop routes."""
    if not _provider_allowed(entry):
        return False
    if entry.get("mode") not in MODE_ALLOWLIST:
        return False
    deprecation_date = entry.get("deprecation_date")
    if isinstance(deprecation_date, str) and deprecation_date < today:
        return False
    return True


def _strip_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only pricing-relevant fields of one model entry."""
    return {
        key: value
        for key, value in entry.items()
        if key in KEEP_FIELDS_EXACT or KEEP_FIELD_SUBSTRING in key
    }


def filter_catalog(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce the upstream map to current, billable, Preloop-routed models."""
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        key: _strip_entry(value)
        for key, value in raw.items()
        if key != "sample_spec" and _entry_relevant(value, today)
    }


def load_current() -> Dict[str, Any]:
    """Load the vendored snapshot, or an empty dict when absent."""
    if not CATALOG_PATH.exists():
        return {}
    return json.loads(CATALOG_PATH.read_text())


def preserve_overlays(current: Dict[str, Any]) -> Dict[str, Any]:
    """Return first-party overlay rows from the current catalog."""
    return {
        key: value
        for key, value in current.items()
        if key != META_KEY and is_overlay_entry(key, value)
    }


def litellm_sourced_catalog(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """Drop meta and first-party overlays so --compare-remote diffs litellm only."""
    return {
        key: value
        for key, value in catalog.items()
        if key != META_KEY and not is_overlay_entry(key, value)
    }


def _split_md_row(line: str) -> List[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: List[str]) -> bool:
    if not cells:
        return False
    return all(_TABLE_SEP.match(cell.replace(" ", "")) for cell in cells if cell)


def zai_model_slug(name: str) -> str:
    """GLM-5.3 -> glm-5.3, GLM-5-Turbo -> glm-5-turbo."""
    return name.strip().lower()


def parse_usd_per_million(cell: str) -> Optional[float]:
    """Convert a Text Models cell ($/1M, Free, or -) to per-token cost.

    Returns None for a dash / empty cell (caller skips the field or row).
    Does not invent a storage-cost field for "Limited-time Free".
    """
    raw = cell.replace("\\", "").replace("$", "").replace(",", "").strip()
    if raw in ("", "-"):
        return None
    if "free" in raw.lower():
        return 0.0
    # 8 significant digits is enough for $/1M / 1e6 and avoids binary noise
    # such as 1.0000000000000001e-07.
    return float(f"{(float(raw) / 1_000_000):.8g}")


def parse_zai_text_models(markdown: str) -> Dict[str, Dict[str, Any]]:
    """Parse the Text Models table only (not vision/image/video/audio/agents).

    Args:
        markdown: Full docs.z.ai pricing page (or a fixture snippet).

    Returns:
        Map of slug -> cost fields (per token). Rows with ``-`` for input
        or output are skipped. Cached-input ``-`` omits that field only.

    Raises:
        ValueError: The Text Models table is missing or has no Model column.
    """
    heading = _TEXT_MODELS_HEADING.search(markdown)
    if heading is None:
        raise ValueError("z.ai pricing markdown has no Text Models section")
    rest = markdown[heading.end() :]
    next_heading = _NEXT_H3.search(rest)
    section = rest[: next_heading.start()] if next_heading else rest

    table_rows: List[List[str]] = []
    for line in section.splitlines():
        if "|" not in line:
            continue
        cells = _split_md_row(line)
        if not cells or _is_separator_row(cells):
            continue
        table_rows.append(cells)
    if len(table_rows) < 2:
        raise ValueError("z.ai Text Models section has no data rows")

    header = [cell.lower() for cell in table_rows[0]]
    try:
        model_idx = header.index("model")
        input_idx = header.index("input")
        output_idx = header.index("output")
    except ValueError as exc:
        raise ValueError("z.ai Text Models table is missing required columns") from exc
    cache_idx = header.index("cached input") if "cached input" in header else None

    parsed: Dict[str, Dict[str, Any]] = {}
    for cells in table_rows[1:]:
        if model_idx >= len(cells):
            continue
        slug = zai_model_slug(cells[model_idx])
        if not slug:
            continue
        input_cell = cells[input_idx] if input_idx < len(cells) else ""
        output_cell = cells[output_idx] if output_idx < len(cells) else ""
        input_cost = parse_usd_per_million(input_cell)
        output_cost = parse_usd_per_million(output_cell)
        if input_cost is None or output_cost is None:
            continue
        entry: Dict[str, Any] = {
            "input_cost_per_token": input_cost,
            "output_cost_per_token": output_cost,
        }
        if cache_idx is not None and cache_idx < len(cells):
            cache_cost = parse_usd_per_million(cells[cache_idx])
            if cache_cost is not None:
                entry["cache_read_input_token_cost"] = cache_cost
        parsed[slug] = entry
    return parsed


def apply_overlays(
    litellm_filtered: Dict[str, Any],
    current: Dict[str, Any],
    zai_rows: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge litellm rows with preserved overlays and upserted z.ai prices.

    Existing ``moonshot/`` and ``zai/`` keys survive a stub or refreshed
    litellm map. z.ai Text Models then overwrite ``zai/<slug>``. glm-5.3
    gets documented max_* limits; other z.ai rows keep existing max_* only.
    """
    merged = dict(litellm_filtered)
    for key, entry in preserve_overlays(current).items():
        merged[key] = entry
    for slug, costs in zai_rows.items():
        key = f"zai/{slug}"
        previous = current.get(key) if isinstance(current.get(key), dict) else {}
        if not previous:
            existing = merged.get(key)
            previous = existing if isinstance(existing, dict) else {}
        entry = {
            "litellm_provider": "zai",
            "mode": "chat",
            **costs,
        }
        if slug == "glm-5.3":
            entry.update(ZAI_GLM_53_LIMITS)
        else:
            for field in ("max_input_tokens", "max_output_tokens", "max_tokens"):
                if field in previous:
                    entry[field] = previous[field]
        merged[key] = entry
    return merged


def diff_catalogs(current: Dict[str, Any], new: Dict[str, Any]) -> list[str]:
    """Return human-readable per-model changes between two snapshots."""
    lines: list[str] = []
    current_models = {k for k in current if k != META_KEY}
    new_models = set(new)
    for model in sorted(new_models - current_models):
        lines.append(f"+ {model}")
    for model in sorted(current_models - new_models):
        lines.append(f"- {model}")
    for model in sorted(new_models & current_models):
        changes = []
        for field in PRICE_FIELDS:
            old_value = (current.get(model) or {}).get(field)
            new_value = (new.get(model) or {}).get(field)
            if old_value != new_value:
                changes.append(f"{field}: {old_value} -> {new_value}")
        if changes:
            lines.append(f"~ {model}: " + "; ".join(changes))
    return lines


def write_catalog(
    filtered: Dict[str, Any],
    source_url: str,
    overlay_sources: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write the snapshot with provenance metadata.

    ``source_url`` stays the litellm map (CI / --check compatibility).
    Overlay provenance lives in ``overlay_sources`` so first-party rows
    are not claimed as litellm-sourced.
    """
    meta: Dict[str, Any] = {
        "source_url": source_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "model_count": len(filtered),
    }
    if overlay_sources:
        meta["overlay_sources"] = overlay_sources
    payload: Dict[str, Any] = {META_KEY: meta}
    payload.update(dict(sorted(filtered.items())))
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n")


def check(max_age_days: int, compare_remote: bool) -> int:
    """Freshness gate: non-zero exit when the snapshot is stale or drifted."""
    current = load_current()
    if not current:
        print(f"FAIL: catalog missing at {CATALOG_PATH}")
        return 1
    meta = current.get(META_KEY) or {}
    fetched_at_raw = meta.get("fetched_at")
    if not fetched_at_raw:
        print("FAIL: catalog has no fetched_at provenance")
        return 1
    fetched_at = datetime.fromisoformat(fetched_at_raw)
    age_days = (datetime.now(timezone.utc) - fetched_at).days
    print(
        f"catalog fetched_at={fetched_at_raw} age={age_days}d models={meta.get('model_count')}"
    )
    if age_days > max_age_days:
        print(f"FAIL: catalog older than {max_age_days} days: rerun this script")
        return 1
    if compare_remote:
        filtered = filter_catalog(fetch_remote())
        drift = diff_catalogs(litellm_sourced_catalog(current), filtered)
        if drift:
            print(f"FAIL: catalog drifted from upstream ({len(drift)} changes):")
            for line in drift[:50]:
                print("  " + line)
            return 1
        print("catalog matches upstream")
    print("OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SOURCE_URL, help="Upstream price map URL")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify freshness instead of rewriting (exit 1 when stale)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        help="Maximum snapshot age accepted by --check (default 30)",
    )
    parser.add_argument(
        "--compare-remote",
        action="store_true",
        help="With --check: also diff against the live upstream map",
    )
    args = parser.parse_args()

    if args.check:
        return check(args.max_age_days, args.compare_remote)

    print(f"Fetching {args.url} ...")
    filtered = filter_catalog(fetch_remote(args.url))
    current = load_current()
    print(f"Fetching {ZAI_PRICING_URL} ...")
    zai_rows = parse_zai_text_models(fetch_text(ZAI_PRICING_URL))
    merged = apply_overlays(filtered, current, zai_rows)
    overlay_fetched_at = datetime.now(timezone.utc).isoformat()
    changes = diff_catalogs({k: v for k, v in current.items() if k != META_KEY}, merged)
    if changes:
        print(f"{len(changes)} model change(s):")
        for line in changes:
            print("  " + line)
    else:
        print("No price/model changes; refreshing provenance only.")
    write_catalog(
        merged,
        args.url,
        overlay_sources=[
            {
                "url": ZAI_PRICING_URL,
                "fetched_at": overlay_fetched_at,
                "section": "Text Models",
            }
        ],
    )
    print(f"Wrote {len(merged)} models to {CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
