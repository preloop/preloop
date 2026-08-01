"""CRUD tests for rate-limit telemetry aggregation and snapshots (#136)."""

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_api_usage


def _log_row(
    db_session,
    test_user,
    *,
    status_code: int,
    model_alias: str = "anthropic/claude-sonnet-4-5",
    provider_name: str = "anthropic",
    retry_after_ms=None,
    rate_limit_meta=None,
    purpose=None,
):
    meta = {"rate_limit": rate_limit_meta}
    if purpose:
        meta["purpose"] = purpose
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=status_code,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias=model_alias,
        provider_name=provider_name,
        rate_limit_retry_after_ms=retry_after_ms,
        meta_data=meta,
    )


def test_rate_limit_summary_aggregates_429_rows(db_session, test_user):
    _log_row(
        db_session,
        test_user,
        status_code=429,
        retry_after_ms=30_000,
        rate_limit_meta={
            "retry_after_ms": 30_000,
            "subtype": "transient",
            "subtype_source": "heuristic",
            "headers": {"retry-after": "30"},
        },
    )
    _log_row(
        db_session,
        test_user,
        status_code=429,
        retry_after_ms=15_000,
        rate_limit_meta={
            "retry_after_ms": 15_000,
            "subtype": "quota_exhausted",
            "subtype_source": "heuristic",
            "headers": {"retry-after": "15"},
        },
    )
    # Success traffic must not count toward 429 totals.
    _log_row(db_session, test_user, status_code=200)
    # A 429 with no provider hint counts as a hit but adds no blocked time.
    _log_row(db_session, test_user, status_code=429)

    now = datetime.now(UTC)
    summary = crud_api_usage.get_rate_limit_summary(
        db_session,
        account_id=str(test_user.account_id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )

    totals = summary["totals"]
    assert totals["rate_limited_requests"] == 3
    assert totals["blocked_ms"] == 45_000
    assert totals["quota_exhausted_count"] == 1
    assert totals["transient_count"] == 1
    assert totals["last_rate_limited_at"] is not None

    assert len(summary["by_model"]) == 1
    model_row = summary["by_model"][0]
    assert model_row["model_alias"] == "anthropic/claude-sonnet-4-5"
    assert model_row["rate_limited_requests"] == 3
    assert model_row["blocked_ms"] == 45_000

    # No runtime session on these rows: single NULL-session group.
    assert len(summary["by_session"]) == 1
    assert summary["by_session"][0]["rate_limited_requests"] == 3


def test_rate_limit_summary_excludes_replay_and_window(db_session, test_user):
    _log_row(
        db_session,
        test_user,
        status_code=429,
        retry_after_ms=5_000,
        rate_limit_meta={"retry_after_ms": 5_000},
        purpose="replay_validation",
    )

    now = datetime.now(UTC)
    summary = crud_api_usage.get_rate_limit_summary(
        db_session,
        account_id=str(test_user.account_id),
        start_date=now - timedelta(hours=1),
        end_date=now + timedelta(hours=1),
    )
    assert summary["totals"]["rate_limited_requests"] == 0
    assert summary["totals"]["blocked_ms"] == 0

    # Rows outside the window are excluded too.
    _log_row(db_session, test_user, status_code=429, retry_after_ms=1_000)
    summary = crud_api_usage.get_rate_limit_summary(
        db_session,
        account_id=str(test_user.account_id),
        start_date=now - timedelta(hours=2),
        end_date=now - timedelta(hours=1),
    )
    assert summary["totals"]["rate_limited_requests"] == 0


def test_latest_rate_limit_snapshots_per_provider_model(db_session, test_user):
    # Older observation.
    _log_row(
        db_session,
        test_user,
        status_code=429,
        retry_after_ms=30_000,
        rate_limit_meta={
            "retry_after_ms": 30_000,
            "requests_remaining": 0,
            "headers": {"retry-after": "30"},
        },
    )
    # Newer success observation for the SAME model: should win.
    _log_row(
        db_session,
        test_user,
        status_code=200,
        rate_limit_meta={
            "requests_remaining": 42,
            "requests_limit": 50,
            "headers": {"anthropic-ratelimit-requests-remaining": "42"},
        },
    )
    # A different model with its own snapshot.
    _log_row(
        db_session,
        test_user,
        status_code=200,
        model_alias="openai/gpt-5",
        provider_name="openai",
        rate_limit_meta={
            "tokens_remaining": 29_000,
            "headers": {"x-ratelimit-remaining-tokens": "29000"},
        },
    )
    # Rows without snapshots (rate_limit is JSON null) never appear.
    _log_row(db_session, test_user, status_code=200)

    snapshots = crud_api_usage.get_latest_rate_limit_snapshots(
        db_session,
        account_id=str(test_user.account_id),
    )

    assert len(snapshots) == 2
    by_model = {snapshot["model_alias"]: snapshot for snapshot in snapshots}
    anthropic = by_model["anthropic/claude-sonnet-4-5"]
    assert anthropic["rate_limit"]["requests_remaining"] == 42
    assert anthropic["status_code"] == 200
    assert anthropic["observed_at"] is not None
    openai = by_model["openai/gpt-5"]
    assert openai["rate_limit"]["tokens_remaining"] == 29_000


def test_rate_limit_report_endpoint(client, db_session, test_user):
    """The account report endpoint returns totals, breakdowns, and snapshots."""
    _log_row(
        db_session,
        test_user,
        status_code=429,
        retry_after_ms=30_000,
        rate_limit_meta={
            "retry_after_ms": 30_000,
            "requests_remaining": 0,
            "subtype": "transient",
            "subtype_source": "heuristic",
            "headers": {"retry-after": "30"},
        },
    )

    response = client.get("/api/v1/account/gateway-usage/rate-limits")

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["rate_limited_requests"] == 1
    assert body["totals"]["blocked_ms"] == 30_000
    assert body["totals"]["transient_count"] == 1
    assert body["by_model"][0]["model_alias"] == "anthropic/claude-sonnet-4-5"
    assert body["latest_snapshots"][0]["rate_limit"]["headers"] == {"retry-after": "30"}
    assert body["latest_snapshots"][0]["observed_at"] is not None
