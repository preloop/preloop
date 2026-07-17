"""Tests for the re-execution replay harness (T14).

The upstream call is injected, so these tests exercise the aggregation and
noise-band logic deterministically without spending tokens.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

import pytest

from preloop.services.replay_harness import (
    BudgetGuardedReexecutor,
    ReplayBudgetExceededError,
    ReplayRunTokens,
    measure_replay_savings,
)
from preloop.services.savings_measurement import (
    compute_input_token_savings,
)

_TWO_TOOL_PAYLOAD: dict[str, Any] = {
    "model": "openai/gpt-5",
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [
        {"type": "function", "function": {"name": "search"}},
        {"type": "function", "function": {"name": "read_file"}},
    ],
}


def _tool_count_reexecute(
    *, base: int = 10, per_tool: int = 100, cost: float | None = None
) -> Callable[[dict[str, Any]], ReplayRunTokens]:
    """A deterministic fake: tokens scale with the number of tools carried."""

    def rx(payload: dict[str, Any]) -> ReplayRunTokens:
        n_tools = len(payload.get("tools") or [])
        return ReplayRunTokens(total_tokens=base + per_tool * n_tools, cost=cost)

    return rx


def _sequence_reexecute(
    totals: Iterable[int], *, cost: float | None = None
) -> Callable[[dict[str, Any]], ReplayRunTokens]:
    """A fake returning preset totals in call order (original, modified, ...)."""
    it = iter(totals)

    def rx(_: dict[str, Any]) -> ReplayRunTokens:
        return ReplayRunTokens(total_tokens=next(it), cost=cost)

    return rx


def test_deterministic_reexecution_yields_conclusive_positive_delta():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(),
        removed_tool_names={"search"},
        n_runs=3,
    )
    # Original carries 2 tools (210), modified carries 1 (110): delta 100 each
    # run, so the band collapses and the result is conclusive.
    assert result.end_to_end_delta_median == 100.0
    assert result.end_to_end_delta_low == 100.0
    assert result.end_to_end_delta_high == 100.0
    assert result.inconclusive is False
    assert result.original_total_median == 210.0
    assert result.modified_total_median == 110.0


def test_input_savings_passthrough_matches_deterministic_module():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(),
        removed_tool_names={"search"},
        n_runs=2,
    )
    expected = compute_input_token_savings(
        _TWO_TOOL_PAYLOAD, removed_tool_names={"search"}
    )
    assert result.input_savings == expected
    assert result.input_savings.delta_tokens > 0


def test_band_straddling_zero_is_inconclusive():
    # Per-run deltas: +5, -5, +1 -> median 1 but the band crosses zero.
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_sequence_reexecute([100, 95, 100, 105, 100, 99]),
        removed_tool_names={"search"},
        n_runs=3,
    )
    assert result.end_to_end_delta_low == -5.0
    assert result.end_to_end_delta_high == 5.0
    assert result.inconclusive is True


def test_band_wider_than_signal_is_inconclusive():
    # Per-run deltas: +10, +2, +8 -> all positive, median 8, band width 8 >= 8.
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_sequence_reexecute([100, 90, 100, 98, 100, 92]),
        removed_tool_names={"search"},
        n_runs=3,
    )
    assert result.end_to_end_delta_median == 8.0
    assert result.inconclusive is True


def test_noop_candidate_is_inconclusive_zero_delta():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(),
        n_runs=3,
    )
    # No tools removed: original == modified, every delta is 0.
    assert result.end_to_end_delta_median == 0.0
    assert result.inconclusive is True
    assert result.input_savings.delta_tokens == 0


def test_reexecute_called_twice_per_run():
    calls: list[int] = []

    def counting(payload: dict[str, Any]) -> ReplayRunTokens:
        calls.append(len(payload.get("tools") or []))
        return ReplayRunTokens(total_tokens=1)

    measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=counting,
        removed_tool_names={"search"},
        n_runs=4,
    )
    assert len(calls) == 8  # 2 sides * 4 runs


def test_cost_is_summed_across_runs():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(cost=0.25),
        removed_tool_names={"search"},
        n_runs=2,
    )
    # 2 runs * 2 sides * 0.25
    assert result.cost_spent == pytest.approx(1.0)


def test_cost_none_when_no_run_reports_cost():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(cost=None),
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert result.cost_spent is None


def test_n_runs_one_is_allowed():
    result = measure_replay_savings(
        _TWO_TOOL_PAYLOAD,
        reexecute=_tool_count_reexecute(),
        removed_tool_names={"search"},
        n_runs=1,
    )
    assert result.n_runs == 1
    assert result.end_to_end_delta_median == 100.0


def test_n_runs_zero_raises():
    with pytest.raises(ValueError, match="n_runs"):
        measure_replay_savings(
            _TWO_TOOL_PAYLOAD,
            reexecute=_tool_count_reexecute(),
            n_runs=0,
        )


# --------------------------------------------------------------------------
# BudgetGuardedReexecutor (T15 money-path guard)
# --------------------------------------------------------------------------


def test_budget_guard_allows_runs_under_cap_and_tracks_spend():
    calls: list[int] = []

    def raw(_: dict[str, Any]) -> ReplayRunTokens:
        calls.append(1)
        return ReplayRunTokens(total_tokens=5, cost=0.1)

    guard = BudgetGuardedReexecutor(
        raw_reexecute=raw, remaining_budget=1.0, estimate_cost=lambda _: 0.1
    )
    for _ in range(3):
        guard({})
    assert len(calls) == 3
    assert guard.spent == pytest.approx(0.3)


def test_budget_guard_aborts_before_spending_when_over_cap():
    calls: list[int] = []

    def raw(_: dict[str, Any]) -> ReplayRunTokens:
        calls.append(1)
        return ReplayRunTokens(total_tokens=5, cost=0.1)

    # Cap 0.25: run1 (0.1) ok, run2 (0.2) ok, run3 would project to 0.3 > 0.25.
    guard = BudgetGuardedReexecutor(
        raw_reexecute=raw, remaining_budget=0.25, estimate_cost=lambda _: 0.1
    )
    guard({})
    guard({})
    with pytest.raises(ReplayBudgetExceededError):
        guard({})
    # The aborted run never called upstream and never moved the meter: clean.
    assert len(calls) == 2
    assert guard.spent == pytest.approx(0.2)


def test_budget_guard_falls_back_to_projected_cost_when_run_cost_absent():
    guard = BudgetGuardedReexecutor(
        raw_reexecute=lambda _: ReplayRunTokens(total_tokens=5, cost=None),
        remaining_budget=1.0,
        estimate_cost=lambda _: 0.15,
    )
    guard({})
    assert guard.spent == pytest.approx(0.15)


def test_harness_propagates_budget_abort_without_partial_measurement():
    # A guard that permits 3 upstream calls then aborts. With n_runs=2 the
    # harness makes 4 calls, so the 4th aborts mid-measurement.
    guard = BudgetGuardedReexecutor(
        raw_reexecute=_tool_count_reexecute(cost=0.1),
        remaining_budget=0.35,  # allows 3 runs at 0.1, blocks the 4th
        estimate_cost=lambda _: 0.1,
    )
    with pytest.raises(ReplayBudgetExceededError):
        measure_replay_savings(
            _TWO_TOOL_PAYLOAD,
            reexecute=guard,
            removed_tool_names={"search"},
            n_runs=2,
        )
    # Three runs' worth of spend landed; no ReplayMeasurement was produced.
    assert guard.spent == pytest.approx(0.3)
