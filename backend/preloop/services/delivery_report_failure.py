"""Product-facing failure documents for reports that produced no report."""

from __future__ import annotations

import re
from typing import Any

from preloop.services.delivery_report import report_window
from preloop.services.flow_failure_category import derive_failure_category

FAILURE_STATUSES = {"FAILED", "TIMEOUT", "TIMED_OUT"}


_REASONS = {
    "launch_payload_too_large": "its repository setup was too large to start",
    "repository_access": "repository access was refused",
    "model_auth": "the model provider refused its credentials",
    "provider_billing": "the model provider requires available credit",
    "model_config": "the selected model configuration was refused",
    "model_transient": "the model provider was unavailable",
    "runner_error": "the runner could not complete the run",
    "runner_conflict": "another runner job occupied this run's slot",
    "setup_failed": "workspace setup failed",
    "tool_error": "a required tool call failed",
    "agent_error": "the agent reported an error",
    "agent_no_progress": "the agent stopped without completing the report",
    "no_confirmation": "the agent did not confirm a completed report",
    "result_schema_invalid": "the agent returned an invalid report",
    "verification_failed": "the report's verification checks failed",
    "verification_blocked": "the report's verification checks could not run",
    "model_stream_idle": "the model provider stopped responding",
    "repeated_upstream_failure": "requests to the model provider repeatedly failed",
    "approval_denied": "a required approval was declined",
    "approval_timeout": "a required approval did not arrive in time",
    "approval_unavailable": "a required approval could not be requested",
    "timeout": "the run reached its time limit",
    "budget_exceeded": "the run reached its spending limit",
    "default_limit_reached": "the run reached its execution limit",
}


def failure_reason(category: str | None) -> str:
    """The plain-language sentence for a failed report run's category."""
    return (
        "This Delivery Report could not finish because "
        + _REASONS.get(category or "unknown", "the report run failed")
        + "."
    )


def failure_report(execution: Any) -> dict[str, Any]:
    """Explain the failing layer without emailing arbitrary runner output."""
    error = getattr(execution, "error_message", None) or ""
    category = derive_failure_category(
        status=execution.status,
        error_message=error,
        explicit_category=getattr(execution, "failure_category", None),
    )
    reason = failure_reason(category)
    if category == "launch_payload_too_large":
        count = re.search(r"(\d+) repositories", error)
        size = re.search(r"(?:is |pass )(\d+) bytes", error)
        limit = re.search(r"(?:limit(?: is)? |at most |the )(\d+) byte", error)
        details = []
        if count:
            details.append(f"{count[1]} repositories")
        if size:
            details.append(f"{size[1]} bytes")
        if limit:
            details.append(f"limit {limit[1]} bytes")
        if details:
            reason += " Setup: " + "; ".join(details) + "."
    trigger = getattr(execution, "trigger_event_details", None) or {}
    return {
        "status": "error",
        "reason": reason,
        "failure_category": category,
        "window": report_window(
            trigger.get("payload"), now=getattr(execution, "start_time", None)
        ),
    }


def report_document(execution: Any) -> dict[str, Any]:
    """Expose failed executions even when startup produced no result object."""
    result = dict(execution.result) if isinstance(execution.result, dict) else {}
    if str(execution.status).upper() in FAILURE_STATUSES:
        return {**failure_report(execution), **result, "status": "error"}
    return result
