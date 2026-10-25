"""The agent usage triggers plan each statement for its own transition table.

PL/pgSQL caches a statement's plan for the session. The planner sizes a
transition table from its row count when it plans, so a plan cached by small
inserts kept a nested loop between ``inserted_usage`` and the claimed ids. The
next bulk insert on that connection then ran quadratically: 500k rows did not
finish in twelve minutes. The trigger functions run their statements through
``EXECUTE`` so every call is planned for the rows it actually has.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

INSERT = """
INSERT INTO api_usage
    (id, account_id, timestamp, endpoint, method, status_code,
     duration, action_type, estimated_cost, meta_data)
SELECT gen_random_uuid(), :account, now(), 'replan', 'POST', 200, 0,
       'model_gateway', 0.001, '{}'::jsonb
FROM generate_series(1, :rows) n
"""


def test_bulk_insert_after_small_inserts_is_not_quadratic(
    db_session: Session, create_account: Callable[..., Any]
) -> None:
    account = create_account()
    # Enough calls for any cached plan to settle on the small-batch shape.
    for _ in range(20):
        db_session.execute(text(INSERT), {"account": account.id, "rows": 1})
    db_session.execute(text("SET LOCAL statement_timeout = '20s'"))
    db_session.execute(text(INSERT), {"account": account.id, "rows": 60000})
    db_session.execute(
        text("UPDATE api_usage SET status_code = 500 WHERE endpoint = 'replan'")
    )
    total = db_session.execute(
        text(
            "SELECT request_count, error_count FROM agent_usage_total "
            "WHERE account_id = :account"
        ),
        {"account": account.id},
    ).one()
    assert tuple(total) == (60020, 60020)
    db_session.execute(text("DELETE FROM api_usage WHERE endpoint = 'replan'"))
    remaining = db_session.execute(
        text(
            "SELECT COALESCE(SUM(request_count), 0) FROM agent_usage_total "
            "WHERE account_id = :account"
        ),
        {"account": account.id},
    ).scalar_one()
    assert remaining == 0
