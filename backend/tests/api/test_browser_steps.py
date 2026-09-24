"""Browser step ingestion: idempotency, redaction, auth, timeline, search."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_key,
    crud_runtime_session,
    crud_runtime_session_activity,
)
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
