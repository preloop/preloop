"""Duration provenance and current-state staleness stay independent."""

from typing import Any

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from preloop.schemas.readiness import (
    GateEvidence,
    ReadinessObservation,
    TicketCreationEvidence,
    parse_jira_created,
)
from preloop.services.readiness.report import readiness_fields


def observation(completed: Any, *, state: Any = "ready") -> Any:
    return ReadinessObservation(
        observation_id=uuid4(),
        account_id=uuid4(),
        tracker_id=uuid4(),
        repository="example/repo",
        pr_id=1,
        source_sha="a" * 40,
        target_sha="b" * 40,
        policy_version=uuid4(),
        started_at=completed - timedelta(seconds=10),
        completed_at=completed,
        state=state,
        coverage="complete",
        gates=(
            GateEvidence(
                name="conflict",
                state="pass" if state == "ready" else "fail",
                source="fixture",
                retrieved_at=completed,
            ),
        ),
    )


@pytest.mark.parametrize(
    "value", [None, "invalid", "2026-01-01", "2026-01-01T08:00:00"]
)
def test_missing_or_non_authoritative_creation_never_falls_back(value: Any) -> Any:
    assert parse_jira_created(value) is None


@pytest.mark.parametrize("hours", [0, 3])
def test_exact_duration_and_stale_current_state(hours: Any) -> Any:
    created = datetime(2026, 1, 1, 8, tzinfo=UTC)
    first = observation(created + timedelta(hours=hours))
    creation = TicketCreationEvidence(
        created_at=created,
        tracker_id=uuid4(),
        issue_key="EXAMPLE-1",
        retrieved_at=created,
    )
    fields = readiness_fields(
        creation, [(first, first)], None, now=first.completed_at + timedelta(minutes=11)
    )
    assert fields["ticket_to_observed_ready_hours"] == hours
    assert fields["readiness_scope"] == "configured_policy"
    assert fields["readiness_policy_version"] == first.policy_version
    assert fields["latest_readiness_state"] == "unknown"
    assert fields["readiness_unknown_reasons"] == ["stale"]


def test_invalid_time_order_and_multi_pr_are_null() -> Any:
    completed = datetime(2026, 1, 1, 8, tzinfo=UTC)
    first = observation(completed)
    creation = TicketCreationEvidence(
        created_at=completed + timedelta(seconds=1),
        tracker_id=uuid4(),
        issue_key="EXAMPLE-1",
        retrieved_at=completed,
    )
    fields = readiness_fields(creation, [(first, first)], None, now=completed)
    assert fields["ticket_to_observed_ready_hours"] is None
    assert "invalid_time_order" in fields["readiness_unknown_reasons"]
    fields = readiness_fields(
        creation, [(first, first), (first, first)], "ambiguous_pr", now=completed
    )
    assert fields["ticket_to_observed_ready_hours"] is None
    assert len(fields["readiness_observations"]) == 2


def test_regression_preserves_historical_duration() -> Any:
    created = datetime(2026, 1, 1, 8, tzinfo=UTC)
    first = observation(created + timedelta(hours=3))
    latest = observation(first.completed_at + timedelta(minutes=2), state="not_ready")
    creation = TicketCreationEvidence(
        created_at=created,
        tracker_id=uuid4(),
        issue_key="EXAMPLE-1",
        retrieved_at=created,
    )
    fields = readiness_fields(
        creation, [(first, latest)], None, now=latest.completed_at
    )
    assert fields["ticket_to_observed_ready_hours"] == 3
    assert fields["latest_readiness_state"] == "not_ready"
