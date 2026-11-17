"""Execution persistence never loses explicit result failures or their cause."""

from types import SimpleNamespace

import pytest

from preloop.services.flow_execution_outcome import normalize_execution_outcome


@pytest.mark.parametrize("category", [None, "", "unknown"])
@pytest.mark.parametrize(
    "status", ["FAILED", "ERROR", "TIMEOUT", "TIMED_OUT", "ABORTED"]
)
def test_missing_category_is_named(status: str, category: str | None) -> None:
    """Both fresh failures and legacy categories receive a persistence fallback."""
    values = {"status": status, "failure_category": category}
    normalize_execution_outcome(values)
    assert values["failure_category"] == (
        "timeout" if status in {"TIMEOUT", "TIMED_OUT"} else "execution_error"
    )


@pytest.mark.parametrize(
    "result",
    [
        {"status": "error"},
        {"verdict": "error", "success": False},
        {"status": "success", "verdict": "error"},
    ],
)
@pytest.mark.parametrize("result_only", [False, True])
def test_result_failure_survives_every_update_shape(
    result: dict, result_only: bool
) -> None:
    """A late result and a status-only write enforce the same contract."""
    existing = SimpleNamespace(status="SUCCEEDED", result=result)
    values = {"result": result} if result_only else {"status": "SUCCEEDED"}
    normalize_execution_outcome(values, existing)
    assert values["status"] == "FAILED"
    assert values["failure_category"] == "agent_reported_failure"
    assert "explicit failure" in values["error_message"]


@pytest.mark.parametrize(
    "verdict", ["pass", "fail", "pass_with_findings", "request_changes"]
)
def test_completed_review_verdict_is_not_execution_failure(verdict: str) -> None:
    """Findings concern the reviewed work, not whether the review ran."""
    values = {"status": "SUCCEEDED", "result": {"verdict": verdict}}
    normalize_execution_outcome(values)
    assert values["status"] == "SUCCEEDED"
    assert "failure_category" not in values


@pytest.mark.parametrize("status", ["FAILED", "ERROR", "ABORTED"])
@pytest.mark.parametrize("category", [None, "unknown", "execution_error"])
def test_failed_result_names_the_cause(status: str, category: str | None) -> None:
    """An already failed completion retains the result's explicit diagnosis."""
    values = {
        "status": status,
        "result": {"verdict": "error"},
        "failure_category": category,
    }
    normalize_execution_outcome(values)
    assert values["status"] == status
    assert values["failure_category"] == "agent_reported_failure"
    assert "explicit failure" in values["error_message"]


@pytest.mark.parametrize("status", ["FAILED", "SUCCEEDED"])
def test_result_error_preserves_specific_failure(status: str) -> None:
    """A retained model diagnosis takes precedence over a later error result."""
    existing = SimpleNamespace(
        status="FAILED",
        failure_category="context_truncated_suspected",
        error_message="The model server appears to have truncated the prompt",
        result=None,
    )
    values = {"status": status, "result": {"status": "error"}}
    normalize_execution_outcome(values, existing)
    assert values["status"] == "FAILED"
    assert values["failure_category"] == "context_truncated_suspected"
    assert values.get("error_message", existing.error_message) == existing.error_message
