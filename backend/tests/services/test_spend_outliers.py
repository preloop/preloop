"""Spend outlier alerts (#960): the three rules, fire-once, dismissal, digest.

Rows are written straight into ``api_usage`` with explicit timestamps so each
test controls which UTC day the spend lands on.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Iterable, Optional
from unittest.mock import MagicMock
import uuid

import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_account,
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
        db_session, test_user.account_id, now=datetime.now(UTC)
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
