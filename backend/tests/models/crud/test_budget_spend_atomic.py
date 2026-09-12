"""Priced usage and its rollups must share one short PostgreSQL transaction."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_api_usage
from preloop.models.crud import budget


@pytest.fixture
def rollup_account(db_engine: Engine) -> Generator[UUID, None, None]:
    """Committed synthetic account visible across independent sessions."""
    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": f"rollup-{uuid4()}", "is_active": True}
        )
        account_id = account.id
    yield account_id
    with Session(db_engine) as db:
        for model in (models.BudgetSpendActivity, models.ApiUsage):
            db.execute(delete(model).where(model.account_id == account_id))
        db.execute(delete(models.Account).where(models.Account.id == account_id))
        db.commit()


def record_usage(db: Session, account_id: UUID) -> models.ApiUsage:
    return crud_api_usage.log_gateway_request(
        db,
        account_id=str(account_id),
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        model_alias="synthetic-priced-model",
        estimated_cost=0.25,
    )


def test_priced_usage_commits_once(db_engine: Engine, rollup_account: UUID) -> None:
    """One usage fact and its 12 account rollups use one INSERT and commit."""
    commits: list[None] = []
    inserts: list[str] = []

    def count_insert(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.startswith("INSERT INTO budget_spend_activities"):
            inserts.append(statement)

    with Session(db_engine) as db:
        event.listen(db, "after_commit", lambda session: commits.append(None))
        event.listen(db_engine, "before_cursor_execute", count_insert)
        try:
            record_usage(db, rollup_account)
        finally:
            event.remove(db_engine, "before_cursor_execute", count_insert)
    assert len(commits) == 1
    assert len(inserts) == 1
    with Session(db_engine) as db:
        rows = db.scalars(
            select(models.BudgetSpendActivity).where(
                models.BudgetSpendActivity.account_id == rollup_account
            )
        ).all()
        assert len(rows) == 12
        assert {row.spend_usd for row in rows} == {0.25}


def test_failure_before_outer_commit_leaves_no_partial_usage_or_rollups(
    db_engine: Engine, rollup_account: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later bookkeeping failure cannot publish an already-committed prefix."""
    original = budget.record_spend_for_request

    def fail_after_rollups(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("synthetic failure before final usage commit")

    monkeypatch.setattr(budget, "record_spend_for_request", fail_after_rollups)
    with Session(db_engine) as db:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            record_usage(db, rollup_account)
        db.rollback()
    with Session(db_engine) as db:
        for model in (models.ApiUsage, models.BudgetSpendActivity):
            assert (
                db.scalars(
                    select(model).where(model.account_id == rollup_account)
                ).all()
                == []
            )


def test_concurrent_scope_rollups_accumulate_without_duplicates(
    db_engine: Engine, rollup_account: UUID
) -> None:
    """Opposite input scope orders must lock consistently and retain increments."""
    workers = 8
    barrier = Barrier(workers)
    api_key, agent, owner = (str(uuid4()) for _ in range(3))
    scopes = [("api_key", api_key), ("managed_agent", agent), ("user", owner)]
    timestamp = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)

    def record(index: int) -> None:
        with Session(db_engine) as db:
            barrier.wait(timeout=10)
            budget.record_spend_for_request(
                db,
                account_id=rollup_account,
                subject_type="api_key",
                subject_id=api_key,
                model_alias="synthetic-priced-model",
                estimated_cost=0.125,
                timestamp=timestamp,
                subject_scopes=scopes if index % 2 else list(reversed(scopes)),
            )
            db.commit()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(record, range(workers)))
    with Session(db_engine) as db:
        rows = db.scalars(
            select(models.BudgetSpendActivity).where(
                models.BudgetSpendActivity.account_id == rollup_account
            )
        ).all()
        assert len(rows) == 4 * 2 * len(models.BudgetPeriod)
        assert {row.spend_usd for row in rows} == {1.0}
        assert sum(row.period_start is None for row in rows) == 8


def test_rollup_helper_leaves_commit_to_caller(
    db_engine: Engine, rollup_account: UUID
) -> None:
    with Session(db_engine) as db:
        budget.record_spend_for_request(
            db,
            account_id=rollup_account,
            subject_type="api_key",
            subject_id=str(uuid4()),
            model_alias="synthetic-priced-model",
            estimated_cost=0.25,
            timestamp=datetime.now(timezone.utc),
        )
        db.rollback()
    with Session(db_engine) as db:
        assert (
            db.scalars(
                select(models.BudgetSpendActivity).where(
                    models.BudgetSpendActivity.account_id == rollup_account
                )
            ).all()
            == []
        )
