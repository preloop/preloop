"""Unit tests for served-schema token estimates."""

from __future__ import annotations

import json

from preloop.services.context_optimization import estimate_tokens
from preloop.services.tool_schema_tokens import (
    apply_justification_to_schema,
    estimate_tool_schema_tokens,
)


def test_apply_justification_optional_adds_property_not_required() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    served = apply_justification_to_schema(schema, "optional")
    assert "justification" in served["properties"]
    assert "justification" not in served["required"]
    # Original schema must not be mutated.
    assert "justification" not in schema["properties"]


def test_apply_justification_required_adds_to_required() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    served = apply_justification_to_schema(schema, "required")
    assert "justification" in served["properties"]
    assert served["required"] == ["query", "justification"]


def test_apply_justification_ignored_for_other_modes() -> None:
    schema = {"type": "object", "properties": {}}
    assert apply_justification_to_schema(schema, None) == schema
    assert apply_justification_to_schema(schema, "disabled") == schema


def test_estimate_includes_justification_tokens() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    base = estimate_tool_schema_tokens(
        name="search",
        description="Search issues",
        schema=schema,
        justification_mode=None,
    )
    with_required = estimate_tool_schema_tokens(
        name="search",
        description="Search issues",
        schema=schema,
        justification_mode="required",
    )
    assert base > 0
    assert with_required > base


def test_estimate_matches_gateway_serialization() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    expected = estimate_tokens(
        json.dumps(
            {
                "description": "desc",
                "name": "tool",
                "parameters": schema,
            },
            default=str,
            sort_keys=True,
        )
    )
    assert (
        estimate_tool_schema_tokens(
            name="tool",
            description="desc",
            schema=schema,
            justification_mode=None,
        )
        == expected
    )
