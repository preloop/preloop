"""Artifact text in session search, through the deposit and search APIs (#1082).

Data goes in through ``POST /runtime-sessions/{id}/artifacts`` with the
warehouse-sim transcripts (#1091), never as hand-written corpus rows.
"""

from __future__ import annotations

import base64
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_api_key, crud_runtime_session
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.models.models.session_search_document import (
    REDACTION_STATE_REDACTED,
    SOURCE_KIND_ARTIFACT,
    SessionSearchDocument,
)
from preloop.services import retention_purge as purge
from preloop.services import session_search_index
from preloop.services.retention_policy import CLASS_RUNTIME_SESSIONS

BASE = "/api/v1/runtime-sessions"
SEARCH = f"{BASE}/search"
STARTED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "fixtures"
    / "warehouse_sim"
    / "transcripts"
)
SUMMARY = (
    "# Nord late shift summary\n\n"
    "- Damaged pallet at dock door 5, delivery 4711, quarantined in lane Q2.\n"
    "- Claim to be opened with the carrier.\n"
)


def _session(db, account_id, source_id, *, started_at=STARTED, ended_at=None):
    return crud_runtime_session.upsert_by_source(
        db,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Warehouse agent",
        started_at=started_at,
        last_activity_at=started_at,
        ended_at=ended_at,
    )


def _token(db, user, session_id):
    _key, token = crud_api_key.create_runtime_key(
        db,
        name="Artifact agent",
        account_id=user.account_id,
        user_id=user.id,
        context_data={"runtime_session_id": str(session_id)},
    )
    return {"Authorization": f"Bearer {token}"}


def _deposit(client, headers, session_id, *, name, mime, text, labels, kind=None):
    body = {
        "name": name,
        "labels": labels,
        "content": {
            "type": "resource",
            "resource": {"uri": f"file:///{name}", "mimeType": mime, "text": text},
        },
    }
    if kind:
        body["kind"] = kind
    created = client.post(f"{BASE}/{session_id}/artifacts", headers=headers, json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _chunks(db, artifact_id):
    db.expire_all()
    return list(
        db.execute(
            select(SessionSearchDocument)
            .where(
                SessionSearchDocument.source_kind == SOURCE_KIND_ARTIFACT,
                SessionSearchDocument.source_id == str(artifact_id),
            )
            .order_by(SessionSearchDocument.chunk_index)
        ).scalars()
    )


@pytest.fixture
def warehouse(client, db_session, test_user):
    """Six warehouse-sim transcripts and a Nord summary, deposited per site."""
    out = {"sessions": {}, "artifacts": {}}
    for site in ("nord", "sued"):
        session = _session(db_session, test_user.account_id, f"warehouse-{site}")
        headers = _token(db_session, test_user, session.id)
        out["sessions"][site] = session.id
        for shift in ("early", "late", "night"):
            out["artifacts"][(site, shift)] = _deposit(
                client,
                headers,
                session.id,
                name=f"{site}-{shift}.vtt",
                mime="text/vtt",
                text=(FIXTURES / site / f"{shift}.vtt").read_text(encoding="utf-8"),
                labels={"site": site, "shift": shift},
            )
        if site == "nord":
            out["summary"] = _deposit(
                client,
                headers,
                session.id,
                name="nord-late-summary.md",
                mime="text/markdown",
                text=SUMMARY,
                labels={"site": "nord"},
                kind="document",
            )
    return out


def _search(client, query, **filters):
    response = client.post(
        SEARCH,
        json={"query": query, "filters": filters, "max_snippets_per_session": 10},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _artifact_snippets(body):
    return [
        snippet
        for result in body["results"]
        for snippet in result["snippets"]
        if snippet["source_kind"] == SOURCE_KIND_ARTIFACT
    ]


def test_damaged_pallet_finds_the_transcript_cue_and_the_summary(client, warehouse):
    body = _search(client, '"damaged pallet"')

    hits = {s["artifact"]["artifact_id"]: s for s in _artifact_snippets(body)}
    nord_late = warehouse["artifacts"][("nord", "late")]
    transcript = hits[nord_late["id"]]
    assert transcript["artifact"]["labels"]["site"] == "nord"
    assert transcript["artifact"]["kind"] == "transcript"
    assert transcript["artifact"]["cue_start"] == 0.0
    assert transcript["artifact"]["activity_id"]
    assert "damaged pallet" in re.sub(r"</?mark>", "", transcript["text"]).lower()
    summary = hits[warehouse["summary"]["id"]]
    assert summary["artifact"]["kind"] == "document"
    assert summary["artifact"]["cue_start"] is None
    # sued/late mentions the claim from site nord.
    assert warehouse["artifacts"][("sued", "late")]["id"] in hits


def test_activity_id_lands_on_the_deposit_timeline_row(client, db_session, warehouse):
    body = _search(client, "recount", kind="transcript")
    snippet = _artifact_snippets(body)[0]
    activity = db_session.get(
        models.RuntimeSessionActivity, snippet["artifact"]["activity_id"]
    )
    assert activity.activity_type == "artifact"
    assert str(activity.runtime_session_id) == snippet["runtime_session_id"]


def test_label_filter_excludes_other_sites(client, warehouse):
    body = _search(client, '"damaged pallet"', label=["site:nord"])
    sessions = {r["runtime_session_id"] for r in body["results"]}
    assert sessions == {str(warehouse["sessions"]["nord"])}
    assert {s["artifact"]["labels"]["site"] for s in _artifact_snippets(body)} == {
        "nord"
    }

    both = _search(client, '"damaged pallet"', label=["site:nord", "shift:late"])
    assert {s["artifact"]["artifact_id"] for s in _artifact_snippets(both)} == {
        warehouse["artifacts"][("nord", "late")]["id"]
    }
    # AND, not OR: no artifact is from both sites.
    assert _search(client, "pallet", label=["site:nord", "site:sued"])["total"] == 0


def test_tags_label_filter_matches_one_tag_of_the_array(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "tags-filter")
    headers = _token(db_session, test_user, session.id)
    tagged = _deposit(
        client,
        headers,
        session.id,
        name="handover.txt",
        mime="text/plain",
        text="damaged pallet at dock four",
        labels={"tags": ["handover", "dock"]},
        kind="transcript",
    )
    _deposit(
        client,
        headers,
        session.id,
        name="other.txt",
        mime="text/plain",
        text="damaged pallet at dock five",
        labels={"tags": ["inventory"]},
        kind="transcript",
    )
    body = _search(client, "pallet", label=["tags:handover"])
    assert {s["artifact"]["artifact_id"] for s in _artifact_snippets(body)} == {
        tagged["id"]
    }
    assert _search(client, "pallet", label=["tags:hand"])["total"] == 0


def test_kind_filter(client, warehouse):
    body = _search(client, '"damaged pallet"', kind="document")
    assert {s["artifact"]["artifact_id"] for s in _artifact_snippets(body)} == {
        warehouse["summary"]["id"]
    }


def test_malformed_label_filter_is_422(client):
    response = client.post(SEARCH, json={"query": "x", "filters": {"label": ["site"]}})
    assert response.status_code == 422


def test_email_in_a_transcript_is_stored_redacted(db_session, warehouse):
    artifact_id = warehouse["artifacts"][("sued", "night")]["id"]
    chunks = _chunks(db_session, artifact_id)
    assert chunks
    joined = "\n".join(chunk.content for chunk in chunks)
    assert "erika.mustermann@example.com" not in joined
    assert "Her email is [redacted]" in joined
    assert {chunk.redaction_state for chunk in chunks} == {REDACTION_STATE_REDACTED}
    # A transcript without anything to mask stays clear.
    clear = _chunks(db_session, warehouse["artifacts"][("nord", "early")]["id"])
    assert {chunk.redaction_state for chunk in clear} == {"clear"}


def test_text_status_and_chunk_metadata(db_session, warehouse):
    artifact_id = warehouse["artifacts"][("nord", "late")]["id"]
    row = db_session.get(models.RuntimeSessionArtifact, artifact_id)
    assert row.text_status == "extracted"
    assert "text_truncated" not in (row.manifest or {})
    chunk = _chunks(db_session, artifact_id)[0]
    assert chunk.meta_data["artifact_id"] == artifact_id
    assert chunk.meta_data["labels"] == {"site": "nord", "shift": "late"}
    assert chunk.meta_data["name"] == "nord-late.vtt"
    assert chunk.role == "artifact"
    assert "-->" not in chunk.content and "WEBVTT" not in chunk.content


def test_cue_start_per_chunk_follows_the_cue_the_chunk_begins_in(
    client, db_session, test_user
):
    session = _session(db_session, test_user.account_id, "long-cues")
    headers = _token(db_session, test_user, session.id)
    cues = "".join(
        f"00:{i // 60:02d}:{i % 60:02d}.000 --> 00:{i // 60:02d}:{i % 60:02d}.900\n"
        f"Cue {i} " + "palette " * 30 + "\n\n"
        for i in range(40)
    )
    created = _deposit(
        client,
        headers,
        session.id,
        name="long.vtt",
        mime="text/vtt",
        text="WEBVTT\n\n" + cues,
        labels={},
    )
    chunks = _chunks(db_session, created["id"])
    assert len(chunks) > 3
    starts = [chunk.meta_data["cue_start"] for chunk in chunks]
    assert starts[0] == 0.0
    assert starts == sorted(starts) and starts[-1] > 30
    for chunk in chunks[1:]:
        first_cue = chunk.content.split("Cue ", 1)
        # The cue named at or after the chunk start is never before cue_start.
        if len(first_cue) > 1 and chunk.content.startswith("Cue "):
            assert int(first_cue[1].split()[0]) == chunk.meta_data["cue_start"]


def test_three_mib_transcript_is_truncated_at_one_mib(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "big-transcript")
    headers = _token(db_session, test_user, session.id)
    text = ("Line about a damaged pallet and more words.\n" * 80_000)[: 3 * 1024**2]
    created = _deposit(
        client,
        headers,
        session.id,
        name="big.txt",
        mime="text/plain",
        text=text,
        labels={},
        kind="transcript",
    )
    row = db_session.get(models.RuntimeSessionArtifact, created["id"])
    db_session.refresh(row)
    assert row.text_status == "extracted"
    assert row.manifest["text_truncated"] is True
    chunks = _chunks(db_session, created["id"])
    assert chunks[0].meta_data["text_truncated"] is True
    indexed = sum(len(chunk.content) for chunk in chunks)
    # Overlapping chunks cover the first MiB, not the 3 MiB.
    assert 1024**2 <= indexed < 1.3 * 1024**2


def test_image_indexes_name_labels_and_tool_only(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "image-meta")
    headers = _token(db_session, test_user, session.id)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=headers,
        json={
            "name": "dock-door-5.png",
            "labels": {"site": "nord"},
            "tool_name": "camera_snapshot",
            "content": {
                "type": "image",
                "data": base64.b64encode(png).decode(),
                "mimeType": "image/png",
            },
        },
    )
    assert created.status_code == 201, created.text
    chunks = _chunks(db_session, created.json()["id"])
    assert len(chunks) == 1
    assert chunks[0].content == (
        "kind: artifact\nartifact_kind: screenshot\nname: dock-door-5.png\n"
        "tool_name: camera_snapshot\nlabels: site=nord"
    )
    row = db_session.get(models.RuntimeSessionArtifact, created.json()["id"])
    assert row.text_status == "none"


def test_pdf_document_has_no_text(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "pdf-doc")
    headers = _token(db_session, test_user, session.id)
    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=headers,
        json={
            "name": "claim.pdf",
            "kind": "document",
            "content": {
                "type": "resource",
                "resource": {
                    "uri": "file:///claim.pdf",
                    "mimeType": "application/pdf",
                    "blob": base64.b64encode(b"%PDF-1.7\n%%EOF").decode(),
                },
            },
        },
    )
    assert created.status_code == 201, created.text
    row = db_session.get(models.RuntimeSessionArtifact, created.json()["id"])
    assert row.text_status == "none"
    assert len(_chunks(db_session, row.id)) == 1


def test_capture_off_indexes_a_descriptor(client, db_session, test_user, monkeypatch):
    monkeypatch.setattr(settings, "model_gateway_capture_content", False)
    session = _session(db_session, test_user.account_id, "capture-off")
    headers = _token(db_session, test_user, session.id)
    created = _deposit(
        client,
        headers,
        session.id,
        name="x.vtt",
        mime="text/vtt",
        text=(FIXTURES / "nord" / "late.vtt").read_text(encoding="utf-8"),
        labels={"site": "nord"},
    )
    chunks = _chunks(db_session, created["id"])
    assert len(chunks) == 1
    assert "pallet" not in chunks[0].content
    assert "content_captured: false" in chunks[0].content
    assert chunks[0].redaction_state == "metadata_only"


def test_kill_switch_writes_no_chunks_and_keeps_text_status(
    client, db_session, test_user, monkeypatch
):
    monkeypatch.setattr(settings, "session_search_index_enabled", False)
    nudges = []
    monkeypatch.setattr(session_search_index, "request_embedding", nudges.append)
    session = _session(db_session, test_user.account_id, "kill-switch")
    headers = _token(db_session, test_user, session.id)
    created = _deposit(
        client,
        headers,
        session.id,
        name="x.txt",
        mime="text/plain",
        text="damaged pallet",
        labels={},
        kind="transcript",
    )
    assert _chunks(db_session, created["id"]) == []
    assert nudges == []
    # The request session is closed with a rollback; only what was committed
    # survives it. The test client shares this session, so roll back the same
    # way before reading, or a flushed-only status would look persisted.
    db_session.rollback()
    db_session.expire_all()
    row = db_session.get(models.RuntimeSessionArtifact, created["id"])
    assert row.text_status == "extracted"


def test_chunks_are_pending_embedding_through_the_existing_nudge(
    client, db_session, test_user, monkeypatch
):
    nudges = []
    monkeypatch.setattr(session_search_index, "request_embedding", nudges.append)
    session = _session(db_session, test_user.account_id, "embedding")
    headers = _token(db_session, test_user, session.id)
    created = _deposit(
        client,
        headers,
        session.id,
        name="x.txt",
        mime="text/plain",
        text="damaged pallet",
        labels={},
        kind="transcript",
    )
    chunks = _chunks(db_session, created["id"])
    assert {chunk.embedding_state for chunk in chunks} == {"pending"}
    assert [str(a) for a in nudges] == [str(test_user.account_id)]


def test_session_purge_removes_the_chunks(client, db_session, test_user, monkeypatch):
    monkeypatch.setattr(settings, "retention_purge_enabled", True, raising=False)
    monkeypatch.setattr(settings, "retention_purge_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "retention_purge_window_utc", "", raising=False)
    old = datetime.now(UTC) - timedelta(days=500)
    session = _session(
        db_session, test_user.account_id, "purge", started_at=old, ended_at=old
    )
    session_id = session.id
    headers = _token(db_session, test_user, session_id)
    created = _deposit(
        client,
        headers,
        session_id,
        name="late.vtt",
        mime="text/vtt",
        text=(FIXTURES / "nord" / "late.vtt").read_text(encoding="utf-8"),
        labels={"site": "nord"},
    )
    assert _chunks(db_session, created["id"])
    db_session.expire_all()
    for row in db_session.execute(
        select(models.RuntimeSessionActivity).where(
            models.RuntimeSessionActivity.runtime_session_id == session_id
        )
    ).scalars():
        row.timestamp = old
    db_session.get(models.RuntimeSession, session_id).last_activity_at = old
    db_session.commit()

    result = purge.purge_class(
        db_session,
        account=db_session.get(models.Account, test_user.account_id),
        record_class=CLASS_RUNTIME_SESSIONS,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert result.deleted == 1
    assert _chunks(db_session, created["id"]) == []


def test_eviction_and_expiry_remove_the_chunks(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "expiry")
    headers = _token(db_session, test_user, session.id)
    first = _deposit(
        client,
        headers,
        session.id,
        name="a.txt",
        mime="text/plain",
        text="damaged pallet one",
        labels={},
        kind="transcript",
    )
    second = _deposit(
        client,
        headers,
        session.id,
        name="b.txt",
        mime="text/plain",
        text="damaged pallet two",
        labels={},
        kind="transcript",
    )
    assert crud_artifact.mark_unavailable(
        db_session,
        account_id=test_user.account_id,
        artifact_id=first["id"],
        availability="evicted",
    )
    assert _chunks(db_session, first["id"]) == []

    row = db_session.get(models.RuntimeSessionArtifact, second["id"])
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()
    assert crud_artifact.cleanup(db_session, now=datetime.now(UTC)) == 1
    assert _chunks(db_session, second["id"]) == []


def test_redact_indexed_source_keeps_the_artifact_identity(db_session, warehouse):
    artifact_id = warehouse["artifacts"][("nord", "late")]["id"]
    outcome = session_search_index.redact_indexed_source(
        db_session,
        source_kind=SOURCE_KIND_ARTIFACT,
        source_id=artifact_id,
        replacement_text="kind: artifact\nreport withdrawn",
        commit=True,
    )
    assert outcome.action == "reindexed"
    chunk = _chunks(db_session, artifact_id)[0]
    assert chunk.content == "kind: artifact\nreport withdrawn"
    assert chunk.meta_data["artifact_id"] == artifact_id
    assert chunk.meta_data["labels"]["site"] == "nord"
    assert chunk.meta_data["redacted_after_indexing"] is True

    dropped = session_search_index.redact_indexed_source(
        db_session, source_kind=SOURCE_KIND_ARTIFACT, source_id=artifact_id, drop=True
    )
    assert dropped.action == "dropped"


def test_five_mib_vtt_indexes_within_bounds(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "five-mib")
    headers = _token(db_session, test_user, session.id)
    cue = "00:00:01.000 --> 00:00:02.000\n<v Picker>Gang 12 Fehlbestand, damaged pallet.\n\n"
    text = ("WEBVTT\n\n" + cue * 80_000)[: 5 * 1024**2 - 1024]
    started = time.perf_counter()
    created = _deposit(
        client,
        headers,
        session.id,
        name="big.vtt",
        mime="text/vtt",
        text=text,
        labels={},
    )
    elapsed = time.perf_counter() - started
    chunks = _chunks(db_session, created["id"])
    assert len(chunks) <= session_search_index.ARTIFACT_MAX_CHUNKS
    assert chunks[-1].meta_data["text_truncated"] is True
    # Generous bound for CI; the PR records the measured p95.
    assert elapsed < 30
