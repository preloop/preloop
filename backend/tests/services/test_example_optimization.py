"""Tests for the bundled example session shown in the Optimize tab.

The example exists to fix an empty first impression without misrepresenting
sample data as the user's own. These tests therefore assert three things:
the numbers are genuinely produced by the production analyzers, they obey the
same invariants as a real session, and nothing about the example touches the
database.
"""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.context_analysis import (
    build_profile_from_events,
    compute_profile_savings,
)
from preloop.services.example_optimization import (
    EXAMPLE_NOTICE,
    EXAMPLE_SESSION_ID,
    ExampleSessionUnavailableError,
    build_example_events,
    build_example_optimization_response,
    load_example_transcript,
)


@pytest.fixture(autouse=True)
def _clear_transcript_cache():
    """Keep the lru_cache from leaking between tests that patch the path."""
    load_example_transcript.cache_clear()
    yield
    load_example_transcript.cache_clear()


def _response():
    # The service takes a Session only to construct SessionOptimizationService;
    # passing None proves no database work happens on this path.
    return build_example_optimization_response(cast(Session, None))


def test_bundled_transcript_is_present_and_well_formed() -> None:
    """The committed fixture must ship and describe a multi-call session."""
    document = load_example_transcript()
    assert document["schema_version"] == 1
    assert len(document["events"]) >= 2
    assert document["provenance"]
    # Provenance must not claim the transcript is captured user traffic.
    assert "not a recording of real user traffic" in document["provenance"].lower()


def test_example_produces_multiple_grounded_suggestions() -> None:
    """The example must fix the empty state: real, varied, non-zero savings."""
    response = _response()

    assert response.is_example is True
    assert len(response.suggestions) >= 3
    ids = {suggestion.id for suggestion in response.suggestions}
    # The waste patterns the transcript was built to exercise.
    assert {"scope-tools", "filter-tool-output", "trim-context"} <= ids
    # The whole point of the seed: a real, non-trivial headline number.
    assert response.potential_savings_tokens > 0
    assert response.potential_savings_usd > 0
    assert response.waste_score is not None and response.waste_score > 0
    # Every suggestion must carry evidence rather than generic advice.
    for suggestion in response.suggestions:
        assert suggestion.evidence


def test_example_savings_never_exceed_analyzed_scope() -> None:
    """The example must satisfy the same invariant as a real session."""
    response = _response()

    assert response.analyzed_scope_total_tokens > 0
    assert response.potential_savings_tokens <= response.analyzed_scope_total_tokens
    # And it must not be hitting the clamp: a clamped example would mean the
    # fixture drifted and the headline number is being silently capped.
    breakdown = compute_profile_savings(
        build_profile_from_events(EXAMPLE_SESSION_ID, build_example_events())
    )
    assert breakdown.clamped is False


def test_savings_are_computed_not_hardcoded() -> None:
    """Recomputing from the transcript must reproduce the headline figure."""
    events = build_example_events()
    profile = build_profile_from_events(EXAMPLE_SESSION_ID, events)
    breakdown = compute_profile_savings(profile)

    assert _response().potential_savings_tokens == breakdown.total_tokens


def test_token_counts_are_derived_from_bundled_content() -> None:
    """Prompt tokens must be measured, so the fixture cannot drift from them."""
    events = build_example_events()

    for event in events:
        payload = event.payload
        assert payload["prompt_tokens"] > 0
        assert (
            payload["total_tokens"]
            == payload["prompt_tokens"] + payload["completion_tokens"]
        )
        # Cost is derived from the measured tokens and the declared rates.
        assert payload["estimated_cost"] > 0


def test_example_is_labelled_and_uses_a_non_uuid_id() -> None:
    """Honesty surface: notice, provenance, and an unmistakable session id."""
    response = _response()

    assert response.example_notice == EXAMPLE_NOTICE
    assert "not your data" in EXAMPLE_NOTICE.lower()
    assert response.example_provenance
    assert response.example_title
    # A non-UUID id can never collide with, or be mistaken for, a real session.
    assert EXAMPLE_SESSION_ID == "example-session"
    assert response.context_profile is not None
    assert response.context_profile["session_id"] == EXAMPLE_SESSION_ID


def test_example_attaches_no_applicable_actions() -> None:
    """A bundled sample must not offer to mutate the account's real config."""
    for suggestion in _response().suggestions:
        assert suggestion.action is None


def test_example_response_is_not_marked_model_generated() -> None:
    """The example is deterministic: no LLM spend, no model attribution."""
    response = _response()

    assert response.generated_by == "local"
    assert response.model_id is None
    assert response.estimated_optimization_cost == 0.0


def test_example_writes_nothing_to_the_database(db_session, test_user) -> None:
    """The core contamination guarantee: the example creates no rows.

    A seeded demo session would land in ``api_usage`` and thereby in account
    cost totals, budget accumulation, and admin stats. This example is computed
    in memory instead, so the guarantee is structural. This test pins it.
    """
    from preloop.models.models.api_usage import ApiUsage
    from preloop.models.models.runtime_session import RuntimeSession
    from preloop.models.models.runtime_session_activity import RuntimeSessionActivity
    from preloop.models.models.runtime_session_optimization_result import (
        RuntimeSessionOptimizationResult,
    )

    tracked = (
        ApiUsage,
        RuntimeSession,
        RuntimeSessionActivity,
        RuntimeSessionOptimizationResult,
    )
    before = {model: db_session.query(model).count() for model in tracked}

    response = build_example_optimization_response(db_session)
    db_session.flush()

    assert response.is_example is True
    for model in tracked:
        assert db_session.query(model).count() == before[model], (
            f"example optimization created {model.__name__} rows"
        )
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


def test_missing_transcript_raises_unavailable(tmp_path) -> None:
    """A missing fixture degrades to the normal empty state, not a 500."""
    with patch(
        "preloop.services.example_optimization.TRANSCRIPT_PATH",
        tmp_path / "does-not-exist.json",
    ):
        load_example_transcript.cache_clear()
        with pytest.raises(ExampleSessionUnavailableError):
            load_example_transcript()


def test_malformed_transcript_raises_unavailable(tmp_path) -> None:
    """Invalid JSON must not surface as an opaque parse error."""
    broken = tmp_path / "example_session.json"
    broken.write_text("{not json", encoding="utf-8")
    with patch("preloop.services.example_optimization.TRANSCRIPT_PATH", broken):
        load_example_transcript.cache_clear()
        with pytest.raises(ExampleSessionUnavailableError):
            load_example_transcript()


def test_transcript_without_events_raises_unavailable(tmp_path) -> None:
    """An empty transcript is unusable and must be reported as such."""
    empty = tmp_path / "example_session.json"
    empty.write_text(json.dumps({"schema_version": 1, "events": []}), encoding="utf-8")
    with patch("preloop.services.example_optimization.TRANSCRIPT_PATH", empty):
        load_example_transcript.cache_clear()
        with pytest.raises(ExampleSessionUnavailableError):
            load_example_transcript()
