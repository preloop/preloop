"""Tests for the unified per-request runtime-session timeline endpoint."""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_api_usage, crud_runtime_session


def _make_session(db_session, account_id):
    """Create a runtime session for the given account."""
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id="workspace-requests",
        runtime_principal_type="managed_agents",
        runtime_principal_id="agent-xyz",
        runtime_principal_name="Requests Workspace",
        last_activity_at=datetime.now(UTC),
    )
    db_session.commit()
    return session


def _log_request(
    db_session,
    *,
    account_id,
    runtime_session_id,
    status_code,
    total_tokens,
    estimated_cost,
    when,
    meta_data=None,
):
    """Insert a single gateway ApiUsage row for a session."""
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/v1/chat/completions",
        method="POST",
        status_code=status_code,
        duration=0.5,
        account_id=str(account_id),
        runtime_session_id=str(runtime_session_id),
        model_alias="gpt-4o",
        provider_name="openai",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens - total_tokens // 2,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        meta_data=meta_data,
    )


def test_runtime_session_requests_returns_per_request_rows(
    client, db_session, test_user
):
    """The endpoint should return per-request rows with tokens, cost and tools."""
    session = _make_session(db_session, test_user.account_id)
    now = datetime.now(UTC)

    _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=200,
        total_tokens=1000,
        estimated_cost=0.02,
        when=now,
        meta_data={
            "finish_reason": "stop",
            "tools_meta": [
                {
                    "name": "search_issues",
                    "source": "github",
                    "schema_tokens_estimate": 120,
                    "stripped": False,
                },
                {
                    "name": "create_pr",
                    "source": "github",
                    "schema_tokens_estimate": 80,
                    "stripped": True,
                },
            ],
        },
    )
    _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=500,
        total_tokens=200,
        estimated_cost=0.0,
        when=now + timedelta(seconds=5),
    )
    db_session.commit()

    response = client.get(f"/api/v1/runtime-sessions/{session.id}/requests")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert body["failed_count"] == 1
    assert len(body["items"]) == 2

    first = body["items"][0]
    assert first["total_tokens"] == 1000
    assert first["estimated_cost"] == 0.02
    assert first["status_code"] == 200
    assert first["is_error"] is False
    assert first["finish_reason"] == "stop"
    assert len(first["tools"]) == 2
    assert first["tools_total_schema_tokens"] == 200
    tool_names = {tool["name"] for tool in first["tools"]}
    assert tool_names == {"search_issues", "create_pr"}
    stripped = {tool["name"]: tool["stripped"] for tool in first["tools"]}
    assert stripped["create_pr"] is True

    second = body["items"][1]
    assert second["status_code"] == 500
    assert second["is_error"] is True


def test_runtime_session_requests_failed_only_filter(client, db_session, test_user):
    """The failed_only filter should restrict to status_code >= 400 rows."""
    session = _make_session(db_session, test_user.account_id)
    now = datetime.now(UTC)
    _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=200,
        total_tokens=500,
        estimated_cost=0.01,
        when=now,
    )
    _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=429,
        total_tokens=10,
        estimated_cost=0.0,
        when=now + timedelta(seconds=1),
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/runtime-sessions/{session.id}/requests?failed_only=true"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["failed_count"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["status_code"] == 429


def test_runtime_session_requests_event_ids_filter(client, db_session, test_user):
    """Passing event_ids should restrict the result to those ApiUsage rows."""
    session = _make_session(db_session, test_user.account_id)
    now = datetime.now(UTC)
    row_a = _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=200,
        total_tokens=500,
        estimated_cost=0.01,
        when=now,
    )
    _log_request(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session.id,
        status_code=200,
        total_tokens=600,
        estimated_cost=0.02,
        when=now + timedelta(seconds=1),
    )
    db_session.commit()

    response = client.get(
        f"/api/v1/runtime-sessions/{session.id}/requests?event_ids={row_a.id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(row_a.id)


def test_runtime_session_requests_unknown_session_returns_404(client):
    """Unknown session ids should return 404."""
    response = client.get(
        "/api/v1/runtime-sessions/00000000-0000-0000-0000-000000000000/requests"
    )
    assert response.status_code == 404


def test_runtime_session_requests_expose_error_class(client, db_session, test_user):
    """Failed rows must carry ``error_class`` so the console can explain them.

    ``ApiUsage.error_class`` is recorded for every gateway failure but was not
    serialized anywhere, so a request killed by a proxy read-timeout
    (``stream_abandoned``) was indistinguishable in the UI from a user who
    simply cancelled a stream (``client_cancelled``) — both are status 499.
    """
    session = _make_session(db_session, test_user.account_id)
    now = datetime.now(UTC)

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/gemini/v1beta/models/test:streamGenerateContent",
        method="POST",
        status_code=499,
        duration=60.0,
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        model_alias="test-model",
        provider_name="openai",
        error_class="stream_abandoned",
        meta_data={"error_detail": "client was gone before the first chunk"},
    )

    response = client.get(
        f"/api/v1/runtime-sessions/{session.id}/requests?failed_only=true"
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["status_code"] == 499
    assert items[0]["is_error"] is True
    assert items[0]["error_class"] == "stream_abandoned"
