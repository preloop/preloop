"""Delayed-day replay and reconciliation of spend outlier findings (#1061).

The real Copilot adapter is registered, imported rows are written through the
Copilot CRUD as the import stores them, and the daily pass is run at a later
"now" so the day under test is well in the past when it is first judged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Optional
import uuid

import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_copilot_import_connection,
    crud_copilot_user_mapping,
    crud_copilot_usage,
    crud_spend_outlier_finding,
    crud_spend_outlier_settings,
)
from preloop.models.crud.copilot_import import LINE_ITEM_PREMIUM_REQUEST
from preloop.services import copilot_spend_source as src
from preloop.services import spend_outliers
from preloop.services.copilot_usage_import import day_start
from preloop.services.spend_outliers import (
    ImportedSpendRow,
    build_spend_outlier_digest_section,
    evaluate_daily_rules,
    list_open_findings,
    register_imported_spend_source,
    run_daily_pass,
    unregister_imported_spend_source,
)
from tests.services.test_copilot_spend_source import (
    connect,
    make_user,
    premium_row,
)

#: The spend day under test and the moment the pass runs: TARGET + 3 days,
#: which is the first pass at which the import can have stored TARGET.
TARGET = date(2026, 9, 24)
NOW = datetime(2026, 9, 27, 0, 30, tzinfo=UTC)
YESTERDAY = NOW.date() - timedelta(days=1)
DISMISSALS = "/api/v1/attention/dismissals"


def _settings(db, account_id, **values) -> None:
    crud_spend_outlier_settings.upsert(
        db, account_id=account_id, values=values, commit=False
    )


def _gateway(db, user, day: date, cost: float, *, model="standard-model") -> None:
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
            timestamp=datetime(day.year, day.month, day.day, 12, 0),
        )
    )
    db.flush()


def _import_day(
    db, account_id, day: date, login: str, amount: Optional[float], *, model="model-a"
) -> None:
    """Store one day's premium-request rows the way a (re)import does."""
    crud_copilot_usage.replace_day_rows(
        db,
        account_id=account_id,
        bucket_start=day_start(day),
        line_items=(LINE_ITEM_PREMIUM_REQUEST,),
        rows=[premium_row(day, login=login, amount=amount, model=model)],
    )


def _all_findings(db, account_id):
    return (
        db.query(models.SpendOutlierFinding)
        .filter(models.SpendOutlierFinding.account_id == account_id)
        .order_by(models.SpendOutlierFinding.day, models.SpendOutlierFinding.rule)
        .all()
    )


@pytest.fixture
def copilot(db_session, test_user):
    """An active connection, alice mapped to the test user, adapter registered."""
    connection = connect(db_session, test_user.account_id)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    src.register_copilot_spend_source()
    try:
        yield connection
    finally:
        unregister_imported_spend_source(src.copilot_imported_spend)


def _seed_spike(db, account_id, *, login="alice", history=(2.0, 2.0, 2.0), target=8.0):
    """Three historical days at $2 and the target day at $8, all imported."""
    _settings(db, account_id, min_history_days=3, daily_multiple=3.0)
    for offset, amount in zip(range(len(history), 0, -1), history, strict=True):
        _import_day(db, account_id, TARGET - timedelta(days=offset), login, amount)
    _import_day(db, account_id, TARGET, login, target)


# --- pure helpers -----------------------------------------------------------


def test_replay_window_is_the_28_completed_days_ending_yesterday():
    days = spend_outliers.replay_days(YESTERDAY)

    assert len(days) == spend_outliers.REPLAY_WINDOW_DAYS == 28
    assert days[0] == YESTERDAY - timedelta(days=27)
    assert days[-1] == YESTERDAY
    assert days == sorted(set(days))


# --- acceptance 1: a day imported three days late ---------------------------


def test_delayed_imported_day_is_evaluated_on_the_next_pass(
    db_session, test_user, copilot
):
    """Imported-only spike on TARGET, judged at TARGET + 3: one finding, dated TARGET."""
    _seed_spike(db_session, test_user.account_id)

    # Yesterday-only evaluation never sees TARGET.
    assert evaluate_daily_rules(db_session, test_user.account_id, NOW) == []

    result = run_daily_pass(db_session, NOW)

    assert result["replayed"] == 1
    assert result["findings"] == 1
    (finding,) = _all_findings(db_session, test_user.account_id)
    assert finding.rule == "daily_spend"
    assert finding.user_id == test_user.id
    assert finding.day == TARGET
    assert finding.detected_at == NOW
    assert finding.details["spend_usd"] == 8.0
    assert finding.details["gateway_usd"] == 0.0
    assert finding.details["imported_usd"] == 8.0
    assert finding.details["imported_sources"] == ["copilot"]
    assert finding.details["median_usd"] == 2.0
    assert finding.fingerprint == f"daily_spend|{test_user.id}|2026-09-24"
    (card,) = list_open_findings(db_session, test_user.account_id, NOW)
    assert card["day"] == "2026-09-24"
    assert "$8.00 of imported copilot spend" in card["summary"]


def test_explicit_day_is_judged_with_detection_time_now(db_session, test_user, copilot):
    _seed_spike(db_session, test_user.account_id)
    later = NOW + timedelta(days=4)

    (finding,) = evaluate_daily_rules(
        db_session, test_user.account_id, later, day=TARGET
    )

    assert finding.day == TARGET
    assert finding.detected_at == later


# --- acceptance 2: model mix on imported days, no imported session rule -----


def test_imported_model_mix_fires_once_for_the_second_day(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _settings(
        db_session, account_id, top_tier_model_prefixes=["premium-"], top_tier_share=0.7
    )
    for day in (TARGET - timedelta(days=1), TARGET):
        crud_copilot_usage.replace_day_rows(
            db_session,
            account_id=account_id,
            bucket_start=day_start(day),
            line_items=(LINE_ITEM_PREMIUM_REQUEST,),
            rows=[
                premium_row(day, login="alice", amount=8.0, model="premium-large"),
                premium_row(day, login="alice", amount=2.0, model="model-a"),
            ],
        )

    run_daily_pass(db_session, NOW)

    findings = _all_findings(db_session, account_id)
    assert [(f.rule, f.day) for f in findings] == [("model_mix", TARGET)]
    assert findings[0].details["share"] == 0.8
    assert findings[0].details["previous_share"] == 0.8
    assert findings[0].details["imported_usd"] == 10.0
    assert findings[0].details["gateway_usd"] == 0.0
    assert not any(f.rule == "session_cost" for f in findings)


def test_one_day_insufficient_history_and_known_zero_do_not_fire(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _settings(
        db_session,
        account_id,
        top_tier_model_prefixes=["premium-"],
        top_tier_share=0.7,
        min_history_days=3,
    )
    # One top-tier day only.
    crud_copilot_usage.replace_day_rows(
        db_session,
        account_id=account_id,
        bucket_start=day_start(TARGET),
        line_items=(LINE_ITEM_PREMIUM_REQUEST,),
        rows=[
            premium_row(TARGET, login="alice", amount=8.0, model="premium-large"),
            premium_row(TARGET, login="alice", amount=2.0, model="model-a"),
        ],
    )
    # Two history days, three needed.
    _import_day(db_session, account_id, TARGET - timedelta(days=2), "alice", 1.0)
    _import_day(db_session, account_id, TARGET - timedelta(days=1), "alice", 1.0)
    # A known zero day.
    _import_day(db_session, account_id, TARGET + timedelta(days=1), "alice", 0.0)

    run_daily_pass(db_session, NOW)

    assert _all_findings(db_session, account_id) == []


# --- acceptance 3: gateway and imported split -------------------------------


def test_gateway_and_imported_spend_combine_with_the_split_preserved(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    _gateway(db_session, test_user, TARGET, 3.0)

    run_daily_pass(db_session, NOW)

    (finding,) = _all_findings(db_session, account_id)
    assert finding.details["spend_usd"] == 11.0
    assert finding.details["gateway_usd"] == 3.0
    assert finding.details["imported_usd"] == 8.0
    # Gateway reporting is untouched: api_usage still says $3.
    gateway = crud_spend_outlier_finding.gateway_spend_by_user_model_day(
        db_session, account_id=account_id, start_day=TARGET, end_day=TARGET
    )
    assert [row.cost_usd for row in gateway] == [3.0]
    assert (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.account_id == account_id)
        .count()
        == 1
    )


# --- acceptance 5: one fingerprint, corrections, mapping changes ------------


def test_replay_and_reimport_keep_one_fingerprint_and_first_detection(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (first,) = _all_findings(db_session, account_id)
    first_detected = first.detected_at

    # The import runs again for the same day with the same numbers, and the
    # pass runs on the following days.
    _import_day(db_session, account_id, TARGET, "alice", 8.0)
    for days in (1, 2):
        result = run_daily_pass(db_session, NOW + timedelta(days=days))
        assert result["findings"] == 0
        assert result["updated"] == 0
        assert result["superseded"] == 0

    (same,) = _all_findings(db_session, account_id)
    assert same.id == first.id
    assert same.detected_at == first_detected
    assert same.details["imported_usd"] == 8.0
    assert same.superseded_at is None


def test_corrected_import_updates_evidence_and_keeps_detection_time(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (finding,) = _all_findings(db_session, account_id)

    _import_day(db_session, account_id, TARGET, "alice", 9.0)
    result = run_daily_pass(db_session, NOW + timedelta(days=1))

    assert result["updated"] == 1
    db_session.refresh(finding)
    assert finding.details["spend_usd"] == 9.0
    assert finding.details["imported_usd"] == 9.0
    assert finding.detected_at == NOW
    assert len(_all_findings(db_session, account_id)) == 1


def test_correction_below_threshold_supersedes_and_hides_the_finding(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (finding,) = _all_findings(db_session, account_id)

    _import_day(db_session, account_id, TARGET, "alice", 2.5)
    later = NOW + timedelta(days=1)
    result = run_daily_pass(db_session, later)

    assert result["superseded"] == 1
    db_session.refresh(finding)
    assert finding.superseded_at == later
    assert finding.superseded_reason == "no_longer_qualifies"
    assert finding.dismissed_at is None
    assert finding.detected_at == NOW
    # Hidden from the open list and the digest, kept as a row.
    assert list_open_findings(db_session, account_id, later) == []
    section = build_spend_outlier_digest_section(
        db_session,
        account_id,
        start=NOW - timedelta(days=1),
        end=later + timedelta(days=1),
    )
    assert section["items"] == []
    assert len(_all_findings(db_session, account_id)) == 1

    # A later correction back over the threshold reinstates it, same row.
    _import_day(db_session, account_id, TARGET, "alice", 8.0)
    result = run_daily_pass(db_session, later + timedelta(days=1))
    assert result["updated"] == 1
    db_session.refresh(finding)
    assert finding.superseded_at is None
    assert finding.detected_at == NOW
    assert len(_all_findings(db_session, account_id)) == 1


def test_mapping_removal_supersedes_and_reassignment_moves_attribution(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (original,) = _all_findings(db_session, account_id)
    assert original.user_id == test_user.id

    # The login turns out to be someone else.
    other = make_user(db_session, account_id, "jane@example.com")
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=copilot,
        github_login="alice",
        user_id=other.id,
        commit=False,
    )
    later = NOW + timedelta(days=1)
    result = run_daily_pass(db_session, later)

    assert result["superseded"] == 1
    assert result["findings"] == 1
    findings = {f.user_id: f for f in _all_findings(db_session, account_id)}
    assert findings[test_user.id].superseded_at == later
    assert findings[other.id].superseded_at is None
    assert findings[other.id].detected_at == later
    assert findings[other.id].details["imported_sources"] == ["copilot"]
    (card,) = list_open_findings(db_session, account_id, later)
    assert card["user_id"] == str(other.id)
    assert card["user_name"] == "Jane"

    # Removing the mapping altogether leaves no visible attribution.
    crud_copilot_user_mapping.delete_for_login(
        db_session, connection=copilot, github_login="alice", commit=False
    )
    result = run_daily_pass(db_session, later + timedelta(days=1))
    assert result["superseded"] == 1
    assert list_open_findings(db_session, account_id, later + timedelta(days=1)) == []
    assert len(_all_findings(db_session, account_id)) == 2


def test_unchanged_dismissed_and_snoozed_findings_stay_that_way(
    client, db_session, test_user, copilot
):
    account_id = test_user.account_id
    _settings(db_session, account_id, min_history_days=3, daily_multiple=3.0)
    other = make_user(db_session, account_id, "jane@example.com")
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=copilot,
        github_login="jane",
        user_id=other.id,
        commit=False,
    )
    for offset, amount in ((3, 2.0), (2, 2.0), (1, 2.0), (0, 8.0)):
        day = TARGET - timedelta(days=offset)
        crud_copilot_usage.replace_day_rows(
            db_session,
            account_id=account_id,
            bucket_start=day_start(day),
            line_items=(LINE_ITEM_PREMIUM_REQUEST,),
            rows=[
                premium_row(day, login="alice", amount=amount),
                premium_row(day, login="jane", amount=amount),
            ],
        )
    run_daily_pass(db_session, NOW)
    by_user = {f.user_id: f for f in _all_findings(db_session, account_id)}
    dismissed = by_user[test_user.id]
    snoozed = by_user[other.id]
    assert (
        client.put(
            f"{DISMISSALS}/{dismissed.item_id}",
            json={"fingerprint": dismissed.fingerprint, "reason": "expected"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{DISMISSALS}/{snoozed.item_id}",
            json={
                "fingerprint": snoozed.fingerprint,
                "reason": "snoozed",
                "snooze_days": 7,
            },
        ).status_code
        == 200
    )

    later = NOW + timedelta(days=1)
    result = run_daily_pass(db_session, later)

    assert result["findings"] == result["updated"] == result["superseded"] == 0
    db_session.refresh(dismissed)
    assert dismissed.dismissed_at is not None
    assert dismissed.superseded_at is None
    section = build_spend_outlier_digest_section(
        db_session,
        account_id,
        start=NOW - timedelta(days=1),
        end=later + timedelta(days=1),
    )
    by_fingerprint = {item["fingerprint"]: item for item in section["items"]}
    assert by_fingerprint[dismissed.fingerprint]["dismissed"] is True
    assert by_fingerprint[snoozed.fingerprint]["dismissed"] is True
    assert len(section["items"]) == 2


def test_superseded_is_distinct_from_dismissed(db_session, test_user, copilot):
    """A dismissed finding that stops qualifying is both; neither erases the other."""
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (finding,) = _all_findings(db_session, account_id)
    crud_spend_outlier_finding.set_dismissed(
        db_session,
        account_id=account_id,
        item_id=finding.item_id,
        fingerprint=finding.fingerprint,
        dismissed_at=NOW + timedelta(hours=1),
        commit=False,
    )

    _import_day(db_session, account_id, TARGET, "alice", 1.0)
    later = NOW + timedelta(days=1)
    run_daily_pass(db_session, later)

    db_session.refresh(finding)
    assert finding.dismissed_at is not None
    assert finding.superseded_at == later
    assert (
        crud_spend_outlier_finding.list_detected_since(
            db_session, account_id=account_id, since=NOW - timedelta(days=1)
        )
        == []
    )
    assert crud_spend_outlier_finding.list_detected_since(
        db_session,
        account_id=account_id,
        since=NOW - timedelta(days=1),
        include_superseded=True,
    ) == [finding]


def test_reconciliation_touches_only_the_replayed_rules_and_days(
    db_session, test_user, copilot
):
    """A finding outside the replay window, and a session finding, are left alone."""
    account_id = test_user.account_id
    old_day = YESTERDAY - timedelta(days=40)
    stale_daily = crud_spend_outlier_finding.record(
        db_session,
        account_id=account_id,
        rule="daily_spend",
        user_id=test_user.id,
        day=old_day,
        item_id=f"spend:daily_spend:{test_user.id}",
        fingerprint=f"daily_spend|{test_user.id}|{old_day.isoformat()}",
        details={"spend_usd": 50.0},
        detected_at=NOW - timedelta(days=39),
        commit=False,
    )
    session_id = uuid.uuid4()
    session_finding = crud_spend_outlier_finding.record(
        db_session,
        account_id=account_id,
        rule="session_cost",
        user_id=test_user.id,
        day=TARGET,
        item_id=f"spend:session_cost:{session_id}",
        fingerprint=f"session_cost|{test_user.id}|{session_id}",
        details={"spend_usd": 50.0},
        detected_at=NOW - timedelta(days=1),
        runtime_session_id=None,
        commit=False,
    )

    result = run_daily_pass(db_session, NOW)

    assert result["superseded"] == 0
    db_session.refresh(stale_daily)
    db_session.refresh(session_finding)
    assert stale_daily.superseded_at is None
    assert session_finding.superseded_at is None


# --- acceptance 6: replay edges, disabled import, source failure ------------


def test_replay_reaches_exactly_28_days_back(db_session, test_user, copilot):
    """Day YESTERDAY-27 is replayed; YESTERDAY-28 is summary-only."""
    account_id = test_user.account_id
    _settings(db_session, account_id, min_history_days=3, daily_multiple=3.0)
    edge = YESTERDAY - timedelta(days=27)
    beyond = YESTERDAY - timedelta(days=28)
    for target in (beyond, edge):
        for offset in (3, 2, 1):
            _import_day(
                db_session, account_id, target - timedelta(days=offset), "alice", 2.0
            )
    _import_day(db_session, account_id, beyond, "alice", 80.0)
    _import_day(db_session, account_id, edge, "alice", 8.0)

    run_daily_pass(db_session, NOW)

    assert [f.day for f in _all_findings(db_session, account_id)] == [edge]
    assert [
        row.cost_usd
        for row in src.copilot_imported_spend(db_session, account_id, beyond, beyond)
    ] == [80.0]


def test_disabled_import_and_missing_connection_do_not_replay(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    crud_copilot_import_connection.update(
        db_session, db_obj=copilot, obj_in={"is_active": False}
    )

    paused = run_daily_pass(db_session, NOW)
    assert paused["replayed"] == 0
    assert paused["accounts"] == 0
    assert _all_findings(db_session, account_id) == []

    crud_copilot_import_connection.delete(db_session, id=copilot.id)
    gone = run_daily_pass(db_session, NOW)
    assert gone["replayed"] == 0
    assert _all_findings(db_session, account_id) == []


def test_gateway_only_account_is_still_evaluated_for_yesterday_only(
    db_session, test_user, copilot
):
    """Without a Copilot connection an account is judged for yesterday, as before."""
    account_id = test_user.account_id
    crud_copilot_import_connection.delete(db_session, id=copilot.id)
    _settings(db_session, account_id, min_history_days=3, daily_multiple=3.0)
    for offset in (3, 2, 1):
        _gateway(db_session, test_user, TARGET - timedelta(days=offset), 2.0)
    _gateway(db_session, test_user, TARGET, 8.0)
    for offset in (3, 2, 1):
        _gateway(db_session, test_user, YESTERDAY - timedelta(days=offset), 2.0)
    _gateway(db_session, test_user, YESTERDAY, 8.0)

    result = run_daily_pass(db_session, NOW)

    assert result["replayed"] == 0
    assert [f.day for f in _all_findings(db_session, account_id)] == [YESTERDAY]


def test_source_failure_keeps_prior_findings_and_judges_yesterday_from_gateway(
    db_session, test_user, copilot
):
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)
    run_daily_pass(db_session, NOW)
    (imported_finding,) = _all_findings(db_session, account_id)
    # Gateway-only spike yesterday (relative to the failing pass).
    later = NOW + timedelta(days=1)
    later_yesterday = later.date() - timedelta(days=1)
    for offset in (3, 2, 1):
        _gateway(db_session, test_user, later_yesterday - timedelta(days=offset), 2.0)
    _gateway(db_session, test_user, later_yesterday, 8.0)

    def broken(db, account_id, start_day, end_day):
        raise RuntimeError("token=should-not-appear import unavailable")

    register_imported_spend_source(broken)
    try:
        result = run_daily_pass(db_session, later)
    finally:
        unregister_imported_spend_source(broken)

    assert result["incomplete"] == 1
    assert result["superseded"] == 0
    assert result["updated"] == 0
    assert result["findings"] == 1
    findings = {f.day: f for f in _all_findings(db_session, account_id)}
    assert findings[TARGET].id == imported_finding.id
    assert findings[TARGET].superseded_at is None
    assert findings[later_yesterday].details["gateway_usd"] == 8.0
    assert findings[later_yesterday].details["imported_usd"] == 0.0


def test_source_failure_diagnostic_is_sanitized(db_session, test_user, caplog):
    def broken(db, account_id, start_day, end_day):
        raise RuntimeError("Authorization: Bearer ghp_secret")

    register_imported_spend_source(broken)
    try:
        with caplog.at_level("WARNING", logger="preloop.services.spend_outliers"):
            result = spend_outliers.evaluate_days(
                db_session, test_user.account_id, [YESTERDAY], NOW
            )
    finally:
        unregister_imported_spend_source(broken)

    assert result.imported_complete is False
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "broken" in message and "RuntimeError" in message for message in warnings
    )
    assert all("ghp_secret" not in message for message in warnings)


def test_explicit_day_evaluation_with_failed_source_judges_nothing_but_yesterday(
    db_session, test_user, copilot
):
    """A replay day cannot be judged from an empty import; yesterday still can."""
    account_id = test_user.account_id
    _seed_spike(db_session, account_id)

    def broken(db, account_id, start_day, end_day):
        raise RuntimeError("import unavailable")

    register_imported_spend_source(broken)
    try:
        result = spend_outliers.evaluate_days(
            db_session, account_id, [TARGET, YESTERDAY], NOW
        )
    finally:
        unregister_imported_spend_source(broken)

    assert result.imported_complete is False
    assert result.days == [YESTERDAY]
    assert result.new == result.updated == result.superseded == []


def test_nonfinite_imported_rows_are_ignored_by_the_evaluator(db_session, test_user):
    """A source that leaks NaN or infinity cannot poison the median."""
    rows = [
        ImportedSpendRow(
            test_user.id, YESTERDAY - timedelta(days=1), "m", float("nan"), "x"
        ),
        ImportedSpendRow(test_user.id, YESTERDAY, "m", float("inf"), "x"),
    ]
    register_imported_spend_source(lambda db, a, s, e: rows)
    source = spend_outliers.registered_imported_spend_sources()[-1]
    try:
        result = spend_outliers.evaluate_days(
            db_session, test_user.account_id, [YESTERDAY], NOW
        )
    finally:
        unregister_imported_spend_source(source)

    assert result.imported_complete is True
    assert result.new == []
