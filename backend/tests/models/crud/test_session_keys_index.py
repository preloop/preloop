"""The Spend session reads stay on one account's index range (20261203)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.api_usage import CRUDApiUsage

INDEX = "ix_api_usage_customer_session_keys"


def test_index_carries_the_session_read_predicate(db_session: Session) -> None:
    definition = db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
        {"name": INDEX},
    ).scalar_one()
    assert '(account_id, "timestamp" DESC)' in definition
    assert "INCLUDE (runtime_session_id, flow_execution_id)" in definition
    assert "'governance_'" in definition
    assert "runtime_session_id IS NOT NULL" in definition


def test_session_key_walk_can_use_the_index(db_session: Session, test_user) -> None:
    """With the global timestamp index out of the way, the planner proves the
    read's predicate implies the partial index's (no heap filter needed)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conditions = CRUDApiUsage._session_usage_conditions(
        account_id=str(test_user.account_id),
        start_date=now - timedelta(days=30),
        end_date=now,
    )
    usage = models.ApiUsage
    query = (
        select(usage.runtime_session_id, usage.flow_execution_id, usage.timestamp)
        .where(*conditions)
        .order_by(usage.timestamp.desc())
        .limit(20)
    )
    sql = str(
        query.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    db_session.execute(text("DROP INDEX IF EXISTS ix_api_usage_timestamp"))
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = "\n".join(row[0] for row in db_session.execute(text(f"EXPLAIN {sql}")).all())
    assert INDEX in plan, plan
