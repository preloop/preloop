"""Endpoint tests for the open-source cost analytics router.

Covers GET /api/v1/cost/summary.

These tests are intentionally NON-overlapping with
tests/endpoints/test_gateway_usage_summary.py, which exercises the underlying
ModelGatewayUsageService via the /account/gateway-usage/* routes. Here we focus
on the public /cost/summary endpoint: success shape, empty-state defaults,
account-scoping isolation, query-param filtering (runtime_principal_id /
date range), and validation (422).
"""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_account, crud_api_usage, crud_user

COST_SUMMARY = "/api/v1/cost/summary"


def _log_usage(db, *, account_id, user_id=None, **overrides):
    """Helper to log a single gateway request with sensible defaults."""
    params = dict(
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(user_id) if user_id else None,
        account_id=str(account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_cost=0.05,
    )
    params.update(overrides)
    return crud_api_usage.log_gateway_request(db, **params)


def _make_account_with_user(db, email):
    """Create a separate account with a user for isolation tests."""
    account = crud_account.create(
        db, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Other User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return account, user


def test_cost_summary_success_returns_aggregated_usage(client, db_session, test_user):
    """A logged gateway request should surface in the cost summary totals."""
    _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    db_session.commit()

    response = client.get(COST_SUMMARY)

    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 1
    assert body["successful_requests"] == 1
    assert body["failed_requests"] == 0
    assert body["token_usage"]["total_tokens"] == 20
    assert body["token_usage"]["prompt_tokens"] == 12
    assert body["token_usage"]["completion_tokens"] == 8
    assert body["estimated_cost"] == 0.05
    assert body["usage_by_model"][0]["model_alias"] == "openai/gpt-5"


def test_cost_summary_response_includes_expected_top_level_keys(
    client, db_session, test_user
):
    """The summary response should always carry the documented envelope keys."""
    _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    db_session.commit()

    body = client.get(COST_SUMMARY).json()

    for key in (
        "period_start",
        "period_end",
        "total_requests",
        "successful_requests",
        "failed_requests",
        "token_usage",
        "estimated_cost",
        "budget",
        "requests_by_day",
        "usage_by_model",
        "usage_by_flow",
        "usage_by_session",
    ):
        assert key in body, f"missing key: {key}"


def test_cost_summary_empty_state_returns_zeroed_defaults(
    client, db_session, test_user
):
    """With no gateway usage, the summary should be a well-formed zero report."""
    response = client.get(COST_SUMMARY)

    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 0
    assert body["successful_requests"] == 0
    assert body["failed_requests"] == 0
    assert body["estimated_cost"] == 0.0
    assert body["token_usage"]["total_tokens"] == 0
    assert body["usage_by_model"] == []
    assert body["usage_by_flow"] == []
    assert body["usage_by_session"] == []


def test_cost_summary_counts_failed_requests(client, db_session, test_user):
    """5xx gateway calls should be reported as failed requests."""
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        status_code=200,
    )
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        status_code=500,
        estimated_cost=0.0,
        total_tokens=0,
        prompt_tokens=0,
        completion_tokens=0,
    )
    db_session.commit()

    body = client.get(COST_SUMMARY).json()

    assert body["total_requests"] == 2
    assert body["successful_requests"] == 1
    assert body["failed_requests"] == 1


def test_cost_summary_is_account_scoped(client, db_session, test_user):
    """One account must never see another account's usage."""
    other_account, other_user = _make_account_with_user(db_session, "iso@example.com")
    # Two requests for the authenticated account...
    _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    # ...and one for a foreign account that must be excluded.
    _log_usage(
        db_session,
        account_id=other_account.id,
        user_id=other_user.id,
        estimated_cost=9.99,
        total_tokens=999,
    )
    db_session.commit()

    body = client.get(COST_SUMMARY).json()

    assert body["total_requests"] == 2
    assert body["token_usage"]["total_tokens"] == 40
    assert round(body["estimated_cost"], 2) == 0.10


def test_cost_summary_filters_by_runtime_principal_id(client, db_session, test_user):
    """The runtime_principal_id query param should scope the summary."""
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-A",
        total_tokens=20,
    )
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        runtime_principal_type="claude_code",
        runtime_principal_id="workspace-B",
        total_tokens=50,
    )
    db_session.commit()

    body = client.get(
        COST_SUMMARY, params={"runtime_principal_id": "workspace-A"}
    ).json()

    assert body["total_requests"] == 1
    assert body["token_usage"]["total_tokens"] == 20


def test_cost_summary_start_date_in_future_excludes_past_usage(
    client, db_session, test_user
):
    """A start_date after the usage timestamp should yield an empty window."""
    usage = _log_usage(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )
    usage.timestamp = datetime.now(UTC) - timedelta(days=10)
    db_session.add(usage)
    db_session.commit()

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    body = client.get(COST_SUMMARY, params={"start_date": future}).json()

    assert body["total_requests"] == 0


def test_cost_summary_date_window_includes_usage_in_range(
    client, db_session, test_user
):
    """A window bracketing the usage timestamp should include it."""
    usage = _log_usage(
        db_session, account_id=test_user.account_id, user_id=test_user.id
    )
    usage.timestamp = datetime.now(UTC) - timedelta(days=2)
    db_session.add(usage)
    db_session.commit()

    start = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    end = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    body = client.get(
        COST_SUMMARY, params={"start_date": start, "end_date": end}
    ).json()

    assert body["total_requests"] == 1


def test_cost_summary_invalid_start_date_returns_422(client):
    """A non-datetime start_date should fail request validation."""
    response = client.get(COST_SUMMARY, params={"start_date": "not-a-date"})
    assert response.status_code == 422


def test_cost_summary_invalid_end_date_returns_422(client):
    """A non-datetime end_date should fail request validation."""
    response = client.get(COST_SUMMARY, params={"end_date": "13/13/2026"})
    assert response.status_code == 422


def test_cost_summary_requires_authentication(app, db_session):
    """Without the auth dependency override, the endpoint should reject access."""
    from preloop.api.auth import get_current_active_user
    from fastapi.testclient import TestClient

    # Drop the authenticated-user override installed by the app fixture so the
    # real auth dependency runs and rejects the anonymous request.
    app.dependency_overrides.pop(get_current_active_user, None)
    with TestClient(app) as anon_client:
        response = anon_client.get(COST_SUMMARY)
    assert response.status_code in (401, 403)


def test_cost_summary_names_unpriced_models(client, db_session, test_user):
    """Unpriced rows must surface their model names next to the counts."""
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        model_alias="openai-compatible/muse-spark",
        estimated_cost=None,
        cost_source="unpriced",
        total_tokens=5000,
    )
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        model_alias="openai-compatible/muse-spark",
        estimated_cost=None,
        cost_source="unpriced",
        total_tokens=1000,
    )
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        model_alias="openai/gpt-5",
        estimated_cost=0.05,
        cost_source="catalog",
        total_tokens=7000,
    )
    db_session.commit()

    body = client.get(COST_SUMMARY).json()

    assert body["unpriced_requests"] == 2
    assert body["unpriced_tokens"] == 6000
    assert body["unpriced_models"] == [
        {"model": "openai-compatible/muse-spark", "requests": 2, "tokens": 6000}
    ]


def test_cost_summary_unpriced_models_empty_when_all_priced(
    client, db_session, test_user
):
    """A fully priced window reports an empty unpriced model list."""
    _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    db_session.commit()

    body = client.get(COST_SUMMARY).json()

    assert body["unpriced_requests"] == 0
    assert body["unpriced_models"] == []
