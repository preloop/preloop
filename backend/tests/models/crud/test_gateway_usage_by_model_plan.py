"""The customer gateway aggregates can use covering indexes in any plan.

``GET /ai-models/overview`` aggregates a 30-day window per model through
``get_gateway_usage_by_model``. ``ix_api_usage_customer_totals`` covers every
column it reads, but PostgreSQL only uses a partial index whose predicate it
can prove from the query's constants. psycopg prepares a statement after a few
executions, and a generic prepared plan sees bound parameters, not constants:
with ``action_type = $1`` and ``meta_data ->> $2 <> $3`` the covering index is
unusable and every request reads the heap. The assertion is the plan.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage

# The values a caller varies: these stay parameters in the generic plan.
VARIABLE = ("account_id", "timestamp", "ai_model_id", "action_type", "cost_source")
COVERING = ("ix_api_usage_customer_totals", "ix_api_usage_customer_model_totals")
ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
MODELS = [f"00000000-0000-0000-0000-0000000000b{n}" for n in range(3)]


def _captured_select(
    db_session: Session, method: str = "get_gateway_usage_by_model", **kwargs
) -> tuple[str, dict]:
    statements: list[tuple[str, dict]] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append((statement, parameters))

    bind = db_session.get_bind()
    end = datetime.now(timezone.utc)
    event.listen(bind, "before_cursor_execute", _record)
    try:
        getattr(crud_api_usage, method)(
            db_session,
            account_id=ACCOUNT,
            **(
                {"start": end - timedelta(days=30), "end": end}
                if method == "get_subscription_usage_summary"
                else {"start_date": end - timedelta(days=30), "end_date": end}
            ),
            **kwargs,
        )
    finally:
        event.remove(bind, "before_cursor_execute", _record)
    assert statements, "aggregate was not issued"
    return statements[0]


def _generic_plan(db_session: Session, statement: str, parameters: dict) -> str:
    """EXPLAIN the statement as a generic prepared plan, other indexes hidden.

    Named ``%(name)s`` placeholders become ``$n`` so PostgreSQL plans it the
    way it plans a prepared statement, whichever driver runs the tests. Only
    the covering indexes and a sequential scan stay available, so the plan
    shows whether their predicate can be matched at all. The test
    transaction rolls the catalogue change back.
    """
    numbers: dict[str, int] = {}

    def _placeholder(match: re.Match) -> str:
        name = match.group(1)
        if not name.startswith(VARIABLE):
            # Constants such as the 0 of a COALESCE: a generic plan cannot
            # type an untyped parameter inside SUM, and they decide nothing.
            value = parameters[name]
            assert isinstance(value, (int, float)) or value in (
                "day",
                "api_equivalent_cost",
                "number",
                "unknown",
            ), (name, value)
            return repr(value)
        number = numbers.setdefault(name, len(numbers) + 1)
        return f"${number}"

    generic = re.sub(r"%\(([^)]+)\)s", _placeholder, statement)
    assert any(name.startswith("timestamp") for name in numbers), numbers
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    db_session.execute(
        text(
            """
            UPDATE pg_index AS i SET indisvalid = false
            FROM pg_class AS c
            WHERE c.oid = i.indexrelid
              AND i.indrelid = 'api_usage'::regclass
              AND NOT (c.relname = ANY(:keep))
            """
        ),
        {"keep": list(COVERING)},
    )
    rows = db_session.connection().exec_driver_sql("EXPLAIN (GENERIC_PLAN) " + generic)
    return "\n".join(row[0] for row in rows)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ai_model_ids": MODELS},
        {
            "ai_model_ids": MODELS,
            "failed_since": {MODELS[0]: datetime(2026, 1, 1, tzinfo=timezone.utc)},
        },
    ],
    ids=["account", "model_ids", "failed_since"],
)
def test_generic_plan_is_index_only_on_the_covering_index(db_session, kwargs):
    """Prepared statements keep the aggregate off the heap."""
    statement, parameters = _captured_select(db_session, **kwargs)
    plan = _generic_plan(db_session, statement, parameters)
    assert any(f"Index Only Scan using {index}" in plan for index in COVERING), plan


def test_index_predicate_is_spelled_with_literals(db_session):
    """The partial-index predicate reaches PostgreSQL as constants."""
    statement, parameters = _captured_select(db_session, ai_model_ids=MODELS)
    assert "api_usage.action_type = 'model_gateway'" in statement
    assert "(api_usage.meta_data ->> 'purpose') IS NULL" in statement
    assert "!= 'replay_validation'" in statement
    assert not any(
        value in ("model_gateway", "purpose", "replay_validation")
        for value in parameters.values()
    )


def test_model_keyed_covering_index_is_built_valid(db_session):
    """The model-keyed covering index exists, is valid, and has the predicate."""
    row = db_session.execute(
        text(
            """
            SELECT i.indisvalid, pg_get_indexdef(i.indexrelid)
            FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = 'ix_api_usage_gateway_model_totals'
            """
        )
    ).one()
    valid, definition = row
    assert valid is True
    assert '(account_id, ai_model_id, "timestamp")' in definition
    for column in ("estimated_cost", "cache_creation_tokens", "status_code"):
        assert column in definition, definition
    assert "replay_validation" in definition


@pytest.mark.parametrize(
    "method",
    [
        "get_gateway_usage_summary",
        "get_gateway_usage_timeseries",
        "get_gateway_usage_by_flow",
        "get_unpriced_model_breakdown",
    ],
)
def test_cost_headline_and_timeseries_use_customer_covering_index(
    db_session: Session, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prepared Cost aggregates can exclude overhead off the heap."""
    if method == "get_gateway_usage_by_flow":
        # The model-keyed index intentionally omits flow_id. On an empty
        # fixture its heap scan can cost less than the account index-only scan;
        # isolate the index that actually covers this query to test eligibility.
        monkeypatch.setattr(
            sys.modules[__name__], "COVERING", ("ix_api_usage_customer_totals",)
        )
    statement, parameters = _captured_select(db_session, method=method)
    plan = _generic_plan(db_session, statement, parameters)
    assert any(f"Index Only Scan using {index}" in plan for index in COVERING), plan


def test_old_replay_covering_index_requires_heap_for_governance_filter(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older covering index cannot filter governance without reading metadata."""
    monkeypatch.setattr(
        sys.modules[__name__],
        "COVERING",
        ("ix_api_usage_gateway_totals", "ix_api_usage_gateway_model_totals"),
    )
    statement, parameters = _captured_select(db_session)
    plan = _generic_plan(db_session, statement, parameters)
    assert "Index Only Scan" not in plan, plan
    assert "governance_" in plan, plan


def test_subscription_totals_use_sparse_index_in_generic_plan(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading absent subscription usage must not scan all gateway history."""
    monkeypatch.setattr(
        sys.modules[__name__], "COVERING", ("ix_api_usage_gateway_subscription",)
    )
    statement, parameters = _captured_select(
        db_session, method="get_subscription_usage_summary"
    )
    plan = _generic_plan(db_session, statement, parameters)
    assert "Index Scan using ix_api_usage_gateway_subscription" in plan, plan
