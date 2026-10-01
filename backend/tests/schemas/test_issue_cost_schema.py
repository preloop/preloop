"""Documentation contract of the per-issue cost-coverage fields (#1057).

These run without a database. What they pin is the published description of
the two run counts: the reviewer of #1075 found that both fields shared one
sentence, so nothing in the public API docs said which field counts priced
runs and which counts unpriced ones, and a consumer had to infer it from the
field name. The check is therefore on the wording of the description, and on
the generated spec that consumers actually read, not on any rollup arithmetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from preloop.schemas.issue_cost import (
    IssueCostRow,
    IssueCostSummary,
    IssueCostUnassigned,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI = REPO_ROOT / "openapi.yaml"

#: Every shape that publishes the counts, and its OpenAPI component name.
SHAPES: dict[type[BaseModel], str] = {
    IssueCostRow: "IssueCostRow",
    IssueCostSummary: "IssueCostSummary",
    IssueCostUnassigned: "IssueCostUnassigned",
}


def _description(model: type[BaseModel], field: str) -> str:
    return model.model_fields[field].description or ""


def _spec_descriptions(component: str) -> tuple[str, str]:
    """Read both count descriptions out of the committed openapi.yaml."""
    schema = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    properties: dict[str, Any] = schema["components"]["schemas"][component][
        "properties"
    ]
    return (
        cast(str, properties["known_cost_run_count"]["description"]),
        cast(str, properties["unknown_cost_run_count"]["description"]),
    )


def test_the_two_counts_are_described_separately() -> None:
    """A consumer must not have to infer which count is which from its name."""
    for model in SHAPES:
        known = _description(model, "known_cost_run_count")
        unknown = _description(model, "unknown_cost_run_count")

        assert known and unknown
        assert known != unknown

        # Each sentence says what its own count holds.
        assert "carry a cost estimate" in known
        assert "no cost estimate" in unknown


def test_the_counts_keep_the_zero_and_subtotal_warnings() -> None:
    """Splitting the sentence must not lose the caveats the old one carried."""
    for model in SHAPES:
        known = _description(model, "known_cost_run_count")
        unknown = _description(model, "unknown_cost_run_count")

        # A known zero is still a priced run, and estimated_cost stays the
        # priced subtotal rather than total spend.
        assert "zero counts as known" in known
        assert "subtotal of the known runs rather than total spend" in unknown
        assert "subscription-backed" in unknown


def test_the_generated_spec_carries_the_distinct_descriptions() -> None:
    """openapi.yaml is what consumers read, so it must not lag the schema."""
    for model, component in SHAPES.items():
        spec_known, spec_unknown = _spec_descriptions(component)

        assert spec_known == _description(model, "known_cost_run_count"), (
            f"{component}.known_cost_run_count is stale in openapi.yaml; "
            "regenerate it with `python scripts/generate_openapi.py`"
        )
        assert spec_unknown == _description(model, "unknown_cost_run_count"), (
            f"{component}.unknown_cost_run_count is stale in openapi.yaml; "
            "regenerate it with `python scripts/generate_openapi.py`"
        )
        assert spec_known != spec_unknown


def test_coverage_fields_are_still_described_once_per_shape() -> None:
    """The sibling coverage fields keep a shared, accurate sentence."""
    shared = _description(IssueCostRow, "cost_coverage")

    for model in SHAPES:
        assert _description(model, "cost_coverage") == shared
        assert _description(model, "attributed_cost_usd") == _description(
            IssueCostRow, "attributed_cost_usd"
        )
