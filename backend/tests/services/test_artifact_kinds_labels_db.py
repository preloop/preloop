"""Artifact kinds, caps, labels and provenance columns against Postgres."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_runtime_session_artifact as crud
from preloop.services import session_artifact_budget as budget
from preloop.services.artifact_media import ARTIFACT_KINDS

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261001_artifact_kinds_labels.py"
)

SAMPLE: dict[str, tuple[str, bytes]] = {
    "screenshot": ("image/png", b"\x89PNG\r\n\x1a\nimg"),
    "recording": ("video/webm", b"\x1a\x45\xdf\xa3rec"),
    "screencast": ("video/mp4", b"\x00\x00\x00\x18ftypmp42"),
    "audio": ("audio/wav", b"RIFF\x24\x00\x00\x00WAVEfmt "),
    "transcript": ("text/vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhallo"),
    "document": ("application/pdf", b"%PDF-1.7\nsummary"),
    "generated_file": ("text/csv", b"a,b\n1,2\n"),
    "trace": ("application/zip", b"PK\x03\x04trace"),
}
WRONG_MAGIC: dict[str, tuple[str, bytes]] = {
    "screencast": ("video/webm", b"not-webm"),
    "audio": ("audio/ogg", b"ID3nope"),
    "transcript": ("application/json", b"{broken"),
    "document": ("application/pdf", b"<html>"),
    "trace": ("application/zip", b"%PDF-"),
    "generated_file": ("application/octet-stream", b"\x7fELF\x02\x01\x01"),
}


@pytest.fixture
def scope(db_session: Session, test_user: models.User) -> dict[str, Any]:
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="browser",
        session_source_id=f"session-{uuid4().hex[:12]}",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    return {
        "account_id": test_user.account_id,
        "runtime_session_id": session.id,
        "session": session,
    }


def _store(
    db: Session, scope: dict[str, Any], sample: str, **overrides: Any
) -> models.RuntimeSessionArtifact:
    """Store the synthetic sample for ``sample``; overrides win."""
    content_type, data = SAMPLE[sample]
    payload: dict[str, Any] = {
        "account_id": scope["account_id"],
        "runtime_session_id": scope["runtime_session_id"],
        "kind": sample,
        "source": "synthetic",
        "source_ref": None,
        "content_type": content_type,
        "plaintext": data,
        "manifest": {},
        "producer": "deposit_api",
    }
    payload.update(overrides)
    return crud.store(db, **payload)


def _count(db: Session, scope: dict[str, Any]) -> int:
    return (
        db.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.runtime_session_id
            == scope["runtime_session_id"]
        )
        .count()
    )


@pytest.mark.parametrize("kind", ARTIFACT_KINDS)
def test_each_kind_stores_its_type_and_round_trips(
    db_session: Session, scope: dict[str, Any], kind: str
) -> None:
    row = _store(db_session, scope, kind, name=f"{kind}-1")
    assert row.kind == kind
    assert row.content_type == SAMPLE[kind][0]
    assert crud.decrypt(row) == SAMPLE[kind][1]
    assert (row.name, row.producer, row.text_status, row.labels) == (
        f"{kind}-1",
        "deposit_api",
        "none",
        {},
    )


@pytest.mark.parametrize("kind", sorted(WRONG_MAGIC))
def test_wrong_magic_bytes_are_refused(
    db_session: Session, scope: dict[str, Any], kind: str
) -> None:
    content_type, data = WRONG_MAGIC[kind]
    before = _count(db_session, scope)
    with pytest.raises(ValueError, match="artifact_content_mismatch"):
        _store(db_session, scope, kind, content_type=content_type, plaintext=data)
    assert _count(db_session, scope) == before


@pytest.mark.parametrize("kind", ARTIFACT_KINDS)
def test_over_the_kind_cap_is_too_large(
    db_session: Session,
    scope: dict[str, Any],
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, f"runtime_session_{kind}_max_bytes", 4)
    with pytest.raises(ValueError, match="artifact_too_large"):
        _store(db_session, scope, kind)


def test_default_caps_match_the_issue() -> None:
    mib = 1024**2
    assert settings.runtime_session_transcript_max_bytes == 5 * mib
    assert settings.runtime_session_document_max_bytes == 10 * mib
    assert settings.runtime_session_generated_file_max_bytes == 10 * mib
    assert settings.runtime_session_audio_max_bytes == 25 * mib
    assert settings.runtime_session_trace_max_bytes == 25 * mib
    assert settings.runtime_session_screencast_max_bytes == 64 * mib


def test_unknown_kind_is_refused(db_session: Session, scope: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="artifact_kind_invalid"):
        _store(db_session, scope, "document", kind="hologram")


def test_wrong_media_type_for_kind_is_refused(
    db_session: Session, scope: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="artifact_media_type_invalid"):
        _store(db_session, scope, "transcript", content_type="application/pdf")


def test_seventeen_label_keys_are_refused(
    db_session: Session, scope: dict[str, Any]
) -> None:
    labels = {f"k{i}": "v" for i in range(17)}
    with pytest.raises(ValueError, match="artifact_labels_invalid"):
        _store(db_session, scope, "transcript", labels=labels)
    sixteen = {f"k{i}": "v" for i in range(16)}
    assert _store(db_session, scope, "transcript", labels=sixteen).labels == sixteen


@pytest.mark.parametrize(
    "labels",
    [
        {"Site": "x"},
        {"1site": "x"},
        {"site name": "x"},
        {"k" * 64: "x"},
        {"site": "x" * 129},
        {"site": 3},
        {"tags": "not-a-list"},
        {"tags": [f"t{i}" for i in range(17)]},
        {"tags": ["ok", 7]},
    ],
)
def test_bad_labels_are_refused(
    db_session: Session, scope: dict[str, Any], labels: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="artifact_labels_invalid"):
        _store(db_session, scope, "transcript", labels=labels)


def test_tags_list_and_reserved_keys_are_accepted(
    db_session: Session, scope: dict[str, Any]
) -> None:
    labels = {
        "site": "heilbronn",
        "tenant_ref": "cust-42",
        "consent_basis": "contract",
        "retention_class": "short",
        "tags": ["call", "demo"],
    }
    assert _store(db_session, scope, "transcript", labels=labels).labels == labels


def test_list_filters_by_label_containment_and_producer(
    db_session: Session, scope: dict[str, Any]
) -> None:
    a = _store(
        db_session,
        scope,
        "transcript",
        labels={"site": "heilbronn", "tags": ["call", "demo"]},
    )
    b = _store(
        db_session,
        scope,
        "audio",
        labels={"site": "berlin", "tags": ["call"]},
        producer="firewall",
        tool_name="voice.record",
    )
    ids = lambda **kw: {  # noqa: E731
        row.id
        for row in crud.list_for_session(
            db_session,
            account_id=scope["account_id"],
            runtime_session_id=scope["runtime_session_id"],
            **kw,
        )
    }
    assert ids(labels={"site": "heilbronn"}) == {a.id}
    assert ids(labels={"tags": ["call"]}) == {a.id, b.id}
    assert ids(labels={"tags": ["demo"]}) == {a.id}
    assert ids(producer="firewall") == {b.id}
    assert ids(producer="firewall", labels={"site": "heilbronn"}) == set()


def test_provenance_and_lineage(db_session: Session, scope: dict[str, Any]) -> None:
    agent_id = uuid4()
    transcript = _store(
        db_session, scope, "transcript", agent_id=agent_id, tool_name="stt"
    )
    summary = _store(
        db_session,
        scope,
        "document",
        parent_artifact_id=transcript.id,
        text_status="extracted",
    )
    assert summary.parent_artifact_id == transcript.id
    assert (transcript.agent_id, transcript.tool_name) == (agent_id, "stt")
    with pytest.raises(ValueError, match="artifact_parent_invalid"):
        _store(db_session, scope, "document", parent_artifact_id=uuid4())
    with pytest.raises(ValueError, match="artifact_producer_invalid"):
        _store(db_session, scope, "document", producer="martian")
    with pytest.raises(ValueError, match="artifact_text_status_invalid"):
        _store(db_session, scope, "document", text_status="guessed")
    db_session.delete(transcript)
    db_session.commit()
    db_session.refresh(summary)
    assert summary.parent_artifact_id is None


def test_transcript_copies_the_session_legal_hold(
    db_session: Session, scope: dict[str, Any]
) -> None:
    scope["session"].legal_hold = True
    db_session.commit()
    assert _store(db_session, scope, "transcript").legal_hold is True


def test_audio_is_evicted_before_a_transcript(
    db_session: Session, scope: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under budget pressure media goes first, even when the transcript is older."""
    transcript = _store(db_session, scope, "transcript")
    transcript.created_at = datetime.now(UTC) - timedelta(days=2)
    audio = _store(db_session, scope, "audio")
    db_session.commit()
    used = transcript.size_bytes + audio.size_bytes
    monkeypatch.setattr(
        settings, "runtime_session_artifact_account_max_bytes", used + 4
    )
    _store(db_session, scope, "trace")
    db_session.refresh(audio)
    db_session.refresh(transcript)
    assert audio.availability == "evicted"
    assert transcript.availability == "available"


def test_usage_lists_every_kind(
    client: TestClient, db_session: Session, scope: dict[str, Any]
) -> None:
    row = _store(db_session, scope, "transcript")
    usage = budget.account_usage(db_session, account_id=scope["account_id"])
    assert set(usage["by_kind"]) == set(ARTIFACT_KINDS)
    assert usage["by_kind"]["transcript"] == row.size_bytes
    response = client.get("/api/v1/account/session-artifacts/usage")
    assert response.status_code == 200
    by_kind = response.json()["by_kind"]
    assert set(by_kind) == set(ARTIFACT_KINDS)
    assert by_kind["transcript"] == row.size_bytes
    assert by_kind["audio"] == 0 and by_kind["trace"] == 0


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "artifact_kinds_labels_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(db: Session, step: str) -> None:
    with Operations.context(MigrationContext.configure(db.connection())):
        getattr(_migration(), step)()


def test_migration_down_up_backfills_existing_screenshots(
    db_session: Session, scope: dict[str, Any]
) -> None:
    bind = db_session.connection()
    _run(db_session, "downgrade")
    columns = {c["name"] for c in inspect(bind).get_columns("runtime_session_artifact")}
    assert "labels" not in columns and "producer" not in columns
    # A screenshot written by the pre-upgrade schema.
    artifact_id = uuid4()
    db_session.execute(
        text(
            "INSERT INTO runtime_session_artifact (id, account_id, "
            "runtime_session_id, kind, source, source_ref, content_type, "
            "size_bytes, sha256, ciphertext) VALUES (:id, :a, :s, 'screenshot', "
            "'browser_use', 'old-1', 'image/png', 3, :sha, NULL)"
        ),
        {
            "id": artifact_id,
            "a": scope["account_id"],
            "s": scope["runtime_session_id"],
            "sha": "0" * 64,
        },
    )
    _run(db_session, "upgrade")
    db_session.expire_all()
    row = crud.get(db_session, account_id=scope["account_id"], artifact_id=artifact_id)
    assert row is not None and row.kind == "screenshot"
    assert (row.producer, row.labels, row.text_status) == ("browser_steps", {}, "none")
    indexes = {
        r[0]: r[1]
        for r in db_session.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'runtime_session_artifact'"
            )
        )
    }
    assert "gin" in indexes["ix_runtime_session_artifact_labels"].lower()
    assert "ix_runtime_session_artifact_account_kind_created" in indexes
