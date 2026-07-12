#!/usr/bin/env python
"""Refresh the vendored model-price catalog from litellm's public price map.

This is the standardized way to update Preloop's default model prices:

    python scripts/update_model_prices.py          # fetch, diff, rewrite
    python scripts/update_model_prices.py --check  # CI freshness gate

The catalog (``preloop/services/data/model_prices.json``) is a filtered
snapshot of litellm's ``model_prices_and_context_window.json``:

- providers limited to the ones Preloop routes,
- modes limited to chat/responses (no image/audio/video/embedding models —
  the gateway never bills those),
- entries past their ``deprecation_date`` dropped,
- fields stripped to what pricing needs (cost fields + provider/mode/limits;
  capability flags are omitted — ``litellm.register_model`` merges per key,
  so bundled entries keep their flags).

Models missing from the snapshot are looked up live, once, at runtime (see
``model_price_catalog.schedule_price_lookup``) — the snapshot is a warm,
deterministic cache, not an exhaustive registry. It is loaded at startup via
``preloop.services.model_price_catalog.load_catalog`` so runtime pricing does
not depend on the installed litellm version. Review the printed diff and
commit the regenerated file.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
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
# vertex_ai-* families).
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

# Only modes the gateway can actually bill (chat completions / responses).
MODE_ALLOWLIST = {"chat", "responses"}

# Fields kept per entry: everything pricing-related plus routing/limit
# metadata. Capability flags (supports_*), sources, and sample specs are
# dropped — register_model merges per key, so litellm's bundled entries keep
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


def _provider_allowed(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    provider = entry.get("litellm_provider") or ""
    return provider in PROVIDER_ALLOWLIST or provider.startswith(PROVIDER_PREFIXES)


def fetch_remote(url: str = SOURCE_URL) -> Dict[str, Any]:
    """Download and parse the upstream litellm price map."""
    context = None
    try:
        import ssl

        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urllib.request.urlopen(  # noqa: S310
        url, timeout=60, context=context
    ) as response:
        return json.loads(response.read().decode("utf-8"))


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


def write_catalog(filtered: Dict[str, Any], source_url: str) -> None:
    """Write the snapshot with provenance metadata."""
    payload: Dict[str, Any] = {
        META_KEY: {
            "source_url": source_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "model_count": len(filtered),
        }
    }
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
        print(f"FAIL: catalog older than {max_age_days} days — rerun this script")
        return 1
    if compare_remote:
        filtered = filter_catalog(fetch_remote())
        drift = diff_catalogs(current, filtered)
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
    changes = diff_catalogs(
        {k: v for k, v in current.items() if k != META_KEY}, filtered
    )
    if changes:
        print(f"{len(changes)} model change(s):")
        for line in changes:
            print("  " + line)
    else:
        print("No price/model changes; refreshing provenance only.")
    write_catalog(filtered, args.url)
    print(f"Wrote {len(filtered)} models to {CATALOG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
