"""Session artifacts in the period export (#1088).

Members ``artifacts/<session>/<artifact>-<name>`` with decrypted bytes, and
``artifacts/manifest.json`` as a list of A2A ``Artifact`` objects.
"""

import hashlib
import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_runtime_session
from preloop.models.crud import runtime_session_artifact as artifact_crud
from preloop.services.artifact_shapes import from_a2a_part, make_payload
from preloop.services.retention_export import (
    MEMBER_ARTIFACT_MANIFEST,
    PeriodExportError,
    build_period_export,
)

PERIOD_START = datetime(2026, 4, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 5, 1, tzinfo=UTC)
INSIDE = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)

VTT = b"WEBVTT\n\n00:00.000 --> 00:02.000\nPick list for aisle 7 confirmed.\n"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _session(db_session, account_id, source_id="warehouse-run-1"):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Warehouse agent",
        started_at=INSIDE,
        last_activity_at=INSIDE,
    )


def _artifact(db_session, session, *, kind, name, content_type, body, **extra):
    row = artifact_crud.store(
        db_session,
        account_id=session.account_id,
        runtime_session_id=session.id,
        kind=kind,
        source="test",
        source_ref=None,
        content_type=content_type,
        plaintext=body,
        manifest={},
        name=name,
        labels={"aisle": "7", "shift": "night"},
        producer="gateway",
        commit=False,
        **extra,
    )
    db_session.execute(
        update(models.RuntimeSessionArtifact)
        .where(models.RuntimeSessionArtifact.id == row.id)
        .values(created_at=INSIDE)
    )
    db_session.flush()
    db_session.refresh(row)
    return row


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


def _export(db_session, account, **kwargs):
    return build_period_export(
        db_session,
        account=account,
        start=PERIOD_START,
        end=PERIOD_END,
        sign=False,
        **kwargs,
    )


@pytest.fixture
def two(db_session, account):
    session = _session(db_session, account.id)
    transcript = _artifact(
        db_session,
        session,
        kind="transcript",
        name="pick-call.vtt",
        content_type="text/vtt",
        body=VTT,
    )
    screenshot = _artifact(
        db_session,
        session,
        kind="screenshot",
        name="../aisle 7/shelf.png",
        content_type="image/png",
        body=PNG,
    )
    return session, transcript, screenshot


def test_transcript_and_screenshot_are_members_with_matching_digests(
    db_session, account, two
):
    session, transcript, screenshot = two
    export = _export(db_session, account)
    members = _members(export.archive)
    listed = {entry["name"]: entry for entry in export.manifest["members"]}
    a2a = json.loads(members[MEMBER_ARTIFACT_MANIFEST])
    by_id = {item["artifact_id"]: item for item in a2a}

    for row, body in ((transcript, VTT), (screenshot, PNG)):
        item = by_id[str(row.id)]
        url = item["parts"][0]["url"]
        assert url.startswith("artifact:")
        path = url.removeprefix("artifact:")
        assert path.startswith(f"artifacts/{session.id}/{row.id}-")
        assert members[path] == body
        digest = hashlib.sha256(members[path]).hexdigest()
        assert digest == item["metadata"]["sha256"] == row.sha256
        assert listed[path]["sha256"] == digest
        assert listed[path]["size_bytes"] == len(body)
    assert export.counts["artifacts"] == 2
    # The traversal in the stored name does not leave the session directory.
    assert all(".." not in name for name in members)


def test_manifest_is_a2a_artifact_json(db_session, account, two):
    session, _, screenshot = two
    a2a = json.loads(
        _members(_export(db_session, account).archive)[MEMBER_ARTIFACT_MANIFEST]
    )
    item = next(i for i in a2a if i["artifact_id"] == str(screenshot.id))

    assert set(item) == {"artifact_id", "name", "parts", "metadata"}
    assert len(item["parts"]) == 1
    part = item["parts"][0]
    assert {"url", "filename", "media_type"} <= set(part)
    assert part["media_type"] == "image/png"
    assert part["filename"] == "shelf.png"
    assert set(item["metadata"]) >= {
        "kind",
        "labels",
        "sha256",
        "size_bytes",
        "producer",
        "runtime_session_id",
        "created_at",
        "legal_hold",
        "availability",
    }
    assert item["metadata"]["kind"] == "screenshot"
    assert item["metadata"]["labels"] == {"aisle": "7", "shift": "night"}
    assert item["metadata"]["runtime_session_id"] == str(session.id)
    assert item["metadata"]["availability"] == "available"


def test_from_a2a_part_plus_member_bytes_reproduces_the_descriptor(
    db_session, account, two
):
    members = _members(_export(db_session, account).archive)
    rows = {str(r.id): r for r in two[1:]}
    for item in json.loads(members[MEMBER_ARTIFACT_MANIFEST]):
        part = item["parts"][0]
        ref = from_a2a_part(part)
        body = members[part["url"].removeprefix("artifact:")]
        rebuilt = make_payload(
            kind=ref.kind,
            name=ref.name,
            content_type=ref.content_type,
            data=body,
            labels=ref.labels,
        )
        row = rows[item["artifact_id"]]
        assert rebuilt.kind == row.kind
        assert rebuilt.labels == row.labels
        assert rebuilt.content_type == row.content_type
        assert rebuilt.sha256 == ref.sha256 == row.sha256


def test_evicted_artifact_is_in_the_manifest_only(db_session, account, two):
    _, transcript, screenshot = two
    artifact_crud.mark_unavailable(
        db_session,
        account_id=account.id,
        artifact_id=screenshot.id,
        availability="evicted",
        commit=False,
    )
    export = _export(db_session, account)
    members = _members(export.archive)
    a2a = {i["artifact_id"]: i for i in json.loads(members[MEMBER_ARTIFACT_MANIFEST])}

    gone = a2a[str(screenshot.id)]
    assert gone["metadata"]["availability"] == "evicted"
    assert gone["parts"][0]["url"].removeprefix("artifact:") not in members
    assert all(str(screenshot.id) not in e["name"] for e in export.manifest["members"])
    assert export.counts == {
        **export.counts,
        "artifacts": 1,
        "artifacts_unavailable": 1,
    }


def test_held_artifact_past_retention_is_still_exported(db_session, account):
    session = _session(db_session, account.id, "held-run")
    row = _artifact(
        db_session,
        session,
        kind="screenshot",
        name="held.png",
        content_type="image/png",
        body=PNG,
        expires_at=INSIDE - timedelta(days=1),
    )
    row.legal_hold = True
    db_session.flush()
    artifact_crud.cleanup(db_session, now=datetime.now(UTC))

    members = _members(_export(db_session, account).archive)
    assert members[f"artifacts/{session.id}/{row.id}-held.png"] == PNG


def test_over_the_byte_cap_is_refused_naming_count_and_bytes(
    db_session, account, two, monkeypatch
):
    monkeypatch.setattr(settings, "retention_export_max_artifact_bytes", 100)
    with pytest.raises(PeriodExportError) as caught:
        _export(db_session, account)
    assert caught.value.code == "export_too_large"
    message = str(caught.value)
    assert "2 artifacts" in message
    assert str(len(VTT) + len(PNG)) in message
    assert "narrow" in message


def test_session_filter_and_account_isolation(db_session, account, two, test_user):
    session, transcript, _ = two
    other = _session(db_session, account.id, "other-run")
    elsewhere = _artifact(
        db_session,
        other,
        kind="document",
        name="note.txt",
        content_type="text/plain",
        body=b"other session",
    )
    foreign_account = models.Account(organization_name=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(foreign_account)
    db_session.flush()
    foreign = _artifact(
        db_session,
        _session(db_session, foreign_account.id, "foreign-run"),
        kind="document",
        name="foreign.txt",
        content_type="text/plain",
        body=b"not yours",
    )

    everything = _members(_export(db_session, account).archive)
    assert any(str(elsewhere.id) in name for name in everything)
    assert not any(str(foreign.id) in name for name in everything)

    scoped = _members(
        _export(db_session, account, runtime_session_id=session.id).archive
    )
    ids = {i["artifact_id"] for i in json.loads(scoped[MEMBER_ARTIFACT_MANIFEST])}
    assert str(transcript.id) in ids
    assert str(elsewhere.id) not in ids


def test_archive_digest_and_size_describe_the_streamed_bytes(db_session, account, two):
    export = _export(db_session, account)
    body = b"".join(export.iter_chunks())
    assert hashlib.sha256(body).hexdigest() == export.sha256
    assert len(body) == export.size_bytes
