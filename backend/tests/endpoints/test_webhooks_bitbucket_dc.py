"""Bitbucket Data Center 10.2 webhook intake: authentication and normalization.

Every delivery is a recorded synthetic fixture from
``tests/fixtures/bitbucket_dc/webhooks_10.2.json`` signed over the exact bytes
sent. No Data Center instance is contacted.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import get_task_publisher
from preloop.utils import bitbucket_dc as dc
from preloop.utils.bitbucket_dc_webhooks import (
    BITBUCKET_DC_EVENT_MAP,
    BITBUCKET_DC_WEBHOOK_EVENTS,
    BITBUCKET_DC_WEBHOOK_MAX_BYTES,
    compute_webhook_signature,
)

INSTANCE = "https://bitbucket.example.com/bitbucket"
SECRET = "dc-webhook-secret"
TRACKER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
URL = f"/api/v1/private/webhooks/bitbucket_dc/{TRACKER_ID}"
FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "bitbucket_dc"
        / "webhooks_10.2.json"
    ).read_text()
)["events"]


def fx(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES[key])


@pytest.fixture(autouse=True)
def dc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRELOOP_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv(dc.ENV_ENABLED, "true")
    monkeypatch.setenv(dc.ENV_INSTANCES, json.dumps([INSTANCE]))


def make_tracker(**details: Any) -> MagicMock:
    tracker = MagicMock()
    tracker.id = TRACKER_ID
    tracker.tracker_type = "bitbucket_dc"
    tracker.is_active = True
    tracker.is_deleted = False
    tracker.url = INSTANCE
    tracker.subscribed_events = None
    tracker.resolved_webhook_secret = SECRET
    connection = {
        "instance_url": INSTANCE,
        "project_key": "PRJ",
        "repository_slug": "my-repo",
        "repository_id": 42,
        "username": "preloop-bot",
    }
    connection.update(details)
    tracker.connection_details = {k: v for k, v in connection.items() if v is not None}
    return tracker


@pytest.fixture
def harness():
    with (
        patch("preloop.api.app.connect_nats", new_callable=AsyncMock),
        patch("preloop.api.app.close_nats", new_callable=AsyncMock),
        patch("preloop.api.endpoints.webhooks.crud_tracker") as crud_tracker,
        patch("preloop.api.endpoints.webhooks.crud_project") as crud_project,
        patch("preloop.api.endpoints.webhooks.crud_organization"),
    ):
        app = create_app()
        session = MagicMock(spec=Session)
        publisher = AsyncMock()

        def override_db():
            yield session

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_task_publisher] = lambda: publisher
        tracker = make_tracker()
        crud_tracker.get.return_value = tracker
        project = MagicMock()
        project.organization_id = uuid.UUID("99999999-0000-0000-0000-000000000001")
        crud_project.get_for_tracker_by_identifier.return_value = project
        yield {
            "client": TestClient(app),
            "publisher": publisher,
            "tracker": tracker,
            "crud_tracker": crud_tracker,
            "crud_project": crud_project,
        }
        app.dependency_overrides.clear()


def body_of(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def post(
    harness: Dict[str, Any],
    event_key: str,
    body: bytes,
    *,
    secret: Optional[str] = SECRET,
    signature: Optional[str] = None,
    request_id: Optional[str] = "req-1",
    url: str = URL,
):
    headers = {"X-Event-Key": event_key, "Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature"] = signature
    elif secret is not None:
        headers["X-Hub-Signature"] = compute_webhook_signature(secret, body)
    if request_id:
        headers["X-Request-Id"] = request_id
    return harness["client"].post(url, content=body, headers=headers)


def dispatched(harness: Dict[str, Any]) -> list:
    return [
        c
        for c in harness["publisher"].publish_task.await_args_list
        if c.args[0] == "process_webhook_event"
    ]


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------


def test_signed_raw_bytes_are_accepted_and_dispatched_once(harness) -> None:
    body = body_of(fx("pr:opened"))
    response = post(harness, "pr:opened", body)
    assert response.status_code == 200, response.text
    calls = dispatched(harness)
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["tracker_type"] == "bitbucket_dc"
    assert kwargs["event_type"] == "pr:opened"
    assert kwargs["delivery_id"] == f"bitbucket_dc:{TRACKER_ID}:req-1"
    assert kwargs["tracker_id"] == str(TRACKER_ID)
    payload = kwargs["payload"]
    assert payload["pull_request"]["number"] == 101
    assert payload["repository"]["id"] == 42


def test_missing_signature_is_rejected_without_unsigned_fallback(harness) -> None:
    response = post(harness, "pr:opened", body_of(fx("pr:opened")), secret=None)
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing Bitbucket signature"
    assert not dispatched(harness)


@pytest.mark.parametrize(
    "signature",
    [
        "sha256=" + "0" * 64,
        "sha1=" + "0" * 40,
        "sha256=nothex",
        "deadbeef",
        "sha256=",
    ],
)
def test_invalid_or_malformed_signature_is_rejected(harness, signature) -> None:
    response = post(harness, "pr:opened", body_of(fx("pr:opened")), signature=signature)
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Bitbucket signature"
    assert not dispatched(harness)


def test_signature_is_checked_before_the_body_is_parsed(harness) -> None:
    response = post(harness, "pr:opened", b"{not json", secret="wrong-secret")
    assert response.status_code == 403
    assert not dispatched(harness)


def test_unicode_body_mutation_breaks_the_signature(harness) -> None:
    payload = fx("pr:comment:added")
    payload["comment"]["text"] = "Café ✓ needs a test"
    signed = body_of(payload)
    # Same JSON value, different bytes: \\u escapes instead of raw UTF-8.
    sent = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    assert json.loads(sent) == json.loads(signed) and sent != signed
    response = post(
        harness,
        "pr:comment:added",
        sent,
        signature=compute_webhook_signature(SECRET, signed),
    )
    assert response.status_code == 403
    # The exact signed bytes, Unicode included, are accepted.
    ok = post(harness, "pr:comment:added", signed)
    assert ok.status_code == 200, ok.text
    assert dispatched(harness)[0].kwargs["payload"]["comment"]["body"] == (
        "Café ✓ needs a test"
    )


def test_one_byte_body_mutation_is_rejected(harness) -> None:
    body = body_of(fx("pr:opened"))
    signature = compute_webhook_signature(SECRET, body)
    response = post(harness, "pr:opened", body + b" ", signature=signature)
    assert response.status_code == 403


def test_secret_rotation_rejects_the_old_secret(harness) -> None:
    body = body_of(fx("pr:opened"))
    assert post(harness, "pr:opened", body).status_code == 200
    harness["tracker"].resolved_webhook_secret = "rotated-secret"
    assert post(harness, "pr:opened", body, secret=SECRET).status_code == 403
    assert post(harness, "pr:opened", body, secret="rotated-secret").status_code == 200


def test_tracker_without_secret_refuses_deliveries(harness) -> None:
    harness["tracker"].resolved_webhook_secret = None
    response = post(harness, "pr:opened", body_of(fx("pr:opened")))
    assert response.status_code == 403
    assert response.json()["detail"] == "Webhook not configured"


def test_other_tenants_tracker_and_unknown_ids_are_not_found(harness) -> None:
    other = make_tracker()
    other.tracker_type = "bitbucket"
    harness["crud_tracker"].get.return_value = other
    assert post(harness, "pr:opened", body_of(fx("pr:opened"))).status_code == 404
    harness["crud_tracker"].get.return_value = None
    assert post(harness, "pr:opened", body_of(fx("pr:opened"))).status_code == 404
    bad = "/api/v1/private/webhooks/bitbucket_dc/not-a-uuid"
    assert post(harness, "pr:opened", b"{}", url=bad).status_code == 404


def test_another_trackers_secret_does_not_authenticate(harness) -> None:
    response = post(
        harness, "pr:opened", body_of(fx("pr:opened")), secret="other-tenant-secret"
    )
    assert response.status_code == 403
    assert not dispatched(harness)


def test_feature_flag_off_hides_the_endpoint(harness, monkeypatch) -> None:
    monkeypatch.setenv(dc.ENV_ENABLED, "false")
    response = post(harness, "pr:opened", body_of(fx("pr:opened")))
    assert response.status_code == 404
    harness["crud_tracker"].get.assert_not_called()


def test_oversized_body_is_refused_before_processing(harness) -> None:
    body = b"{" + b" " * (BITBUCKET_DC_WEBHOOK_MAX_BYTES + 1) + b"}"
    response = post(harness, "pr:opened", body)
    assert response.status_code == 413
    harness["crud_tracker"].get.assert_not_called()


def test_secret_and_signature_are_never_logged(harness, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    body = body_of(fx("pr:opened"))
    post(harness, "pr:opened", body, secret="wrong")
    post(harness, "pr:opened", body)
    text = caplog.text
    assert SECRET not in text
    assert compute_webhook_signature(SECRET, body) not in text
    assert "jdoe@example.com" not in text


# ----------------------------------------------------------------------
# Binding to instance and repository
# ----------------------------------------------------------------------


def test_wrong_repository_is_rejected(harness) -> None:
    payload = fx("pr:opened")
    for ref in ("fromRef", "toRef"):
        payload["pullRequest"][ref]["repository"]["id"] = 77
    response = post(harness, "pr:opened", body_of(payload))
    assert response.status_code == 403
    assert response.json()["detail"] == "repository_mismatch"
    assert not dispatched(harness)


def test_wrong_project_with_reused_repository_id_is_rejected(harness) -> None:
    payload = fx("repo:refs_changed")
    payload["repository"]["project"]["key"] = "OTHER"
    response = post(harness, "repo:refs_changed", body_of(payload))
    assert response.status_code == 403


def test_unbound_tracker_accepts_only_imported_repositories(harness) -> None:
    harness["tracker"].connection_details = {
        "instance_url": INSTANCE,
        "project_key": "PRJ",
    }
    harness["crud_project"].get_for_tracker_by_identifier.return_value = None
    response = post(harness, "pr:opened", body_of(fx("pr:opened")))
    assert response.status_code == 403
    assert response.json()["detail"] == "repository_mismatch"


def test_links_to_another_instance_are_rejected(harness) -> None:
    payload = fx("pr:opened")
    payload["pullRequest"]["links"]["self"][0]["href"] = (
        "https://evil.example.net/projects/PRJ/repos/my-repo/pull-requests/101"
    )
    response = post(harness, "pr:opened", body_of(payload))
    assert response.status_code == 403
    assert response.json()["detail"] == "instance_mismatch"


def test_instance_removed_from_the_allowlist_is_rejected(harness, monkeypatch) -> None:
    monkeypatch.setenv(dc.ENV_INSTANCES, json.dumps(["https://scm.example.com"]))
    response = post(harness, "pr:opened", body_of(fx("pr:opened")))
    assert response.status_code == 403


def test_cloud_event_payload_is_not_accepted(harness) -> None:
    cloud = {
        "pullrequest": {"id": 7, "title": "x"},
        "repository": {"full_name": "ws/repo", "uuid": "{r-1}"},
    }
    response = post(harness, "pullrequest:created", body_of(cloud))
    assert response.status_code == 200
    assert response.json()["reason"] == "unsupported_event"
    response = post(harness, "pr:opened", body_of({**cloud, "eventKey": "pr:opened"}))
    assert response.status_code == 400
    assert not dispatched(harness)


def test_header_and_payload_event_keys_must_agree(harness) -> None:
    response = post(harness, "pr:merged", body_of(fx("pr:opened")))
    assert response.status_code == 400
    assert response.json()["detail"] == "event_key_mismatch"


# ----------------------------------------------------------------------
# Event families
# ----------------------------------------------------------------------


@pytest.mark.parametrize("event_key", sorted(BITBUCKET_DC_WEBHOOK_EVENTS))
def test_every_selected_event_family_dispatches(harness, event_key) -> None:
    response = post(harness, event_key, body_of(fx(event_key)))
    assert response.status_code == 200, response.text
    calls = dispatched(harness)
    assert len(calls) == 1
    assert calls[0].kwargs["event_type"] == event_key
    assert BITBUCKET_DC_EVENT_MAP[event_key]


@pytest.mark.parametrize(
    ("event_key", "reason"),
    [("repo:forked", "unsupported_event"), ("diagnostics:ping", "ping")],
)
def test_unsupported_events_are_acknowledged_without_work(
    harness, event_key, reason
) -> None:
    response = post(harness, event_key, body_of(fx(event_key)))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == reason
    assert not dispatched(harness)


def test_ref_update_without_a_new_head_is_not_dispatched(harness) -> None:
    payload = fx("pr:from_ref_updated")
    payload["previousFromHash"] = payload["pullRequest"]["fromRef"]["latestCommit"]
    response = post(harness, "pr:from_ref_updated", body_of(payload))
    assert response.json()["reason"] == "head_unchanged"
    assert not dispatched(harness)


def test_unsubscribed_event_is_acknowledged(harness) -> None:
    harness["tracker"].subscribed_events = ["pr:opened"]
    response = post(harness, "pr:merged", body_of(fx("pr:merged")))
    assert response.json()["reason"] == "not_subscribed"
    assert not dispatched(harness)


def test_missing_request_id_derives_a_stable_identity(harness) -> None:
    body = body_of(fx("pr:comment:added"))
    post(harness, "pr:comment:added", body, request_id=None)
    post(harness, "pr:comment:added", body, request_id=None)
    other = fx("pr:comment:added")
    other["comment"]["id"] = 503
    post(harness, "pr:comment:added", body_of(other), request_id=None)
    ids = [c.kwargs["delivery_id"] for c in dispatched(harness)]
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]
    assert ids[0].startswith(f"bitbucket_dc:{TRACKER_ID}:derived:")


def test_inactive_tracker_acknowledges_after_authentication(harness) -> None:
    harness["tracker"].is_active = False
    response = post(harness, "pr:opened", body_of(fx("pr:opened")))
    assert response.json() == {"status": "ignored", "reason": "tracker_inactive"}
    assert post(harness, "pr:opened", b"{}", secret="x").status_code == 403
