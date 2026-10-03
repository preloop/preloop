"""Spend outlier alerts (#960): the three rules, fire-once, dismissal, digest.

Rows are written straight into ``api_usage`` with explicit timestamps so each
test controls which UTC day the spend lands on.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional
from unittest.mock import MagicMock
import uuid

import pytest
from sqlalchemy import event

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_attention_dismissal,
    crud_spend_outlier_finding,
    crud_spend_outlier_settings,
    crud_user,
)
from preloop.services import spend_outliers
from preloop.services.spend_outliers import (
    ImportedSpendRow,
    SpendOutlierConfig,
    build_spend_outlier_digest_section,
    evaluate_daily_rules,
    evaluate_session_rule,
    finding_item_id,
    is_top_tier,
    model_family,
    list_open_findings,
    register_imported_spend_source,
    run_daily_pass,
    unregister_imported_spend_source,
)

#: Evaluation time: "yesterday" is 2026-09-26.
NOW = datetime(2026, 9, 27, 0, 30, tzinfo=UTC)
YESTERDAY = date(2026, 9, 26)
DISMISSALS = "/api/v1/attention/dismissals"


def _spend(
    db,
    user: models.User,
    day: date,
    cost: float,
    *,
    model: str = "standard-model",
    session_id: Optional[uuid.UUID] = None,
    hour: int = 12,
    purpose: Optional[str] = None,
) -> None:
    db.add(
        models.ApiUsage(
            user_id=user.id,
            account_id=user.account_id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            model_alias=model,
            estimated_cost=cost,
            runtime_session_id=session_id,
            meta_data={"purpose": purpose} if purpose else None,
            timestamp=datetime(day.year, day.month, day.day, hour, 0),
        )
    )
    db.flush()


def _history(db, user, days: Iterable[int], cost: float, **kwargs) -> None:
    for offset in days:
        _spend(db, user, YESTERDAY - timedelta(days=offset), cost, **kwargs)


def _second_user(db, account_id, email="jane@example.com") -> models.User:
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": "Jane Doe",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def _session(db, account_id) -> models.RuntimeSession:
    session = models.RuntimeSession(
        account_id=account_id,
        session_source_type="cli",
        session_source_id=str(uuid.uuid4()),
        title="Refactor the parser",
        started_at=datetime(2026, 9, 26, 10, 0),
    )
    db.add(session)
    db.flush()
    return session


# --- pure rule helpers ------------------------------------------------------


def test_top_tier_matches_prefix_with_or_without_provider():
    """A prefix matches the alias and the alias without its provider part."""
    assert is_top_tier("premium-large", ("premium-",))
    assert is_top_tier("vendor/premium-large", ("premium-",))
    assert is_top_tier("Vendor/Premium-Large", ("vendor/premium",))
    assert not is_top_tier("standard-model", ("premium-",))
    assert not is_top_tier("premium-large", ())


def test_config_defaults_without_settings_row():
    """No row means N=3, 7 days of history, 50 percent, session rule off."""
    config = SpendOutlierConfig.from_row(None)
    assert config.daily_multiple == 3.0
    assert config.min_history_days == 7
    assert config.top_tier_share == 0.5
    assert config.top_tier_model_prefixes == ()
    assert config.session_cost_threshold_usd is None


# --- rule 1: daily spend ----------------------------------------------------


def test_daily_spike_fires_once_per_day(db_session, test_user):
    """Median 10, yesterday 40, N=3: one finding; a rerun adds none."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)

    first = evaluate_daily_rules(db_session, test_user.account_id, NOW)
    second = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert len(first) == 1
    assert second == []
    finding = first[0]
    assert finding.rule == "daily_spend"
    assert finding.day == YESTERDAY
    assert finding.item_id == f"spend:daily_spend:{test_user.id}"
    assert finding.fingerprint == f"daily_spend|{test_user.id}|2026-09-26"
    assert finding.details["spend_usd"] == 40.0
    assert finding.details["median_usd"] == 10.0
    assert finding.details["multiple"] == 4.0
    assert finding.details["imported_usd"] == 0.0

    open_items = list_open_findings(db_session, test_user.account_id, NOW)
    assert len(open_items) == 1
    assert open_items[0]["user_name"] == "Test User"
    assert "4.0x the 28-day median of $10.00" in open_items[0]["summary"]
    assert "not metered" not in open_items[0]["summary"]


def test_daily_spike_needs_seven_days_of_history(db_session, test_user):
    """Six days with spend is not enough history to judge a spike."""
    _history(db_session, test_user, range(1, 7), 10.0)
    _spend(db_session, test_user, YESTERDAY, 100.0)

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_daily_spike_ignores_days_without_spend_in_median(db_session, test_user):
    """Only days that had spend count: sparse history still yields median 10."""
    _history(db_session, test_user, range(2, 28, 3), 10.0)
    _spend(db_session, test_user, YESTERDAY, 30.0)

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert len(found) == 1
    assert found[0].details["median_usd"] == 10.0
    assert found[0].details["history_days"] == 9


def test_daily_spend_below_multiple_does_not_fire(db_session, test_user):
    """29 against a median of 10 is under 3x."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 29.0)

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_daily_multiple_is_configurable(db_session, test_user):
    """With N=2, 25 against a median of 10 fires."""
    crud_spend_outlier_settings.upsert(
        db_session,
        account_id=test_user.account_id,
        values={"daily_multiple": 2.0},
        commit=False,
    )
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 25.0)

    assert len(evaluate_daily_rules(db_session, test_user.account_id, NOW)) == 1


def test_replay_only_account_is_not_evaluated(db_session, test_user):
    """The account list reads the same replay-excluded spend as the rules."""
    _spend(db_session, test_user, YESTERDAY, 40.0, purpose="replay_validation")
    db_session.flush()

    assert (
        crud_spend_outlier_finding.list_account_ids_with_gateway_spend(
            db_session, day=YESTERDAY
        )
        == []
    )

    _spend(db_session, test_user, YESTERDAY, 1.0)
    db_session.flush()
    assert crud_spend_outlier_finding.list_account_ids_with_gateway_spend(
        db_session, day=YESTERDAY
    ) == [test_user.account_id]


def test_settings_upsert_is_one_statement_that_tolerates_a_raced_insert(
    db_session, test_user
):
    """A row another writer inserted first is updated, not a unique error.

    The row is written with a plain INSERT the ORM never saw, which is what a
    concurrent first save looks like from this session.
    """
    db_session.execute(
        models.SpendOutlierSettings.__table__.insert().values(
            id=uuid.uuid4(),
            account_id=test_user.account_id,
            daily_multiple=5.0,
            min_history_days=7,
            top_tier_model_prefixes=["premium-"],
            top_tier_share=0.5,
        )
    )

    row = crud_spend_outlier_settings.upsert(
        db_session,
        account_id=test_user.account_id,
        values={"session_cost_threshold_usd": 25.0},
        commit=False,
    )

    assert row.session_cost_threshold_usd == 25.0
    assert row.daily_multiple == 5.0
    assert row.top_tier_model_prefixes == ["premium-"]
    assert (
        db_session.query(models.SpendOutlierSettings)
        .filter_by(account_id=test_user.account_id)
        .count()
        == 1
    )


def test_replay_validation_spend_is_not_counted(db_session, test_user):
    """Preloop's own replay traffic is not the developer's spend."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0, purpose="replay_validation")
    _spend(db_session, test_user, YESTERDAY, 5.0)

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_daily_rule_is_per_user(db_session, test_user):
    """A heavy colleague does not make a steady user an outlier."""
    other = _second_user(db_session, test_user.account_id)
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 11.0)
    _history(db_session, other, range(1, 11), 1.0)
    _spend(db_session, other, YESTERDAY, 50.0)

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert [finding.user_id for finding in found] == [other.id]


def test_dismissal_hides_until_next_day_brings_new_fingerprint(
    client, db_session, test_user
):
    """Dismissed today, back tomorrow with a new fingerprint if still high."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    (finding,) = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    response = client.put(
        f"{DISMISSALS}/{finding.item_id}",
        json={"fingerprint": finding.fingerprint, "reason": "expected"},
    )
    assert response.status_code == 200
    db_session.refresh(finding)
    assert finding.dismissed_at is not None

    # Same day: nothing new, and the open card still carries the dismissed
    # fingerprint, so the console keeps it hidden.
    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []
    (open_item,) = list_open_findings(db_session, test_user.account_id, NOW)
    assert open_item["fingerprint"] == finding.fingerprint

    # Next UTC day, still three times the median: a new finding, same item id,
    # new fingerprint, which a dismissal of yesterday does not hide.
    _spend(db_session, test_user, YESTERDAY + timedelta(days=1), 45.0)
    tomorrow = NOW + timedelta(days=1)
    (again,) = evaluate_daily_rules(db_session, test_user.account_id, tomorrow)
    assert again.item_id == finding.item_id
    assert again.fingerprint == f"daily_spend|{test_user.id}|2026-09-27"
    (open_item,) = list_open_findings(db_session, test_user.account_id, tomorrow)
    assert open_item["fingerprint"] == again.fingerprint


def test_restore_clears_the_recorded_dismissal(client, db_session, test_user):
    """Restoring a card un-marks the finding the digest reads."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    (finding,) = evaluate_daily_rules(db_session, test_user.account_id, NOW)
    client.put(
        f"{DISMISSALS}/{finding.item_id}",
        json={"fingerprint": finding.fingerprint, "reason": "fixed"},
    )

    response = client.delete(f"{DISMISSALS}/{finding.item_id}")

    assert response.status_code == 204
    db_session.refresh(finding)
    assert finding.dismissed_at is None


def test_non_spend_dismissal_does_not_touch_findings(client, db_session, test_user):
    """Dismissing another kind leaves spend findings alone."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    (finding,) = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    client.put(
        f"{DISMISSALS}/agent:agent-1",
        json={"fingerprint": finding.fingerprint, "reason": "expected"},
    )

    db_session.refresh(finding)
    assert finding.dismissed_at is None


# --- rule 2: model mix ------------------------------------------------------


def _mark_top_tier(db, account_id, prefixes=("premium-",), share=0.5):
    crud_spend_outlier_settings.upsert(
        db,
        account_id=account_id,
        values={"top_tier_model_prefixes": list(prefixes), "top_tier_share": share},
        commit=False,
    )


def test_model_mix_two_days_at_eighty_percent_fires_once(db_session, test_user):
    """80 percent on a top-tier model two UTC days running: one card."""
    _mark_top_tier(db_session, test_user.account_id)
    for day in (YESTERDAY - timedelta(days=1), YESTERDAY):
        _spend(db_session, test_user, day, 8.0, model="vendor/premium-large")
        _spend(db_session, test_user, day, 2.0, model="standard-model")

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)
    again = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert [finding.rule for finding in found] == ["model_mix"]
    assert again == []
    details = found[0].details
    assert details["model"] == "vendor/premium-large"
    assert details["share"] == 0.8
    assert details["previous_share"] == 0.8
    assert found[0].fingerprint == f"model_mix|{test_user.id}|2026-09-26"


def test_model_family_drops_provider_and_case():
    """One model reached with and without a provider is one family."""
    assert model_family("Vendor/Premium-Large") == "premium-large"
    assert model_family("premium-large") == "premium-large"
    assert model_family("a/b/premium-large") == "premium-large"


def test_model_mix_matches_spellings_across_the_two_days(db_session, test_user):
    """Bare on one day, provider-prefixed on the other: still two days."""
    _mark_top_tier(db_session, test_user.account_id)
    _spend(db_session, test_user, YESTERDAY - timedelta(days=1), 8.0, model="premium-x")
    _spend(db_session, test_user, YESTERDAY - timedelta(days=1), 2.0)
    _spend(db_session, test_user, YESTERDAY, 8.0, model="vendor/premium-x")
    _spend(db_session, test_user, YESTERDAY, 2.0)

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert [finding.rule for finding in found] == ["model_mix"]
    assert found[0].details["model"] == "vendor/premium-x"
    assert found[0].details["previous_share"] == 0.8


def test_model_mix_adds_up_spellings_on_the_same_day(db_session, test_user):
    """40 percent under each of two spellings is 80 percent of one model."""
    _mark_top_tier(db_session, test_user.account_id)
    for day in (YESTERDAY - timedelta(days=1), YESTERDAY):
        _spend(db_session, test_user, day, 4.0, model="premium-x")
        _spend(db_session, test_user, day, 4.0, model="vendor/premium-x")
        _spend(db_session, test_user, day, 2.0)

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert [finding.rule for finding in found] == ["model_mix"]
    assert found[0].details["share"] == 0.8
    assert found[0].details["model_usd"] == 8.0


def test_model_mix_one_day_does_not_fire(db_session, test_user):
    """One expensive day is not a pattern."""
    _mark_top_tier(db_session, test_user.account_id)
    _spend(db_session, test_user, YESTERDAY - timedelta(days=1), 2.0, model="premium-x")
    _spend(db_session, test_user, YESTERDAY - timedelta(days=1), 8.0)
    _spend(db_session, test_user, YESTERDAY, 8.0, model="premium-x")
    _spend(db_session, test_user, YESTERDAY, 2.0)

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_model_mix_off_without_top_tier_prefixes(db_session, test_user):
    """No operator list, no model mix rule: there is no vendor default."""
    for day in (YESTERDAY - timedelta(days=1), YESTERDAY):
        _spend(db_session, test_user, day, 10.0, model="premium-large")

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_model_mix_day_boundary_is_utc(db_session, test_user):
    """Spend at 23:00 and 01:00 UTC lands on two different days."""
    _mark_top_tier(db_session, test_user.account_id)
    _spend(
        db_session,
        test_user,
        YESTERDAY - timedelta(days=1),
        9.0,
        model="premium-x",
        hour=23,
    )
    _spend(db_session, test_user, YESTERDAY, 9.0, model="premium-x", hour=1)
    _spend(db_session, test_user, YESTERDAY, 1.0, hour=2)

    found = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert [finding.rule for finding in found] == ["model_mix"]
    assert found[0].details["previous_share"] == 1.0
    assert found[0].details["share"] == 0.9


# --- rule 3: session cost ---------------------------------------------------


def test_session_rule_is_off_until_threshold_set(db_session, test_user):
    """Unset threshold: no session cards however large the session."""
    session = _session(db_session, test_user.account_id)
    _spend(db_session, test_user, YESTERDAY, 500.0, session_id=session.id)

    assert evaluate_session_rule(db_session, test_user.account_id, NOW) == []
    assert (
        evaluate_session_rule(
            db_session, test_user.account_id, NOW, active_within=timedelta(days=2)
        )
        == []
    )


def test_session_over_threshold_fires_once(db_session, test_user):
    """A session over the threshold produces one card, a rerun none."""
    crud_spend_outlier_settings.upsert(
        db_session,
        account_id=test_user.account_id,
        values={"session_cost_threshold_usd": 5.0},
        commit=False,
    )
    expensive = _session(db_session, test_user.account_id)
    cheap = _session(db_session, test_user.account_id)
    _spend(db_session, test_user, YESTERDAY, 3.0, session_id=expensive.id, hour=22)
    _spend(db_session, test_user, YESTERDAY, 4.0, session_id=expensive.id, hour=23)
    _spend(db_session, test_user, YESTERDAY, 4.0, session_id=cheap.id, hour=23)
    window = timedelta(hours=3)

    found = evaluate_session_rule(
        db_session, test_user.account_id, NOW, active_within=window
    )
    again = evaluate_session_rule(
        db_session, test_user.account_id, NOW, active_within=window
    )

    assert len(found) == 1
    assert again == []
    finding = found[0]
    assert finding.runtime_session_id == expensive.id
    assert finding.user_id == test_user.id
    assert finding.item_id == f"spend:session_cost:{expensive.id}"
    assert finding.fingerprint == f"session_cost|{test_user.id}|{expensive.id}"
    assert finding.details["spend_usd"] == 7.0
    (open_item,) = list_open_findings(db_session, test_user.account_id, NOW)
    assert open_item["session_title"] == "Refactor the parser"


def test_session_check_only_reads_recently_active_sessions(db_session, test_user):
    """The periodic check does not rescan sessions idle for days."""
    crud_spend_outlier_settings.upsert(
        db_session,
        account_id=test_user.account_id,
        values={"session_cost_threshold_usd": 5.0},
        commit=False,
    )
    old = _session(db_session, test_user.account_id)
    _spend(
        db_session, test_user, YESTERDAY - timedelta(days=3), 50.0, session_id=old.id
    )

    assert evaluate_session_rule(db_session, test_user.account_id, NOW) == []


# --- imported spend ---------------------------------------------------------


@pytest.fixture
def copilot_source():
    rows: list[ImportedSpendRow] = []

    def source(db, account_id, start_day, end_day):
        return [row for row in rows if start_day <= row.day <= end_day]

    register_imported_spend_source(source)
    try:
        yield rows
    finally:
        unregister_imported_spend_source(source)


def test_imported_spend_counts_and_is_labelled(db_session, test_user, copilot_source):
    """Imported dollars push a day over, and the card says they are not metered."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 20.0)
    copilot_source.append(
        ImportedSpendRow(
            user_id=test_user.id,
            day=YESTERDAY,
            model="premium-x",
            cost_usd=15.0,
            source="copilot",
        )
    )

    (finding,) = evaluate_daily_rules(db_session, test_user.account_id, NOW)

    assert finding.details["spend_usd"] == 35.0
    assert finding.details["gateway_usd"] == 20.0
    assert finding.details["imported_usd"] == 15.0
    assert finding.details["imported_sources"] == ["copilot"]
    (open_item,) = list_open_findings(db_session, test_user.account_id, NOW)
    assert "$15.00 of imported copilot spend" in open_item["summary"]
    assert "not metered by the gateway" in open_item["summary"]


def test_without_imported_rows_gateway_spend_alone_decides(db_session, test_user):
    """No registered source: the same gateway spend stays under 3x."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 20.0)

    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []


def test_broken_imported_source_does_not_block_gateway_alerts(db_session, test_user):
    """A failing import is logged and skipped."""

    def broken(db, account_id, start_day, end_day):
        raise RuntimeError("import unavailable")

    register_imported_spend_source(broken)
    try:
        _history(db_session, test_user, range(1, 11), 10.0)
        _spend(db_session, test_user, YESTERDAY, 40.0)
        found = evaluate_daily_rules(db_session, test_user.account_id, NOW)
    finally:
        unregister_imported_spend_source(broken)

    assert len(found) == 1
    assert found[0].details["imported_usd"] == 0.0


# --- daily pass and digest --------------------------------------------------


def test_daily_pass_evaluates_accounts_with_spend_yesterday(db_session, test_user):
    """The scheduled pass finds the account from yesterday's usage."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)

    result = run_daily_pass(db_session, NOW)

    assert result["findings"] >= 1
    assert crud_spend_outlier_finding.list_detected_since(
        db_session, account_id=test_user.account_id, since=NOW - timedelta(hours=1)
    )


def test_digest_lists_each_finding_once_with_dismissed_marked(
    client, db_session, test_user
):
    """A fake digest service reads the section: one entry, dismissed marked."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    evaluate_daily_rules(db_session, test_user.account_id, NOW)
    evaluate_daily_rules(db_session, test_user.account_id, NOW)
    (finding,) = crud_spend_outlier_finding.list_detected_since(
        db_session, account_id=test_user.account_id, since=NOW - timedelta(days=1)
    )
    client.put(
        f"{DISMISSALS}/{finding.item_id}",
        json={"fingerprint": finding.fingerprint, "reason": "expected"},
    )

    captured: dict = {}

    def fake_digest_service(db, account_id=None):
        captured["section"] = build_spend_outlier_digest_section(
            db, test_user.account_id, now=NOW + timedelta(days=2)
        )
        return captured["section"]

    from preloop.sync import tasks

    manager = MagicMock()
    manager.get_service.return_value = fake_digest_service
    session_factory = MagicMock(return_value=iter([db_session]))
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("preloop.plugins.base.get_plugin_manager", lambda: manager)
        patcher.setattr(tasks, "get_db_session", session_factory)
        patcher.setattr(db_session, "close", lambda: None)
        tasks.send_optimization_digest(account_id=str(test_user.account_id))

    section = captured["section"]
    assert section["title"] == "Spend outliers"
    assert len(section["items"]) == 1
    item = section["items"][0]
    assert item["fingerprint"] == finding.fingerprint
    assert item["rule"] == "daily_spend"
    assert item["user_name"] == "Test User"
    assert item["details"]["multiple"] == 4.0
    assert item["dismissed"] is True


def test_digest_marks_findings_hidden_by_a_snooze(client, db_session, test_user):
    """A snooze on yesterday's card also covers the next day's finding."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    (first,) = evaluate_daily_rules(db_session, test_user.account_id, NOW)
    client.put(
        f"{DISMISSALS}/{first.item_id}",
        json={"fingerprint": first.fingerprint, "reason": "snoozed", "snooze_days": 7},
    )
    _spend(db_session, test_user, YESTERDAY + timedelta(days=1), 45.0)
    (second,) = evaluate_daily_rules(
        db_session, test_user.account_id, NOW + timedelta(days=1)
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=NOW - timedelta(days=1),
        end=NOW + timedelta(days=2),
    )

    by_fingerprint = {item["fingerprint"]: item for item in section["items"]}
    assert by_fingerprint[first.fingerprint]["dismissed"] is True
    assert by_fingerprint[second.fingerprint]["dismissed"] is True


def test_digest_is_empty_and_account_scoped(db_session, test_user):
    """Another account's findings never show up in this digest."""
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    other = _second_user(db_session, other_account.id, email="john@example.com")
    _history(db_session, other, range(1, 11), 10.0)
    _spend(db_session, other, YESTERDAY, 40.0)
    evaluate_daily_rules(db_session, other_account.id, NOW)

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=NOW
    )

    assert section["items"] == []


def test_open_findings_expire_after_the_open_window(db_session, test_user):
    """A card leaves the page after OPEN_WINDOW_DAYS without a new finding."""
    _history(db_session, test_user, range(1, 11), 10.0)
    _spend(db_session, test_user, YESTERDAY, 40.0)
    evaluate_daily_rules(db_session, test_user.account_id, NOW)

    later = NOW + timedelta(days=spend_outliers.OPEN_WINDOW_DAYS, minutes=1)

    assert list_open_findings(db_session, test_user.account_id, later) == []


# --- one account, one window ------------------------------------------------

#: Frozen end of the window the digest boundary tests report on.
WINDOW_END = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def _finding(
    db,
    user,
    *,
    detected_at,
    day,
    fingerprint,
    details=None,
    rule="daily_spend",
    runtime_session_id=None,
    user_id=None,
    account_id=None,
):
    """One finding stored at an exact instant, without running a rule."""
    return crud_spend_outlier_finding.record(
        db,
        account_id=account_id or user.account_id,
        rule=rule,
        user_id=user.id if user_id is None else user_id,
        runtime_session_id=runtime_session_id,
        day=day,
        item_id=finding_item_id(rule, user.id, runtime_session_id),
        fingerprint=fingerprint,
        details=details or {"spend_usd": 40.0, "median_usd": 10.0, "multiple": 4.0},
        detected_at=detected_at,
        commit=False,
    )


@contextmanager
def _captured_sql(db_engine) -> Iterator[list]:
    """Collect the statements a builder sends while it is inside the block."""
    statements = []

    def capture(connection, cursor, statement, *args):
        statements.append(statement)

    event.listen(db_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(db_engine, "before_cursor_execute", capture)


def _other_account(db):
    return crud_account.create(
        db, obj_in={"organization_name": "Other Org", "is_active": True}
    )


def test_digest_window_is_half_open_at_both_ends(db_session, test_user):
    """Only findings detected inside [start, end) are listed."""
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(days=7, seconds=1),
        day=date(2026, 9, 20),
        fingerprint="daily_spend|before-start|2026-09-20",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(days=7),
        day=date(2026, 9, 21),
        fingerprint="daily_spend|at-start|2026-09-21",
        details={"spend_usd": 41.0, "median_usd": 10.0, "multiple": 4.1},
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(minutes=1),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|before-end|2026-09-26",
        details={"spend_usd": 42.0, "median_usd": 10.0, "multiple": 4.2},
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END,
        day=date(2026, 9, 27),
        fingerprint="daily_spend|at-end|2026-09-27",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END + timedelta(days=1),
        day=date(2026, 9, 28),
        fingerprint="daily_spend|after-end|2026-09-28",
    )

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    assert section["window_start"] == (WINDOW_END - timedelta(days=7)).isoformat()
    assert section["window_end"] == WINDOW_END.isoformat()
    assert [item["fingerprint"] for item in section["items"]] == [
        "daily_spend|at-start|2026-09-21",
        "daily_spend|before-end|2026-09-26",
    ]
    # Every finding's own numbers, never a sum across accounts or rules.
    assert [item["details"]["multiple"] for item in section["items"]] == [4.1, 4.2]
    assert set(section) == {"title", "window_start", "window_end", "items"}


def test_digest_section_follows_an_explicit_two_day_window(db_session, test_user):
    start = WINDOW_END - timedelta(days=2)
    _finding(
        db_session,
        test_user,
        detected_at=start - timedelta(seconds=1),
        day=date(2026, 9, 25),
        fingerprint="daily_spend|before-start|2026-09-25",
    )
    _finding(
        db_session,
        test_user,
        detected_at=start,
        day=date(2026, 9, 26),
        fingerprint="daily_spend|at-start|2026-09-26",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(minutes=1),
        day=date(2026, 9, 27),
        fingerprint="daily_spend|before-end|2026-09-27",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END,
        day=date(2026, 9, 28),
        fingerprint="daily_spend|at-end|2026-09-28",
    )

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, start=start, end=WINDOW_END
    )

    assert section["window_start"] == start.isoformat()
    assert section["window_end"] == WINDOW_END.isoformat()
    assert [item["fingerprint"] for item in section["items"]] == [
        "daily_spend|at-start|2026-09-26",
        "daily_spend|before-end|2026-09-27",
    ]


def test_the_same_window_in_another_offset_reads_the_same(db_session, test_user):
    """A window written in local time covers the same findings as one in UTC."""
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|inside|2026-09-26",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END + timedelta(hours=1),
        day=date(2026, 9, 27),
        fingerprint="daily_spend|future|2026-09-27",
    )
    start = WINDOW_END - timedelta(days=1)
    offset = timezone(timedelta(hours=-5))

    in_utc = build_spend_outlier_digest_section(
        db_session, test_user.account_id, start=start, end=WINDOW_END
    )
    shifted = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=start.astimezone(offset),
        end=WINDOW_END.astimezone(offset),
    )
    naive = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=start.replace(tzinfo=None),
        end=WINDOW_END.replace(tzinfo=None),
    )

    assert shifted["items"] == in_utc["items"]
    assert shifted["window_start"] == in_utc["window_start"]
    assert naive["window_end"] == in_utc["window_end"]
    assert [item["fingerprint"] for item in naive["items"]] == [
        "daily_spend|inside|2026-09-26"
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"start": WINDOW_END - timedelta(days=1)},
            "start and end must be given together",
        ),
        ({"end": WINDOW_END}, "start and end must be given together"),
        (
            {"start": WINDOW_END, "end": WINDOW_END - timedelta(days=1)},
            "end must be after start",
        ),
        ({"start": WINDOW_END, "end": WINDOW_END}, "end must be after start"),
        (
            {
                "start": WINDOW_END - timedelta(days=1),
                "end": WINDOW_END,
                "now": WINDOW_END,
            },
            "now cannot be combined",
        ),
    ],
)
def test_a_window_that_cannot_be_reported_is_refused(
    db_session, test_user, kwargs, message
):
    with pytest.raises(ValueError, match=message):
        build_spend_outlier_digest_section(db_session, test_user.account_id, **kwargs)


def test_a_refused_window_is_refused_before_any_query(db_engine, db_session, test_user):
    """Nothing is read for a window that was never going to be rendered."""
    with _captured_sql(db_engine) as statements:
        with pytest.raises(ValueError, match="end must be after start"):
            build_spend_outlier_digest_section(
                db_session,
                test_user.account_id,
                start=WINDOW_END,
                end=WINDOW_END - timedelta(days=1),
            )

    assert statements == []


def test_two_accounts_sharing_a_fingerprint_and_display_name(db_session, test_user):
    """The same fingerprint and the same name in two accounts stay apart."""
    other_account = _other_account(db_session)
    other_user = _second_user(db_session, other_account.id, email="jane@example.com")
    # A developer of this account displayed under the other account's name.
    twin = _second_user(db_session, test_user.account_id, email="twin@example.com")
    assert twin.full_name == other_user.full_name

    _finding(
        db_session,
        twin,
        detected_at=WINDOW_END - timedelta(hours=3),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|shared-fingerprint|2026-09-26",
        details={"spend_usd": 40.0, "median_usd": 10.0, "multiple": 4.0},
    )
    _finding(
        db_session,
        other_user,
        detected_at=WINDOW_END - timedelta(hours=3),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|shared-fingerprint|2026-09-26",
        details={"spend_usd": 99.0, "median_usd": 10.0, "multiple": 9.9},
    )

    mine = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )
    theirs = build_spend_outlier_digest_section(
        db_session, other_account.id, now=WINDOW_END
    )

    (item,) = mine["items"]
    assert item["user_id"] == str(twin.id)
    assert item["user_name"] == "Jane Doe"
    assert item["details"]["spend_usd"] == 40.0
    (their_item,) = theirs["items"]
    assert their_item["user_id"] == str(other_user.id)
    assert their_item["details"]["spend_usd"] == 99.0


def test_a_finding_naming_another_accounts_user_names_nobody(db_session, test_user):
    """A user id of another account resolves to no name, not to their name."""
    other_account = _other_account(db_session)
    foreign = _second_user(db_session, other_account.id, email="john@example.com")

    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|foreign-user|2026-09-26",
        user_id=foreign.id,
    )

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    (item,) = section["items"]
    assert "Jane Doe" not in item["user_name"]
    assert "Jane Doe" not in item["summary"]
    assert item["session_title"] is None


def test_a_finding_naming_another_accounts_session_titles_nobody(db_session, test_user):
    """A session of another account resolves to no title, not to its title."""
    other_account = _other_account(db_session)
    foreign_session = _session(db_session, other_account.id)

    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 26),
        fingerprint=f"session_cost|{test_user.id}|{foreign_session.id}",
        rule="session_cost",
        runtime_session_id=foreign_session.id,
        details={"spend_usd": 25.0, "threshold_usd": 20.0},
    )

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    (item,) = section["items"]
    assert item["session_title"] is None
    assert "Refactor the parser" not in str(section)


def test_one_entry_per_fingerprint_and_one_per_day(db_session, test_user):
    """The same fingerprint fires once; a later day is its own finding."""
    first = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(days=2),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    repeat = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(days=1),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    second = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 27),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-27",
    )

    assert repeat is None
    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    assert [item["fingerprint"] for item in section["items"]] == [
        first.fingerprint,
        second.fingerprint,
    ]
    assert [item["day"] for item in section["items"]] == ["2026-09-26", "2026-09-27"]


def test_digest_keeps_the_recorded_numbers_and_the_day(db_session, test_user):
    """The numbers a rule recorded, and its spend day, survive the window."""
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|imported|2026-09-26",
        details={
            "spend_usd": 40.0,
            "median_usd": 10.0,
            "multiple": 4.0,
            "gateway_usd": 25.0,
            "imported_usd": 15.0,
            "imported_sources": ["copilot"],
        },
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    (item,) = section["items"]
    assert item["detected_at"] == (WINDOW_END - timedelta(hours=1)).isoformat()
    assert item["details"]["gateway_usd"] == 25.0
    assert item["summary"].startswith("Test User spent $40.00 on 2026-09-26")
    assert "not metered by the gateway" in item["summary"]


def test_a_dismissed_finding_inside_the_window_is_marked(db_session, test_user):
    """Dismissal still reads the way it did, inside an explicit window."""
    finding = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=2),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    crud_spend_outlier_finding.set_dismissed(
        db_session,
        account_id=test_user.account_id,
        item_id=finding.item_id,
        fingerprint=finding.fingerprint,
        dismissed_at=WINDOW_END - timedelta(hours=1),
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    (item,) = section["items"]
    assert item["fingerprint"] == finding.fingerprint
    assert item["dismissed"] is True


def test_a_dismissal_after_the_window_end_still_marks_the_finding(
    db_session, test_user
):
    """A dismissal is read as it stands; only snoozes are window-end.

    The finding was still open when the window closed and the operator
    dismissed it afterwards, so it is reported as dismissed: ``dismissed_at``
    is read from the finding, not from the window. Pinned so that narrowing
    ``dismissed`` to a window-end snapshot is a deliberate change.
    """
    finding = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=2),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    crud_spend_outlier_finding.set_dismissed(
        db_session,
        account_id=test_user.account_id,
        item_id=finding.item_id,
        fingerprint=finding.fingerprint,
        dismissed_at=WINDOW_END + timedelta(hours=1),
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    (item,) = section["items"]
    assert item["dismissed"] is True


def test_a_snooze_still_in_force_at_the_window_end_hides_the_finding(
    db_session, test_user
):
    """The other half of the same rule: a snooze is resolved at the window end.

    The snooze ran out an hour after the window closed, long before this
    section was generated. It still covers the window, so the finding is
    reported as dismissed.
    """
    finding = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=2),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    crud_attention_dismissal.upsert(
        db_session,
        account_id=test_user.account_id,
        item_id=finding.item_id,
        fingerprint=finding.fingerprint,
        reason="snoozed",
        snooze_until=WINDOW_END + timedelta(hours=1),
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    (item,) = section["items"]
    assert item["dismissed"] is True


def test_a_snooze_that_ran_out_before_the_window_end_is_not_active(
    db_session, test_user
):
    """A snooze that had already run out when the window closed hides nothing."""
    finding = _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=2),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    crud_attention_dismissal.upsert(
        db_session,
        account_id=test_user.account_id,
        item_id=finding.item_id,
        fingerprint=finding.fingerprint,
        reason="snoozed",
        snooze_until=WINDOW_END - timedelta(hours=3),
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    (item,) = section["items"]
    assert item["dismissed"] is False


def test_items_keep_a_stable_order(db_session, test_user):
    """Two findings detected at once keep the same order on every call."""
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 26),
        fingerprint=f"daily_spend|{test_user.id}|2026-09-26",
    )
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(hours=1),
        day=date(2026, 9, 27),
        fingerprint=f"model_mix|{test_user.id}|2026-09-27",
        rule="model_mix",
        details={"model": "premium-large", "share": 0.8, "previous_share": 0.7},
    )

    section = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )
    again = build_spend_outlier_digest_section(
        db_session, test_user.account_id, now=WINDOW_END
    )

    assert [item["rule"] for item in section["items"]] == ["daily_spend", "model_mix"]
    assert again["items"] == section["items"]


def test_an_empty_window_is_reported_as_empty(db_session, test_user):
    """A window with no finding in it is empty, not a stale listing."""
    _finding(
        db_session,
        test_user,
        detected_at=WINDOW_END - timedelta(days=2),
        day=date(2026, 9, 26),
        fingerprint="daily_spend|before|2026-09-26",
    )

    section = build_spend_outlier_digest_section(
        db_session,
        test_user.account_id,
        start=WINDOW_END - timedelta(days=1),
        end=WINDOW_END,
    )

    assert section["items"] == []
