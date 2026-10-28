"""Admission rules for schema-constrained Codex cold-start executions."""

from typing import Any


RESTORE_KEYS = (
    "cli_session_restore_archive",
    "native_session_reference",
    "confirmation_nudge",
)


def validate_native_result_configuration(config: Any) -> None:
    """Reject saved policies that require a schema-less native resume."""
    if not isinstance(config, dict) or config.get("native_result") is not True:
        return
    feedback = config.get("feedback")
    if isinstance(feedback, dict) and feedback.get("enabled") is True:
        raise ValueError("native_result does not support feedback session resumes")
    if "max_resumes_per_pr" in config and config["max_resumes_per_pr"] not in (0, "0"):
        raise ValueError("native_result requires max_resumes_per_pr to be zero")
    if any(config.get(key) for key in RESTORE_KEYS):
        raise ValueError("native_result does not support native session restoration")


def validate_native_result_admission(
    flow: Any, event_data: dict[str, Any] | None = None
) -> None:
    """Reject historical incompatible policies and follow-ups before launch.

    Existing records can predate save validation. A correlated continuation is
    refused rather than silently losing its restored state in a cold start.
    """
    config = getattr(flow, "agent_config", None)
    validate_native_result_configuration(config)
    if not isinstance(config, dict) or config.get("native_result") is not True:
        return
    event_data = event_data or {}
    if event_data.get("_resume") or any(event_data.get(key) for key in RESTORE_KEYS):
        raise ValueError(
            "native_result requires a cold start; correlated resumes are unsupported"
        )
