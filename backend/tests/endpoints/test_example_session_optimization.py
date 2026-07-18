"""Endpoint tests for the bundled example-session optimization route."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from preloop.models.crud import crud_account


@pytest.fixture
def cost_client(app, db_session, test_user):
    """Test client bound to the test account for the cost routes."""
    from fastapi.testclient import TestClient

    from preloop.api.common import get_account_for_user

    account = crud_account.get(db_session, id=test_user.account_id)
    app.dependency_overrides[get_account_for_user] = lambda: account
    with TestClient(app) as test_client:
        yield test_client


EXAMPLE_URL = "/api/v1/billing/cost/runtime-sessions/example/optimization"


def test_example_endpoint_returns_labelled_suggestions(cost_client) -> None:
    """A new account can see a real, clearly-labelled example."""
    response = cost_client.get(EXAMPLE_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["is_example"] is True
    assert body["example_notice"]
    assert body["potential_savings_tokens"] > 0
    assert len(body["suggestions"]) >= 3
    assert body["potential_savings_tokens"] <= body["analyzed_scope_total_tokens"]


def test_example_endpoint_writes_no_audit_event(
    cost_client, db_session, test_user
) -> None:
    """Viewing a bundled sample must not enter launch telemetry.

    ``optimization_result_viewed`` powers the "reached-their-number" metric.
    Counting a bundled example would overstate activation, so this route
    deliberately does not emit it.
    """
    from preloop.models.models.audit_log import AuditLog

    before = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "optimization_result_viewed")
        .count()
    )

    assert cost_client.get(EXAMPLE_URL).status_code == 200

    after = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "optimization_result_viewed")
        .count()
    )
    assert after == before


def test_example_endpoint_404s_when_transcript_missing(cost_client, tmp_path) -> None:
    """A missing fixture degrades to the console's normal empty state."""
    from preloop.services.example_optimization import load_example_transcript

    load_example_transcript.cache_clear()
    with patch(
        "preloop.services.example_optimization.TRANSCRIPT_PATH",
        tmp_path / "missing.json",
    ):
        response = cost_client.get(EXAMPLE_URL)
    load_example_transcript.cache_clear()

    assert response.status_code == 404


def test_example_route_does_not_shadow_real_session_route(
    cost_client, db_session, test_user
) -> None:
    """A real session's POST route must still reach the real-session service.

    The example route is a GET on ``.../example/optimization``; the real route
    is a POST on ``.../{id}/optimizations``. This pins that adding the former
    did not capture traffic intended for the latter.
    """
    from datetime import UTC, datetime

    from preloop.models.crud import crud_runtime_session

    now = datetime.now(UTC)
    runtime_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id="workspace-example-shadow",
        session_reference="claude-session-shadow",
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-example-shadow",
        runtime_principal_name="Claude Workspace",
        started_at=now,
        last_activity_at=now,
    )
    db_session.commit()

    response = cost_client.post(
        f"/api/v1/billing/cost/runtime-sessions/{runtime_session.id}/optimizations",
        json={},
    )

    assert response.status_code == 200
    # The real route must never return the bundled sample.
    assert response.json()["is_example"] is False
