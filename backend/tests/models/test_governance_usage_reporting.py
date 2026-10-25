"""Governance overhead stays out of customer usage, but remains budgeted."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage, crud_spend_outlier_finding
from preloop.models.crud.weekly_report_data import usage_snapshot
from preloop.models.crud.billing import billing
from preloop.models.crud.plan_eligibility import ingest_tokens_this_month
from preloop.models.crud.weekly_signals import gateway_latency_signals


@pytest.mark.parametrize("funding", ["platform", "account"])
@pytest.mark.parametrize(
    "customer_metadata", [None, {}, {"purpose": None}, {"purpose": "customer_report"}]
)
def test_governance_overhead_is_not_customer_usage(
    db_session: Session,
    test_user: models.User,
    funding: str,
    customer_metadata: dict | None,
) -> None:
    end = datetime.now(UTC) + timedelta(minutes=1)
    start = end - timedelta(days=7)
    for purpose, cost, status in [
        (None, 2.0, 200),
        ("governance_session_digest", 3.0, 500),
        ("governance_review", 4.0, 500),
        ("governance_future_stage", 5.0, 500),
    ]:
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/v1/chat/completions",
            method="POST",
            account_id=str(test_user.account_id),
            user_id=str(test_user.id),
            status_code=status,
            duration=1 if purpose is None else 100,
            estimated_cost=cost,
            total_tokens=10 if purpose is None else 100,
            model_alias="customer" if purpose is None else "analysis",
            meta_data={
                "purpose": purpose,
                "funding": funding,
                "rate_limit": {"remaining_requests": 1},
            }
            if purpose
            else customer_metadata,
        )
    summary = crud_api_usage.get_gateway_usage_summary(
        db_session, account_id=str(test_user.account_id), start_date=start, end_date=end
    )
    assert summary["request_count"] == 1
    assert summary["error_count"] == 0
    assert summary["estimated_cost"] == 2
    snapshot = usage_snapshot(
        db_session, account_id=test_user.account_id, start=start, end=end
    )
    assert snapshot["request_count"] == 1
    assert snapshot["total_gateway_cost_usd"] == 2
    assert snapshot["error_count"] == 0
    latency = gateway_latency_signals(
        db_session, account_id=test_user.account_id, start=start, end=end
    )
    assert latency[0].request_count == 1
    assert latency[0].error_count == 0
    assert latency[0].p95_duration == 1
    by_model = crud_api_usage.get_gateway_usage_by_model(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start,
        end_date=end,
    )
    assert len(by_model) == 1
    assert by_model[0]["model_alias"] == "customer"
    timeline = crud_api_usage.get_gateway_usage_timeseries(
        db_session,
        account_id=str(test_user.account_id),
        start_date=start,
        end_date=end,
    )
    assert sum(day["request_count"] for day in timeline) == 1
    spends = crud_spend_outlier_finding.gateway_spend_by_user_model_day(
        db_session,
        account_id=test_user.account_id,
        start_day=start.date(),
        end_day=end.date(),
    )
    assert len(spends) == 1 and spends[0].cost_usd == 2
    assert (
        crud_api_usage.get_latest_rate_limit_snapshots(
            db_session, account_id=str(test_user.account_id)
        )
        == []
    )
    assert (
        crud_api_usage.get_gateway_spend(
            db_session,
            account_id=str(test_user.account_id),
            start=start,
            purpose="governance_review",
            funding=funding,
        )
        == 4
    )

    month = datetime.now(UTC).strftime("%Y-%m")
    observations = billing.monthly_observations(
        db_session,
        account_id=str(test_user.account_id),
        start=start,
        end=end,
        hosted_model_ids=[],
        byok_model_ids=[],
    )
    assert observations[month]["request_count"] == 1
    assert (
        ingest_tokens_this_month(db_session, account_id=str(test_user.account_id)) == 10
    )
