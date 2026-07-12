"""Endpoint tests for the gateway accounting self-check.

Covers GET /api/v1/cost/health: empty-window skip behavior, healthy pass
state, streaming-usage regression detection, unpriced-cost detection,
account scoping of the counters, query-param validation, and auth.
"""

from preloop.models.crud import crud_account, crud_api_usage, crud_audit_log, crud_user

COST_HEALTH = "/api/v1/cost/health"


def _log_usage(db, *, account_id, user_id=None, **overrides):
    """Helper to log a single gateway request with healthy defaults."""
    params = dict(
        endpoint="/openai/v1/chat/completions",
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
        cost_source="catalog",
        usage_source="provider",
        meta_data={"endpoint_kind": "chat_completions_stream"},
    )
    params.update(overrides)
    return crud_api_usage.log_gateway_request(db, **params)


def _log_audit_event(db, *, account_id, user_id=None):
    """Log one gateway audit event for the account."""
    return crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="model_gateway_request",
        resource_type="model_gateway",
        status="success",
    )


def _checks_by_key(body):
    """Index the response checklist by check key."""
    return {check["key"]: check for check in body["checks"]}


def test_cost_health_no_traffic_all_checks_skip(client, db_session, test_user):
    """With no gateway traffic, every check (and the overall status) skips."""
    response = client.get(COST_HEALTH)

    assert response.status_code == 200
    body = response.json()
    assert body["window_hours"] == 24
    assert body["status"] == "skip"
    checks = _checks_by_key(body)
    assert set(checks) == {
        "gateway_traffic_seen",
        "streaming_usage_recorded",
        "costs_priced",
        "usage_source_health",
        "audit_events_present",
    }
    for check in checks.values():
        assert check["status"] == "skip"
        assert check["detail"] == "no gateway traffic in window"


def test_cost_health_healthy_streaming_traffic_passes(client, db_session, test_user):
    """Priced streaming rows with provider usage and audit events all pass."""
    for _ in range(3):
        _log_usage(db_session, account_id=test_user.account_id, user_id=test_user.id)
    _log_audit_event(db_session, account_id=test_user.account_id, user_id=test_user.id)
    db_session.commit()

    body = client.get(COST_HEALTH).json()

    assert body["status"] == "pass"
    checks = _checks_by_key(body)
    for key in (
        "gateway_traffic_seen",
        "streaming_usage_recorded",
        "costs_priced",
        "usage_source_health",
        "audit_events_present",
    ):
        assert checks[key]["status"] == "pass", checks[key]


def test_cost_health_streaming_rows_without_tokens_fail(client, db_session, test_user):
    """Successful streaming rows recording 0 tokens must fail the check."""
    for _ in range(2):
        _log_usage(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=None,
            cost_source=None,
        )
    db_session.commit()

    body = client.get(COST_HEALTH).json()

    assert body["status"] == "fail"
    checks = _checks_by_key(body)
    assert checks["gateway_traffic_seen"]["status"] == "pass"
    assert checks["streaming_usage_recorded"]["status"] == "fail"
    assert "0/2" in checks["streaming_usage_recorded"]["detail"]


def test_cost_health_unpriced_rows_fail_costs_priced(client, db_session, test_user):
    """Token-bearing rows without a stored cost must fail costs_priced."""
    for _ in range(2):
        _log_usage(
            db_session,
            account_id=test_user.account_id,
            user_id=test_user.id,
            estimated_cost=None,
            cost_source="unpriced",
            meta_data={"endpoint_kind": "chat_completions"},
        )
    db_session.commit()

    body = client.get(COST_HEALTH).json()

    assert body["status"] == "fail"
    checks = _checks_by_key(body)
    assert checks["costs_priced"]["status"] == "fail"
    assert "2 rows tagged cost_source=unpriced" in checks["costs_priced"]["detail"]
    assert "reprice" in checks["costs_priced"]["detail"]
    # Non-streaming traffic only, so the streaming check skips rather than fails.
    assert checks["streaming_usage_recorded"]["status"] == "skip"


def test_cost_health_subscription_rows_count_as_priced(client, db_session, test_user):
    """Subscription-covered rows (cost 0) must not be flagged as unpriced."""
    _log_usage(
        db_session,
        account_id=test_user.account_id,
        user_id=test_user.id,
        estimated_cost=0.0,
        cost_source="subscription",
    )
    db_session.commit()

    body = client.get(COST_HEALTH).json()

    checks = _checks_by_key(body)
    assert checks["costs_priced"]["status"] == "pass"


def test_cost_health_is_account_scoped(client, db_session, test_user):
    """Broken rows in a foreign account must not fail this account's check."""
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    other_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "email": "iso-health@example.com",
            "username": "iso-health",
            "full_name": "Other User",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db_session.flush()
    # Foreign account has a broken streaming row; ours has none at all.
    _log_usage(
        db_session,
        account_id=other_account.id,
        user_id=other_user.id,
        total_tokens=0,
        prompt_tokens=0,
        completion_tokens=0,
        estimated_cost=None,
        cost_source=None,
    )
    db_session.commit()

    body = client.get(COST_HEALTH).json()

    assert body["status"] == "skip"
    assert _checks_by_key(body)["gateway_traffic_seen"]["status"] == "skip"


def test_cost_health_hours_param_bounds_window(client, db_session, test_user):
    """The hours query param is validated and echoed back in the response."""
    body = client.get(COST_HEALTH, params={"hours": 48}).json()
    assert body["window_hours"] == 48

    assert client.get(COST_HEALTH, params={"hours": 0}).status_code == 422
    assert client.get(COST_HEALTH, params={"hours": 169}).status_code == 422


def test_cost_health_requires_authentication(app, db_session):
    """Without the auth dependency override, the endpoint should reject access."""
    from preloop.api.auth import get_current_active_user
    from fastapi.testclient import TestClient

    # Drop the authenticated-user override installed by the app fixture so the
    # real auth dependency runs and rejects the anonymous request.
    app.dependency_overrides.pop(get_current_active_user, None)
    with TestClient(app) as anon_client:
        response = anon_client.get(COST_HEALTH)
    assert response.status_code in (401, 403)
