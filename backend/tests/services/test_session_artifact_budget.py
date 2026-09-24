"""Account storage budget for runtime-session artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_runtime_session_artifact as crud
from preloop.services import session_artifact_budget as budget

_KIB = 1024
_CHUNK = 400 * _KIB


@pytest.fixture
def account_id(test_user: models.User) -> Any:
    return test_user.account_id


def _session(db: Session, account_id: Any) -> models.RuntimeSession:
    row = models.RuntimeSession(
        account_id=account_id,
        session_source_type="browser",
        session_source_id=f"session-{uuid4().hex[:12]}",
        started_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def _browser_step(
    db: Session, *, account_id: Any, runtime_session_id: Any
) -> models.RuntimeSessionActivity:
    row = models.RuntimeSessionActivity(
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        activity_type="browser_step",
        metadata_={"screenshot": None, "source": "browser_use"},
        timestamp=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def _recording(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_ref: str,
    activity_id: Any = None,
    created_at: datetime | None = None,
) -> models.RuntimeSessionArtifact:
    row = crud.store(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        kind="recording",
        source="browser_use",
        source_ref=source_ref,
        content_type="video/webm",
        plaintext=b"x" * _CHUNK,
        manifest={},
        activity_id=activity_id,
    )
    if created_at is not None:
        row.created_at = created_at
        db.commit()
        db.refresh(row)
    return row


def _hold(
    db: Session, session: models.RuntimeSession, artifact: models.RuntimeSessionArtifact
) -> None:
    session.legal_hold = True
    artifact.legal_hold = True
    db.commit()


def test_third_recording_evicts_the_oldest(
    db_session: Session, account_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1 MiB budget keeps the two newest 400 KiB recordings."""
    monkeypatch.setattr(
        settings, "runtime_session_artifact_account_max_bytes", _KIB * _KIB
    )
    sessions = [_session(db_session, account_id) for _ in range(3)]
    first = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=sessions[0].id,
        source_ref="clip-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=sessions[1].id,
        source_ref="clip-2",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    third = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=sessions[2].id,
        source_ref="clip-3",
        created_at=datetime(2026, 1, 3, tzinfo=UTC),
    )

    db_session.refresh(first)
    db_session.refresh(second)
    db_session.refresh(third)
    assert first.availability == "evicted"
    assert first.ciphertext is None
    assert second.availability == "available"
    assert second.ciphertext is not None
    assert third.availability == "available"
    marker = (
        db_session.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == sessions[0].id,
            models.RuntimeSessionActivity.activity_type == "artifact_evicted",
        )
        .one()
    )
    assert marker.metadata_["artifact_id"] == str(first.id)
    assert marker.metadata_["kind"] == "recording"
    assert marker.metadata_["reason"] == "account_budget"


def test_held_session_is_skipped_and_both_held_raises(
    db_session: Session, account_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held recording stays; when every recording is held the store raises."""
    monkeypatch.setattr(
        settings, "runtime_session_artifact_account_max_bytes", _KIB * _KIB
    )
    first_session = _session(db_session, account_id)
    second_session = _session(db_session, account_id)
    first = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=first_session.id,
        source_ref="clip-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=second_session.id,
        source_ref="clip-2",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    _hold(db_session, first_session, first)

    third_session = _session(db_session, account_id)
    third = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=third_session.id,
        source_ref="clip-3",
    )
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.availability == "available"
    assert first.ciphertext is not None
    assert second.availability == "evicted"
    assert second.ciphertext is None
    assert third.availability == "available"

    _hold(db_session, third_session, third)
    with pytest.raises(ValueError, match="storage_budget_exhausted"):
        _recording(
            db_session,
            account_id=account_id,
            runtime_session_id=_session(db_session, account_id).id,
            source_ref="clip-4",
        )
    db_session.refresh(first)
    db_session.refresh(third)
    assert first.availability == "available"
    assert third.availability == "available"


def test_recording_is_evicted_before_an_older_screenshot(
    db_session: Session, account_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recordings are the eviction preference even when a screenshot is older."""
    monkeypatch.setattr(
        settings, "runtime_session_artifact_account_max_bytes", _KIB * _KIB
    )
    session = _session(db_session, account_id)
    step = _browser_step(
        db_session, account_id=account_id, runtime_session_id=session.id
    )
    screenshot = crud.store(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-1",
        content_type="image/png",
        plaintext=b"x" * _CHUNK,
        manifest={},
        activity_id=step.id,
    )
    screenshot.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.commit()
    recording = _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_ref="clip-1",
        activity_id=step.id,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    other = _session(db_session, account_id)
    _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=other.id,
        source_ref="clip-2",
    )
    db_session.refresh(screenshot)
    db_session.refresh(recording)
    db_session.refresh(step)
    assert screenshot.availability == "available"
    assert recording.availability == "evicted"
    assert step.metadata_["screenshot"] is None


def test_screenshot_eviction_marks_the_browser_step(
    db_session: Session, account_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an evicted screenshot stamps the browser step's screenshot field."""
    monkeypatch.setattr(settings, "runtime_session_artifact_account_max_bytes", _CHUNK)
    session = _session(db_session, account_id)
    step = _browser_step(
        db_session, account_id=account_id, runtime_session_id=session.id
    )
    crud.store(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-1",
        content_type="image/png",
        plaintext=b"x" * _CHUNK,
        manifest={},
        activity_id=step.id,
    )
    crud.store(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-2",
        content_type="image/png",
        plaintext=b"y" * _CHUNK,
        manifest={},
    )
    db_session.refresh(step)
    assert step.metadata_["screenshot"]["availability"] == "evicted"


def test_exhausted_budget_publishes_nothing(
    db_session: Session, account_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that still cannot fit does not announce evictions it will roll back."""
    monkeypatch.setattr(settings, "runtime_session_artifact_account_max_bytes", 100)
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(budget, "emit_account_event", emitted.append)
    session = _session(db_session, account_id)
    crud.store(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        kind="recording",
        source="browser_use",
        source_ref="clip-small",
        content_type="video/webm",
        plaintext=b"x" * 40,
        manifest={},
    )
    with pytest.raises(ValueError, match="storage_budget_exhausted"):
        crud.store(
            db_session,
            account_id=account_id,
            runtime_session_id=session.id,
            kind="recording",
            source="browser_use",
            source_ref="clip-large",
            content_type="video/webm",
            plaintext=b"y" * 120,
            manifest={},
        )
    assert emitted == []


def test_one_session_update_per_affected_session(
    db_session: Session,
    account_id: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two evictions on one session emit a single runtime_session_updated."""
    monkeypatch.setattr(settings, "runtime_session_artifact_account_max_bytes", _CHUNK)
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(budget, "emit_account_event", emitted.append)
    session = _session(db_session, account_id)
    _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_ref="clip-1",
    )
    _recording(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_ref="clip-2",
    )
    updates = [
        event for event in emitted if event.get("type") == "runtime_session_updated"
    ]
    assert len(updates) == 1
    assert updates[0]["payload"]["runtime_session_id"] == str(session.id)
    assert updates[0]["payload"]["activity_type"] == "artifact_evicted"
