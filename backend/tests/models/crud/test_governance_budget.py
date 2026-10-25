"""Funding-aware spend boundaries and finalization transaction rollback."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from time import perf_counter

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_governance_review_run,
    crud_governance_review_settings,
)
from preloop.models.crud.governance_budget import finalization_transaction, run_spend
from preloop.models.crud.governance_budget import _run_spend_query


def test_weekly_spend_with_500k_rows_uses_generic_index_within_default_timeout(
    db_session: Session, test_user: models.User
) -> None:
    """Both weekly guard reads must remain indexed under the worker's 30s limit."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    run_id = uuid4()
    # Bound fixture loading too: a cached trigger plan previously hung here
    # before the read timeout below was installed, consuming the CI job limit.
    db_session.execute(text("SET LOCAL statement_timeout = '120s'"))
    db_session.execute(
        text("""
        INSERT INTO api_usage
            (id, account_id, timestamp, endpoint, method, status_code,
             duration, action_type, estimated_cost, meta_data)
        SELECT gen_random_uuid(), :account, :when, 'weekly-plan', 'POST',
               200, 0, 'model_gateway', 0.001,
               CASE WHEN n <= 1000 THEN
                 jsonb_build_object('purpose', 'governance_session_digest',
                   'funding', CASE WHEN n % 2 = 0 THEN 'platform' ELSE 'account' END)
                 || CASE WHEN n <= 100 THEN
                    jsonb_build_object('governance_run_id', CAST(:run AS text))
                    ELSE '{}'::jsonb END
               ELSE '{"funding":"platform"}'::jsonb END
        FROM generate_series(1, 500000) n
        """),
        {"account": test_user.account_id, "when": start, "run": str(run_id)},
    )
    db_session.execute(text("ANALYZE api_usage"))
    db_session.execute(text("SET LOCAL statement_timeout = '30s'"))
    db_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    for window, expected in [(start, 0.5), (end, 0.05)]:
        query = _run_spend_query(
            db_session,
            account_id=test_user.account_id,
            window_start=window,
            window_end=end,
            run_id=run_id,
        )
        compiled = query.statement.compile(
            dialect=postgresql.dialect(paramstyle="numeric_dollar"),
            compile_kwargs={"render_postcompile": True},
        )
        db_session.connection().exec_driver_sql(
            "PREPARE weekly_spend_probe AS " + str(compiled)
        )
        values = {
            f"p{i}": compiled.params[name]
            for i, name in enumerate(compiled.positiontup or [])
        }
        invocation = text(
            "EXPLAIN (COSTS OFF) EXECUTE weekly_spend_probe ("
            + ", ".join(":" + name for name in values)
            + ")"
        ).bindparams(**values)
        explain = invocation.compile(
            dialect=db_session.get_bind().dialect,
            compile_kwargs={"literal_binds": True},
        )
        try:
            plan = db_session.execute(text(str(explain))).all()
        finally:
            db_session.execute(text("DEALLOCATE weekly_spend_probe"))
        plan_text = "\n".join(row[0] for row in plan)
        assert "ix_api_usage_governance_spend" in plan_text
        started = perf_counter()
        assert run_spend(
            db_session,
            account_id=test_user.account_id,
            window_start=window,
            window_end=end,
            run_id=run_id,
        ) == pytest.approx({"platform": expected, "account": expected})
        print(f"Weekly spend ({window.isoformat()}): {perf_counter() - started:.3f}s")
        print(plan_text)


def test_spend_filters_funding_purpose_account_and_window(db_session, test_user):
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    run_id = uuid4()
    for funding, purpose, timestamp, cost, extra in [
        ("platform", "governance_session_digest", start, 0.04, {}),
        ("platform", "governance_review", end - timedelta(seconds=1), 0.07, {}),
        ("account", "governance_review", start, 2.0, {}),
        (None, "governance_review", start, 9.0, {}),
        ("platform", "session_title", start, 9.0, {}),
        ("platform", "governance_review", end, 9.0, {}),
        ("platform", "governance_review", start - timedelta(seconds=1), 9.0, {}),
        (
            "platform",
            "governance_review",
            end,
            0.02,
            {"governance_run_id": str(run_id)},
        ),
        (
            "platform",
            "governance_review",
            start,
            0.01,
            {"governance_run_id": str(run_id)},
        ),
    ]:
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                action_type="model_gateway",
                endpoint="/governance",
                method="POST",
                status_code=200,
                duration=0.1,
                timestamp=timestamp,
                estimated_cost=cost,
                meta_data={"funding": funding, "purpose": purpose, **extra},
            )
        )
    db_session.add(
        models.ApiUsage(
            account_id=test_user.account_id,
            action_type="model_gateway",
            endpoint="/governance",
            method="POST",
            status_code=200,
            duration=0.1,
            timestamp=start,
            estimated_cost=5,
            meta_data={
                "funding": "platform",
                "purpose": "governance_review",
                "governance_run_id": str(uuid4()),
            },
        )
    )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "other budget account"}, commit=False
    )
    db_session.add(
        models.ApiUsage(
            account_id=other.id,
            action_type="model_gateway",
            endpoint="/governance",
            method="POST",
            status_code=200,
            duration=0.1,
            timestamp=start,
            estimated_cost=100,
            meta_data={
                "funding": "platform",
                "purpose": "governance_review",
                "governance_run_id": str(run_id),
            },
        )
    )
    db_session.flush()
    assert run_spend(
        db_session,
        account_id=test_user.account_id,
        window_start=start,
        window_end=end,
        run_id=run_id,
    ) == pytest.approx({"platform": 0.14, "account": 2.0})


def test_finalization_rolls_back_mode_and_run_together(db_engine, monkeypatch):
    monkeypatch.setenv("PRELOOP_HOSTED", "true")
    with Session(db_engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": "budget rollback"}
        )
        account_id = account.id
        start = datetime(2026, 10, 5, tzinfo=timezone.utc)
        run = crud_governance_review_run.create_run(
            db,
            account_id=account_id,
            window_start=start,
            window_end=start + timedelta(days=7),
        )
        try:
            with pytest.raises(RuntimeError, match="delivery failed"):
                with finalization_transaction(
                    db, account_id=account_id, run_id=run.id
                ) as (settings, locked):
                    crud_governance_review_settings.set_mode(
                        db, account_id=account_id, mode="off_over_budget", commit=False
                    )
                    crud_governance_review_run.update(
                        db, db_obj=locked, obj_in={"platform_cost": 0.11}, commit=False
                    )
                    raise RuntimeError("delivery failed")
            db.expire_all()
            assert (
                crud_governance_review_settings.get_for_account(
                    db, account_id=account_id
                )
                is None
            )
            assert crud_governance_review_run.get(db, run.id).platform_cost == 0
        finally:
            crud_account.delete(db, id=account_id)


def test_finalization_commits_mode_run_and_audit_together(db_engine, monkeypatch):
    from preloop.models.crud import crud_audit_log

    monkeypatch.setenv("PRELOOP_HOSTED", "true")
    with Session(db_engine) as db:
        account = crud_account.create(db, obj_in={"organization_name": "budget commit"})
        account_id = account.id
        start = datetime(2026, 10, 5, tzinfo=timezone.utc)
        run = crud_governance_review_run.create_run(
            db,
            account_id=account_id,
            window_start=start,
            window_end=start + timedelta(days=7),
        )
        run_id = run.id
        try:
            with finalization_transaction(db, account_id=account_id, run_id=run_id) as (
                _,
                locked,
            ):
                crud_governance_review_settings.set_mode(
                    db, account_id=account_id, mode="off_over_budget", commit=False
                )
                crud_governance_review_run.update(
                    db, db_obj=locked, obj_in={"platform_cost": 0.11}, commit=False
                )
                crud_audit_log.log_action(
                    db,
                    account_id=account_id,
                    action="governance_review.mode_changed",
                    resource_type="governance_review_settings",
                    status="success",
                    commit=False,
                )
            with Session(db_engine) as reader:
                settings = crud_governance_review_settings.get_for_account(
                    reader, account_id=account_id
                )
                assert settings.mode == "off_over_budget"
                assert (
                    crud_governance_review_run.get(reader, run_id).platform_cost == 0.11
                )
                assert (
                    len(
                        crud_audit_log.get_by_account(
                            reader,
                            account_id=account_id,
                            action="governance_review.mode_changed",
                        )
                    )
                    == 1
                )
        finally:
            crud_account.delete(db, id=account_id)
