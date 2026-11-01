"""Preset 003 carries its machine-result shape through the real preset loader."""

from pathlib import Path

import yaml

from preloop.models.schemas.flow import FlowCreate
from preloop.services.flow_result_schema import validate_result_document


def test_observe_eval_schema_requires_the_documented_contract() -> None:
    preset = yaml.safe_load(
        (Path(__file__).parents[1] / "presets/003-observe-eval.yaml").read_text()
    )
    flow = FlowCreate.model_validate(preset)
    assert flow.validate_result is True
    schema = flow.result_schema
    valid = {
        "schema": "preloop.eval.result/v1",
        "status": "pass",
        "summary": "fixture",
        "metrics": {"count": 1},
        "checks": [{"name": "fixture", "passed": True, "details": "observed"}],
        "artifacts": {},
    }
    assert validate_result_document(valid, schema)["valid"] is True
    for missing in ("schema", "status", "summary", "metrics", "checks", "artifacts"):
        assert (
            validate_result_document(
                {k: v for k, v in valid.items() if k != missing}, schema
            )["valid"]
            is False
        )
    assert (
        validate_result_document({**valid, "metrics": {"count": "1"}}, schema)["valid"]
        is False
    )
