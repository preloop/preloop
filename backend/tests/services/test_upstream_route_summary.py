"""Which OpenRouter upstream provider served an execution, and when to say so."""

from types import SimpleNamespace

import pytest

from preloop.schemas.openrouter import pins_upstream_provider
from preloop.services.review_completion_evidence import (
    timings_projection,
    upstream_route_summary,
)


def _request(
    provider: str | None, finish: str | None, routing: dict | None = None
) -> dict:
    return {
        "upstream_provider": provider,
        "finish_reason": finish,
        "upstream_parameters": {"provider": routing} if routing else {},
    }


def _execution(category: str | None = None, native: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status="FAILED" if category else "SUCCEEDED",
        failure_category=category,
        trigger_event_details={"_native_result": {"value": native, "source": "flow"}},
        result=None,
        execution_context={},
    )


SPREAD = [
    _request("Provider A", "tool_calls"),
    _request("Provider B", "tool_calls"),
    _request("Provider A", "tool_calls"),
    _request("Provider C", "stop"),
    _request(None, None),
]


def test_counts_distinct_upstreams_and_names_the_final_answer() -> None:
    summary = upstream_route_summary(_execution(), SPREAD)
    assert summary["upstream_providers"] == [
        {"provider": "Provider A", "request_count": 2},
        {"provider": "Provider B", "request_count": 1},
        {"provider": "Provider C", "request_count": 1},
    ]
    assert summary["final_answer_upstream_provider"] == "Provider C"
    assert summary["upstream_provider_pinned"] is False
    assert summary["upstream_hint"] is None


def test_timings_projection_carries_the_summary() -> None:
    timing = timings_projection(_execution(), SPREAD)
    assert timing["final_answer_upstream_provider"] == "Provider C"
    assert len(timing["upstream_providers"]) == 3


@pytest.mark.parametrize(
    "category", ["no_confirmation", "result_schema_invalid", "review_not_inspected"]
)
def test_native_failure_on_spread_route_gets_one_sentence(category: str) -> None:
    hint = upstream_route_summary(_execution(category), SPREAD)["upstream_hint"]
    assert hint is not None
    assert hint.startswith("Provider C served the final answer on OpenRouter")
    assert "3 upstream providers" in hint
    assert "#structured-results-and-upstream-providers" in hint
    assert "require_parameters" not in hint


def test_single_unpinned_upstream_still_suggests_a_preference() -> None:
    hint = upstream_route_summary(
        _execution("no_confirmation"), [_request("Provider A", "stop")]
    )["upstream_hint"]
    assert hint is not None
    assert "sets no provider preference" in hint


@pytest.mark.parametrize(
    ("execution", "requests"),
    [
        # Pinned to one provider: nothing to suggest.
        (
            _execution("no_confirmation"),
            [_request("Provider A", "stop", {"only": ["provider-a"]})],
        ),
        # Not a native-result run.
        (_execution("no_confirmation", native=False), SPREAD),
        # Another failure category.
        (_execution("timeout"), SPREAD),
        # Not OpenRouter: no upstream recorded.
        (_execution("no_confirmation"), [_request(None, "stop")]),
    ],
)
def test_no_hint_outside_the_documented_conditions(
    execution: SimpleNamespace, requests: list
) -> None:
    assert upstream_route_summary(execution, requests)["upstream_hint"] is None


@pytest.mark.parametrize(
    ("routing", "pinned"),
    [
        ({"only": ["provider-a"]}, True),
        ({"order": ["provider-a"], "allow_fallbacks": False}, True),
        ({"order": ["provider-a"]}, False),
        ({"only": []}, False),
        ({"require_parameters": True}, False),
        (None, False),
    ],
)
def test_pins_upstream_provider(routing: dict | None, pinned: bool) -> None:
    assert pins_upstream_provider(routing) is pinned
