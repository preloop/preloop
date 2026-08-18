"""Endpoint tests for the OSS synchronous reprice + CSV ledger backfill.

Covers POST /api/v1/cost/reprice (real counters, window cap) and
POST /api/v1/cost/ledger-backfill/csv (dry-run vs apply, idempotency,
"Other" residual reporting, estimate-vs-reconciled separation). All CSV
content is synthetic fixture data in the Explore export's shape.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from preloop.models.crud import crud_ai_model, crud_api_usage

REPRICE = "/api/v1/cost/reprice"
LEDGER_CSV = "/api/v1/cost/ledger-backfill/csv"

DAY = date(2026, 8, 5)

FIXTURE_CSV = (
    "date__day,model,total_usage\n"
    "2026-08-05,Acme Z9 Mini 0731,0.30\n"
    "2026-08-05,Other,0.05\n"
)


def _create_model(db_session, test_user, *, pricing=None, provider="openrouter"):
    meta = {}
    if pricing:
        meta["pricing"] = pricing
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Acme Z9 Mini",
            "provider_name": provider,
            "model_identifier": "acme/acme-z9-mini-0731",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": meta,
        },
        account_id=test_user.account_id,
    )


def _log_unpriced_row(db_session, test_user, ai_model, *, tokens, day=DAY):
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openrouter/acme/acme-z9-mini-0731",
        provider_name="openrouter",
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        estimated_cost=None,
        cost_source="unpriced",
    )
    row.timestamp = datetime(day.year, day.month, day.day, 12, 0, 0)
    db_session.commit()
    return row


def _upload(client, *, apply=False, csv_text=FIXTURE_CSV, **form):
    data = {"apply": "true" if apply else "false", **form}
    return client.post(
        LEDGER_CSV,
        files={"file": ("export.csv", csv_text, "text/csv")},
        data=data,
    )


# ---------------------------------------------------------------------------
# POST /cost/reprice
# ---------------------------------------------------------------------------


def test_reprice_endpoint_returns_real_counts_synchronously(
    client, db_session, test_user
):
    """No async cliff: a multi-day window is scanned in-request and the
    response carries actual examined/updated counters."""
    ai_model = _create_model(
        db_session,
        test_user,
        pricing={"input_price_per_1k": 0.01},
    )
    row = _log_unpriced_row(db_session, test_user, ai_model, tokens=1000)
    now = datetime.now(timezone.utc)
    row.timestamp = now - timedelta(days=3)
    db_session.commit()

    response = client.post(
        REPRICE,
        json={
            "start_date": (now - timedelta(days=14)).isoformat(),
            "end_date": now.isoformat(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submitted_async"] is False
    assert body["rows_examined"] == 1
    assert body["rows_updated"] == 1
    db_session.expire_all()
    refreshed = crud_api_usage.get(db_session, id=row.id)
    assert refreshed.estimated_cost == 0.01
    assert refreshed.cost_source == "model_config"


def test_reprice_endpoint_rejects_oversized_windows(client, db_session, test_user):
    now = datetime.now(timezone.utc)
    response = client.post(
        REPRICE,
        json={
            "start_date": (now - timedelta(days=200)).isoformat(),
            "end_date": now.isoformat(),
        },
    )
    assert response.status_code == 422
    assert "92 days" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /cost/ledger-backfill/csv
# ---------------------------------------------------------------------------


def test_ledger_csv_dry_run_reports_plan_and_writes_nothing(
    client, db_session, test_user
):
    ai_model = _create_model(db_session, test_user)
    row_a = _log_unpriced_row(db_session, test_user, ai_model, tokens=100)
    row_b = _log_unpriced_row(db_session, test_user, ai_model, tokens=300)

    response = _upload(client, apply=False)

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["rows_updated"] is None
    assert body["start"] == "2026-08-05"
    assert body["end"] == "2026-08-05"
    assert body["eligible_rows"] == 2
    assert body["rows_to_reconcile"] == 2
    assert body["total_allocated"] == pytest.approx(0.30)
    # The export's "Other" bucket is residual — reported, never allocated.
    assert body["other_residual_usd"] == pytest.approx(0.05)
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["family"] == "acme-z9-mini-0731"

    db_session.expire_all()
    for row in (row_a, row_b):
        refreshed = crud_api_usage.get(db_session, id=row.id)
        assert refreshed.estimated_cost is None
        assert refreshed.cost_source == "unpriced"


def test_ledger_csv_apply_reconciles_pro_rata_and_is_idempotent(
    client, db_session, test_user
):
    ai_model = _create_model(db_session, test_user)
    row_a = _log_unpriced_row(db_session, test_user, ai_model, tokens=100)
    row_b = _log_unpriced_row(db_session, test_user, ai_model, tokens=300)

    response = _upload(client, apply=True)

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is False
    assert body["rows_updated"] == 2

    db_session.expire_all()
    refreshed_a = crud_api_usage.get(db_session, id=row_a.id)
    refreshed_b = crud_api_usage.get(db_session, id=row_b.id)
    assert refreshed_a.cost_source == "reconciled"
    assert refreshed_b.cost_source == "reconciled"
    assert refreshed_a.estimated_cost == pytest.approx(0.075)
    assert refreshed_b.estimated_cost == pytest.approx(0.225)
    assert (refreshed_a.meta_data or {})["reconciled"]["ledger_day"] == "2026-08-05"

    # Re-uploading the same export changes nothing: the rows are no longer
    # unpriced, so they are no longer eligible.
    second = _upload(client, apply=True)
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["eligible_rows"] == 0
    assert second_body["rows_updated"] == 0
    db_session.expire_all()
    refreshed_a = crud_api_usage.get(db_session, id=row_a.id)
    assert refreshed_a.estimated_cost == pytest.approx(0.075)


def test_reconciled_costs_survive_a_full_reprice(client, db_session, test_user):
    """Estimate-vs-reconciled separation end to end: after a ledger apply,
    even only_unpriced=false repricing leaves the reconciled figures alone."""
    ai_model = _create_model(
        db_session,
        test_user,
        pricing={"input_price_per_1k": 0.01},
    )
    row = _log_unpriced_row(db_session, test_user, ai_model, tokens=100)
    assert _upload(client, apply=True).status_code == 200
    db_session.expire_all()
    reconciled_cost = crud_api_usage.get(db_session, id=row.id).estimated_cost

    response = client.post(
        REPRICE,
        json={
            "start_date": "2026-08-01T00:00:00+00:00",
            "end_date": "2026-08-10T00:00:00+00:00",
            "only_unpriced": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["rows_skipped"] >= 1
    db_session.expire_all()
    refreshed = crud_api_usage.get(db_session, id=row.id)
    assert refreshed.cost_source == "reconciled"
    assert refreshed.estimated_cost == pytest.approx(reconciled_cost)


def test_ledger_csv_rejects_non_explorer_exports(client, db_session, test_user):
    response = _upload(client, csv_text="Date,Kind,Cost\n2026-08-05,usage,1\n")
    assert response.status_code == 422
    assert "missing column" in response.json()["detail"]


def test_ledger_csv_unmatched_ledger_spend_is_reported_not_invented(
    client, db_session, test_user
):
    """Ledger lines with no matching unpriced rows surface as residual."""
    response = _upload(client)  # no usage rows exist at all
    assert response.status_code == 200
    body = response.json()
    assert body["eligible_rows"] == 0
    assert body["rows_to_reconcile"] == 0
    assert body["unallocated_ledger"] == [
        {"day": "2026-08-05", "family": "acme-z9-mini-0731", "amount_usd": 0.30}
    ]
