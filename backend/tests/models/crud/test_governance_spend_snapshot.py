"""Governance admission snapshots use the same purpose and funding scope."""

from datetime import datetime, timedelta, timezone
from collections.abc import Iterator
import importlib.util
from pathlib import Path
import re
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_api_usage
from preloop.models.crud.governance_budget import PURPOSES, _run_spend_query
from preloop.models.gateway_usage import GATEWAY_USAGE_PURPOSES


def create_usage(
    db: Session, account_id: UUID, cost: float | None, when: datetime
) -> models.ApiUsage:
    """Create a committed, platform-funded governance usage row."""
    return crud_api_usage.create(
        db,
        obj_in={
            "account_id": account_id,
            "endpoint": "delta-test",
            "method": "POST",
            "status_code": 202,
            "duration": 0,
            "timestamp": when,
            "action_type": "model_gateway",
            "estimated_cost": cost,
            "meta_data": {"purpose": "governance_review", "funding": "platform"},
        },
    )


@pytest.fixture
def committed_account(db_engine: Engine) -> Iterator[UUID]:
    """Make an account visible across independent READ COMMITTED connections."""
    with Session(db_engine) as db:
        account = crud_account.create(db, obj_in={"organization_name": "spend delta"})
        account_id = account.id
    try:
        yield account_id
    finally:
        with Session(db_engine) as db:
            db.query(models.ApiUsage).filter_by(account_id=account_id).delete()
            crud_account.delete(db, id=account_id)


def test_delta_includes_backdated_insert_committed_after_snapshot(
    db_engine: Engine, committed_account: UUID
) -> None:
    """An insert already in flight must appear after the admission lock wait."""
    now = datetime.now(timezone.utc)
    with Session(db_engine) as reader, Session(db_engine) as writer:
        row = create_usage(writer, committed_account, 0.03, now)
        late = models.ApiUsage(
            account_id=committed_account,
            endpoint=row.endpoint,
            method="POST",
            status_code=202,
            duration=0,
            timestamp=now - timedelta(hours=1),
            action_type="model_gateway",
            estimated_cost=0.04,
            meta_data={"purpose": "governance_review", "funding": "platform"},
        )
        writer.add(late)
        writer.flush()
        snapshot = crud_api_usage.snapshot_governance_spend(
            reader, account_id=str(committed_account), start=now - timedelta(days=7)
        )
        assert snapshot.total == pytest.approx(0.03)
        writer.commit()
        assert crud_api_usage.governance_spend_since_snapshot(
            reader, snapshot=snapshot
        ) == pytest.approx(0.07)


@pytest.mark.parametrize("before,after", [(None, 0.04), (0.01, 0.04), (0.03, 0.01)])
def test_delta_applies_only_settlement_difference(
    db_session: Session, test_user: models.User, before: float | None, after: float
) -> None:
    now = datetime.now(timezone.utc)
    row = create_usage(db_session, test_user.account_id, before, now)
    snapshot = crud_api_usage.snapshot_governance_spend(
        db_session, account_id=str(test_user.account_id), start=now - timedelta(days=7)
    )
    crud_api_usage.update(db_session, db_obj=row, obj_in={"estimated_cost": after})
    assert crud_api_usage.governance_spend_since_snapshot(
        db_session, snapshot=snapshot
    ) == pytest.approx(after)


@pytest.mark.parametrize("change", ["delete", "purpose", "funding", "timestamp"])
def test_delta_retains_baseline_when_historical_row_leaves_scope(
    db_session: Session, test_user: models.User, change: str
) -> None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    row = create_usage(db_session, test_user.account_id, 0.03, now)
    snapshot = crud_api_usage.snapshot_governance_spend(
        db_session, account_id=str(test_user.account_id), start=start
    )
    if change == "delete":
        crud_api_usage.delete(db_session, id=row.id)
    else:
        changes = {
            "purpose": {
                "meta_data": {"purpose": "session_title", "funding": "platform"}
            },
            "funding": {
                "meta_data": {"purpose": "governance_review", "funding": "account"}
            },
            "timestamp": {"timestamp": start - timedelta(seconds=1)},
        }
        crud_api_usage.update(db_session, db_obj=row, obj_in=changes[change])
    assert crud_api_usage.governance_spend_since_snapshot(
        db_session, snapshot=snapshot
    ) == pytest.approx(0.03)


def test_other_account_stays_out_of_snapshot_and_delta(
    db_session: Session, test_user: models.User
) -> None:
    now = datetime.now(timezone.utc)
    create_usage(db_session, test_user.account_id, 0.03, now)
    other = crud_account.create(db_session, obj_in={"organization_name": "other spend"})
    foreign = create_usage(db_session, other.id, 10, now)
    snapshot = crud_api_usage.snapshot_governance_spend(
        db_session, account_id=str(test_user.account_id), start=now - timedelta(days=7)
    )
    assert snapshot.total == pytest.approx(0.03)
    crud_api_usage.update(db_session, db_obj=foreign, obj_in={"estimated_cost": 20})
    create_usage(db_session, other.id, 30, now)
    assert crud_api_usage.governance_spend_since_snapshot(
        db_session, snapshot=snapshot
    ) == pytest.approx(0.03)


def test_snapshot_filters_funding_purpose_account_and_week(
    db_session: Session, test_user: models.User
) -> None:
    """Keep agent traffic and account-funded analysis out of platform admission."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    rows = [
        ("governance_session_digest", "platform", now, 0.01),
        ("governance_review", "platform", start, 0.02),
        ("governance_session_digest", "account", now, 5),
        ("governance_review", None, now, 5),
        ("session_title", "platform", now, 5),
        ("governance_review", "platform", start - timedelta(seconds=1), 5),
    ]
    for purpose, funding, timestamp, cost in rows:
        crud_api_usage.create(
            db_session,
            obj_in={
                "account_id": test_user.account_id,
                "endpoint": "snapshot-test",
                "method": "POST",
                "status_code": 202,
                "duration": 0,
                "timestamp": timestamp,
                "action_type": "model_gateway",
                "estimated_cost": cost,
                "meta_data": {"purpose": purpose, "funding": funding},
            },
        )
    snapshot = crud_api_usage.snapshot_governance_spend(
        db_session, account_id=str(test_user.account_id), start=start
    )
    assert snapshot.total == pytest.approx(0.03)
    assert len(snapshot.costs) == 2
    assert crud_api_usage.governance_spend_since_snapshot(
        db_session, snapshot=snapshot
    ) == pytest.approx(0.03)


def test_generic_plan_can_match_partial_index_predicate(
    db_session: Session, test_user: models.User
) -> None:
    """Static purpose/action/funding predicates must survive prepared queries."""
    now = datetime.now(timezone.utc)
    db_session.execute(
        text("""
        INSERT INTO api_usage
            (id, account_id, timestamp, endpoint, method, status_code,
             duration, action_type, estimated_cost, meta_data)
        SELECT gen_random_uuid(), :account, :now, 'core-plan', 'POST',
               200, 0, 'model_gateway', 0.001,
               CASE WHEN n <= 20 THEN
                 '{"purpose":"governance_review","funding":"platform"}'::jsonb
               ELSE '{"funding":"platform"}'::jsonb END
        FROM generate_series(1, 10000) n
        """),
        {"account": test_user.account_id, "now": now},
    )
    db_session.execute(text("ANALYZE api_usage"))
    query = crud_api_usage._governance_spend_query(
        db_session,
        account_id=str(test_user.account_id),
        start=now - timedelta(days=7),
    )
    compiled = query.statement.compile(dialect=postgresql.dialect())
    assert set(compiled.params) == {"account_id_1", "timestamp_1"}
    sql = str(compiled)
    assert "'governance_session_digest', 'governance_review'" in sql
    assert "->> 'funding'" in sql
    prepared = query.statement.compile(
        dialect=postgresql.dialect(paramstyle="numeric_dollar")
    )
    db_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    db_session.connection().exec_driver_sql(
        "PREPARE core_governance_probe AS " + str(prepared)
    )
    # psycopg 3 cannot server-bind PostgreSQL EXECUTE utility arguments.
    # Only these invocation values are literal; the SELECT stays parameterized.
    invocation = text(
        "EXPLAIN (COSTS OFF) EXECUTE core_governance_probe (:account, :start)"
    ).bindparams(
        account=test_user.account_id,
        start=now - timedelta(days=7),
    )
    explain = invocation.compile(
        dialect=db_session.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    try:
        plan = db_session.execute(text(str(explain))).all()
    finally:
        db_session.execute(text("DEALLOCATE core_governance_probe"))
    assert "ix_api_usage_governance_spend" in "\n".join(row[0] for row in plan)


def test_spend_predicates_match_frozen_migration_and_purpose_vocabulary(
    db_session: Session,
) -> None:
    """Vocabulary changes require explicit query, ORM and migration review."""
    path = (
        Path(__file__).resolve().parents[3]
        / "preloop/models/alembic/versions/20261018_governance_spend_idx.py"
    )
    spec = importlib.util.spec_from_file_location("governance_spend_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    index = next(
        item
        for item in models.ApiUsage.__table__.indexes
        if item.name == migration.INDEX
    )
    assert str(index.dialect_options["postgresql"]["where"]) == migration.PREDICATE
    purposes = set(re.findall(r"'(governance_[^']+)'", migration.PREDICATE))
    assert purposes == {
        purpose
        for purpose in GATEWAY_USAGE_PURPOSES
        if purpose.startswith("governance_")
    }
    query = crud_api_usage._governance_spend_query(
        db_session,
        account_id="00000000-0000-0000-0000-000000000001",
        start=datetime.now(timezone.utc),
    )
    assert migration.PREDICATE in str(query.statement).replace("api_usage.", "")
    assert set(PURPOSES) == purposes
    weekly = _run_spend_query(
        db_session,
        account_id=UUID("00000000-0000-0000-0000-000000000001"),
        window_start=datetime.now(timezone.utc) - timedelta(days=7),
        window_end=datetime.now(timezone.utc),
        run_id=uuid4(),
    )
    assert migration.PREDICATE in str(weekly.statement).replace("api_usage.", "")


def test_migrated_governance_index_survives_autogenerate(db_session: Session) -> None:
    """A later autogenerated migration must preserve the admission index."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    name = "ix_api_usage_governance_spend"
    index = next(
        (item for item in models.ApiUsage.__table__.indexes if item.name == name), None
    )
    assert index is not None, "Declare the migrated governance index in ORM metadata"
    context = MigrationContext.configure(db_session.connection())
    diffs = compare_metadata(context, models.ApiUsage.metadata)
    changes = [
        diff
        for diff in diffs
        if isinstance(diff, tuple)
        and len(diff) > 1
        and getattr(diff[1], "name", None) == name
    ]
    assert changes == []
