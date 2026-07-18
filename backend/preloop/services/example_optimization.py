"""Bundled example session for the Optimize tab's first-run experience.

A brand-new account has no runtime sessions, so the Optimize tab — the surface
that carries Preloop's headline cost claim — renders empty on first visit.
This module supplies a bundled *example* session so the tab can demonstrate
what it produces, without ever presenting the example as the user's own data.

Two properties make this honest:

* **The numbers are real analysis, not fixtures.** The transcript in
  ``data/example_session.json`` is fed through exactly the same analyzers and
  the same deterministic suggestion generator that run on real sessions
  (:func:`~preloop.services.context_analysis.build_profile_from_events`,
  :meth:`SessionOptimizationService._local_optimization_suggestions`,
  :func:`~preloop.services.context_analysis.compute_profile_savings`). No
  savings figure is hardcoded; token counts are measured from the bundled
  content itself. If the analyzers change, this example changes with them.
* **It cannot contaminate anything.** Nothing here writes to the database.
  There is no ``RuntimeSession`` row, no ``ApiUsage`` row, no cached
  optimization result and no audit event, so the example is invisible to
  cost totals, budget accumulation, admin stats, and launch telemetry. That
  is a structural guarantee, not a filter that a future aggregate query could
  forget to apply.

The transcript is a constructed example (see ``provenance`` in the document),
not a recording of real user traffic; the response carries that provenance
string so the console can state it plainly.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.schemas.gateway_usage import (
    GatewayTokenUsage,
    RuntimeSessionOptimizationResponse,
    RuntimeSessionSummary,
)
from preloop.services.context_analysis import (
    GatewayCallEvent,
    build_profile_from_events,
    estimate_tokens,
)

TRANSCRIPT_PATH = Path(__file__).resolve().parent / "data" / "example_session.json"

# Session id for the example. Deliberately not a UUID so it can never collide
# with a real ``runtime_session.id`` and can never be mistaken for one by a
# client that stores it.
EXAMPLE_SESSION_ID = "example-session"

# Shown verbatim in the console next to the example's numbers.
EXAMPLE_NOTICE = (
    "This is a bundled example session, not your data. It is analyzed by the "
    "same engine that analyzes your own sessions. Your sessions appear here "
    "once an onboarded agent makes its first model call."
)


class ExampleSessionUnavailableError(RuntimeError):
    """Raised when the bundled example transcript is missing or unreadable."""


@lru_cache(maxsize=1)
def load_example_transcript() -> dict[str, Any]:
    """Load and cache the bundled example transcript document.

    Returns:
        The parsed transcript document.

    Raises:
        ExampleSessionUnavailableError: If the file is absent or not valid
            JSON describing at least one event.
    """
    try:
        raw = TRANSCRIPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExampleSessionUnavailableError(
            f"Example session transcript not readable at {TRANSCRIPT_PATH}"
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ExampleSessionUnavailableError(
            f"Example session transcript is not valid JSON at {TRANSCRIPT_PATH}"
        ) from exc
    if not isinstance(document, dict) or not document.get("events"):
        raise ExampleSessionUnavailableError(
            "Example session transcript contains no events"
        )
    return document


def _measure_prompt_tokens(request: dict[str, Any]) -> int:
    """Estimate prompt tokens for one request from its own content.

    Deriving this from the bundled bytes (rather than storing a number in the
    fixture) keeps the example self-consistent: the reported scope can never
    drift from the content the analyzers actually measure.

    Args:
        request: The request object holding ``messages`` and ``tools``.

    Returns:
        Estimated prompt tokens for the request.
    """
    total = 0
    messages = request.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                total += estimate_tokens(content)
            elif content is not None:
                total += estimate_tokens(
                    json.dumps(content, ensure_ascii=False, default=str)
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                total += estimate_tokens(
                    json.dumps(tool_calls, ensure_ascii=False, default=str)
                )
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        total += estimate_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    return total


def build_example_events(
    document: Optional[dict[str, Any]] = None,
) -> list[GatewayCallEvent]:
    """Build analyzer input events from the bundled transcript.

    Args:
        document: Parsed transcript; loaded from disk when omitted.

    Returns:
        Gateway call events ordered oldest-first, with prompt and cost fields
        derived from the transcript's own content and stated pricing.
    """
    document = document or load_example_transcript()
    input_rate = float(document.get("input_cost_per_token") or 0.0)
    output_rate = float(document.get("output_cost_per_token") or 0.0)

    events: list[GatewayCallEvent] = []
    for index, entry in enumerate(document["events"]):
        request = entry.get("request") or {}
        prompt_tokens = _measure_prompt_tokens(request)
        completion_tokens = int(entry.get("completion_tokens") or 0)
        payload: dict[str, Any] = {
            "request": request,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "outcome": entry.get("outcome", "success"),
            "status_code": entry.get("status_code", 200),
            "estimated_cost": round(
                prompt_tokens * input_rate + completion_tokens * output_rate, 6
            ),
            "model_alias": entry.get("model_alias") or document.get("model_alias"),
            "provider_name": entry.get("provider_name")
            or document.get("provider_name"),
        }
        events.append(
            GatewayCallEvent(
                event_id=str(entry.get("event_id") or f"example-event-{index + 1}"),
                payload=payload,
            )
        )
    return events


def _build_example_summary(
    document: dict[str, Any], events: list[GatewayCallEvent]
) -> RuntimeSessionSummary:
    """Build the session summary the suggestion generator expects.

    Totals are summed from the measured events rather than declared in the
    fixture, so the summary and the profile always agree.

    Args:
        document: Parsed transcript document.
        events: Measured gateway call events.

    Returns:
        A summary describing the example session.
    """
    from datetime import datetime, timezone

    prompt_tokens = sum(int(event.payload["prompt_tokens"]) for event in events)
    completion_tokens = sum(int(event.payload["completion_tokens"]) for event in events)
    estimated_cost = round(
        sum(float(event.payload["estimated_cost"]) for event in events), 6
    )
    started_at = datetime(2026, 7, 18, 9, 14, 22, tzinfo=timezone.utc)

    return RuntimeSessionSummary(
        id=EXAMPLE_SESSION_ID,
        session_source_type="example",
        session_source_id=str(document.get("session_reference") or "example"),
        session_reference=str(document.get("session_reference") or "example"),
        runtime_principal_type="example",
        runtime_principal_id="example",
        runtime_principal_name="Example agent",
        title=str(document.get("title") or "Example session"),
        started_at=started_at,
        last_activity_at=started_at,
        latest_model_alias=document.get("model_alias"),
        latest_provider_name=document.get("provider_name"),
        total_requests=len(events),
        successful_requests=len(events),
        failed_requests=0,
        token_usage=GatewayTokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        estimated_cost=estimated_cost,
    )


def build_example_optimization_response(
    db: Session,
) -> RuntimeSessionOptimizationResponse:
    """Analyze the bundled example session and return its suggestions.

    Runs the production deterministic path end to end. No LLM is called (so
    the example costs nothing and is reproducible), no action specs are
    attached (a bundled example is not applicable to the account's agents),
    and nothing is written to the database.

    Args:
        db: Database session, used only to construct the optimization service.
            No query or write is issued against it.

    Returns:
        The example's optimization response, flagged as an example.

    Raises:
        ExampleSessionUnavailableError: If the bundled transcript is missing.
    """
    # Imported here to avoid a circular import: session_optimization imports
    # from context_analysis, and the endpoint module imports both.
    from preloop.services.session_optimization import SessionOptimizationService

    document = load_example_transcript()
    events = build_example_events(document)
    profile = build_profile_from_events(EXAMPLE_SESSION_ID, events)
    summary = _build_example_summary(document, events)

    service = SessionOptimizationService(db)
    suggestions = service._local_optimization_suggestions(summary, profile)

    scope_tokens = int(profile.total_prompt_tokens) + int(
        profile.total_completion_tokens
    )
    response = RuntimeSessionOptimizationResponse(
        generated_by="local",
        generated_at=None,
        suggestions=suggestions,
        context_profile=profile.model_dump(exclude_none=True),
        analyzed_scope_total_tokens=scope_tokens,
        analyzed_scope_estimated_cost=summary.estimated_cost,
        is_example=True,
        example_notice=EXAMPLE_NOTICE,
        example_provenance=str(document.get("provenance") or ""),
        example_title=str(document.get("title") or "Example session"),
        example_pricing_note=str(document.get("pricing_note") or ""),
    )
    # Reuse the production roll-up so the example's headline number obeys the
    # same dedupe and the same "savings cannot exceed analyzed scope" invariant.
    SessionOptimizationService._apply_savings_rollup(
        response, summary=summary, profile=profile, scope_tokens=scope_tokens
    )
    response.waste_score = SessionOptimizationService._compute_waste_score(
        summary, profile
    )
    return response
