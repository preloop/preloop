"""The agents list reads each principal's usage from the right indexes.

``GET /agents`` sums each listed principal's gateway usage and reads its
latest request. With only ``ix_api_usage_account_principal_ts`` every usage
row was a heap fetch: 175k buffers and 2.2 s on a cold prod cache (#1694).
``ix_api_usage_principal_totals`` carries the summed columns, so both LATERAL
reads must plan as index-only scans. The assertion is the plan.
Subscription totals use a separate partial index so each principal does not
filter its entire gateway history from heap rows.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from preloop.models.crud.managed_agent import _usage_aggregates_for_principals

INDEX = "ix_api_usage_principal_totals"
SUBSCRIPTION_INDEX = "ix_api_usage_principal_subscription"
ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
PRINCIPALS = [("claude_code", "principal-1"), ("codex", "principal-2")]


def _captured_usage_select(
    db_session: Session, account_id: str = ACCOUNT
) -> tuple[str, dict]:
    statements: list[tuple[str, dict]] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "principal_usage_totals" in statement:
            statements.append((statement, parameters))

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", _record)
    try:
        _usage_aggregates_for_principals(
            db_session,
            account_id=account_id,
            principals=PRINCIPALS,
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
        )
    finally:
        event.remove(bind, "before_cursor_execute", _record)
    assert len(statements) == 1, statements
    return statements[0]


def _literal(value: object) -> str:
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def test_gateway_predicate_is_a_literal(db_session: Session) -> None:
    """Prepared statements can match the partial index predicate."""
    statement, parameters = _captured_usage_select(db_session)
    assert "api_usage.action_type = 'model_gateway'" in statement
    assert "model_gateway" not in parameters.values()


def test_principal_totals_plan_is_index_only(
    db_session: Session, create_account: Callable[..., Any]
) -> None:
    """The planner picks the covering index over every valid competitor.

    All ``api_usage`` indexes stay valid, including the principal-keyed
    ``ix_api_usage_account_principal_ts`` and ``..._principal_id_ts``, on a
    populated, analysed table; only sequential and bitmap scans are off.
    """
    account = create_account()
    db_session.execute(
        text(
            """
            INSERT INTO api_usage (
                id, account_id, endpoint, method, status_code, duration,
                action_type, runtime_principal_type, runtime_principal_id,
                cost_source,
                prompt_tokens, completion_tokens, total_tokens,
                estimated_cost, model_alias, provider_name, timestamp
            )
            SELECT gen_random_uuid(), :account, '/v1/messages', 'POST',
                   CASE WHEN n % 20 = 0 THEN 500 ELSE 200 END, 0.1,
                   CASE WHEN n % 5 = 0 THEN 'api_request' ELSE 'model_gateway' END,
                   (ARRAY['claude_code', 'codex', 'flow_execution'])[n % 3 + 1],
                   'principal-' || (n % 7),
                   CASE WHEN n % 4 = 0 THEN 'subscription' ELSE NULL END,
                   10, 5, 15, 0.001, 'model', 'provider',
                   now() - n * interval '1 minute'
            FROM generate_series(1, 5000) AS n
            """
        ),
        {"account": str(account.id)},
    )
    db_session.execute(text("ANALYZE api_usage"))
    statement, parameters = _captured_usage_select(db_session, str(account.id))
    inlined = re.sub(
        r"%\(([^)]+)\)s", lambda m: _literal(parameters[m.group(1)]), statement
    )
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = db_session.connection().exec_driver_sql("EXPLAIN " + inlined)
    plan = "\n".join(row[0] for row in rows)
    assert plan.count(f"Index Only Scan using {INDEX}") == 2, plan
    assert f"Index Scan using {SUBSCRIPTION_INDEX}" in plan, plan
    subscription = plan.split(f"Index Scan using {SUBSCRIPTION_INDEX}", 1)[1]
    assert "Index Cond:" in subscription, plan
    assert "runtime_principal_type" in subscription.split("Index Cond:", 1)[1], plan
    assert "runtime_principal_id" in subscription.split("Index Cond:", 1)[1], plan

    # The app also uses prepared statements. Their generic plan must still
    # recognize the literal partial-index predicates, even with unknown
    # account and principal parameter values.
    placeholders: dict[str, int] = {}
    generic_sql = re.sub(
        r"%\(([^)]+)\)s",
        lambda match: f"${placeholders.setdefault(match.group(1), len(placeholders) + 1)}",
        statement,
    )
    generic_plan = "\n".join(
        row[0]
        for row in db_session.connection().exec_driver_sql(
            "EXPLAIN (GENERIC_PLAN TRUE) " + generic_sql
        )
    )
    assert f"Index Scan using {SUBSCRIPTION_INDEX}" in generic_plan, generic_plan
