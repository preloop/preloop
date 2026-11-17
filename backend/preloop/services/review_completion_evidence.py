"""Sanitised controller facts for a review, independent of its verdict."""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import crud
from preloop.services.execution_tool_evidence import digest
from preloop.services.upstream_request_summary import public_upstream_summary

POST_RESULT_FAILURES = frozenset(
    {
        "checkpoint_upload",
        "artifact_quota",
        "artifact_capture",
        "artifact_storage",
        "log_shipping",
        "lease_revocation",
        "completion_event_queueing",
        "publication",
        "finalization",
    }
)
UPSTREAM_CLASSES = frozenset(
    {
        "network",
        "upstream_overloaded",
        "upstream_rate_limited",
        "upstream_quota_exhausted",
        "upstream_auth",
        "upstream_protocol",
        "upstream_error",
        "upstream_disconnect",
    }
)


def completion_facts(execution: Any) -> dict[str, Any]:
    """Read only controller-owned completion observations from the receipt."""
    receipt = getattr(execution, "startup_receipt", None)
    facts = receipt.get("_completion") if isinstance(receipt, dict) else None
    return facts if isinstance(facts, dict) else {}


def result_sha256(document: Any) -> str:
    """SHA-256 of a stored result, as published to API callers.

    Canonical form (version 1), applied to the stored result document:

    1. Drop controller-private members at every depth (the restricted
       projection a CI identity reads as ``result``; members named in
       ``result_withheld_paths`` are not part of it).
    2. Serialise as JSON with keys sorted by code point, no whitespace
       (separators ``,`` and ``:``), non-ASCII characters written as UTF-8
       rather than escaped, and no NaN or Infinity.
    3. Hash the UTF-8 bytes with SHA-256; the value is lowercase hex.

    Key order and whitespace of the original document therefore never change
    the digest. In Python: ``hashlib.sha256(json.dumps(result,
    sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())``.

    Args:
        document: The stored ``result`` of an execution.

    Returns:
        Lowercase hexadecimal SHA-256.
    """
    from preloop.services.ci_execution import public_ci_result

    return digest(public_ci_result(document))


def result_availability(execution: Any) -> str:
    """Distinguish a result artifact from pending capture and controller metadata."""
    facts = completion_facts(execution)
    state = facts.get("capture_state")
    if state == "pending" or facts.get("finalization_state") == "pending":
        return "pending"
    document = getattr(execution, "result", None)
    if state == "absent" or document is None:
        return "absent"
    if state == "captured" or facts.get("result_captured_at"):
        return "available"
    # Historical writers put runtime facts in result even without a file.
    # Preserve arbitrary historical documents; these keys are platform-owned.
    metadata = {"container_termination", "_private_publication"}
    if isinstance(document, dict) and document and set(document) <= metadata:
        validation = getattr(execution, "result_schema_validation", None)
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return "absent"
    return "available"


def completion_projection(
    execution: Any, *, publication: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Separate process termination, captured bytes, schema and publication.

    The digest covers the restricted result projection, exactly like the CI
    callback and export grant. Withheld values never contribute a digest.
    """
    facts = completion_facts(execution)
    agent = facts.get("agent_finished")
    finished = None
    if isinstance(agent, dict):
        finished = {
            "status": agent.get("status")
            if agent.get("status")
            in {
                "SUCCEEDED",
                "FAILED",
                "STOPPED",
                "TIMEOUT",
                "CANCELLED",
            }
            else "unknown",
            "exit_code": agent.get("exit_code")
            if type(agent.get("exit_code")) is int
            else None,
        }
    available = result_availability(execution) == "available"
    captured = (
        True
        if available and facts.get("result_captured_at")
        else None
        if available
        else False
    )
    validation = getattr(execution, "result_schema_validation", None)
    valid = validation.get("valid") if isinstance(validation, dict) else None
    return {
        "agent_finished": finished,
        "result_captured": {
            "captured": captured,
            "source": facts.get("result_source")
            if facts.get("result_source")
            in {
                "native_structured_result",
                "file",
            }
            else None,
            "result_sha256": result_sha256(execution.result) if available else None,
        },
        "result_valid": valid if type(valid) is bool else None,
        "review_integrity": review_integrity_projection(execution),
        # The publication package owns this projection. This is a pointer,
        # never an agent-authored publication claim copied from result.json.
        "publication": "#/platform_review" if publication is not None else None,
        "post_result_failures": sorted(
            (set(facts.get("post_result_failures") or []) & POST_RESULT_FAILURES)
            | (
                {"publication"}
                if available
                and publication is not None
                and publication.get("status") == "failed"
                else set()
            )
        ),
    }


def review_integrity_projection(execution: Any) -> dict[str, Any] | None:
    """The controller's binding and evidence-floor outcome, without values.

    Null when the checks did not run (not a read-only review, a run that did
    not otherwise succeed, or an execution finished before they existed).
    """
    stored = completion_facts(execution).get("review_integrity")
    if not isinstance(stored, dict) or stored.get("status") not in {
        "verified",
        "rejected",
    }:
        return None
    inspection = stored.get("inspection")
    inspection = inspection if isinstance(inspection, dict) else {}
    return {
        "status": stored["status"],
        "category": stored.get("category")
        if stored.get("category")
        in {"review_binding_mismatch", "review_not_inspected", "review_degenerate"}
        else None,
        "reasons": [r for r in stored.get("reasons") or [] if isinstance(r, str)],
        "bindings": [
            {
                key: item.get(key)
                for key in ("field", "fact", "outcome")
                if isinstance(item.get(key), str)
            }
            for item in stored.get("bindings") or []
            if isinstance(item, dict)
        ],
        "inspection": {
            "required": inspection.get("required") is True,
            "inspected": inspection.get("inspected") is True,
            **{
                key: inspection.get(key) if type(inspection.get(key)) is int else 0
                for key in (
                    "repository_tool_calls",
                    "other_tool_calls",
                    "native_commands",
                    "diff_commands",
                )
            },
        },
    }


def _timestamp(value: Any) -> datetime | None:
    """Normalize controller timestamps; leave absent observations unknown."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _seconds(start: Any, end: Any) -> float | None:
    """Measure only when both boundaries are recorded and ordered."""
    first, last = _timestamp(start), _timestamp(end)
    if first is None or last is None or last < first:
        return None
    return round((last - first).total_seconds(), 6)


def terminal_cause(execution: Any) -> str | None:
    """Map controller outcomes without guessing a cause from model prose."""
    status = getattr(execution, "status", None)
    category = getattr(execution, "failure_category", None)
    if status not in {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "TIMED_OUT",
        "TIMEOUT",
        "CANCELLED",
    }:
        return None
    if status in {"STOPPED", "CANCELLED"}:
        return "stopped"
    if status in {"TIMED_OUT", "TIMEOUT"} or category in {
        "timeout",
        "execution_timeout",
    }:
        return "timeout"
    if category in {"budget_exceeded", "default_limit_reached"}:
        return "budget"
    if category in {
        "model_transient",
        "model_auth",
        "model_quota",
        "provider_billing",
        "repeated_upstream_failure",
    }:
        return "upstream_error"
    if status == "SUCCEEDED":
        return "completed"
    if (
        set(completion_facts(execution).get("post_result_failures") or [])
        & POST_RESULT_FAILURES
    ):
        return "post_result_failure"
    if category in {"agent_no_progress", "no_confirmation"} or (
        getattr(execution, "result", None) is None and category in {None, "unknown"}
    ):
        return "no_result"
    return "failed"


def _identifier(value: Any) -> str | None:
    """Only model/provider identifiers, never arbitrary diagnostic strings."""
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}", value)
        else None
    )


def timings_projection(
    execution: Any, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    """Aggregate persisted request facts; unknown boundaries stay null.

    Args:
        execution: Authorized execution row.
        requests: Account- and execution-scoped gateway facts from CRUD.

    Returns:
        Durations in seconds, counts of recorded requests and effective settings.
    """
    receipt = getattr(execution, "startup_receipt", None)
    receipt = receipt if isinstance(receipt, dict) else {}
    phases = receipt.get("_phases") or {}
    facts = completion_facts(execution)
    effective: Counter[tuple[Any, ...]] = Counter()
    retry_count: int | None = 0
    total = 0.0
    first_error = None
    first_response_seconds: float | None = None
    for request in requests:
        response_seconds = _seconds(
            receipt.get("agent_started_at"), request.get("first_response_at")
        )
        if response_seconds is not None:
            first_response_seconds = (
                min(first_response_seconds, response_seconds)
                if first_response_seconds is not None
                else response_seconds
            )
        params = request.get("upstream_parameters") or {}
        params = params if isinstance(params, dict) else {}
        effort = params.get("reasoning_effort")
        effort = (
            effort
            if effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
            else None
        )
        limit = params.get("max_tokens")
        limit = limit if type(limit) is int and limit > 0 else None
        effective[
            (
                _identifier(params.get("model")),
                _identifier(request.get("provider_name")),
                effort,
                limit,
                json.dumps(public_upstream_summary(params), sort_keys=True),
                (
                    request.get("upstream_provider")
                    if isinstance(request.get("upstream_provider"), str)
                    and re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9 ./_-]{0,199}",
                        request["upstream_provider"],
                    )
                    else None
                ),
            )
        ] += 1
        duration = request.get("duration")
        if isinstance(duration, (int, float)) and duration >= 0:
            total += duration
        retries = request.get("upstream_retries")
        if type(retries) is not int or retries < 0:
            retry_count = None
        elif retry_count is not None:
            retry_count += retries + int(request.get("is_retry") is True)
        if request.get("error_class") is None:
            first_error = None
        elif first_error is None and request.get("error_class") in UPSTREAM_CLASSES:
            first_error = request["error_class"]
    cause = terminal_cause(execution)
    return {
        "queue_wait_seconds": _seconds(
            getattr(execution, "created_at", None),
            getattr(execution, "launch_requested_at", None),
        ),
        "startup_seconds": _seconds(
            getattr(execution, "launch_requested_at", None),
            receipt.get("agent_started_at"),
        ),
        "clone_seconds": _seconds(
            phases.get("clone_started"), phases.get("clone_finished")
        ),
        "setup_seconds": _seconds(
            phases.get("setup_started"), phases.get("setup_finished")
        ),
        "first_model_response_seconds": first_response_seconds,
        "first_model_response_unavailable_reason": (
            "not_recorded" if first_response_seconds is None else None
        ),
        "model_request_count": len(requests),
        "retry_count": retry_count,
        "total_model_seconds": round(total, 6),
        "finalization_seconds": _seconds(
            facts.get("finalization_started_at"), getattr(execution, "end_time", None)
        ),
        "terminal_cause": cause,
        "first_causal_upstream_error_class": first_error
        if cause == "upstream_error"
        else None,
        "effective_requests": [
            {
                "model": key[0],
                "provider": key[1],
                "reasoning_effort": key[2],
                "max_output_tokens": key[3],
                "request_count": count,
                "upstream_parameters": json.loads(key[4]),
                "upstream_provider": key[5],
            }
            for key, count in effective.items()
        ],
    }


def execution_timings(db: Session, execution: Any, account_id: Any) -> dict[str, Any]:
    """Load narrow request facts only after the caller authorizes this execution."""
    requests = crud.crud_api_usage.review_request_facts(
        db, execution_id=execution.id, account_id=account_id
    )
    return timings_projection(execution, requests)
