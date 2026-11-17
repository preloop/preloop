"""Terminal outcome invariants shared by controller and CRUD writers."""

from typing import Any

from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.flow_failure_messages import AGENT_REPORTED_FAILURE


def result_failure_signal(result: Any) -> tuple[str, str] | None:
    """Return an explicit failure in either result contract field.

    A review verdict of ``fail`` describes completed work. ``error`` means
    the work did not complete, even if a success marker was also emitted.
    """
    if not isinstance(result, dict):
        return None
    for field, failures in (
        ("status", {"error", "failed", "failure"}),
        ("verdict", {"error"}),
    ):
        value = result.get(field)
        if isinstance(value, str) and value.strip().lower() in failures:
            return field, value
    return None


def normalize_execution_outcome(
    values: dict[str, Any], existing: Any = None, *, fallback: str = "execution_error"
) -> None:
    """Enforce result failure and category invariants on a persistence update.

    Args:
        values: Fields to persist; updated in place.
        existing: Existing execution, for status-only and result-only updates.
        fallback: Layer recording an otherwise unclassified failure.
    """
    status = values.get("status") or getattr(existing, "status", None)
    result = values.get("result", getattr(existing, "result", None))
    signal = result_failure_signal(result)
    if status == "SUCCEEDED" and signal is not None:
        status = values["status"] = "FAILED"
    if not isinstance(status, str):
        return
    error_message = values.get("error_message") or getattr(
        existing, "error_message", None
    )
    category = derive_failure_category(
        status=status,
        error_message=error_message,
        explicit_category=values.get("failure_category")
        or getattr(existing, "failure_category", None),
        fallback_category=fallback,
    )
    if (
        signal is not None
        and status in {"FAILED", "ERROR", "ABORTED"}
        and category in {None, "unknown", "execution_error", "agent_reported_failure"}
    ):
        field, value = signal
        values["error_message"] = error_message or AGENT_REPORTED_FAILURE.format(
            field=field, value=value
        )
        category = "agent_reported_failure"
    if category is not None:
        values["failure_category"] = category
