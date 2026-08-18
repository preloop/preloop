"""Tests for the provider daily-ledger CSV backfill (Explore-export mode).

All ledger figures here are synthetic fixtures shaped like an OpenRouter
Activity -> Explore export (``date__day,model,total_usage``); no real
provider data appears in this repository.
"""

import uuid
from datetime import date, datetime

import pytest

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.services.ledger_backfill import (
    UsageRowInfo,
    alias_family,
    apply_ledger_backfill,
    display_name_family,
    load_unpriced_rows,
    parse_explorer_csv,
    plan_ledger_allocation,
)

DAY = date(2026, 8, 5)


# ---------------------------------------------------------------------------
# display_name_family: the export names models for humans; our rows record
# slugs. Both must land on the same family key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("display_name", "family"),
    [
        # Dated display name matches the dated slug (dates are separate SKUs).
        ("Acme Z9 Mini 0731", "acme-z9-mini-0731"),
        ("Gemma 9.9 Flash Lite", "gemma-9.9-flash-lite"),
        # Already-sluggy names pass through unchanged.
        ("gpt-test-120b", "gpt-test-120b"),
        # Extra whitespace collapses; case folds.
        ("  Acme   Z9  ", "acme-z9"),
        # Slugs with org paths defer to the slug normalizer.
        ("acme/acme-z9-mini", "acme-z9-mini"),
        ("", None),
        (None, None),
    ],
)
def test_display_name_family(display_name, family):
    assert display_name_family(display_name) == family


def test_display_name_family_agrees_with_alias_family():
    """The invariant the whole CSV mode rests on: display name and recorded
    alias reduce to the same key."""
    assert display_name_family("Acme Z9 Mini 0731") == alias_family(
        "openrouter/acme/acme-z9-mini-0731"
    )


# ---------------------------------------------------------------------------
# parse_explorer_csv: fixture CSV in the export's exact shape.
# ---------------------------------------------------------------------------

FIXTURE_CSV = (
    "date__day,model,total_usage\n"
    "2026-08-05,Acme Z9 Mini 0731,0.30\n"
    "2026-08-05,Other,0.05\n"
    "2026-08-06,Gemma 9.9 Flash,0.12\n"
    "2026-08-06,Other,4.4e-16\n"
)


def test_parse_explorer_csv_splits_entries_and_other_bucket():
    ledger = parse_explorer_csv(FIXTURE_CSV)

    assert [(e.day, e.model, e.usage_usd) for e in ledger.entries] == [
        (DAY, "acme-z9-mini-0731", pytest.approx(0.30)),
        (date(2026, 8, 6), "gemma-9.9-flash", pytest.approx(0.12)),
    ]
    # "Other" names no model: reported per-day as residual, never allocated.
    assert ledger.other_by_day == [
        (DAY, pytest.approx(0.05)),
        (date(2026, 8, 6), pytest.approx(4.4e-16)),
    ]
    assert ledger.other_total == pytest.approx(0.05, rel=1e-6)
    assert ledger.skipped == []


def test_parse_explorer_csv_tolerates_bom_blank_lines_and_extra_columns():
    text = "﻿date__day,model,total_usage,requests\n2026-08-05,Acme Z9,0.10,42\n\n"
    ledger = parse_explorer_csv(text)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].model == "acme-z9"


def test_parse_explorer_csv_rejects_wrong_header():
    with pytest.raises(ValueError, match="missing column"):
        parse_explorer_csv("Date,Kind,Cost\n2026-08-05,included,1\n")


def test_parse_explorer_csv_collects_malformed_rows_instead_of_aborting():
    text = (
        "date__day,model,total_usage\n"
        "not-a-date,Acme Z9,0.10\n"
        "2026-08-05,Acme Z9,not-a-number\n"
        "2026-08-05,Acme Z9,-0.10\n"
        "2026-08-05,Acme Z9,0.10\n"
    )
    ledger = parse_explorer_csv(text)
    assert len(ledger.entries) == 1
    assert len(ledger.skipped) == 3
    assert "line 2" in ledger.skipped[0]
    assert "line 3" in ledger.skipped[1]
    assert "negative" in ledger.skipped[2]


def test_parsed_entries_allocate_against_display_named_rows():
    """End-to-end (pure): a display-named ledger line finds slug-aliased
    rows, pro-rata by tokens."""
    ledger = parse_explorer_csv(
        "date__day,model,total_usage\n2026-08-05,Acme Z9 Mini 0731,0.30\n"
    )
    rows = [
        UsageRowInfo(
            api_usage_id=uuid.uuid4(),
            day=DAY,
            model_alias="openrouter/acme/acme-z9-mini-0731",
            weight_tokens=100,
        ),
        UsageRowInfo(
            api_usage_id=uuid.uuid4(),
            day=DAY,
            model_alias="openrouter/acme/acme-z9-mini-0731",
            weight_tokens=300,
        ),
    ]

    plan = plan_ledger_allocation(ledger.entries, rows)

    assert [a.allocated_cost for a in plan.allocations] == [
        pytest.approx(0.075),
        pytest.approx(0.225),
    ]
    assert not plan.unallocated_ledger
    assert not plan.unmatched_rows


# ---------------------------------------------------------------------------
# DB round-trip: legacy pre-provenance rows are eligible; re-runs are inert.
# ---------------------------------------------------------------------------


def _create_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Acme Z9 Mini",
            "provider_name": "openrouter",
            "model_identifier": "acme/acme-z9-mini-0731",
            "api_endpoint": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
        },
        account_id=test_user.account_id,
    )


def _log_row(db_session, test_user, ai_model, *, cost_source, estimated_cost, tokens):
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
        estimated_cost=estimated_cost,
        cost_source=cost_source,
    )
    row.timestamp = datetime(DAY.year, DAY.month, DAY.day, 12, 0, 0)
    db_session.commit()
    return row


def test_legacy_rows_without_provenance_are_eligible_and_reconciled(
    db_session, test_user
):
    """Rows from before the cost_source column existed (NULL/NULL) receive
    ledger shares alongside explicitly 'unpriced' rows."""
    ai_model = _create_model(db_session, test_user)
    legacy = _log_row(
        db_session,
        test_user,
        ai_model,
        cost_source=None,
        estimated_cost=None,
        tokens=100,
    )
    tagged = _log_row(
        db_session,
        test_user,
        ai_model,
        cost_source="unpriced",
        estimated_cost=None,
        tokens=300,
    )
    # A catalog-priced row must stay untouched.
    priced = _log_row(
        db_session,
        test_user,
        ai_model,
        cost_source="catalog",
        estimated_cost=0.9,
        tokens=100,
    )

    ledger = parse_explorer_csv(
        "date__day,model,total_usage\n2026-08-05,Acme Z9 Mini 0731,0.40\n"
    )
    rows = load_unpriced_rows(
        db_session,
        account_id=str(test_user.account_id),
        provider_name="openrouter",
        start=DAY,
        end=DAY,
    )
    assert {r.api_usage_id for r in rows} == {legacy.id, tagged.id}

    plan = plan_ledger_allocation(ledger.entries, rows)
    updated = apply_ledger_backfill(db_session, plan)
    assert updated == 2

    db_session.refresh(legacy)
    db_session.refresh(tagged)
    db_session.refresh(priced)
    assert legacy.cost_source == "reconciled"
    assert tagged.cost_source == "reconciled"
    assert legacy.estimated_cost + tagged.estimated_cost == pytest.approx(0.40)
    assert (legacy.meta_data or {})["reconciled"]["method"] == (
        "ledger_daily_proportional"
    )
    assert priced.cost_source == "catalog"
    assert priced.estimated_cost == 0.9

    # Idempotency: a second run finds nothing eligible and writes nothing.
    rows_again = load_unpriced_rows(
        db_session,
        account_id=str(test_user.account_id),
        provider_name="openrouter",
        start=DAY,
        end=DAY,
    )
    assert rows_again == []
    plan_again = plan_ledger_allocation(ledger.entries, rows_again)
    assert apply_ledger_backfill(db_session, plan_again) == 0
    db_session.refresh(tagged)
    assert tagged.cost_source == "reconciled"
