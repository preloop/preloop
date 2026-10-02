"""Artifact deposit and list REST API (#1080)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.api.auth.jwt import create_access_token
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_account, crud_api_key, crud_runtime_session
from preloop.services import artifact_deposit
from preloop.services.artifact_media import ARTIFACT_KINDS
from preloop.services import retention_purge as purge
from preloop.services.legal_hold import HOLD_RESOURCE_RUNTIME_SESSION, place_hold
from preloop.services.retention_policy import CLASS_RUNTIME_SESSIONS

BASE = "/api/v1/runtime-sessions"
STARTED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
LABELS = {"site": "nord", "consent_basis": "works-agreement-2026-03"}


def _vtt(size: int = 200 * 1024) -> str:
    cue = "00:00:01.000 --> 00:00:02.000\nPicker 4 meldet Fehlbestand in Gang 12.\n\n"
    body = "WEBVTT\n\n"
    while len(body) < size:
        body += cue
    return body[:size]


def _session(db_session, account_id, source_id, *, started_at=STARTED, ended_at=None):
    return crud_runtime_session.upsert_by_source(
        db_session,
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


def _token(db_session, user, *, runtime_session_id=None, context=None):
    data = dict(context or {})
    if runtime_session_id is not None:
        data["runtime_session_id"] = str(runtime_session_id)
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Artifact agent",
        account_id=user.account_id,
        user_id=user.id,
        context_data=data or None,
    )
    return token


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_auth(user):
    return _auth(
        create_access_token(
            {"sub": str(user.id)},
            auth_generation=getattr(user, "auth_generation", 0) or 0,
        )
    )


def _transcript_body(**extra):
    body = {
        "name": "call-0412.vtt",
        "labels": LABELS,
        "content": {
            "type": "resource",
            "resource": {
                "uri": "file:///calls/call-0412.vtt",
                "mimeType": "text/vtt",
                "text": _vtt(),
            },
        },
    }
    body.update(extra)
    return body


def _rows(db_session, session_id):
    db_session.expire_all()
    return list(
        db_session.execute(
            select(models.RuntimeSessionArtifact).where(
                models.RuntimeSessionArtifact.runtime_session_id == session_id
            )
        ).scalars()
    )


def _activity_rows(db_session, session_id):
    db_session.expire_all()
    return list(
        db_session.execute(
            select(models.RuntimeSessionActivity).where(
                models.RuntimeSessionActivity.runtime_session_id == session_id,
                models.RuntimeSessionActivity.activity_type == "artifact",
            )
        ).scalars()
    )


def _assert_transcript_flow(client, db_session, test_user, session, created):
    assert created.status_code == 201, created.text
    out = created.json()
    assert out["kind"] == "transcript"
    assert out["name"] == "call-0412.vtt"
    assert out["content_type"] == "text/vtt"
    assert out["size_bytes"] == 200 * 1024
    assert out["labels"] == LABELS
    assert out["producer"] == "deposit_api"
    assert out["runtime_session_id"] == str(session.id)
    assert out["availability"] == "available"
    assert out["legal_hold"] is False
    assert len(out["sha256"]) == 64
    uri = f"/api/v1/runtime-sessions/{session.id}/artifacts/{out['id']}"
    block = out["content_block"]
    assert block["type"] == "resource_link"
    assert block["uri"] == uri
    assert block["name"] == "call-0412.vtt"
    assert block["mimeType"] == "text/vtt"
    assert block["size"] == 200 * 1024
    assert block["_meta"]["preloop.dev/artifact"]["artifact_id"] == out["id"]

    # Listed for the console user and for the bound agent.
    listed = client.get(f"{BASE}/{session.id}/artifacts", headers=_user_auth(test_user))
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [out["id"]]
    by_label = client.get(
        f"{BASE}/{session.id}/artifacts",
        params={"kind": "transcript", "label": "site:nord"},
        headers=_user_auth(test_user),
    )
    assert [item["id"] for item in by_label.json()["items"]] == [out["id"]]
    miss = client.get(
        f"{BASE}/{session.id}/artifacts",
        params={"label": "site:sued"},
        headers=_user_auth(test_user),
    )
    assert miss.json()["items"] == []

    # Bytes through the existing byte route.
    fetched = client.get(uri)
    assert fetched.status_code == 200
    assert fetched.content.decode() == _vtt()
    assert fetched.headers["content-type"].startswith("text/vtt")

    usage = client.get("/api/v1/account/session-artifacts/usage")
    assert usage.json()["by_kind"]["transcript"] >= 200 * 1024
    return out


def test_json_transcript_deposit_list_fetch_usage_and_timeline(
    client, db_session, test_user
):
    session = _session(db_session, test_user.account_id, "deposit-json")
    token = _token(db_session, test_user, runtime_session_id=session.id)

    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=_auth(token),
        json=_transcript_body(tool_name="transcribe_call"),
    )
    out = _assert_transcript_flow(client, db_session, test_user, session, created)
    assert out["tool_name"] == "transcribe_call"

    agent_list = client.get(f"{BASE}/{session.id}/artifacts", headers=_auth(token))
    assert agent_list.status_code == 200
    assert agent_list.json()["items"][0]["id"] == out["id"]

    # The deposit is a timeline row.
    timeline = client.get(f"{BASE}/{session.id}/activity")
    assert timeline.status_code == 200
    rows = [i for i in timeline.json()["items"] if i["activity_type"] == "artifact"]
    assert len(rows) == 1
    assert rows[0]["title"] == "transcript call-0412.vtt"
    meta = rows[0]["metadata"]["artifact"]
    assert meta == {
        "id": out["id"],
        "kind": "transcript",
        "name": "call-0412.vtt",
        "content_type": "text/vtt",
        "size_bytes": 200 * 1024,
        "labels": LABELS,
        "producer": "deposit_api",
    }
    assert out["activity_id"] == str(_activity_rows(db_session, session.id)[0].id)


def test_multipart_transcript_deposit(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "deposit-multipart")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    metadata = {"name": "call-0412.vtt", "labels": LABELS, "kind": "transcript"}

    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=_auth(token),
        files={
            "file": ("call-0412.vtt", _vtt().encode(), "text/vtt"),
            "metadata": (None, json.dumps(metadata), "application/json"),
        },
    )
    _assert_transcript_flow(client, db_session, test_user, session, created)


def test_image_block_is_inferred_as_screenshot(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "deposit-image")
    token = _token(db_session, test_user, runtime_session_id=session.id)

    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=_auth(token),
        json={
            "name": "shelf-12.png",
            "content": {
                "type": "image",
                "data": base64.b64encode(PNG).decode(),
                "mimeType": "image/png",
            },
        },
    )

    assert created.status_code == 201, created.text
    out = created.json()
    assert out["kind"] == "screenshot"
    assert out["content_block"]["mimeType"] == "image/png"
    fetched = client.get(out["content_block"]["uri"])
    assert fetched.content == PNG


def test_meta_labels_are_kept_and_top_level_labels_win(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "deposit-meta-labels")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    body = _transcript_body(labels={"site": "sued"})
    body["content"]["_meta"] = {
        "preloop.dev/artifact": {
            "labels": {"site": "nord", "consent_basis": "works-agreement-2026-03"}
        }
    }

    created = client.post(
        f"{BASE}/{session.id}/artifacts", headers=_auth(token), json=body
    )

    assert created.status_code == 201, created.text
    assert created.json()["labels"] == {
        "site": "sued",
        "consent_basis": "works-agreement-2026-03",
    }
    [row] = _rows(db_session, session.id)
    assert row.labels == created.json()["labels"]


def test_error_codes(client, db_session, test_user, monkeypatch):
    session = _session(db_session, test_user.account_id, "deposit-errors")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    url = f"{BASE}/{session.id}/artifacts"

    monkeypatch.setattr(settings, "runtime_session_transcript_max_bytes", 1024)
    too_large = client.post(url, headers=_auth(token), json=_transcript_body())
    wrong_type = client.post(
        url,
        headers=_auth(token),
        json={
            "name": "x.exe",
            "kind": "transcript",
            "content": {
                "type": "resource",
                "resource": {
                    "uri": "file:///x",
                    "mimeType": "image/gif",
                    "blob": "R0lG",
                },
            },
        },
    )
    bad_labels = client.post(
        url,
        headers=_auth(token),
        json={
            "name": "n.txt",
            "labels": {"Bad Key": "x"},
            "content": {"type": "text", "text": "hello"},
        },
    )
    audio = client.post(
        url,
        headers=_auth(token),
        json={
            "name": "call.ogg",
            "content": {
                "type": "audio",
                "data": base64.b64encode(b"OggS\x00\x02rest").decode(),
                "mimeType": "audio/ogg",
            },
        },
    )

    assert too_large.status_code == 413
    assert too_large.json()["detail"] == "artifact_too_large"
    assert wrong_type.status_code == 415
    assert wrong_type.json()["detail"] == "artifact_media_type_invalid"
    assert bad_labels.status_code == 422
    assert bad_labels.json()["detail"] == "artifact_labels_invalid"
    assert audio.status_code == 409
    assert audio.json()["detail"] == "artifact_audio_storage_disabled"
    assert _rows(db_session, session.id) == []
    assert _activity_rows(db_session, session.id) == []


def test_request_body_over_the_limit_is_refused_before_parsing(
    client, db_session, test_user, monkeypatch
):
    session = _session(db_session, test_user.account_id, "deposit-body-limit")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    monkeypatch.setattr(artifact_deposit, "max_request_bytes", lambda: 4096)

    declared = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers={**_auth(token), "Content-Type": "application/json"},
        content=b"{" + b" " * 8192 + b"}",
    )

    def chunks():
        for _ in range(8):
            yield b" " * 1024

    streamed = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers={**_auth(token), "Content-Type": "application/json"},
        content=chunks(),
    )

    assert declared.status_code == 413
    assert declared.json()["detail"] == "artifact_too_large"
    assert streamed.status_code == 413
    assert streamed.json()["detail"] == "artifact_too_large"


def test_body_limit_is_the_largest_kind_cap_plus_one_mib(monkeypatch):
    for kind in ARTIFACT_KINDS:
        monkeypatch.setattr(settings, f"runtime_session_{kind}_max_bytes", 1024**2)
    monkeypatch.setattr(settings, "runtime_session_trace_max_bytes", 300 * 1024**2)
    assert artifact_deposit.max_request_bytes() == 301 * 1024**2


def test_session_binding_and_account_isolation(client, db_session, test_user):
    own = _session(db_session, test_user.account_id, "deposit-own")
    sibling = _session(db_session, test_user.account_id, "deposit-sibling")
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = _session(db_session, other.id, "deposit-foreign")
    pinned = _token(db_session, test_user, runtime_session_id=own.id)
    unpinned = _token(db_session, test_user)
    body = {"name": "n.txt", "content": {"type": "text", "text": "hello"}}

    to_sibling = client.post(
        f"{BASE}/{sibling.id}/artifacts", headers=_auth(pinned), json=body
    )
    list_sibling = client.get(f"{BASE}/{sibling.id}/artifacts", headers=_auth(pinned))
    to_foreign = client.post(
        f"{BASE}/{foreign.id}/artifacts", headers=_auth(unpinned), json=body
    )
    list_foreign = client.get(
        f"{BASE}/{foreign.id}/artifacts", headers=_user_auth(test_user)
    )
    no_bearer = client.post(f"{BASE}/{own.id}/artifacts", json=body)
    no_bearer_list = client.get(f"{BASE}/{own.id}/artifacts")

    assert to_sibling.status_code == 403
    assert list_sibling.status_code == 403
    assert to_foreign.status_code == 404
    assert list_foreign.status_code == 404
    assert no_bearer.status_code == 401
    assert no_bearer_list.status_code == 401
    assert _rows(db_session, sibling.id) == []
    assert _rows(db_session, foreign.id) == []


def test_idempotency_key_returns_the_same_artifact_and_stores_once(
    client, db_session, test_user
):
    session = _session(db_session, test_user.account_id, "deposit-idem")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    headers = {**_auth(token), "Idempotency-Key": "call-0412-transcript"}

    first = client.post(
        f"{BASE}/{session.id}/artifacts", headers=headers, json=_transcript_body()
    )
    second = client.post(
        f"{BASE}/{session.id}/artifacts", headers=headers, json=_transcript_body()
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert len(_rows(db_session, session.id)) == 1
    assert len(_activity_rows(db_session, session.id)) == 1
    row = _rows(db_session, session.id)[0]
    assert (row.source, row.source_ref) == ("deposit", "call-0412-transcript")


def test_managed_agent_credential_sets_agent_id_and_parent_links(
    client, db_session, test_user
):
    session = _session(db_session, test_user.account_id, "deposit-agent")
    agent_id = uuid.uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        models.ManagedAgent(
            id=agent_id,
            account_id=test_user.account_id,
            agent_kind="custom",
            session_source_type="custom",
            session_source_id="warehouse-agent",
            session_reference="warehouse-agent",
            display_name="Warehouse agent",
            enrolled_via="runtime_session_token",
            lifecycle_state="active",
            lifecycle_updated_at=now,
            last_seen_at=now,
            tags={},
        )
    )
    db_session.commit()
    token = _token(
        db_session,
        test_user,
        runtime_session_id=session.id,
        context={"managed_agent_id": str(agent_id)},
    )
    first = client.post(
        f"{BASE}/{session.id}/artifacts", headers=_auth(token), json=_transcript_body()
    )
    summary = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=_auth(token),
        json={
            "name": "summary.md",
            "kind": "document",
            "parent_artifact_id": first.json()["id"],
            "content": {
                "type": "resource",
                "resource": {
                    "uri": "file:///summary.md",
                    "mimeType": "text/markdown",
                    "text": "# Summary",
                },
            },
        },
    )

    assert first.status_code == 201, first.text
    assert first.json()["agent_id"] == str(agent_id)
    assert summary.status_code == 201, summary.text
    assert summary.json()["parent_artifact_id"] == first.json()["id"]


def test_list_is_newest_first_with_cursor_pagination(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "deposit-page")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    ids = []
    for index in range(5):
        response = client.post(
            f"{BASE}/{session.id}/artifacts",
            headers=_auth(token),
            json={
                "name": f"note-{index}.txt",
                "content": {"type": "text", "text": f"n{index}"},
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])

    seen = []
    cursor = None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get(
            f"{BASE}/{session.id}/artifacts", params=params, headers=_auth(token)
        )
        assert page.status_code == 200, page.text
        seen.extend(item["id"] for item in page.json()["items"])
        cursor = page.json()["next_cursor"]
        if not cursor:
            break

    assert seen == list(reversed(ids))
    too_many = client.get(
        f"{BASE}/{session.id}/artifacts", params={"limit": 201}, headers=_auth(token)
    )
    bad_cursor = client.get(
        f"{BASE}/{session.id}/artifacts",
        params={"cursor": "nope"},
        headers=_auth(token),
    )
    bad_label = client.get(
        f"{BASE}/{session.id}/artifacts",
        params={"label": "nocolon"},
        headers=_auth(token),
    )
    assert too_many.status_code == 422
    assert bad_cursor.status_code == 422
    assert bad_label.status_code == 422


def test_on_stored_hooks_run_after_commit(client, db_session, test_user, monkeypatch):
    session = _session(db_session, test_user.account_id, "deposit-hook")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    seen = []
    monkeypatch.setattr(
        artifact_deposit,
        "ON_STORED",
        [lambda db, artifact: seen.append(artifact.id), lambda db, artifact: 1 / 0],
    )

    created = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=_auth(token),
        json={"name": "n.txt", "content": {"type": "text", "text": "hello"}},
    )

    assert created.status_code == 201
    assert [str(i) for i in seen] == [created.json()["id"]]


def test_legal_hold_marks_the_deposit_held(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, "deposit-hold")
    place_hold(
        db_session,
        account_id=test_user.account_id,
        resource_type=HOLD_RESOURCE_RUNTIME_SESSION,
        resource_id=str(session.id),
        reason="works council inquiry",
    )
    token = _token(db_session, test_user, runtime_session_id=session.id)

    created = client.post(
        f"{BASE}/{session.id}/artifacts", headers=_auth(token), json=_transcript_body()
    )

    assert created.status_code == 201, created.text
    assert created.json()["legal_hold"] is True


def test_retention_purge_removes_a_deposit(client, db_session, test_user, monkeypatch):
    monkeypatch.setattr(settings, "retention_purge_enabled", True, raising=False)
    monkeypatch.setattr(settings, "retention_purge_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "retention_purge_window_utc", "", raising=False)
    old = datetime.now(UTC) - timedelta(days=500)
    session = _session(
        db_session, test_user.account_id, "deposit-purge", started_at=old, ended_at=old
    )
    session_id = session.id
    token = _token(db_session, test_user, runtime_session_id=session_id)
    created = client.post(
        f"{BASE}/{session_id}/artifacts", headers=_auth(token), json=_transcript_body()
    )
    assert created.status_code == 201, created.text
    # Age the deposit's own timeline row with its session.
    for row in _activity_rows(db_session, session_id):
        row.timestamp = old
    stored = db_session.get(models.RuntimeSession, session_id)
    stored.last_activity_at = old
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
    assert _rows(db_session, session_id) == []


@pytest.mark.parametrize(
    "value",
    ["site", ":x", "site:"],
)
def test_parse_label_filter_rejects_malformed(value):
    with pytest.raises(ValueError, match="artifact_label_filter_invalid"):
        artifact_deposit.parse_label_filters([value])


def test_parse_label_filter_merges_tags():
    assert artifact_deposit.parse_label_filters(["site:nord", "tags:a", "tags:b"]) == {
        "site": "nord",
        "tags": ["a", "b"],
    }


def test_audio_storage_is_off_until_the_account_setting_exists():
    assert artifact_deposit.audio_storage_enabled(object()) is False
