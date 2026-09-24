"""Encrypted runtime-session artifacts: isolation, idempotency, and migration."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_runtime_session_artifact as crud

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260924_runtime_session_artifact.py"
)


@pytest.fixture
def scope(db_session: Session, test_user: models.User) -> dict[str, Any]:
    """Account, session, and activity rows the artifact foreign keys require."""
    now = datetime.now(UTC)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="browser",
        session_source_id=f"session-{uuid4().hex[:12]}",
        started_at=now,
    )
    db_session.add(session)
    db_session.flush()
    activity = models.RuntimeSessionActivity(
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        activity_type="tool_call",
        timestamp=now,
    )
    db_session.add(activity)
    db_session.flush()
    return {
        "account_id": test_user.account_id,
        "runtime_session_id": session.id,
        "activity_id": activity.id,
    }


def _store(
    db_session: Session, scope: dict[str, Any], **overrides: Any
) -> models.RuntimeSessionArtifact:
    payload: dict[str, Any] = {
        "account_id": scope["account_id"],
        "runtime_session_id": scope["runtime_session_id"],
        "kind": "screenshot",
        "source": "browser_use",
        "source_ref": "step-1",
        "content_type": "image/png",
        "plaintext": b"unpublished-screenshot-bytes",
        "manifest": {"step_index": 1},
        "activity_id": scope["activity_id"],
    }
    payload.update(overrides)
    return crud.store(db_session, **payload)


def _count(db_session: Session, scope: dict[str, Any]) -> int:
    return (
        db_session.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.runtime_session_id
            == scope["runtime_session_id"]
        )
        .count()
    )


def test_configured_kind_bounds() -> None:
    """Screenshot and recording caps are the values the store enforces."""
    assert settings.runtime_session_screenshot_max_bytes == 2 * 1024**2
    assert settings.runtime_session_recording_max_bytes == 512 * 1024**2


def test_store_same_source_ref_is_idempotent(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """A repeated source key returns the original row and does not update it."""
    first = _store(db_session, scope)
    second = _store(
        db_session,
        scope,
        plaintext=b"a-different-screenshot",
        manifest={"step_index": 99},
    )
    assert second.id == first.id
    assert _count(db_session, scope) == 1
    assert crud.decrypt(second) == b"unpublished-screenshot-bytes"
    assert second.manifest == {"step_index": 1}

    other_kind = _store(db_session, scope, kind="recording", content_type="video/webm")
    assert other_kind.id != first.id
    assert _count(db_session, scope) == 2


def test_null_source_ref_is_not_idempotent(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """Rows with no source-native id are stored independently."""
    first = _store(db_session, scope, source_ref=None)
    second = _store(db_session, scope, source_ref=None)
    assert first.id != second.id
    assert _count(db_session, scope) == 2


def test_get_is_isolated_by_account(db_session: Session, scope: dict[str, Any]) -> None:
    """get returns the row for its account and None for any other account."""
    stored = _store(db_session, scope)
    assert (
        crud.get(db_session, account_id=scope["account_id"], artifact_id=stored.id)
        == stored
    )
    assert crud.get(db_session, account_id=uuid4(), artifact_id=stored.id) is None


def test_decrypt_round_trip_ciphertext_differs_from_plaintext(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """Stored ciphertext decrypts to the plaintext and is not the plaintext."""
    plaintext = b"unpublished-screenshot-bytes"
    stored = _store(db_session, scope, plaintext=plaintext)
    db_session.refresh(stored)
    ciphertext = bytes(stored.ciphertext)
    assert ciphertext != plaintext
    assert plaintext not in ciphertext
    assert crud.decrypt(stored) == plaintext
    assert stored.sha256 == hashlib.sha256(plaintext).hexdigest()
    assert stored.size_bytes == len(plaintext)
    assert stored.activity_id == scope["activity_id"]


def test_decrypt_corrupt_ciphertext_raises_artifact_undecryptable(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """A non-null token that is not valid Fernet is one documented error."""
    stored = _store(db_session, scope)
    stored.ciphertext = b"not-a-fernet-token"
    with pytest.raises(ValueError, match="artifact_undecryptable"):
        crud.decrypt(stored)


def test_mark_unavailable_clears_ciphertext_and_bytes(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """Evicting an artifact drops its ciphertext and its byte totals."""
    plaintext = b"unpublished-screenshot-bytes"
    stored = _store(db_session, scope, plaintext=plaintext)
    assert crud.account_bytes(db_session, account_id=scope["account_id"]) == len(
        plaintext
    )
    assert crud.session_bytes(
        db_session,
        account_id=scope["account_id"],
        runtime_session_id=scope["runtime_session_id"],
    ) == len(plaintext)

    assert crud.mark_unavailable(
        db_session,
        account_id=scope["account_id"],
        artifact_id=stored.id,
        availability="evicted",
    )
    db_session.refresh(stored)
    assert stored.ciphertext is None
    assert stored.availability == "evicted"
    with pytest.raises(ValueError, match="artifact_unavailable"):
        crud.decrypt(stored)
    assert crud.account_bytes(db_session, account_id=scope["account_id"]) == 0
    assert (
        crud.session_bytes(
            db_session,
            account_id=scope["account_id"],
            runtime_session_id=scope["runtime_session_id"],
        )
        == 0
    )
    assert (
        crud.mark_unavailable(
            db_session,
            account_id=uuid4(),
            artifact_id=stored.id,
            availability="expired",
        )
        is False
    )
    db_session.refresh(stored)
    assert stored.availability == "evicted"


def test_oversized_plaintext_writes_nothing(
    db_session: Session, scope: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload over the kind cap raises and leaves the table unchanged."""
    monkeypatch.setattr(settings, "runtime_session_screenshot_max_bytes", 4)
    monkeypatch.setattr(settings, "runtime_session_recording_max_bytes", 8)
    before = _count(db_session, scope)
    with pytest.raises(ValueError, match="artifact_too_large"):
        _store(db_session, scope, plaintext=b"12345")
    assert _count(db_session, scope) == before

    recording = _store(
        db_session,
        scope,
        kind="recording",
        content_type="video/webm",
        source_ref="clip-1",
        plaintext=b"123456",
    )
    assert recording.size_bytes == 6
    with pytest.raises(ValueError, match="artifact_too_large"):
        _store(
            db_session,
            scope,
            kind="recording",
            content_type="video/webm",
            source_ref="clip-2",
            plaintext=b"123456789",
        )


def test_list_for_session_orders_and_filters_kind(
    db_session: Session, scope: dict[str, Any]
) -> None:
    """Listing is oldest-first and can be limited to one kind."""
    first = _store(db_session, scope, source_ref="step-1")
    second = _store(
        db_session,
        scope,
        kind="recording",
        content_type="video/webm",
        source_ref="clip-1",
    )
    listed = crud.list_for_session(
        db_session,
        account_id=scope["account_id"],
        runtime_session_id=scope["runtime_session_id"],
    )
    assert [row.id for row in listed] == [first.id, second.id]
    screenshots = crud.list_for_session(
        db_session,
        account_id=scope["account_id"],
        runtime_session_id=scope["runtime_session_id"],
        kind="screenshot",
    )
    assert [row.id for row in screenshots] == [first.id]
    assert (
        crud.account_bytes(db_session, account_id=scope["account_id"], kind="recording")
        == second.size_bytes
    )


def _migration() -> Any:
    """Load the revision off disk, as the sibling migration tests do."""
    spec = importlib.util.spec_from_file_location(
        "runtime_session_artifact_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(db_session: Session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().downgrade()


def _upgrade(db_session: Session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().upgrade()


def test_migration_upgrade_and_downgrade(db_session: Session) -> None:
    """Downgrade drops the table; upgrade restores it and the partial unique index."""
    bind = db_session.connection()
    _downgrade(db_session)
    assert not inspect(bind).has_table("runtime_session_artifact")
    _upgrade(db_session)
    assert inspect(bind).has_table("runtime_session_artifact")
    indexdef = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_runtime_session_artifact_source'"
        )
    ).scalar_one()
    assert "UNIQUE" in indexdef.upper()
    assert "source_ref IS NOT NULL" in indexdef
    constraints = db_session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'runtime_session_artifact'::regclass "
            "AND contype = 'f'"
        )
    ).scalars()
    rendered = " ".join(constraints)
    assert "ON DELETE CASCADE" in rendered
    assert "ON DELETE SET NULL" in rendered
    _downgrade(db_session)
    assert not inspect(bind).has_table("runtime_session_artifact")
    _upgrade(db_session)
    assert inspect(bind).has_table("runtime_session_artifact")
