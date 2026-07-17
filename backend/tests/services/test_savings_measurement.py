"""Tests for deterministic input-token savings measurement.

Golden fixtures are hand-constructed so the exact token delta is reasoned
about directly (via the shared ``estimate_tokens`` heuristic), never inferred
from the function under test. The independent ``_tok`` helper builds the
expected "modified" payload by hand so the transform logic is checked against
a golden structure rather than against itself.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from preloop.services.savings_measurement import (
    InputTokenSavings,
    compute_input_token_savings,
)
from preloop.services.context_optimization import estimate_tokens


def _tok(obj: Any) -> int:
    """Tokenize a payload exactly as the measurement does (independent copy)."""
    return estimate_tokens(json.dumps(obj, default=str, sort_keys=True))


def test_remove_present_tool_exact_delta() -> None:
    """Removing a present tool yields the exact hand-computed delta."""
    payload = {"tools": [{"name": "a"}]}
    result = compute_input_token_savings(payload, removed_tool_names={"a"})
    # {"tools": [{"name": "a"}]} -> 26 chars -> 6 tokens.
    # {"tools": []}              -> 13 chars -> 3 tokens.
    assert result.original_tokens == 6
    assert result.modified_tokens == 3
    assert result.delta_tokens == 3
    assert result.pct_saved == 0.5


def test_remove_present_tool_reduces_tokens() -> None:
    """Removing a present tool strictly reduces tokens and matches golden."""
    payload = {
        "model": "gpt-4o",
        "tools": [
            {"type": "function", "function": {"name": "search", "x": "y" * 40}},
            {"type": "function", "function": {"name": "keep_me"}},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    expected_modified = {
        "model": "gpt-4o",
        "tools": [{"type": "function", "function": {"name": "keep_me"}}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = compute_input_token_savings(payload, removed_tool_names={"search"})
    assert result.delta_tokens > 0
    assert result.modified_tokens == _tok(expected_modified)
    assert result.delta_tokens == _tok(payload) - _tok(expected_modified)


def test_remove_absent_tool_is_noop() -> None:
    """Removing a tool that is not present changes nothing (delta 0)."""
    payload = {"tools": [{"name": "a"}]}
    result = compute_input_token_savings(payload, removed_tool_names={"nope"})
    assert result.delta_tokens == 0
    assert result.original_tokens == result.modified_tokens
    assert result.pct_saved == 0.0


def test_no_removed_tool_names_is_noop() -> None:
    """Omitting removed_tool_names leaves tools untouched."""
    payload = {"tools": [{"name": "a"}, {"name": "b"}]}
    result = compute_input_token_savings(payload)
    assert result.delta_tokens == 0
    assert result.modified_tokens == result.original_tokens


def test_empty_removed_tool_names_is_noop() -> None:
    """An empty removed_tool_names set is a no-op even with tools present."""
    payload = {"tools": [{"name": "a"}]}
    result = compute_input_token_savings(payload, removed_tool_names=set())
    assert result.delta_tokens == 0


def test_empty_payload_is_noop() -> None:
    """An empty payload has no tokens to save and never crashes."""
    result = compute_input_token_savings(
        {}, removed_tool_names={"a"}, filtered_output_fields={"a": ["b"]}
    )
    assert result.original_tokens == result.modified_tokens
    assert result.delta_tokens == 0
    assert result.pct_saved == 0.0


def test_filter_output_field_string_content_reduces_tokens() -> None:
    """Dropping a field from a JSON-string tool result reduces tokens."""
    content = json.dumps({"keep": "x", "drop": "y" * 50})
    payload = {
        "messages": [
            {"role": "assistant", "content": "calling"},
            {"role": "tool", "name": "search", "content": content},
        ]
    }
    expected_modified = {
        "messages": [
            {"role": "assistant", "content": "calling"},
            {
                "role": "tool",
                "name": "search",
                "content": json.dumps({"keep": "x"}),
            },
        ]
    }
    result = compute_input_token_savings(
        payload, filtered_output_fields={"search": ["drop"]}
    )
    assert result.delta_tokens > 0
    assert result.modified_tokens == _tok(expected_modified)


def test_filter_output_field_resolved_via_tool_call_id() -> None:
    """Tool name resolves through the assistant tool_call_id back-reference."""
    content = json.dumps({"keep": 1, "verbose": "z" * 60})
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fetch", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": content},
        ]
    }
    result = compute_input_token_savings(
        payload, filtered_output_fields={"fetch": ["verbose"]}
    )
    assert result.delta_tokens > 0


def test_filter_field_absent_from_result_is_noop() -> None:
    """Filtering a field that the tool result lacks changes nothing."""
    content = json.dumps({"only": "value"})
    payload = {"messages": [{"role": "tool", "name": "search", "content": content}]}
    result = compute_input_token_savings(
        payload, filtered_output_fields={"search": ["missing"]}
    )
    assert result.delta_tokens == 0


def test_non_json_tool_content_left_untouched() -> None:
    """A non-JSON tool-result content is left untouched and never crashes."""
    payload = {
        "messages": [{"role": "tool", "name": "search", "content": "not json at all"}]
    }
    result = compute_input_token_savings(
        payload, filtered_output_fields={"search": ["drop"]}
    )
    assert result.delta_tokens == 0
    assert result.modified_tokens == result.original_tokens


def test_does_not_mutate_caller_payload() -> None:
    """The caller's dict is deep-copied and never mutated."""
    content = json.dumps({"keep": "x", "drop": "y" * 30})
    payload = {
        "tools": [{"name": "a"}, {"name": "b"}],
        "messages": [{"role": "tool", "name": "a", "content": content}],
    }
    snapshot = copy.deepcopy(payload)
    compute_input_token_savings(
        payload,
        removed_tool_names={"a"},
        filtered_output_fields={"a": ["drop"]},
    )
    assert payload == snapshot


def test_determinism_identical_results() -> None:
    """Two calls on the same input produce identical results."""
    content = json.dumps({"keep": "x", "drop": "y" * 40})
    payload = {
        "tools": [
            {"type": "function", "function": {"name": "search"}},
            {"type": "function", "function": {"name": "fetch"}},
        ],
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "tool", "name": "search", "content": content},
        ],
    }
    first = compute_input_token_savings(
        payload,
        removed_tool_names={"fetch"},
        filtered_output_fields={"search": ["drop"]},
    )
    second = compute_input_token_savings(
        payload,
        removed_tool_names={"fetch"},
        filtered_output_fields={"search": ["drop"]},
    )
    assert first == second
    assert isinstance(first, InputTokenSavings)


def test_pct_saved_zero_when_no_delta() -> None:
    """pct_saved is 0.0 whenever nothing is saved (no division surprises)."""
    # An empty dict serializes to "{}" (1 token); with no optimization applied
    # the delta is 0, so pct_saved must be exactly 0.0.
    result = compute_input_token_savings({})
    assert result.delta_tokens == 0
    assert result.pct_saved == 0.0
