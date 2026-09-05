"""Tests for unknown-project backoff/degraded state on trackers.

Covers the webhook sync loop fix: a webhook naming a project we never
imported must retry with exponential backoff and eventually mark the project
degraded instead of triggering a tracker sync on every single event.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from preloop.models.crud import crud_tracker
from preloop.models.crud.tracker import (
    UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED,
    UNKNOWN_PROJECT_BASE_BACKOFF,
    UNKNOWN_PROJECT_MAX_BACKOFF,
    UNKNOWN_PROJECTS_META_KEY,
)

PROJECT = "1327729611"


def test_first_unknown_project_event_triggers_sync(db_session: Session, test_tracker):
    """The first sighting of an unknown project should trigger one sync."""
    state = crud_tracker.record_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT
    )

    assert state.should_sync is True
    assert state.attempts == 1
    assert state.degraded is False


def test_immediate_repeat_is_suppressed_by_backoff(db_session: Session, test_tracker):
    """A second webhook right away must not trigger another sync."""
    now = datetime.now(timezone.utc)
    first = crud_tracker.record_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT, now=now
    )
    second = crud_tracker.record_unknown_project(
        db_session,
        id=str(test_tracker.id),
        project_identifier=PROJECT,
        now=now + timedelta(seconds=30),
    )

    assert first.should_sync is True
    assert second.should_sync is False
    # Suppressed attempts must not inflate the counter.
    assert second.attempts == 1


def test_sync_retried_once_backoff_elapsed(db_session: Session, test_tracker):
    """Once the backoff window passes, one more sync is allowed."""
    now = datetime.now(timezone.utc)
    crud_tracker.record_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT, now=now
    )
    later = crud_tracker.record_unknown_project(
        db_session,
        id=str(test_tracker.id),
        project_identifier=PROJECT,
        now=now + UNKNOWN_PROJECT_BASE_BACKOFF + timedelta(seconds=1),
    )

    assert later.should_sync is True
    assert later.attempts == 2


def test_backoff_grows_exponentially_and_is_capped(db_session: Session, test_tracker):
    """Retry interval doubles per attempt and never exceeds the cap."""
    now = datetime.now(timezone.utc)
    seen = []
    for _ in range(UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED):
        state = crud_tracker.record_unknown_project(
            db_session,
            id=str(test_tracker.id),
            project_identifier=PROJECT,
            now=now,
        )
        seen.append(state.retry_after_seconds)
        # Jump past whatever backoff was just set.
        now += timedelta(seconds=state.retry_after_seconds + 1)

    assert seen[0] == int(UNKNOWN_PROJECT_BASE_BACKOFF.total_seconds())
    assert seen[1] == 2 * seen[0]
    assert all(s <= UNKNOWN_PROJECT_MAX_BACKOFF.total_seconds() for s in seen)


def test_becomes_degraded_and_stops_syncing(db_session: Session, test_tracker):
    """After the attempt budget is spent the project stops triggering syncs."""
    now = datetime.now(timezone.utc)
    for _ in range(UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED):
        state = crud_tracker.record_unknown_project(
            db_session,
            id=str(test_tracker.id),
            project_identifier=PROJECT,
            now=now,
        )
        now += timedelta(seconds=state.retry_after_seconds + 1)

    assert state.degraded is True

    # Far in the future the backoff has long elapsed, but degraded wins.
    after = crud_tracker.record_unknown_project(
        db_session,
        id=str(test_tracker.id),
        project_identifier=PROJECT,
        now=now + timedelta(days=7),
    )
    assert after.should_sync is False
    assert after.degraded is True
    assert after.attempts == UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED


def test_degraded_state_is_persisted_on_tracker(db_session: Session, test_tracker):
    """Degraded projects are recorded in meta_data so the API can show them."""
    now = datetime.now(timezone.utc)
    for _ in range(UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED):
        state = crud_tracker.record_unknown_project(
            db_session,
            id=str(test_tracker.id),
            project_identifier=PROJECT,
            now=now,
        )
        now += timedelta(seconds=state.retry_after_seconds + 1)

    db_session.refresh(test_tracker)
    entry = (test_tracker.meta_data or {})[UNKNOWN_PROJECTS_META_KEY][PROJECT]
    assert entry["degraded"] is True
    assert entry["attempts"] == UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED
    assert entry["first_seen_at"]


def test_separate_projects_have_independent_counters(db_session: Session, test_tracker):
    """Backoff for one project must not suppress a different project."""
    now = datetime.now(timezone.utc)
    crud_tracker.record_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT, now=now
    )
    other = crud_tracker.record_unknown_project(
        db_session,
        id=str(test_tracker.id),
        project_identifier="1227805380",
        now=now,
    )

    assert other.should_sync is True
    assert other.attempts == 1


def test_clear_unknown_project_resets_state(db_session: Session, test_tracker):
    """A project that later syncs successfully clears its failure state."""
    now = datetime.now(timezone.utc)
    for _ in range(UNKNOWN_PROJECT_ATTEMPTS_BEFORE_DEGRADED):
        state = crud_tracker.record_unknown_project(
            db_session,
            id=str(test_tracker.id),
            project_identifier=PROJECT,
            now=now,
        )
        now += timedelta(seconds=state.retry_after_seconds + 1)

    crud_tracker.clear_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT
    )

    db_session.refresh(test_tracker)
    assert UNKNOWN_PROJECTS_META_KEY not in (test_tracker.meta_data or {})

    # And syncing is allowed again from scratch.
    fresh = crud_tracker.record_unknown_project(
        db_session, id=str(test_tracker.id), project_identifier=PROJECT, now=now
    )
    assert fresh.should_sync is True
    assert fresh.attempts == 1


def test_missing_tracker_is_a_no_op(db_session: Session):
    """An unknown tracker id must not raise or trigger a sync."""
    state = crud_tracker.record_unknown_project(
        db_session,
        id="00000000-0000-0000-0000-000000000000",
        project_identifier=PROJECT,
    )
    assert state.should_sync is False
    assert state.degraded is False
