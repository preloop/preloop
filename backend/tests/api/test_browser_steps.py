"""Browser step ingestion: idempotency, redaction, auth, timeline, search."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_session_search_document,
)
from preloop.models.crud.runtime_session_activity import CRUDRuntimeSessionActivity
from preloop.models.models.session_search_document import (
    REDACTION_STATE_METADATA_ONLY,
    SOURCE_KIND_BROWSER_STEP,
)
from preloop.schemas.browser_step import BrowserStepIn
from preloop.services.runtime_session_explorer import _default_activity_title

BASE = "/api/v1/runtime-sessions"
SEARCH_URL = "/api/v1/runtime-sessions/search"
STARTED = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)


def _session(db_session, account_id, source_id, *, ended_at=None):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=STARTED,
        last_activity_at=STARTED,
        ended_at=ended_at,
    )


def _token(db_session, test_user, *, runtime_session_id=None):
    context = {}
    if runtime_session_id is not None:
        context["runtime_session_id"] = str(runtime_session_id)
    _key, token = crud_api_key.create_runtime_key(
        db_session,
        name="Browser agent",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data=context or None,
    )
    return token


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _step(step_id, *, action="click", url=None, target=None, reasoning=None, **extra):
    body = {
        "source": "playwright_mcp",
        "source_step_id": step_id,
        "step_index": extra.pop("step_index", 0),
        "action": action,
        "status": extra.pop("status", "success"),
    }
    if url is not None:
        body["url"] = url
    if target is not None:
        body["target"] = target
    if reasoning is not None:
        body["reasoning"] = reasoning
    occurred_at = extra.pop("occurred_at", None)
    if occurred_at is not None:
        body["occurred_at"] = occurred_at
    if extra:
        body["extra"] = extra
    return body


def _browser_rows(db_session, session_id):
    return (
        db_session.query(models.RuntimeSessionActivity)
        .filter(
            models.RuntimeSessionActivity.runtime_session_id == session_id,
            models.RuntimeSessionActivity.activity_type == "browser_step",
        )
        .order_by(models.RuntimeSessionActivity.timestamp.asc())
        .all()
    )


def test_posting_a_batch_twice_is_idempotent(client, db_session, test_user):
    """Three steps create three rows; the same batch is three duplicates."""
    session = _session(db_session, test_user.account_id, "browser-batch")
    token = _token(db_session, test_user)
    body = {
        "steps": [
            _step("step-1", action="navigate", url="https://app.example.com/inbox"),
            _step("step-2", action="click", target="Inbox", step_index=1),
            _step(
                "step-3",
                action="extract",
                reasoning="Read the first row.",
                step_index=2,
            ),
        ]
    }

    with patch("preloop.services.browser_steps.emit_account_event") as emit:
        first = client.post(
            f"{BASE}/{session.id}/browser-steps",
            json=body,
            headers=_headers(token),
        )
        assert emit.call_count == 1
        event = emit.call_args.args[0]
        assert event["type"] == "runtime_session_updated"
        assert event["topic"] == "runtime_sessions"
        assert event["payload"]["activity_type"] == "browser_step"

        second = client.post(
            f"{BASE}/{session.id}/browser-steps",
            json=body,
            headers=_headers(token),
        )
        assert emit.call_count == 1

    assert first.status_code == 200
    assert first.json() == {"accepted": 3, "duplicates": 0, "rejected": []}
    assert second.status_code == 200
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 3
    assert len(_browser_rows(db_session, session.id)) == 3


def test_url_query_token_is_masked_on_the_stored_row(client, db_session, test_user):
    """A token in the step URL is not stored in clear text."""
    session = _session(db_session, test_user.account_id, "browser-redact")
    token = _token(db_session, test_user)
    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={
            "steps": [
                _step(
                    "step-secret",
                    action="navigate",
                    url="https://x.example/?token=abc123secret",
                )
            ]
        },
    )

    assert response.status_code == 200
    row = _browser_rows(db_session, session.id)[0]
    assert row.activity_type == "browser_step"
    assert row.tool_name == "navigate"
    assert row.server_name == "playwright_mcp"
    assert "abc123secret" not in (row.summary or "")
    assert "abc123secret" not in row.metadata_["url"]
    assert row.metadata_["screenshot"] is None
    assert row.metadata_["source"] == "playwright_mcp"
    assert row.metadata_["source_step_id"] == "step-secret"


def test_batch_limits_auth_and_session_ownership(client, db_session, test_user):
    """201 steps, a bad bearer, another account, and an ended session."""
    session = _session(db_session, test_user.account_id, "browser-auth")
    ended = _session(
        db_session,
        test_user.account_id,
        "browser-ended",
        ended_at=STARTED + timedelta(minutes=5),
    )
    token = _token(db_session, test_user)
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    foreign = _session(db_session, other.id, "browser-foreign")

    too_many = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={
            "steps": [
                _step(f"step-{index}", action="wait", step_index=index)
                for index in range(201)
            ]
        },
    )
    missing = client.post(
        f"{BASE}/{session.id}/browser-steps",
        json={"steps": [_step("step-1", action="wait")]},
    )
    rejected = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers={"Authorization": "Bearer not-a-real-token"},
        json={"steps": [_step("step-1", action="wait")]},
    )
    foreign_response = client.post(
        f"{BASE}/{foreign.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [_step("step-1", action="wait")]},
    )
    ended_response = client.post(
        f"{BASE}/{ended.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [_step("step-ended", action="done")]},
    )

    assert too_many.status_code == 422
    assert missing.status_code == 401
    assert rejected.status_code == 401
    assert foreign_response.status_code == 404
    assert _browser_rows(db_session, foreign.id) == []
    assert ended_response.status_code == 200
    assert ended_response.json()["accepted"] == 1
    assert len(_browser_rows(db_session, ended.id)) == 1


def test_a_pinned_credential_cannot_write_another_session(
    client, db_session, test_user
):
    """A key bound to one session receives 403 for a different path."""
    own = _session(db_session, test_user.account_id, "browser-pinned")
    other = _session(db_session, test_user.account_id, "browser-other")
    token = _token(db_session, test_user, runtime_session_id=own.id)

    forbidden = client.post(
        f"{BASE}/{other.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [_step("step-1", action="wait")]},
    )
    allowed = client.post(
        f"{BASE}/{own.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [_step("step-1", action="wait")]},
    )

    assert forbidden.status_code == 403
    assert _browser_rows(db_session, other.id) == []
    assert allowed.status_code == 200
    assert allowed.json()["accepted"] == 1


def test_an_oversized_extra_is_rejected_without_failing_the_batch(
    client, db_session, test_user
):
    """One oversized extra is a row error; the sibling step is stored."""
    session = _session(db_session, test_user.account_id, "browser-extra")
    token = _token(db_session, test_user)
    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={
            "steps": [
                _step("step-ok", action="wait"),
                _step("step-big", action="wait", step_index=1, blob="a" * 5000),
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 0
    assert body["rejected"] == [{"index": 1, "error": "extra_too_large"}]
    assert len(_browser_rows(db_session, session.id)) == 1


def test_activity_timeline_interleaves_browser_steps_with_tool_calls(
    client, db_session, test_user
):
    """Browser steps sit on the timeline by timestamp, with step metadata."""
    session = _session(db_session, test_user.account_id, "browser-timeline")
    token = _token(db_session, test_user)
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        server_name="filesystem",
        tool_name="read_file",
        status="success",
        summary="read the checklist",
        timestamp=STARTED + timedelta(seconds=30),
        commit=False,
    )
    crud_runtime_session_activity.log_tool_call(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        server_name="filesystem",
        tool_name="write_file",
        status="success",
        summary="write the checklist",
        timestamp=STARTED + timedelta(minutes=2),
        commit=False,
    )
    db_session.commit()
    secret_url = "https://x.example/?token=abc123secret"
    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={
            "steps": [
                _step(
                    "step-mid",
                    action="navigate",
                    url=secret_url,
                    target="Inbox",
                    reasoning="Open the inbox.",
                    step_index=4,
                    occurred_at="2026-09-24T10:01:00+00:00",
                )
            ]
        },
    )
    assert response.status_code == 200

    timeline = client.get(f"{BASE}/{session.id}/activity")
    assert timeline.status_code == 200
    items = [
        item
        for item in timeline.json()["items"]
        if item["activity_type"] in {"tool_call", "browser_step"}
    ]
    assert [item["activity_type"] for item in items] == [
        "tool_call",
        "browser_step",
        "tool_call",
    ]
    assert [item["tool_name"] for item in items] == [
        "write_file",
        "navigate",
        "read_file",
    ]
    step = items[1]
    assert step["metadata"]["source"] == "playwright_mcp"
    assert step["metadata"]["source_step_id"] == "step-mid"
    assert step["metadata"]["step_index"] == 4
    assert step["metadata"]["action"] == "navigate"
    assert step["metadata"]["screenshot"] is None
    assert "abc123secret" not in step["metadata"]["url"]
    assert "abc123secret" not in (step["summary"] or "")
    assert step["title"] == _default_activity_title(
        SimpleNamespace(activity_type="browser_step", metadata_=step["metadata"])
    )
    assert step["title"].startswith("navigate ")
    assert len(step["title"]) <= 120


def test_session_search_finds_a_term_that_appears_only_in_reasoning(
    client, db_session, test_user
):
    """Session search returns the session from a browser step's reasoning."""
    session = _session(db_session, test_user.account_id, "browser-search")
    token = _token(db_session, test_user)
    posted = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={
            "steps": [
                _step(
                    "step-reason",
                    action="click",
                    target="Save",
                    reasoning="confirmed the zephyrledger total before saving",
                )
            ]
        },
    )
    assert posted.status_code == 200

    found = client.post(
        SEARCH_URL,
        json={"query": "zephyrledger", "mode": "keyword"},
    )

    assert found.status_code == 200
    results = found.json()["results"]
    assert [item["runtime_session_id"] for item in results] == [str(session.id)]
    assert results[0]["snippets"][0]["source_kind"] == "browser_step"


def test_pinned_key_can_flush_after_the_session_ends(client, db_session, test_user):
    """A key bound to a finished session can still post its last steps."""
    session = _session(
        db_session,
        test_user.account_id,
        "browser-pinned-ended",
        ended_at=STARTED + timedelta(minutes=5),
    )
    token = _token(db_session, test_user, runtime_session_id=session.id)

    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [_step("step-after", action="done")]},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert len(_browser_rows(db_session, session.id)) == 1


def test_capture_disabled_indexes_a_descriptor_without_reasoning(
    client, db_session, test_user
):
    """With content capture off, the chunk records the shape, not the text."""
    session = _session(db_session, test_user.account_id, "browser-capture-off")
    token = _token(db_session, test_user)

    with patch.object(settings, "model_gateway_capture_content", False):
        response = client.post(
            f"{BASE}/{session.id}/browser-steps",
            headers=_headers(token),
            json={
                "steps": [
                    _step(
                        "step-private",
                        action="type",
                        reasoning="the zephyrledger passphrase",
                    )
                ]
            },
        )

    assert response.status_code == 200
    rows = _browser_rows(db_session, session.id)
    chunks = crud_session_search_document.list_for_source(
        db_session,
        source_kind=SOURCE_KIND_BROWSER_STEP,
        source_id=str(rows[0].id),
    )
    assert len(chunks) == 1
    assert chunks[0].redaction_state == REDACTION_STATE_METADATA_ONLY
    assert "content_captured: false" in chunks[0].content
    assert "zephyrledger" not in chunks[0].content


def test_a_concurrent_insert_of_the_same_step_is_a_duplicate(db_session, test_user):
    """Losing the unique-index race returns the row the other writer stored."""
    session = _session(db_session, test_user.account_id, "browser-race")
    step = BrowserStepIn(
        source="api",
        source_step_id="step-race",
        step_index=0,
        action="wait",
    )
    first, created = crud_runtime_session_activity.log_browser_step(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        api_key_id=None,
        step=step,
        commit=False,
    )
    assert created is True
    original = CRUDRuntimeSessionActivity._find_browser_step
    calls = {"count": 0}

    def miss_once(self, db, *, runtime_session_id, source, source_step_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original(
            self,
            db,
            runtime_session_id=runtime_session_id,
            source=source,
            source_step_id=source_step_id,
        )

    with patch.object(CRUDRuntimeSessionActivity, "_find_browser_step", miss_once):
        raced, raced_created = crud_runtime_session_activity.log_browser_step(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            api_key_id=None,
            step=step,
            commit=False,
        )

    assert raced_created is False
    assert raced.id == first.id
    assert len(_browser_rows(db_session, session.id)) == 1


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"screenshot-body"


def _png(tag: str = "") -> bytes:
    return PNG_BYTES + tag.encode("utf-8")


def _shot(data: bytes, content_type: str = "image/png") -> dict:
    return {
        "content_type": content_type,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _screenshot_artifacts(db_session, session_id):
    return (
        db_session.query(models.RuntimeSessionArtifact)
        .filter(
            models.RuntimeSessionArtifact.runtime_session_id == session_id,
            models.RuntimeSessionArtifact.kind == "screenshot",
        )
        .order_by(models.RuntimeSessionArtifact.created_at.asc())
        .all()
    )


def test_a_step_with_a_png_stores_an_artifact_served_to_the_console(
    client, db_session, test_user
):
    """The image is an artifact; the step points at it; GET returns the bytes."""
    session = _session(db_session, test_user.account_id, "browser-shot")
    token = _token(db_session, test_user)
    data = _png("one")
    step = _step("shot-1", action="screenshot", step_index=3)
    step["screenshot"] = _shot(data)

    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [step]},
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
    artifacts = _screenshot_artifacts(db_session, session.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.availability == "available"
    assert artifact.content_type == "image/png"
    assert artifact.size_bytes == len(data)
    assert artifact.manifest == {"step_index": 3, "action": "screenshot"}
    [row] = _browser_rows(db_session, session.id)
    db_session.refresh(row)
    assert artifact.activity_id == row.id
    assert row.metadata_["screenshot"] == {
        "artifact_id": str(artifact.id),
        "availability": "available",
        "content_type": "image/png",
        "size_bytes": len(data),
    }
    assert "data_base64" not in str(row.metadata_)

    served = client.get(f"{BASE}/{session.id}/artifacts/{artifact.id}")
    assert served.status_code == 200
    assert served.content == data
    assert served.headers["content-type"] == "image/png"
    assert served.headers["cache-control"] == "private, max-age=300"
    assert served.headers["x-content-type-options"] == "nosniff"

    timeline = client.get(f"{BASE}/{session.id}/activity")
    [item] = [
        item
        for item in timeline.json()["items"]
        if item["activity_type"] == "browser_step"
    ]
    assert item["metadata"]["screenshot"]["artifact_id"] == str(artifact.id)


def test_the_artifact_route_is_scoped_to_the_session_and_account(
    client, db_session, test_user
):
    """Another session's path, an unknown id, or a bad id is a 404."""
    session = _session(db_session, test_user.account_id, "browser-shot-scope")
    other = _session(db_session, test_user.account_id, "browser-shot-other")
    token = _token(db_session, test_user)
    step = _step("scope-1", action="screenshot")
    step["screenshot"] = _shot(_png("scope"))
    client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [step]},
    )
    [artifact] = _screenshot_artifacts(db_session, session.id)

    assert client.get(f"{BASE}/{other.id}/artifacts/{artifact.id}").status_code == 404
    assert (
        client.get(f"{BASE}/{session.id}/artifacts/{uuid.uuid4()}").status_code == 404
    )
    assert client.get(f"{BASE}/{session.id}/artifacts/not-a-uuid").status_code == 404

    foreign_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other screenshots account"}
    )
    foreign = _session(db_session, foreign_account.id, "browser-shot-foreign")
    assert client.get(f"{BASE}/{foreign.id}/artifacts/{artifact.id}").status_code == 404


def test_the_oldest_screenshot_is_evicted_past_the_session_bound(
    client, db_session, test_user, monkeypatch
):
    """With a bound of 2, the third screenshot evicts the oldest step's image."""
    monkeypatch.setattr(settings, "runtime_session_screenshots_per_session_max", 2)
    session = _session(db_session, test_user.account_id, "browser-shot-bound")
    token = _token(db_session, test_user)
    steps = []
    for index in range(3):
        step = _step(
            f"bound-{index}",
            action="screenshot",
            step_index=index,
            occurred_at=(STARTED + timedelta(seconds=index)).isoformat(),
        )
        step["screenshot"] = _shot(_png(f"bound-{index}"))
        steps.append(step)

    # Two batches: the bound holds across flushes, not only inside one.
    first = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": steps[:2]},
    )
    second = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": steps[2:]},
    )
    assert first.json()["accepted"] == 2
    assert second.json()["accepted"] == 1

    db_session.expire_all()
    artifacts = {
        artifact.source_ref: artifact
        for artifact in _screenshot_artifacts(db_session, session.id)
    }
    assert artifacts["bound-0"].availability == "evicted"
    assert artifacts["bound-0"].ciphertext is None
    assert artifacts["bound-1"].availability == "available"
    assert artifacts["bound-2"].availability == "available"
    rows = {
        row.metadata_["source_step_id"]: row
        for row in _browser_rows(db_session, session.id)
    }
    evicted_meta = rows["bound-0"].metadata_["screenshot"]
    assert evicted_meta["availability"] == "evicted"
    assert evicted_meta["artifact_id"] == str(artifacts["bound-0"].id)
    assert evicted_meta["content_type"] == "image/png"
    assert rows["bound-1"].metadata_["screenshot"]["availability"] == "available"

    gone = client.get(f"{BASE}/{session.id}/artifacts/{artifacts['bound-0'].id}")
    assert gone.status_code == 410
    assert gone.json() == {"availability": "evicted"}
    kept = client.get(f"{BASE}/{session.id}/artifacts/{artifacts['bound-2'].id}")
    assert kept.status_code == 200
    assert kept.content == _png("bound-2")


def test_the_session_bound_evicts_by_step_time_not_arrival(
    client, db_session, test_user, monkeypatch
):
    """A late flush of an older step is the oldest, so its image is dropped."""
    monkeypatch.setattr(settings, "runtime_session_screenshots_per_session_max", 1)
    session = _session(db_session, test_user.account_id, "browser-shot-order")
    token = _token(db_session, test_user)
    newer = _step(
        "order-new", action="screenshot", occurred_at="2026-09-24T10:05:00+00:00"
    )
    newer["screenshot"] = _shot(_png("new"))
    older = _step(
        "order-old", action="screenshot", occurred_at="2026-09-24T10:01:00+00:00"
    )
    older["screenshot"] = _shot(_png("old"))
    for step in (newer, older):
        client.post(
            f"{BASE}/{session.id}/browser-steps",
            headers=_headers(token),
            json={"steps": [step]},
        )

    db_session.expire_all()
    availability = {
        artifact.source_ref: artifact.availability
        for artifact in _screenshot_artifacts(db_session, session.id)
    }
    assert availability == {"order-new": "available", "order-old": "evicted"}


def test_a_session_under_legal_hold_keeps_screenshots_past_the_bound(
    client, db_session, test_user, monkeypatch
):
    """Held screenshots are never evicted by the per-session bound."""
    monkeypatch.setattr(settings, "runtime_session_screenshots_per_session_max", 1)
    session = _session(db_session, test_user.account_id, "browser-shot-held")
    session.legal_hold = True
    db_session.commit()
    token = _token(db_session, test_user)
    steps = []
    for index in range(2):
        step = _step(f"held-{index}", action="screenshot", step_index=index)
        step["screenshot"] = _shot(_png(f"held-{index}"))
        steps.append(step)
    client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": steps},
    )

    db_session.expire_all()
    assert [
        artifact.availability
        for artifact in _screenshot_artifacts(db_session, session.id)
    ] == ["available", "available"]


def test_a_bad_screenshot_rejects_only_its_row(
    client, db_session, test_user, monkeypatch
):
    """Oversized, bad base64, or mismatched bytes are row errors."""
    monkeypatch.setattr(settings, "runtime_session_screenshot_max_bytes", 64)
    session = _session(db_session, test_user.account_id, "browser-shot-big")
    token = _token(db_session, test_user)
    ok = _step("big-ok", action="screenshot")
    ok["screenshot"] = _shot(_png("ok"))
    big = _step("big-big", action="screenshot", step_index=1)
    big["screenshot"] = _shot(_png("x" * 80))
    just_over = _step("big-edge", action="screenshot", step_index=2)
    just_over["screenshot"] = _shot(_png().ljust(65, b"z"))
    bad = _step("big-bad", action="screenshot", step_index=3)
    bad["screenshot"] = {"content_type": "image/png", "data_base64": "not base64!"}
    wrong = _step("big-wrong", action="screenshot", step_index=4)
    wrong["screenshot"] = _shot(_png("jpeg?"), content_type="image/jpeg")
    plain = _step("big-plain", action="click", step_index=5)

    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [ok, big, just_over, bad, wrong, plain]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 2
    assert body["rejected"] == [
        {"index": 1, "error": "screenshot_too_large"},
        {"index": 2, "error": "screenshot_too_large"},
        {"index": 3, "error": "screenshot_invalid"},
        {"index": 4, "error": "screenshot_invalid"},
    ]
    stored = {
        row.metadata_["source_step_id"] for row in _browser_rows(db_session, session.id)
    }
    assert stored == {"big-ok", "big-plain"}
    [artifact] = _screenshot_artifacts(db_session, session.id)
    assert artifact.source_ref == "big-ok"


def test_reposting_a_step_does_not_store_a_second_screenshot(
    client, db_session, test_user
):
    """Same idempotency key: a duplicate step and no second artifact."""
    session = _session(db_session, test_user.account_id, "browser-shot-dup")
    token = _token(db_session, test_user)
    step = _step("dup-1", action="screenshot")
    step["screenshot"] = _shot(_png("first"))
    first = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [step]},
    )
    step["screenshot"] = _shot(_png("second"))
    second = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [step]},
    )

    assert first.json()["accepted"] == 1
    assert second.json() == {"accepted": 0, "duplicates": 1, "rejected": []}
    [artifact] = _screenshot_artifacts(db_session, session.id)
    served = client.get(f"{BASE}/{session.id}/artifacts/{artifact.id}")
    assert served.content == _png("first")


def test_a_full_account_budget_rejects_the_screenshot_row_only(
    client, db_session, test_user, monkeypatch
):
    """storage_budget_exhausted drops that step and keeps its siblings."""
    session = _session(db_session, test_user.account_id, "browser-shot-budget")
    session.legal_hold = True
    db_session.commit()
    token = _token(db_session, test_user)
    first = _step("budget-1", action="screenshot")
    first["screenshot"] = _shot(_png("a"))
    client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [first]},
    )
    # The held screenshot fills the budget and cannot be evicted.
    monkeypatch.setattr(
        settings, "runtime_session_artifact_account_max_bytes", len(_png("a"))
    )
    second = _step("budget-2", action="screenshot", step_index=1)
    second["screenshot"] = _shot(_png("b"))
    plain = _step("budget-3", action="click", step_index=2)

    response = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=_headers(token),
        json={"steps": [second, plain]},
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": 1,
        "duplicates": 0,
        "rejected": [{"index": 0, "error": "storage_budget_exhausted"}],
    }
    stored = {
        row.metadata_["source_step_id"] for row in _browser_rows(db_session, session.id)
    }
    assert stored == {"budget-1", "budget-3"}
    assert [a.source_ref for a in _screenshot_artifacts(db_session, session.id)] == [
        "budget-1"
    ]
