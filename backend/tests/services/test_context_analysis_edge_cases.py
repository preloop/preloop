"""Supplemental edge-case coverage for deterministic context analysis.

Targets branches not exercised by ``test_context_analysis.py``: measured
provider cache-read aggregation, ``budget_denied`` retry classification,
homogeneous-array compressibility rejection heuristics, and the
``history_truncated`` / ``message_inserted`` cache-breaking reason hints.
"""

from __future__ import annotations

import json

from preloop.services.context_analysis import (
    GatewayCallEvent,
    _homogeneous_array_compressible_tokens,
    analyze_cache_profile,
    analyze_retry_profile,
    analyze_tool_bloat,
)


def _event(event_id: str, *, messages: list[dict], **payload_extra) -> GatewayCallEvent:
    payload: dict = {"request": {"messages": messages}}
    payload.update(payload_extra)
    return GatewayCallEvent(event_id=event_id, payload=payload)


class TestMeasuredCacheRead:
    def test_aggregates_cache_read_tokens_across_shapes(self) -> None:
        """measured_cache_read_tokens sums usage shapes from each request."""
        events = [
            _event(
                "e1",
                messages=[{"role": "user", "content": "one"}],
                usage={"cache_read_input_tokens": 100},
            ),
            _event(
                "e2",
                messages=[{"role": "user", "content": "two"}],
                usage={"prompt_tokens_details": {"cached_tokens": 250}},
            ),
        ]
        profile = analyze_cache_profile(events)
        assert profile.measured_cache_read_tokens == 350

    def test_top_level_cache_read_field_is_used(self) -> None:
        """A top-level cache_read_input_tokens field is counted too."""
        events = [
            _event(
                "e1",
                messages=[{"role": "user", "content": "one"}],
                cache_read_input_tokens=40,
            ),
            _event(
                "e2",
                messages=[{"role": "user", "content": "two"}],
                cache_read_input_tokens=60,
            ),
        ]
        profile = analyze_cache_profile(events)
        assert profile.measured_cache_read_tokens == 100


class TestRetryClassification:
    def test_budget_denied_outcome_counts_as_failure(self) -> None:
        """A budget_denied gateway call is failure-attributed, not retry."""
        events = [
            _event(
                "e1",
                messages=[{"role": "user", "content": "q"}],
                prompt_tokens=300,
                completion_tokens=0,
                total_tokens=300,
                outcome="budget_denied",
                status_code=403,
                estimated_cost=0.0,
            ),
        ]
        profile = analyze_retry_profile(events)
        assert profile.failed_requests == 1
        assert profile.retry_requests == 0
        assert profile.failure_event_ids == ["e1"]
        assert profile.wasted_tokens == 300

    def test_retry_by_fingerprint_match_after_failure(self) -> None:
        """A later success sharing a failed request's fingerprint is a retry."""
        events = [
            _event(
                "e1",
                messages=[{"role": "user", "content": "q"}],
                prompt_tokens=100,
                total_tokens=100,
                outcome="error",
                status_code=500,
                request_fingerprint="fp-x",
                estimated_cost=0.01,
            ),
            _event(
                "e2",
                messages=[{"role": "user", "content": "q"}],
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
                outcome="success",
                status_code=200,
                request_fingerprint="fp-x",
                estimated_cost=0.02,
            ),
        ]
        profile = analyze_retry_profile(events)
        assert profile.failed_requests == 1
        assert profile.retry_requests == 1
        # failed total (100) + retry prompt tokens (120)
        assert profile.wasted_tokens == 220


class TestHomogeneousArrayRejection:
    def test_too_few_items_not_compressible(self) -> None:
        """Arrays at or below the minimum item count are not compressible."""
        rows = [{"id": i, "name": f"r{i}"} for i in range(5)]
        assert _homogeneous_array_compressible_tokens(json.dumps(rows)) == 0

    def test_heterogeneous_key_sets_rejected(self) -> None:
        """More than two distinct key-sets means it is not a homogeneous table."""
        rows = [{f"k{i}": i} for i in range(20)]  # 20 distinct key sets
        assert _homogeneous_array_compressible_tokens(json.dumps(rows)) == 0

    def test_mostly_non_dict_items_rejected(self) -> None:
        """An array that is mostly scalars is not treated as a record table."""
        items = list(range(50))
        assert _homogeneous_array_compressible_tokens(json.dumps(items)) == 0

    def test_non_array_text_returns_zero(self) -> None:
        """Plain (non-JSON-array) text yields no homogeneous-array savings."""
        assert _homogeneous_array_compressible_tokens("just some prose") == 0


class TestCacheBreakingReasonHints:
    def test_history_truncated_reason(self) -> None:
        """A shorter request that diverges inside the prefix reads as truncation."""
        previous = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "C"},
        ]
        current = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "X"},
            {"role": "assistant", "content": "B"},
        ]
        profile = analyze_cache_profile(
            [_event("e1", messages=previous), _event("e2", messages=current)]
        )
        assert profile.prefix_stability == "unstable"
        assert profile.cache_breaking_events[0].reason_hint == "history_truncated"

    def test_message_inserted_reason(self) -> None:
        """A longer request diverging inside the prefix reads as an insertion."""
        previous = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        current = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "X"},
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        profile = analyze_cache_profile(
            [_event("e1", messages=previous), _event("e2", messages=current)]
        )
        assert profile.prefix_stability == "unstable"
        assert profile.cache_breaking_events[0].reason_hint == "message_inserted"


class TestToolNameResolutionFallback:
    def test_unmapped_tool_output_uses_default_name(self) -> None:
        """A tool result with no matching tool_call id falls back to 'tool'."""
        messages = [
            {"role": "tool", "tool_call_id": "missing", "content": "x" * 200},
            {"role": "user", "content": "continue"},
        ]
        profile = analyze_tool_bloat([_event("e1", messages=messages)])
        assert profile.largest_outputs
        assert profile.largest_outputs[0].tool_name == "tool"
