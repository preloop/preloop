"""Tests for the OpenRouter ledger-proportional backfill (issue #219 follow-up).

The allocation core is a pure function, exercised here against fixture
ledger data; no OpenRouter call is ever made (the fetch test mocks HTTP).
"""

import uuid
from datetime import date, datetime

import pytest

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.services import ledger_backfill, usage_repricing
from preloop.services.ledger_backfill import (
    LedgerEntry,
    UsageRowInfo,
    alias_family,
    apply_ledger_backfill,
    fetch_openrouter_activity,
    load_unpriced_rows,
    plan_ledger_allocation,
)

DAY = date(2026, 8, 5)


def _row(alias, tokens, day=DAY, execution_id=None):
    return UsageRowInfo(
        api_usage_id=uuid.uuid4(),
        day=day,
        model_alias=alias,
        weight_tokens=tokens,
        flow_execution_id=execution_id,
    )


# ---------------------------------------------------------------------------
# alias_family: both sides of the reconciliation must agree on a family key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("openrouter/auto-beta", "auto-beta"),
        ("preloop/openrouter/auto", "auto"),
        ("openrouter/deepseek/deepseek-chat", "deepseek-chat"),
        ("deepseek/deepseek-chat:free", "deepseek-chat"),
        ("openai/gpt-4.1-2025-04-14", "gpt-4.1"),
        ("Claude-3-5-Sonnet-20241022", "claude-3-5-sonnet"),
        ("", None),
        (None, None),
    ],
)
def test_alias_family(name, family):
    assert alias_family(name) == family


# ---------------------------------------------------------------------------
# plan_ledger_allocation: pure allocation against fixture ledger data.
# ---------------------------------------------------------------------------


def test_exact_family_bucket_allocates_proportionally_by_tokens():
    """A (day x family) bucket splits the ledger total by prompt+completion
    tokens; the allocation sums back to the ledger figure."""
    ledger = [LedgerEntry(day=DAY, model="deepseek/deepseek-chat", usage_usd=0.09)]
    rows = [
        _row("openrouter/deepseek/deepseek-chat", 1000),
        _row("openrouter/deepseek/deepseek-chat", 2000),
    ]

    plan = plan_ledger_allocation(ledger, rows)

    assert [a.allocated_cost for a in plan.allocations] == [
        pytest.approx(0.03),
        pytest.approx(0.06),
    ]
    assert len(plan.buckets) == 1
    bucket = plan.buckets[0]
    assert bucket.ledger_total == pytest.approx(0.09)
    assert bucket.row_count == 2
    assert bucket.allocated_total == pytest.approx(0.09)
    assert not plan.unallocated_ledger
    assert not plan.unmatched_rows


def test_auto_rows_claim_the_days_unmatched_ledger_spend():
    """Auto Router rows have no fixed family; the ledger reports the routed
    models instead, so auto rows absorb the day's unmatched ledger total."""
    ledger = [
        LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=0.05),
        LedgerEntry(day=DAY, model="anthropic/claude-sonnet-4", usage_usd=0.15),
        LedgerEntry(day=DAY, model="deepseek/deepseek-chat", usage_usd=0.02),
    ]
    rows = [
        _row("openrouter/auto-beta", 300),
        _row("openrouter/auto-beta", 100),
        # A concrete-alias row claims its own family first...
        _row("openrouter/deepseek/deepseek-chat", 500),
    ]

    plan = plan_ledger_allocation(ledger, rows)

    by_family = {b.family: b for b in plan.buckets}
    # ...so the auto bucket gets only gpt-4.1 + claude spend (0.20).
    assert by_family["auto*"].ledger_total == pytest.approx(0.20)
    assert by_family["auto*"].allocated_total == pytest.approx(0.20)
    assert set(by_family["auto*"].ledger_models) == {
        "openai/gpt-4.1",
        "anthropic/claude-sonnet-4",
    }
    auto_allocs = [a for a in plan.allocations if a.bucket_family == "auto*"]
    assert [a.allocated_cost for a in auto_allocs] == [
        pytest.approx(0.15),
        pytest.approx(0.05),
    ]
    assert by_family["deepseek-chat"].allocated_total == pytest.approx(0.02)
    assert not plan.unallocated_ledger


def test_buckets_do_not_mix_days():
    """Each day allocates independently: same family, different days."""
    ledger = [
        LedgerEntry(day=DAY, model="deepseek/deepseek-chat", usage_usd=0.10),
        LedgerEntry(
            day=date(2026, 8, 6), model="deepseek/deepseek-chat", usage_usd=0.30
        ),
    ]
    rows = [
        _row("deepseek/deepseek-chat", 100, day=DAY),
        _row("deepseek/deepseek-chat", 100, day=date(2026, 8, 6)),
    ]

    plan = plan_ledger_allocation(ledger, rows)

    by_day = {a.day: a.allocated_cost for a in plan.allocations}
    assert by_day == {DAY: pytest.approx(0.10), date(2026, 8, 6): pytest.approx(0.30)}


def test_zero_token_bucket_splits_equally():
    """Rows recorded with 0 tokens still receive an equal share (documented
    fallback: no better weight exists)."""
    ledger = [LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=0.04)]
    rows = [_row("openai/gpt-4.1", 0), _row("openai/gpt-4.1", 0)]

    plan = plan_ledger_allocation(ledger, rows)

    assert [a.allocated_cost for a in plan.allocations] == [
        pytest.approx(0.02),
        pytest.approx(0.02),
    ]


def test_unmatched_ledger_and_rows_are_reported_not_invented():
    """Ledger spend with no rows stays unallocated; rows with no ledger
    spend stay unpriced — both reported for the dry-run output."""
    ledger = [LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=0.07)]
    rows = [_row("mistralai/mistral-large", 500)]

    plan = plan_ledger_allocation(ledger, rows)

    assert plan.allocations == []
    assert plan.unallocated_ledger == [(DAY, "gpt-4.1", pytest.approx(0.07))]
    assert plan.unmatched_rows == [(DAY, "mistral-large", 1)]


def test_auto_rows_without_ledger_remainder_stay_unpriced():
    """If concrete rows claimed all ledger spend, auto rows get nothing."""
    ledger = [LedgerEntry(day=DAY, model="deepseek/deepseek-chat", usage_usd=0.02)]
    rows = [
        _row("openrouter/deepseek/deepseek-chat", 100),
        _row("openrouter/auto-beta", 100),
    ]

    plan = plan_ledger_allocation(ledger, rows)

    assert len(plan.allocations) == 1
    assert plan.unmatched_rows == [(DAY, "auto*", 1)]


def test_negative_or_zero_ledger_entries_are_ignored():
    """The ledger's zero rows carry no money to allocate."""
    ledger = [LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=0.0)]
    rows = [_row("openai/gpt-4.1", 100)]

    plan = plan_ledger_allocation(ledger, rows)

    assert plan.allocations == []
    assert plan.unmatched_rows == [(DAY, "gpt-4.1", 1)]


def test_execution_deltas_sum_per_flow_execution():
    execution = uuid.uuid4()
    ledger = [LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=0.10)]
    rows = [
        _row("openai/gpt-4.1", 100, execution_id=execution),
        _row("openai/gpt-4.1", 300, execution_id=execution),
        _row("openai/gpt-4.1", 600),  # no execution: excluded from deltas
    ]

    plan = plan_ledger_allocation(ledger, rows)

    assert plan.execution_deltas == {execution: pytest.approx(0.04)}


# ---------------------------------------------------------------------------
# fetch_openrouter_activity: HTTP shape and auth failures (mocked; the test
# suite never calls OpenRouter).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_fetch_activity_parses_daily_items_and_sums_byok(monkeypatch):
    """usage + byok_usage_inference are both customer spend; date comes from
    the item, model from the undated slug."""
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers))
        return _FakeResponse(
            payload={
                "data": [
                    {
                        "date": "2026-08-05",
                        "model": "openai/gpt-4.1",
                        "model_permaslug": "openai/gpt-4.1-2025-04-14",
                        "usage": 0.015,
                        "byok_usage_inference": 0.012,
                        "requests": 5,
                        "prompt_tokens": 50,
                        "completion_tokens": 125,
                    }
                ]
            }
        )

    monkeypatch.setattr(ledger_backfill.requests, "get", fake_get)

    entries = fetch_openrouter_activity("test-key", [DAY])

    assert entries == [
        LedgerEntry(day=DAY, model="openai/gpt-4.1", usage_usd=pytest.approx(0.027))
    ]
    url, params, headers = calls[0]
    assert url == "https://openrouter.ai/api/v1/activity"
    assert params == {"date": "2026-08-05"}
    assert headers["Authorization"] == "Bearer test-key"


def test_fetch_activity_403_explains_management_key_requirement(monkeypatch):
    monkeypatch.setattr(
        ledger_backfill.requests,
        "get",
        lambda *a, **k: _FakeResponse(status_code=403),
    )

    with pytest.raises(RuntimeError, match="management/provisioning key"):
        fetch_openrouter_activity("inference-key", [DAY])


# ---------------------------------------------------------------------------
# DB round-trip: eligibility scope, the reconciled marker, rollup sync.
# ---------------------------------------------------------------------------


def _create_openrouter_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "OpenRouter Auto",
            "provider_name": "openrouter",
            "model_identifier": "openrouter/auto-beta",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "meta_data": {
                "gateway": {"enabled": True, "model_alias": "openrouter/auto-beta"}
            },
        },
        account_id=test_user.account_id,
    )


def _log_row(
    db_session,
    test_user,
    ai_model,
    *,
    cost_source="unpriced",
    estimated_cost=None,
    provider_name="openrouter",
    tokens=(900, 100),
    day=DAY,
):
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.4,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        ai_model_id=str(ai_model.id),
        model_alias="openrouter/auto-beta",
        provider_name=provider_name,
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        total_tokens=sum(tokens),
        estimated_cost=estimated_cost,
        cost_source=cost_source,
        meta_data={"usage_details": {"prompt_tokens": tokens[0]}},
    )
    row.timestamp = datetime(day.year, day.month, day.day, 12, 0, 0)
    db_session.commit()
    return row


def test_load_unpriced_rows_only_selects_unpriced_openrouter_rows(
    db_session, test_user
):
    """Scope guard: catalog/provider-priced rows and other providers are
    never eligible."""
    ai_model = _create_openrouter_model(db_session, test_user)
    eligible = _log_row(db_session, test_user, ai_model)
    _log_row(
        db_session,
        test_user,
        ai_model,
        cost_source="catalog",
        estimated_cost=0.01,
    )
    _log_row(
        db_session,
        test_user,
        ai_model,
        cost_source="provider",
        estimated_cost=0.002,
    )
    _log_row(db_session, test_user, ai_model, provider_name="openai")

    rows = load_unpriced_rows(
        db_session,
        account_id=str(test_user.account_id),
        provider_name="openrouter",
        start=DAY,
        end=DAY,
    )

    assert [r.api_usage_id for r in rows] == [eligible.id]
    assert rows[0].weight_tokens == 1000
    assert rows[0].day == DAY


def test_apply_writes_reconciled_cost_marker_and_syncs_rollups(
    db_session, test_user, monkeypatch
):
    """Applying a plan tags rows 'reconciled' with the meta_data marker and
    triggers the existing execution rollup sync."""
    ai_model = _create_openrouter_model(db_session, test_user)
    row = _log_row(db_session, test_user, ai_model)
    execution_id = uuid.uuid4()
    synced = []
    monkeypatch.setattr(
        usage_repricing, "_sync_execution_rollups", lambda db, ids: synced.append(ids)
    )

    ledger = [LedgerEntry(day=DAY, model="anthropic/claude-sonnet-4", usage_usd=0.2)]
    rows = [
        UsageRowInfo(
            api_usage_id=row.id,
            day=DAY,
            model_alias=row.model_alias,
            weight_tokens=1000,
            flow_execution_id=execution_id,
        )
    ]
    plan = plan_ledger_allocation(ledger, rows)

    updated = apply_ledger_backfill(db_session, plan)

    assert updated == 1
    db_session.refresh(row)
    assert row.cost_source == "reconciled"
    assert row.estimated_cost == pytest.approx(0.2)
    marker = (row.meta_data or {}).get("reconciled")
    assert marker["method"] == "ledger_daily_proportional"
    assert marker["ledger_day"] == "2026-08-05"
    assert marker["ledger_total"] == pytest.approx(0.2)
    assert marker["allocated_at"]
    assert synced == [[execution_id]]


def test_apply_skips_rows_priced_since_planning(db_session, test_user):
    """A row that gained a price between plan and apply is left alone."""
    ai_model = _create_openrouter_model(db_session, test_user)
    row = _log_row(db_session, test_user, ai_model)
    plan = plan_ledger_allocation(
        [LedgerEntry(day=DAY, model="anthropic/claude-sonnet-4", usage_usd=0.2)],
        [
            UsageRowInfo(
                api_usage_id=row.id,
                day=DAY,
                model_alias=row.model_alias,
                weight_tokens=1000,
            )
        ],
    )
    # Simulate a concurrent repricing before apply.
    crud_api_usage.update_cost_fields(
        db_session, api_usage_id=row.id, estimated_cost=0.001, cost_source="provider"
    )

    updated = apply_ledger_backfill(db_session, plan)

    assert updated == 0
    db_session.refresh(row)
    assert row.cost_source == "provider"
    assert row.estimated_cost == pytest.approx(0.001)
