"""Unit tests for the post-deploy backfill runner (no database needed)."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from preloop.models import backfills
from preloop.models.backfills import run_pending_backfills


def _session(pending: list[str], batch_results: Iterator[object]) -> MagicMock:
    """A session whose registry lists ``pending`` and whose batches yield results."""
    db = MagicMock()

    def execute(statement: object, params: object = None) -> MagicMock:
        sql = str(statement)
        result = MagicMock()
        if "to_regclass" in sql:
            result.scalar.return_value = "agent_usage_backfill"
        elif "WHERE NOT ready" in sql:
            result.scalars.return_value = iter(pending)
        else:
            outcome = next(batch_results)
            if isinstance(outcome, Exception):
                raise outcome
            result.scalar_one.return_value = outcome
        return result

    db.execute.side_effect = execute
    return db


def test_runs_batches_until_ready_and_commits_each() -> None:
    db = _session(["agents"], iter([False, False, True]))
    (result,) = run_pending_backfills(db)
    assert (result.name, result.batches, result.ready) == ("agents", 3, True)
    # One commit for the registry read plus one per batch.
    assert db.commit.call_count == 4


def test_missing_registry_table_is_a_no_op() -> None:
    db = MagicMock()
    db.execute.return_value.scalar.return_value = None
    assert run_pending_backfills(db) == []
    assert db.execute.call_count == 1


def test_nothing_pending_runs_no_batch() -> None:
    db = _session([], iter([]))
    assert run_pending_backfills(db) == []


def test_unknown_registry_rows_are_ignored() -> None:
    db = _session(["retired-backfill"], iter([]))
    assert run_pending_backfills(db) == []


def test_batch_cap_leaves_the_backfill_pending() -> None:
    db = _session(["agents"], iter([False] * 10))
    (result,) = run_pending_backfills(db, max_batches=2)
    assert (result.batches, result.ready) == (2, False)


def test_time_budget_stops_the_pass() -> None:
    ticks = iter([0.0, 0.0, 5.0, 11.0])
    db = _session(["agents"], iter([False] * 10))
    (result,) = run_pending_backfills(
        db, time_budget_seconds=10, clock=lambda: next(ticks)
    )
    assert (result.batches, result.ready) == (2, False)


def test_failed_batch_rolls_back_and_is_reported() -> None:
    db = _session(["agents"], iter([False, RuntimeError("boom")]))
    (result,) = run_pending_backfills(db)
    assert (result.batches, result.ready, result.errors) == (1, False, ["boom"])
    db.rollback.assert_called_once()


def test_every_registered_backfill_reports_readiness() -> None:
    """A batch statement must return the registry row's new ``ready`` flag."""
    for name, statement in backfills.BACKFILLS.items():
        assert len(name) <= 32, "registry name column is varchar(32)"
        assert "RETURNING ready" in statement


@pytest.mark.parametrize("errors,code", [([], 0), (["boom"], 1)])
def test_cli_exit_code(
    monkeypatch: pytest.MonkeyPatch, errors: list[str], code: int
) -> None:
    session = MagicMock()
    monkeypatch.setattr(
        "preloop.models.db.session.get_db_session", lambda: iter([session])
    )
    monkeypatch.setattr(
        backfills,
        "run_pending_backfills",
        lambda db, **_: [backfills.BackfillResult("agents", errors=errors)],
    )
    assert backfills.main(["--time-budget", "1"]) == code
    session.close.assert_called_once()
