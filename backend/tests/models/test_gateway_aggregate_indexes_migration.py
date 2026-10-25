"""The gateway aggregate index migration (#1582, #1583) and the model-detail
index (#1688).

Pins that the model metadata declares these indexes (else autogenerate
proposes dropping them) and that the reads they exist for can use them.
"""

from datetime import datetime, timedelta, timezone

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from preloop.models.models.base import Base

INDEXES = (
    "ix_api_usage_gateway_totals",
    "ix_api_usage_gateway_subscription",
    "ix_api_usage_rate_limit_snapshot",
    "ix_api_usage_account_model_ts",
    "ix_api_usage_principal_totals",
)


def test_model_metadata_declares_the_indexes(db_session):
    """Autogenerate must not see a migrated index as one to drop.

    Alembic cannot compare DESC or expression columns, so such indexes show
    up as a remove + add pair of the same name (as every DESC index in this
    schema does). A remove without the matching add means the model lost it.
    """
    context = MigrationContext.configure(db_session.connection())
    diffs = compare_metadata(context, Base.metadata)
    ops: dict[str, set[str]] = {"add_index": set(), "remove_index": set()}
    for diff in diffs:
        for item in diff if isinstance(diff, list) else [diff]:
            if isinstance(item, tuple) and item[0] in ops:
                ops[item[0]].add(getattr(item[1], "name", None))
    for name in INDEXES:
        assert name not in ops["remove_index"] - ops["add_index"], name


def test_indexes_exist_and_are_valid(db_session):
    rows = db_session.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(:names)"
        ),
        {"names": list(INDEXES)},
    ).all()
    assert sorted(rows) == sorted((name, True) for name in INDEXES)


def _captured(db_session, call):
    from sqlalchemy import event

    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", _capture)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", _capture)
    return statements


def test_gateway_totals_read_only_covered_columns(db_session, test_user):
    """The window totals read only columns the covering index carries.

    An index-only scan needs every referenced column in the index. The plan
    itself also depends on the visibility map, which a rolled-back test
    transaction cannot set, so the coverage is pinned structurally: key and
    predicate columns plus GATEWAY_TOTALS_INCLUDE.
    """
    import re

    from preloop.models.crud import crud_api_usage
    from preloop.models.models.api_usage import GATEWAY_TOTALS_INCLUDE

    end = datetime.now(timezone.utc)
    statements = _captured(
        db_session,
        lambda: crud_api_usage.get_gateway_usage_summary(
            db_session,
            account_id=str(test_user.account_id),
            start_date=end - timedelta(days=30),
            end_date=end,
        ),
    )
    statement, _ = statements[-1]
    referenced = set(re.findall(r"\bapi_usage\.(\w+)", statement))
    key_and_predicate = {"account_id", "timestamp", "action_type", "meta_data"}
    assert referenced - key_and_predicate <= set(GATEWAY_TOTALS_INCLUDE), (
        referenced - key_and_predicate - set(GATEWAY_TOTALS_INCLUDE)
    )


def test_subscription_block_can_use_its_partial_index(db_session, test_user):
    """The Spend subscription block filters on the partial index predicate.

    Postgres uses ix_api_usage_gateway_subscription only when the query
    implies ``action_type = 'model_gateway' AND cost_source = 'subscription'``.
    Without it the block was a sequential scan of api_usage. The plan is not
    asserted: ANALYZE would be needed for a realistic one, and it updates
    pg_class in place, outside the test's rollback.
    """
    from preloop.models.crud import crud_api_usage

    end = datetime.now(timezone.utc)
    statements = _captured(
        db_session,
        lambda: crud_api_usage.get_subscription_usage_summary(
            db_session,
            account_id=str(test_user.account_id),
            start=end - timedelta(days=30),
            end=end,
        ),
    )
    statement, parameters = statements[-1]
    values = set(parameters.values()) if isinstance(parameters, dict) else set()
    # Fixed predicates must remain constants when psycopg prepares this
    # statement; bound values make the partial index unusable in generic plans.
    assert "api_usage.action_type = 'model_gateway'" in statement
    assert "api_usage.cost_source = 'subscription'" in statement
    assert not {"model_gateway", "subscription"} & values

    # And the index exists with exactly that predicate, so the planner can
    # prove the query implies it.
    indexdef = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_api_usage_gateway_subscription'"
        )
    ).scalar()
    assert "(action_type)::text = 'model_gateway'::text" in indexdef
    assert "(cost_source)::text = 'subscription'::text" in indexdef
    assert '(account_id, "timestamp")' in indexdef
