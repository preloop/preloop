#!/usr/bin/env python3
"""Benchmark the account gateway-usage summary query shapes.

Seeds a synthetic ``api_usage`` dataset with the shape named in issue #914
(200k rows over 400 days, 10 runtime principals, 16 models, 400 sessions),
then measures the FastAPI ``TestClient`` latency of the account summary for
7d/30d/1y windows, a one-year per-user window, and the totals-only variant.
It also captures ``EXPLAIN (ANALYZE, BUFFERS)`` plans for the two CRUD
queries the issue calls out:

* ``get_gateway_usage_by_session`` (aggregation-first session breakdown)
* ``get_gateway_usage_timeseries`` (daily buckets without the external sort)

This is a local measurement harness, not a test. It writes only through
``DATABASE_URL``; it never drops tables, and it only ever deletes rows
belonging to the account it created. Run it against a disposable database.

    PRELOOP_DISABLE_TELEMETRY=true \\
    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/preloop \\
        python scripts/perf/benchmark_gateway_usage.py \\
            --output scripts/perf/results/latest.json

Set ``--rows 3000 --days 30`` for a quick smoke run. Pass ``--cleanup`` to
delete the seeded account and its rows when the run finishes.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.orm import Query  # noqa: E402
from sqlalchemy import text  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from preloop.api.app import create_app  # noqa: E402
from preloop.api.auth import get_current_active_user  # noqa: E402
from preloop.models import models  # noqa: E402
from preloop.models.crud import (  # noqa: E402
    crud_account,
    crud_ai_model,
    crud_api_usage,
    crud_runtime_session,
    crud_user,
)
from preloop.models.db.session import get_session_factory  # noqa: E402

PERF_ACCOUNT_NAME = "Perf Benchmark Account (issue #914)"
SUMMARY_PATH = "/api/v1/account/gateway-usage/summary"


def _git_revision() -> str | None:
    """Return the current git revision, or None when not in a checkout."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _summarize(samples: list[float]) -> dict[str, Any]:
    """Turn raw millisecond samples into the issue's first/warm/min/max stats."""
    first, *warm = samples
    return {
        "first_ms": round(first, 3),
        "warm_median_ms": round(statistics.median(warm), 3) if warm else None,
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def _seed(session, args: argparse.Namespace) -> Any:
    """Create the disposable perf account and populate its usage rows.

    Returns the account id. The dataset is deterministic in shape (always the
    same row/day/user/model/session counts) even though ids and timestamps are
    generated per run.
    """
    account = crud_account.create(
        session,
        obj_in={"organization_name": PERF_ACCOUNT_NAME, "is_active": True},
    )
    owner = crud_user.create(
        session,
        obj_in={
            "username": f"perf-benchmark-user-{account.id}",
            "email": f"perf-{account.id}@example.com",
            "account_id": account.id,
            "hashed_password": "not-used",
            "is_active": True,
        },
    )
    ai_models = [
        crud_ai_model.create_with_account(
            db=session,
            obj_in={
                "name": f"Perf Model {index}",
                "provider_name": "openai",
                "model_identifier": f"perf-model-{index}",
                "api_key": "perf-placeholder",
            },
            account_id=account.id,
        )
        for index in range(args.models)
    ]
    runtime_sessions = [
        crud_runtime_session.upsert_by_source(
            session,
            account_id=account.id,
            session_source_type="claude_code",
            session_source_id=f"perf-session-{index}",
            runtime_principal_type="managed_agent",
            runtime_principal_id=f"perf-agent-{index % args.users}",
            runtime_principal_name=f"Perf Agent {index % args.users}",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
        )
        for index in range(args.sessions)
    ]
    session.commit()

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    period_start = now - timedelta(days=args.days)
    span_seconds = args.days * 86400
    payload: list[dict[str, Any]] = []
    for index in range(args.rows):
        session_index = index % args.sessions
        model_index = index % args.models
        principal = f"perf-agent-{session_index % args.users}"
        prompt = 100 + (index % 900)
        completion = 50 + (index % 450)
        payload.append(
            {
                "account_id": account.id,
                "user_id": owner.id,
                "action_type": "model_gateway",
                "endpoint": "/openai/v1/chat/completions",
                "method": "POST",
                "status_code": 200 if index % 50 else 500,
                "duration": 0.1,
                "ai_model_id": ai_models[model_index].id,
                "runtime_session_id": runtime_sessions[session_index].id,
                "model_alias": f"perf-model-{model_index}",
                "provider_name": "openai",
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "estimated_cost": (prompt + completion) / 1_000_000 * 10.0,
                "runtime_principal_type": "managed_agent",
                "runtime_principal_id": principal,
                "runtime_principal_name": f"Perf Agent {principal.rsplit('-', 1)[-1]}",
                "timestamp": period_start
                + timedelta(seconds=(index * span_seconds) // max(args.rows, 1)),
            }
        )
        if len(payload) >= 5000:
            session.bulk_insert_mappings(models.ApiUsage, payload)
            payload.clear()
    if payload:
        session.bulk_insert_mappings(models.ApiUsage, payload)
    session.commit()
    # Bulk inserts leave stale planner statistics, which produce a misleading
    # plan and a misleading baseline. Refresh before any query is explained.
    for table in (
        "api_usage",
        "runtime_session",
        "managed_agent",
        "flow",
        "flow_execution",
        "ai_model",
    ):
        session.execute(text(f"ANALYZE {table}"))
    session.commit()
    return account.id


def _build_app(session, account_id: Any):
    """Return an app whose auth is pinned to a seeded user and normal DB deps."""
    owner = (
        session.query(models.User)
        .filter(models.User.account_id == account_id)
        .order_by(models.User.created_at.asc())
        .first()
    )
    if owner is None:
        raise RuntimeError("seeded account has no user to authenticate as")
    owner_id = owner.id

    app = create_app()

    def _current_user():
        return crud_user.get(session, id=owner_id)

    app.dependency_overrides[get_current_active_user] = _current_user
    return app


def _measure_window(
    client: TestClient,
    *,
    start_date: datetime,
    end_date: datetime,
    runtime_principal_id: str | None = None,
    include_breakdown: bool = True,
    warm_iterations: int = 5,
) -> dict[str, Any]:
    """Measure one summary window: first request plus warm iterations."""
    params: dict[str, Any] = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "include_breakdown": str(include_breakdown).lower(),
    }
    if runtime_principal_id is not None:
        params["runtime_principal_id"] = runtime_principal_id

    samples: list[float] = []
    for _ in range(1 + warm_iterations):
        started = time.perf_counter()
        response = client.get(SUMMARY_PATH, params=params)
        samples.append((time.perf_counter() - started) * 1000.0)
        if response.status_code != 200:
            raise RuntimeError(
                f"summary request failed ({response.status_code}): "
                f"{response.text[:500]}"
            )
    return _summarize(samples)


def _capture_query_sql(session, account_id: Any) -> dict[str, str]:
    """Capture SQL for the two CRUD queries the benchmark explains.

    Patching ``Query.all`` lets the real CRUD builder produce the SQL without
    touching the database, so the plans below match the shipped shape instead
    of a hand-copied approximation.
    """
    captured: dict[str, str] = {}
    dialect = postgresql.dialect()
    now = datetime.now(UTC)
    builders = {
        "session_breakdown": (
            crud_api_usage.get_gateway_usage_by_session,
            {"limit": 250},
        ),
        "timeseries": (crud_api_usage.get_gateway_usage_timeseries, {}),
    }

    def _fake_all(query: Query) -> list[Any]:
        captured["__pending__"] = str(
            query.statement.compile(
                dialect=dialect, compile_kwargs={"literal_binds": True}
            )
        )
        return []

    with patch.object(Query, "all", _fake_all):
        for key, (function, extra) in builders.items():
            captured.pop("__pending__", None)
            function(
                session,
                account_id=str(account_id),
                start_date=now - timedelta(days=365),
                end_date=now,
                **extra,
            )
            captured[key] = captured.pop("__pending__", "")
    return captured


def _explain(session, sql: str) -> str:
    """Run EXPLAIN (ANALYZE, BUFFERS) and return the plan text."""
    rows = session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}")
    ).fetchall()
    return "\n".join(str(row[0]) for row in rows)


def _cleanup(session, account_id: Any) -> None:
    """Delete the seeded account and its usage rows.

    Bulk-deletes the large ``api_usage`` table first so the account purge does
    not have to cascade through hundreds of thousands of rows, then purges the
    account and its owner users through the CRUD layer.
    """
    session.query(models.ApiUsage).filter(
        models.ApiUsage.account_id == account_id
    ).delete(synchronize_session=False)
    session.flush()
    crud_account.purge(session, account_id=str(account_id))


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and write the JSON results file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--models", type=int, default=16)
    parser.add_argument("--sessions", type=int, default=400)
    parser.add_argument("--warm-iterations", type=int, default=5)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "results" / "latest.json",
    )
    parser.add_argument(
        "--skip-explain",
        action="store_true",
        help="Skip the EXPLAIN (ANALYZE, BUFFERS) capture.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the seeded account and rows after the run.",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "Reuse an already-seeded perf account (from a prior run's JSON) "
            "instead of seeding a new one, so a code change can be compared "
            "against the exact same rows."
        ),
    )
    args = parser.parse_args(argv)

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    session = get_session_factory()()
    account_id: Any = None
    try:
        account_id = args.account_id or _seed(session, args)
        app = _build_app(session, account_id)
        now = datetime.now(UTC)
        windows = {
            "account_7d": (now - timedelta(days=7), now),
            "account_30d": (now - timedelta(days=30), now),
            "account_1y": (now - timedelta(days=365), now),
        }
        with TestClient(app) as client:
            timings: dict[str, Any] = {}
            for name, (start, end) in windows.items():
                timings[name] = _measure_window(
                    client,
                    start_date=start,
                    end_date=end,
                    warm_iterations=args.warm_iterations,
                )
            timings["account_1y_totals_only"] = _measure_window(
                client,
                start_date=now - timedelta(days=365),
                end_date=now,
                include_breakdown=False,
                warm_iterations=args.warm_iterations,
            )
            timings["per_user_1y"] = _measure_window(
                client,
                start_date=now - timedelta(days=365),
                end_date=now,
                runtime_principal_id=f"perf-agent-{args.users - 1}",
                warm_iterations=args.warm_iterations,
            )

        result: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "revision": _git_revision(),
            "account_id": str(account_id),
            "dataset": {
                "rows": args.rows,
                "days": args.days,
                "users": args.users,
                "models": args.models,
                "sessions": args.sessions,
            },
            "timings": timings,
            "thresholds": {
                "one_year_warm_budget_ms": 500.0,
                "one_year_warm_under_budget": (
                    timings["account_1y"]["warm_median_ms"] is not None
                    and timings["account_1y"]["warm_median_ms"] < 500.0
                ),
            },
        }

        if not args.skip_explain:
            queries = _capture_query_sql(session, account_id)
            result["explain"] = {
                "session_breakdown": _explain(session, queries["session_breakdown"]),
                "timeseries": _explain(session, queries["timeseries"]),
            }

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result["thresholds"], indent=2))
        print(f"wrote {args.output}")
        return 0
    finally:
        if args.cleanup and account_id is not None:
            try:
                _cleanup(session, account_id)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                print(f"cleanup failed: {exc}", file=sys.stderr)
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
