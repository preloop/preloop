"""The agent usage triggers plan each statement for its own transition table.

PL/pgSQL caches a statement's plan for the session. The planner sizes a
transition table from its row count when it plans, so a plan cached by small
inserts kept a nested loop between ``inserted_usage`` and the claimed ids. The
next bulk insert on that connection then ran quadratically: 500k rows did not
finish in twelve minutes. The trigger functions run their statements through
``EXECUTE`` so every call is planned for the rows it actually has.
"""

from __future__ import annotations

from time import perf_counter
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
UPDATE = "UPDATE api_usage SET status_code = 500 WHERE endpoint = 'replan'"
DELETE = "DELETE FROM api_usage WHERE endpoint = 'replan'"

# Eight times the rows costs about eight times as long with a plan made for
# the batch, and about 64 times with the nested loop a small-batch plan keeps
# (measured locally: 6-12x against 32-53x at these sizes). Comparing the two
# sizes on the same connection cancels out how fast the runner is, which an
# absolute timeout on one large batch did not.
SMALL = 1000
LARGE = 8 * SMALL
MAX_RATIO = 24
ATTEMPTS = 3


def _totals(db_session: Session, account: Any) -> tuple[int, int]:
    """Return the account's summed request and error counts."""
    row = db_session.execute(
        text(
            "SELECT COALESCE(SUM(request_count), 0), "
            "COALESCE(SUM(error_count), 0) FROM agent_usage_total "
            "WHERE account_id = :account"
        ),
        {"account": account.id},
    ).one()
    return int(row[0]), int(row[1])


def _timed(db_session: Session, sql: str, **params: Any) -> float:
    """Run one statement and return its wall-clock duration in seconds."""
    started = perf_counter()
    db_session.execute(text(sql), params)
    return perf_counter() - started


def _cycle(db_session: Session, account: Any, rows: int) -> dict[str, float]:
    """Insert, fail and delete ``rows`` usage rows, checking the roll-up.

    Args:
        db_session: Session whose connection holds the warmed trigger plans.
        account: Account the usage rows belong to.
        rows: Number of rows each statement touches.

    Returns:
        Seconds taken by each of the insert, update and delete statements.
    """
    base = _totals(db_session, account)
    took = {"insert": _timed(db_session, INSERT, account=account.id, rows=rows)}
    assert _totals(db_session, account) == (base[0] + rows, base[1])
    took["update"] = _timed(db_session, UPDATE)
    assert _totals(db_session, account) == (base[0] + rows, base[0] + rows)
    took["delete"] = _timed(db_session, DELETE)
    assert _totals(db_session, account) == (0, 0)
    return took


def test_bulk_insert_after_small_inserts_is_not_quadratic(
    db_session: Session, create_account: Callable[..., Any]
) -> None:
    """Bulk statements after small ones scale linearly through every trigger."""
    account = create_account()
    # Enough calls for any cached plan to settle on the small-batch shape.
    for _ in range(20):
        db_session.execute(text(INSERT), {"account": account.id, "rows": 1})
    # Warm the update and delete triggers with single-row statements too.
    small = "SELECT id FROM api_usage WHERE endpoint = 'replan' LIMIT 1"
    for _ in range(20):
        db_session.execute(
            text(f"UPDATE api_usage SET duration = 1 WHERE id = ({small})")
        )
    for _ in range(10):
        db_session.execute(text(f"DELETE FROM api_usage WHERE id = ({small})"))
    db_session.execute(text(DELETE))
    assert _totals(db_session, account) == (0, 0)
    # Alternate the sizes and keep each one's fastest run, so a burst of load
    # on a shared runner has to hit every large attempt to skew the ratio.
    fastest: dict[int, dict[str, float]] = {}
    for _ in range(ATTEMPTS):
        for rows in (SMALL, LARGE):
            took = _cycle(db_session, account, rows)
            best = fastest.setdefault(rows, took)
            for name, seconds in took.items():
                best[name] = min(best[name], seconds)
    ratios = {
        name: fastest[LARGE][name] / fastest[SMALL][name] for name in fastest[SMALL]
    }
    assert all(ratio < MAX_RATIO for ratio in ratios.values()), ratios


def test_replanned_bodies_wrap_every_statement() -> None:
    """The derived bodies keep each statement of the frozen 20261109 DDL."""
    import importlib

    replan = importlib.import_module(
        "preloop.models.alembic.versions.20261112_agent_usage_replan"
    )
    counts = {}
    for name in replan.FUNCTIONS:
        original = replan._original(name)
        body = replan._replanned(original)
        counts[name] = body.count("EXECUTE $stmt$")
        assert body.count("$stmt$") == 2 * counts[name]
        assert body.endswith(" RETURN NULL;\nEND $$;")
        unwrapped = body.replace("EXECUTE $stmt$", "").replace("$stmt$", "")
        assert unwrapped.split() == original.split()
    assert counts == {
        "agent_usage_track_insert": 1,
        "agent_usage_track_delete": 3,
        "agent_usage_track_update": 4,
    }
