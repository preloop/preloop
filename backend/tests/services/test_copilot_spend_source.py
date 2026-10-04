"""Mapped Copilot premium-request spend as an outlier source (#1061).

Rows are written straight into ``provider_billing_snapshot`` through the
Copilot CRUD, exactly as the import stores them, so each test controls what
the adapter sees without any GitHub stand-in.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_copilot_import_connection,
    crud_copilot_usage,
    crud_copilot_user_mapping,
    crud_user,
)
from preloop.models.crud.copilot_import import (
    COPILOT_PROVIDER,
    LINE_ITEM_PREMIUM_REQUEST,
    LINE_ITEM_SEAT,
    LINE_ITEM_SEAT_SUMMARY,
    LINE_ITEM_USAGE_METRICS,
    canonical_github_login,
)
from preloop.models.crud.provider_billing import IMPORTED_USAGE_SOURCE
from preloop.services import copilot_spend_source as src
from preloop.services import spend_outliers
from preloop.services.copilot_usage_import import (
    COPILOT_IMPORT_SECRET_KIND,
    day_start,
)
from preloop.services.secret_service import get_secret_service

ORG = "example-org"
DAY = date(2026, 9, 24)
FETCHED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


# --- fixtures and helpers ---------------------------------------------------


def connect(
    db: Session,
    account_id: Any,
    *,
    organization: str = ORG,
    is_active: bool = True,
) -> models.CopilotImportConnection:
    """A connection row with a throwaway token, no GitHub involved."""
    secret = get_secret_service().create_local_secret_reference(
        db,
        account_id=account_id,
        name="org",
        secret_kind=COPILOT_IMPORT_SECRET_KIND,
        secret_value="unused-token",
    )
    return crud_copilot_import_connection.create(
        db,
        obj_in={
            "account_id": account_id,
            "organization": organization,
            "secret_reference_id": secret.id,
            "is_active": is_active,
        },
    )


def make_user(
    db: Session, account_id: Any, email: str, *, is_active: bool = True
) -> models.User:
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account_id,
            "email": email,
            "username": email.split("@")[0],
            "full_name": email.split("@")[0].title(),
            "is_active": is_active,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db.flush()
    return user


def premium_row(
    day: date,
    *,
    login: Optional[str],
    model: str = "model-a",
    amount: Optional[float] = 1.0,
    org: str = ORG,
    currency: str = "USD",
    unattributed: bool = False,
    granularity: str = "1d",
    service_tier: Optional[str] = None,
    bucket_start: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One premium-request row shaped like the import writes it."""
    raw: Dict[str, Any] = {"netQuantity": 25, "scope": "organization"}
    if amount is not None and math.isfinite(amount):
        raw["netAmount"] = amount
    if unattributed:
        raw["unattributed"] = True
    return {
        "provider": COPILOT_PROVIDER,
        "granularity": granularity,
        "bucket_start": bucket_start or day_start(day),
        "bucket_end": day_start(day + timedelta(days=1)),
        "model": model,
        "line_item": LINE_ITEM_PREMIUM_REQUEST,
        "project_or_workspace_id": org,
        "user_login": login,
        "service_tier": service_tier,
        "usage_source": IMPORTED_USAGE_SOURCE,
        "cost_basis": "reconciled",
        "cost_amount": amount,
        "currency": currency,
        "raw": raw,
        "fetched_at": FETCHED_AT,
    }


def store(db: Session, account_id: Any, *rows: Dict[str, Any]) -> None:
    crud_copilot_usage.upsert_snapshots(
        db, account_id=account_id, rows=list(rows), commit=False
    )
    db.flush()


def seat_rows(day: date, *logins: str, org: str = ORG) -> list:
    common = {
        "provider": COPILOT_PROVIDER,
        "granularity": "1d",
        "bucket_start": day_start(day),
        "bucket_end": day_start(day + timedelta(days=1)),
        "project_or_workspace_id": org,
        "usage_source": IMPORTED_USAGE_SOURCE,
        "cost_amount": None,
        "fetched_at": FETCHED_AT,
    }
    rows = [
        {
            **common,
            "line_item": LINE_ITEM_SEAT_SUMMARY,
            "raw": {"total_seats": len(logins), "plan_type": "business"},
        }
    ]
    rows.extend(
        {
            **common,
            "line_item": LINE_ITEM_SEAT,
            "user_login": login,
            "raw": {"last_activity_at": "2026-09-24T08:00:00Z"},
        }
        for login in logins
    )
    return rows


def metrics_row(day: date, login: str, count: int, *, org: str = ORG) -> Dict[str, Any]:
    return {
        "provider": COPILOT_PROVIDER,
        "granularity": "1d",
        "bucket_start": day_start(day),
        "bucket_end": day_start(day + timedelta(days=1)),
        "line_item": LINE_ITEM_USAGE_METRICS,
        "project_or_workspace_id": org,
        "user_login": login,
        "usage_source": IMPORTED_USAGE_SOURCE,
        "cost_amount": None,
        "raw": {
            "user_initiated_interaction_count": count,
            "models": [{"model": "model-a", "user_initiated_interaction_count": count}],
        },
        "fetched_at": FETCHED_AT,
    }


def by_key(rows):
    return {(row.user_id, row.day, row.model): row for row in rows}


@pytest.fixture
def connection(db_session, test_user):
    return connect(db_session, test_user.account_id)


@pytest.fixture
def registered():
    """The real adapter registered for the test, removed afterwards."""
    src.register_copilot_spend_source()
    try:
        yield
    finally:
        spend_outliers.unregister_imported_spend_source(src.copilot_imported_spend)


# --- canonical login and registration --------------------------------------


def test_canonical_login_trims_and_lowercases():
    assert canonical_github_login("  Alice-Dev ") == "alice-dev"
    with pytest.raises(ValueError):
        canonical_github_login("   ")


def test_registration_is_idempotent(registered):
    """Registering at every task start leaves exactly one entry."""
    src.register_copilot_spend_source()
    src.register_copilot_spend_source()

    sources = spend_outliers.registered_imported_spend_sources()

    assert sources.count(src.copilot_imported_spend) == 1


# --- what the adapter emits -------------------------------------------------


def test_adapter_emits_only_mapped_per_user_daily_usd_rows(
    db_session, test_user, connection
):
    """Acceptance 3: of everything stored, only the mapped $8 reaches the rules.

    Stored for the day: alice $8 (mapped), the organization total $10 kept as
    an aggregate row, the $2 unattributed residual, a seat snapshot with a
    seat price, a usage-metrics row with 100 requests, a mapped row with no
    amount, and $99 in another account under the same login.
    """
    account_id = test_user.account_id
    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"seat_price_monthly": 19.0}
    )
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(
        db_session,
        account_id,
        premium_row(DAY, login="alice", amount=8.0),
        premium_row(DAY, login=None, model="model-b", amount=10.0),
        premium_row(DAY, login=None, amount=2.0, unattributed=True),
        premium_row(DAY, login="alice", model="model-unknown-cost", amount=None),
        *seat_rows(DAY, "alice", "bob"),
        metrics_row(DAY, "alice", 100),
    )
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    other_user = make_user(db_session, other_account.id, "alice@other.example.com")
    other_connection = connect(db_session, other_account.id)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=other_connection,
        github_login="alice",
        user_id=other_user.id,
        commit=False,
    )
    store(db_session, other_account.id, premium_row(DAY, login="alice", amount=99.0))

    rows = src.copilot_imported_spend(db_session, account_id, DAY, DAY)

    assert [
        (row.user_id, row.day, row.model, row.cost_usd, row.source) for row in rows
    ] == [(test_user.id, DAY, "model-a", 8.0, "copilot")]

    coverage = src.spend_coverage(
        db_session, account_id=account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["organization"] == ORG
    assert coverage["connection_active"] is True
    assert coverage["mapped_rows"] == 1
    assert coverage["mapped_net_amount"] == 8.0
    assert coverage["excluded"] == {
        "unmapped": 0,
        "unknown_amount": 1,
        "unsupported_currency": 0,
        "nonfinite_amount": 0,
        "aggregate_only": 1,
        "unattributed": 1,
        "not_daily": 0,
    }
    assert coverage["unmapped_logins"] == []
    assert coverage["mapped_logins"] == ["alice"]
    # Nothing in the diagnostics is a token or a stored payload.
    assert "unused-token" not in repr(coverage)
    assert "netQuantity" not in repr(coverage)

    # The other account's $99 stays theirs.
    (theirs,) = src.copilot_imported_spend(db_session, other_account.id, DAY, DAY)
    assert theirs.user_id == other_user.id
    assert theirs.cost_usd == 99.0


def test_adapter_uses_stored_net_amounts_not_quantity_times_price(
    db_session, test_user, connection
):
    """$1.60 billed for 100 requests at a $0.04 list price is $1.60, not $4."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    row = premium_row(DAY, login="alice", amount=1.60)
    row["raw"].update({"netQuantity": 100, "pricePerUnit": 0.04})
    store(db_session, test_user.account_id, row)

    (emitted,) = src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY)

    assert emitted.cost_usd == pytest.approx(1.60)


def test_unknown_amount_is_not_a_zero_sample(db_session, test_user, connection):
    """A mapped row without an amount is reported unknown and emits nothing."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(
        db_session, test_user.account_id, premium_row(DAY, login="alice", amount=None)
    )

    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["mapped_rows"] == 0
    assert coverage["mapped_net_amount"] is None
    assert coverage["excluded"]["unknown_amount"] == 1


def test_known_zero_is_known_and_emits_nothing(db_session, test_user, connection):
    """A $0 day is a real zero: counted as mapped, never a spike input."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(db_session, test_user.account_id, premium_row(DAY, login="alice", amount=0.0))

    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["mapped_rows"] == 1
    assert coverage["known_zero_rows"] == 1
    assert coverage["mapped_net_amount"] == 0.0


def test_credits_net_within_user_day_model_and_bad_rows_are_counted(
    db_session, test_user, connection
):
    """Acceptance 5: $10 and a $2 credit net to $8; NaN, EUR and null are excluded."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(
        db_session,
        test_user.account_id,
        premium_row(DAY, login="alice", amount=10.0),
        premium_row(DAY, login="alice", amount=-2.0, service_tier="credit"),
        premium_row(DAY, login="alice", model="model-nan", amount=math.nan),
        premium_row(DAY, login="alice", model="model-inf", amount=math.inf),
        premium_row(DAY, login="alice", model="model-eur", amount=5.0, currency="EUR"),
        premium_row(DAY, login="alice", model="model-null", amount=None),
        premium_row(DAY, login="alice", model="model-refund", amount=-3.0),
    )

    rows = by_key(
        src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY)
    )

    assert set(rows) == {(test_user.id, DAY, "model-a")}
    assert rows[(test_user.id, DAY, "model-a")].cost_usd == pytest.approx(8.0)
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["excluded"]["nonfinite_amount"] == 2
    assert coverage["excluded"]["unsupported_currency"] == 1
    assert coverage["excluded"]["unknown_amount"] == 1
    # The pure refund model nets negative and is not a spend sample.
    assert coverage["mapped_rows"] == 3


def test_two_logins_for_one_user_are_summed(db_session, test_user, connection):
    for login in ("alice", "alice-laptop"):
        crud_copilot_user_mapping.upsert(
            db_session,
            connection=connection,
            github_login=login,
            user_id=test_user.id,
            commit=False,
        )
    store(
        db_session,
        test_user.account_id,
        premium_row(DAY, login="alice", amount=3.0),
        premium_row(DAY, login="alice-laptop", amount=5.0),
    )

    (row,) = src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY)

    assert row.cost_usd == 8.0


def test_day_bounds_are_inclusive_and_utc(db_session, test_user, connection):
    """``[start, end]`` in UTC days, whatever offset the stored instant carries."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    before = DAY - timedelta(days=1)
    after = DAY + timedelta(days=2)
    # Midnight UTC of DAY written as 02:00 in a +02:00 zone.
    offset_midnight = datetime(
        DAY.year, DAY.month, DAY.day, 2, 0, tzinfo=timezone(timedelta(hours=2))
    )
    store(
        db_session,
        test_user.account_id,
        premium_row(before, login="alice", amount=1.0),
        premium_row(DAY, login="alice", amount=2.0, bucket_start=offset_midnight),
        premium_row(DAY + timedelta(days=1), login="alice", amount=3.0),
        premium_row(after, login="alice", amount=4.0),
    )

    rows = src.copilot_imported_spend(
        db_session, test_user.account_id, DAY, DAY + timedelta(days=1)
    )

    assert sorted((row.day, row.cost_usd) for row in rows) == [
        (DAY, 2.0),
        (DAY + timedelta(days=1), 3.0),
    ]
    assert (
        src.copilot_imported_spend(db_session, test_user.account_id, after, before)
        == []
    )


def test_blank_stored_login_is_unmapped_not_an_error(db_session, test_user, connection):
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(db_session, test_user.account_id, premium_row(DAY, login="   ", amount=5.0))

    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["excluded"]["unmapped"] == 1
    assert coverage["unmapped_logins"] == []


def test_non_daily_rows_are_excluded(db_session, test_user, connection):
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(
        db_session,
        test_user.account_id,
        premium_row(DAY, login="alice", amount=7.0, granularity="1m"),
    )

    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["excluded"]["not_daily"] == 1


# --- mapping validity -------------------------------------------------------


def test_unmapped_login_is_reported_not_guessed(db_session, test_user, connection):
    """A login that looks like the user's username is still unmapped."""
    store(
        db_session,
        test_user.account_id,
        premium_row(DAY, login=test_user.username, amount=5.0),
    )

    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []
    coverage = src.spend_coverage(
        db_session, account_id=test_user.account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["excluded"]["unmapped"] == 1
    assert coverage["unmapped_logins"] == [test_user.username.lower()]


def test_canonical_case_duplicate_updates_the_one_mapping(
    db_session, test_user, connection
):
    other = make_user(db_session, test_user.account_id, "jane@example.com")

    first = crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="Alice",
        user_id=test_user.id,
        commit=False,
    )
    second = crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login=" alice ",
        user_id=other.id,
        commit=False,
    )

    assert first.id == second.id
    assert second.github_login == "alice"
    assert second.user_id == other.id
    assert (
        len(
            crud_copilot_user_mapping.list_for_connection(
                db_session, connection=connection
            )
        )
        == 1
    )
    assert crud_copilot_user_mapping.resolve_user_ids(
        db_session, connection=connection
    ) == {"alice": other.id}


def test_imported_login_case_matches_canonical_mapping(
    db_session, test_user, connection
):
    """GitHub may return ``Alice``; the mapping stored ``alice`` still applies."""
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    store(db_session, test_user.account_id, premium_row(DAY, login="Alice", amount=4.0))

    (row,) = src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY)

    assert row.user_id == test_user.id


def test_inactive_user_removed_mapping_and_changed_organization_do_not_contribute(
    db_session, test_user, connection
):
    account_id = test_user.account_id
    inactive = make_user(db_session, account_id, "gone@example.com", is_active=False)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="gone",
        user_id=inactive.id,
        commit=False,
    )
    store(
        db_session,
        account_id,
        premium_row(DAY, login="alice", amount=8.0),
        premium_row(DAY, login="gone", amount=6.0),
    )

    rows = src.copilot_imported_spend(db_session, account_id, DAY, DAY)
    assert [(row.user_id, row.cost_usd) for row in rows] == [(test_user.id, 8.0)]
    coverage = src.spend_coverage(
        db_session, account_id=account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["excluded"]["unmapped"] == 1
    assert coverage["unmapped_logins"] == ["gone"]

    # Removing the mapping removes the spend from the rules.
    assert (
        crud_copilot_user_mapping.delete_for_login(
            db_session, connection=connection, github_login="ALICE", commit=False
        )
        == 1
    )
    assert src.copilot_imported_spend(db_session, account_id, DAY, DAY) == []

    # Mappings written for one organization do not follow the connection
    # to another, and come back when it returns.
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"organization": "other-org"}
    )
    assert (
        crud_copilot_user_mapping.list_for_connection(db_session, connection=connection)
        == []
    )
    assert src.copilot_imported_spend(db_session, account_id, DAY, DAY) == []
    store(
        db_session,
        account_id,
        premium_row(DAY, login="alice", amount=9.0, org="other-org"),
    )
    assert src.copilot_imported_spend(db_session, account_id, DAY, DAY) == []
    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"organization": ORG}
    )
    (row,) = src.copilot_imported_spend(db_session, account_id, DAY, DAY)
    assert row.cost_usd == 8.0


def test_deleted_user_takes_its_mapping_with_it(db_session, test_user, connection):
    leaver = make_user(db_session, test_user.account_id, "leaver@example.com")
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="leaver",
        user_id=leaver.id,
        commit=False,
    )
    store(
        db_session, test_user.account_id, premium_row(DAY, login="leaver", amount=5.0)
    )

    db_session.delete(leaver)
    db_session.flush()
    db_session.expire_all()

    assert (
        crud_copilot_user_mapping.list_for_connection(db_session, connection=connection)
        == []
    )
    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []


def test_corrupt_mapping_to_another_accounts_user_is_ignored(
    db_session, test_user, connection
):
    """A row that points across accounts resolves to nobody on read."""
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = make_user(db_session, other_account.id, "foreign@other.example.com")
    db_session.execute(
        models.CopilotUserMapping.__table__.insert().values(
            id=uuid.uuid4(),
            account_id=test_user.account_id,
            connection_id=connection.id,
            organization=ORG,
            github_login="alice",
            user_id=foreign.id,
        )
    )
    store(db_session, test_user.account_id, premium_row(DAY, login="alice", amount=8.0))

    assert (
        crud_copilot_user_mapping.resolve_user_ids(db_session, connection=connection)
        == {}
    )
    assert src.copilot_imported_spend(db_session, test_user.account_id, DAY, DAY) == []


def test_paused_or_missing_connection_yields_nothing(db_session, test_user):
    account_id = test_user.account_id
    store(db_session, account_id, premium_row(DAY, login="alice", amount=8.0))

    assert src.copilot_imported_spend(db_session, account_id, DAY, DAY) == []
    assert src.copilot_replay_account_ids(db_session) == []
    coverage = src.spend_coverage(
        db_session, account_id=account_id, start_day=DAY, end_day=DAY
    )
    assert coverage["organization"] is None
    assert coverage["connection_active"] is False
    assert coverage["mapped_net_amount"] is None

    connection = connect(db_session, account_id, is_active=False)
    crud_copilot_user_mapping.upsert(
        db_session,
        connection=connection,
        github_login="alice",
        user_id=test_user.id,
        commit=False,
    )
    assert src.copilot_imported_spend(db_session, account_id, DAY, DAY) == []
    assert src.copilot_replay_account_ids(db_session) == []
    paused = src.spend_coverage(
        db_session, account_id=account_id, start_day=DAY, end_day=DAY
    )
    assert paused["connection_active"] is False
    # The diagnostics still show what would apply once resumed.
    assert paused["mapped_logins"] == ["alice"]

    crud_copilot_import_connection.update(
        db_session, db_obj=connection, obj_in={"is_active": True}
    )
    assert src.copilot_replay_account_ids(db_session) == [account_id]
    (row,) = src.copilot_imported_spend(db_session, account_id, DAY, DAY)
    assert row.cost_usd == 8.0


# --- the service's write validation ----------------------------------------


def test_upsert_refuses_foreign_inactive_and_unknown_targets_alike(
    db_session, test_user, connection
):
    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = make_user(db_session, other_account.id, "secret-name@other.example.com")
    inactive = make_user(
        db_session, test_user.account_id, "inactive@example.com", is_active=False
    )
    messages = set()
    for target in (foreign.id, inactive.id, uuid.uuid4()):
        with pytest.raises(src.CopilotMappingError) as excinfo:
            src.upsert_user_mapping(
                db_session,
                account_id=test_user.account_id,
                github_login="alice",
                user_id=target,
            )
        assert excinfo.value.reason == "invalid_user"
        messages.add(str(excinfo.value))
    assert len(messages) == 1
    assert "secret-name" not in messages.pop()
    assert (
        crud_copilot_user_mapping.list_for_connection(db_session, connection=connection)
        == []
    )


def test_upsert_needs_an_active_connection_and_a_login(db_session, test_user):
    with pytest.raises(src.CopilotMappingError) as excinfo:
        src.upsert_user_mapping(
            db_session,
            account_id=test_user.account_id,
            github_login="alice",
            user_id=test_user.id,
        )
    assert excinfo.value.reason == "no_connection"

    connect(db_session, test_user.account_id, is_active=False)
    with pytest.raises(src.CopilotMappingError) as excinfo:
        src.upsert_user_mapping(
            db_session,
            account_id=test_user.account_id,
            github_login="alice",
            user_id=test_user.id,
        )
    assert excinfo.value.reason == "no_connection"


def test_upsert_stores_canonical_login_and_returns_in_account_name(
    db_session, test_user, connection
):
    mapping, name = src.upsert_user_mapping(
        db_session,
        account_id=test_user.account_id,
        github_login=" Alice ",
        user_id=test_user.id,
    )

    assert mapping.github_login == "alice"
    assert mapping.organization == ORG
    assert name == "Test User"
    listing = src.list_user_mappings(db_session, account_id=test_user.account_id)
    assert listing["organization"] == ORG
    assert [(item["github_login"], item["user_name"]) for item in listing["items"]] == [
        ("alice", "Test User")
    ]
    assert src.delete_user_mapping(
        db_session, account_id=test_user.account_id, github_login="ALICE"
    )
    assert not src.delete_user_mapping(
        db_session, account_id=test_user.account_id, github_login="alice"
    )
