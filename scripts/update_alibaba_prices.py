#!/usr/bin/env python
"""Regenerate the Alibaba Singapore International seed from a native catalog dump.

The runtime overlay fetches native GET /api/v1/models with the model's key.
This script is for the checked-in seed used before that refresh, and for the
weekly review when a native dump is attached as evidence.

    python scripts/update_alibaba_prices.py --from-native dump.json

The dump is either a native catalog response (``output.models``) or a JSON
array of those model objects. No API keys are read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from preloop.services.alibaba_price_catalog import parse_native_model  # noqa: E402

SEED_PATH = (
    ROOT
    / "backend"
    / "preloop"
    / "services"
    / "data"
    / "alibaba_international_prices.json"
)
SOURCE_URL = "https://www.alibabacloud.com/help/en/model-studio/list-models"


def _models_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise SystemExit("native dump must be an object or array")
    output = payload.get("output")
    if isinstance(output, dict) and isinstance(output.get("models"), list):
        return [row for row in output["models"] if isinstance(row, dict)]
    models = payload.get("models")
    if isinstance(models, list):
        return [row for row in models if isinstance(row, dict)]
    raise SystemExit("native dump has no output.models or models array")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-native", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=SEED_PATH)
    args = parser.parse_args()
    payload = json.loads(args.from_native.read_text())
    models: dict[str, dict[str, Any]] = {}
    for entry in _models_from_payload(payload):
        ident = str(entry.get("model") or "").strip()
        tariff = parse_native_model(entry)
        if tariff is None or not ident:
            continue
        tiers = list(tariff.tiers) if tariff.tiers else [tariff]
        models[ident] = {
            "tiers": [
                {
                    "max_input": tier.max_input,
                    "input": tier.input,
                    "output": tier.output,
                    **(
                        {"implicit_read": tier.implicit_read}
                        if tier.implicit_read is not None
                        else {}
                    ),
                    **(
                        {"explicit_read": tier.explicit_read}
                        if tier.explicit_read is not None
                        else {}
                    ),
                    **(
                        {"creation": tier.creation} if tier.creation is not None else {}
                    ),
                }
                for tier in tiers
            ]
        }
    seed = {
        "_meta": {
            "currency": "USD",
            "service_site": "international",
            "region": "singapore",
            "source_url": SOURCE_URL,
            "retrieved_at": datetime.now(timezone.utc).date().isoformat(),
            "note": (
                "Singapore International token tariffs from a native catalog "
                "dump. Estimates, not invoices."
            ),
            "model_count": len(models),
        },
        "models": {key: models[key] for key in sorted(models)},
    }
    args.output.write_text(json.dumps(seed, indent=2) + "\n")
    print(f"wrote {len(models)} models to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
