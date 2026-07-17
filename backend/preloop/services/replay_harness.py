"""Re-execution replay harness for session-optimization savings (T14).

The deterministic ``savings_measurement`` module owns the exact INPUT-token
delta of applying a candidate optimization. This module layers the END-TO-END
delta on top: it re-executes both the original and the candidate-modified
request against the upstream model ``n_runs`` times and reports the median
output-inclusive token delta together with a noise band.

Model re-execution is nondeterministic (GPU batching, decoding), so a single
run's delta is untrustworthy. The harness runs each side ``n_runs`` times,
reports the median and the observed min/max as a band, and flags the result
``inconclusive`` when the band is as wide as the signal or straddles zero. When
inconclusive, the honest number to surface is the deterministic input-token
delta from ``input_savings``, never a noisy end-to-end figure.

The upstream call itself is injected as ``reexecute`` so this core is pure and
unit-testable without spending real tokens; the live wiring (fetching the
stored request, calling the gateway under the user's budget cap, per-run
consent) lives in the endpoint layer.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from preloop.services.savings_measurement import (
    InputTokenSavings,
    apply_candidate,
    compute_input_token_savings,
)


class ReplayBudgetExceededError(Exception):
    """Raised before a re-execution that would exceed the user's remaining cap.

    The guard checks the projected cost BEFORE calling upstream, so raising this
    means no spend occurred on the aborted run and the accumulated ``spent`` is
    unchanged. The replay harness holds all results in local state and only
    materializes a measurement at the end, so propagating this mid-loop leaves
    no partial records — the abort is clean.
    """


@dataclass
class BudgetGuardedReexecutor:
    """Wrap a raw re-execution callable with a hard remaining-budget cap.

    Per-user replay (T15) re-executes against the user's own budget. This guard
    enforces their hard cap by projecting each run's cost BEFORE spending and
    aborting (``ReplayBudgetExceededError``) rather than tripping their production
    budget mid-run.

    Attributes:
        raw_reexecute: The underlying upstream re-execution callable.
        remaining_budget: The user's remaining hard-cap headroom in dollars.
        estimate_cost: Projects a payload's cost before it is run.
    """

    raw_reexecute: Callable[[dict[str, Any]], "ReplayRunTokens"]
    remaining_budget: float
    estimate_cost: Callable[[dict[str, Any]], float]
    _spent: float = field(default=0.0, init=False)

    def __call__(self, payload: dict[str, Any]) -> "ReplayRunTokens":
        projected = self.estimate_cost(payload)
        if self._spent + projected > self.remaining_budget:
            raise ReplayBudgetExceededError(
                f"replay would exceed remaining budget: spent={self._spent:.4f} "
                f"+ projected={projected:.4f} > cap={self.remaining_budget:.4f}"
            )
        run = self.raw_reexecute(payload)
        self._spent += run.cost if run.cost is not None else projected
        return run

    @property
    def spent(self) -> float:
        """Total dollars actually spent across completed runs."""
        return self._spent


@dataclass(frozen=True)
class ReplayRunTokens:
    """Token/cost outcome of a single upstream re-execution of one payload.

    Attributes:
        total_tokens: Total (prompt + completion) tokens the run consumed.
        cost: Estimated dollar cost of the run, or ``None`` when unknown (e.g.
            subscription-auth traffic with no marginal dollar price).
    """

    total_tokens: int
    cost: float | None = None


@dataclass(frozen=True)
class ReplayMeasurement:
    """End-to-end replay savings for one candidate optimization.

    Attributes:
        input_savings: The deterministic, exact input-token delta. This is the
            authoritative headline number; the end-to-end fields below add the
            output side but carry measurement noise.
        n_runs: Number of re-execution pairs performed per side.
        end_to_end_delta_median: Median of per-run ``original_total -
            modified_total`` token deltas. Positive means tokens saved.
        end_to_end_delta_low: Smallest observed per-run delta (band floor).
        end_to_end_delta_high: Largest observed per-run delta (band ceiling).
        original_total_median: Median total tokens of the original request.
        modified_total_median: Median total tokens of the modified request.
        inconclusive: ``True`` when the end-to-end signal is within measurement
            noise (band as wide as the median, or straddling zero, or a zero
            median). Callers must fall back to ``input_savings`` for display.
        cost_spent: Sum of all run costs across both sides, or ``None`` when no
            run reported a cost (e.g. subscription-auth).
    """

    input_savings: InputTokenSavings
    n_runs: int
    end_to_end_delta_median: float
    end_to_end_delta_low: float
    end_to_end_delta_high: float
    original_total_median: float
    modified_total_median: float
    inconclusive: bool
    cost_spent: float | None


def measure_replay_savings(
    request_payload: dict[str, Any],
    *,
    reexecute: Callable[[dict[str, Any]], ReplayRunTokens],
    removed_tool_names: set[str] | None = None,
    filtered_output_fields: Mapping[str, Sequence[str]] | None = None,
    n_runs: int = 3,
) -> ReplayMeasurement:
    """Measure end-to-end savings by re-executing original vs modified.

    Args:
        request_payload: The stored gateway request payload to replay.
        reexecute: Injected callable that runs one payload upstream and returns
            its :class:`ReplayRunTokens`. Called ``2 * n_runs`` times.
        removed_tool_names: Tool names the candidate removes; ``None``/empty
            leaves tools untouched.
        filtered_output_fields: Mapping of tool name to output field names the
            candidate drops; ``None``/empty is a no-op.
        n_runs: Re-execution pairs per side. Must be >= 1. More runs tighten the
            noise band at the cost of more model spend.

    Returns:
        A :class:`ReplayMeasurement` with the deterministic input-token delta
        and the noise-banded end-to-end delta.

    Raises:
        ValueError: If ``n_runs`` is less than 1.
    """
    if n_runs < 1:
        raise ValueError("n_runs must be at least 1")

    input_savings = compute_input_token_savings(
        request_payload,
        removed_tool_names=removed_tool_names,
        filtered_output_fields=filtered_output_fields,
    )
    modified_payload = apply_candidate(
        request_payload,
        removed_tool_names=removed_tool_names,
        filtered_output_fields=filtered_output_fields,
    )

    original_totals: list[int] = []
    modified_totals: list[int] = []
    deltas: list[int] = []
    costs: list[float] = []
    for _ in range(n_runs):
        original_run = reexecute(request_payload)
        modified_run = reexecute(modified_payload)
        original_totals.append(original_run.total_tokens)
        modified_totals.append(modified_run.total_tokens)
        deltas.append(original_run.total_tokens - modified_run.total_tokens)
        for run in (original_run, modified_run):
            if run.cost is not None:
                costs.append(run.cost)

    median_delta = float(statistics.median(deltas))
    low = float(min(deltas))
    high = float(max(deltas))
    band_width = high - low
    inconclusive = (
        median_delta == 0.0 or (low <= 0.0 <= high) or band_width >= abs(median_delta)
    )

    return ReplayMeasurement(
        input_savings=input_savings,
        n_runs=n_runs,
        end_to_end_delta_median=median_delta,
        end_to_end_delta_low=low,
        end_to_end_delta_high=high,
        original_total_median=float(statistics.median(original_totals)),
        modified_total_median=float(statistics.median(modified_totals)),
        inconclusive=inconclusive,
        cost_spent=sum(costs) if costs else None,
    )
