"""Operator ledger reads are scoped and relabel restoration is reversible."""

from datetime import datetime, timezone
import importlib
from collections.abc import Callable

import pytest

from sqlalchemy import Engine, text
from preloop.models.crud.event import OPERATOR_DIGEST_LOCK_KEY
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_event


def test_operator_day_marker_is_global(db_session: Session) -> None:
    day = "2099-01-01"
    assert not crud_event.operator_digest_sent(db_session, day=day)
    crud_event.log_event(
        db_session,
        event_type="report_operator_email",
        event_data={"day": day},
        commit=False,
    )
    assert crud_event.operator_digest_sent(db_session, day=day)
    assert not crud_event.operator_digest_sent(db_session, day="2099-01-02")


@pytest.mark.parametrize("prior_action", ["governance_analysis", None])
def test_relabel_restore_roundtrip(
    db_session: Session,
    create_account: Callable[..., models.Account],
    monkeypatch: pytest.MonkeyPatch,
    prior_action: str | None,
) -> None:
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261102_restore_usage_action"
    )
    monkeypatch.setattr(
        migration.op, "execute", lambda sql: db_session.execute(text(sql))
    )
    account = create_account()
    rows = []
    for purpose, before in [
        ("governance_review", "model_gateway"),
        ("governance_session_digest", "model_gateway"),
        ("gateway", "model_gateway"),
        ("governance_review", None),
    ]:
        row = models.ApiUsage(
            account_id=account.id,
            timestamp=datetime.now(timezone.utc),
            action_type=prior_action,
            endpoint="/test",
            method="POST",
            status_code=200,
            duration=0.1,
            meta_data={"purpose": purpose, "action_type_before": before},
        )
        db_session.add(row)
        rows.append(row)
    db_session.flush()
    migration.upgrade()
    db_session.expire_all()
    assert [r.action_type for r in rows] == [
        "model_gateway",
        "model_gateway",
        prior_action,
        prior_action,
    ]
    migration.upgrade()
    migration.downgrade()
    db_session.expire_all()
    assert all(r.action_type == prior_action for r in rows)
    assert all("report_restore_action_type" not in r.meta_data for r in rows)
    migration.upgrade()
    db_session.expire_all()
    assert rows[0].action_type == "model_gateway"


def test_operator_events_are_bounded_and_account_scoped(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    from datetime import timedelta

    account = create_account()
    foreign = create_account()
    start = datetime.now(timezone.utc)
    for index, owner in enumerate([account.id, foreign.id, account.id, account.id]):
        crud_event.log_event(
            db_session,
            event_type="report_operator_failure",
            account_id=owner,
            timestamp=start + timedelta(seconds=index),
            event_data={"index": index},
            commit=False,
        )
    rows = crud_event.report_operator_events(
        db_session,
        start=start,
        end=start + timedelta(minutes=1),
        account_id=account.id,
        limit=2,
    )
    assert [row.event_data["index"] for row in rows] == [3, 2]
    assert (
        len(
            crud_event.report_operator_events(
                db_session,
                start=start,
                end=start + timedelta(minutes=1),
                account_id=account.id,
                limit=None,
            )
        )
        == 3
    )


@pytest.mark.parametrize(
    "usage_source,action_type",
    [
        ("imported", "imported_usage"),
        (None, "imported_usage"),
        ("imported", "governance_analysis"),
    ],
)
def test_restore_never_trusts_imported_metadata(
    db_session: Session,
    create_account: Callable[..., models.Account],
    monkeypatch: pytest.MonkeyPatch,
    usage_source: str | None,
    action_type: str,
) -> None:
    migration = importlib.import_module(
        "preloop.models.alembic.versions.20261102_restore_usage_action"
    )
    monkeypatch.setattr(
        migration.op, "execute", lambda sql: db_session.execute(text(sql))
    )
    row = models.ApiUsage(
        account_id=create_account().id,
        timestamp=datetime.now(timezone.utc),
        action_type=action_type,
        usage_source=usage_source,
        endpoint="/usage/import",
        method="POST",
        status_code=200,
        duration=0.1,
        meta_data={
            "purpose": "governance_review",
            "action_type_before": "model_gateway",
        },
    )
    db_session.add(row)
    db_session.flush()
    migration.upgrade()
    db_session.expire_all()
    assert row.action_type == action_type
    assert "report_restore_action_type" not in row.meta_data
    migration.downgrade()
    db_session.expire_all()
    assert row.action_type == action_type


def test_operator_delivery_lock_serializes_connections(db_engine: Engine) -> None:
    params = {
        "namespace": OPERATOR_DIGEST_LOCK_KEY[0],
        "key": OPERATOR_DIGEST_LOCK_KEY[1],
    }
    try_lock = text("SELECT pg_try_advisory_xact_lock(:namespace, :key)")
    with Session(db_engine) as owner:
        crud_event.operator_digest_sent(owner, day="2099-02-01")
        with db_engine.connect() as contender:
            assert not contender.execute(try_lock, params).scalar_one()
        owner.rollback()
        with db_engine.connect() as contender:
            assert contender.execute(try_lock, params).scalar_one()


def test_operator_window_filters_type_boundaries_and_orders_ties(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    from datetime import timedelta

    account, foreign = create_account(), create_account()
    start = datetime(2099, 3, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rows = []
    for owner, when, kind in [
        (account.id, start, "report_operator_failure"),
        (foreign.id, end - timedelta(seconds=1), "report_operator_failure"),
        (account.id, start, "report_operator_failure"),
        (account.id, end, "report_operator_failure"),
        (account.id, start - timedelta(seconds=1), "report_operator_failure"),
        (account.id, start, "other_event"),
    ]:
        rows.append(
            crud_event.log_event(
                db_session,
                event_type=kind,
                account_id=owner,
                timestamp=when,
                commit=False,
            )
        )
    expected = sorted([rows[0], rows[2]], key=lambda row: row.id)
    assert (
        crud_event.report_operator_events(
            db_session, start=start, end=end, account_id=account.id
        )
        == expected
    )
    assert crud_event.report_operator_events(db_session, start=start, end=end) == [
        rows[1],
        *expected,
    ]
