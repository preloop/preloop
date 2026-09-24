"""Local synthetic long-range benchmark using real FastAPI handlers and CRUD.

Run from the repository root:

    PYTHONPATH=backend PRELOOP_DISABLE_TELEMETRY=true \\
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/p2_368 \\
    python scripts/perf/benchmark_summary.py

Authentication is supplied by fixture dependencies. No server lifespan,
background workers, or external calls run. Direct SQL is confined to synthetic
fixture setup and EXPLAIN instrumentation.

``--phase before|after`` selects the results filename. ``--drop`` deletes the
synthetic account (and cascaded rows) so the test suite does not see it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from uuid import uuid4

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
os.environ["DISABLE_RBAC"] = "true"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert, text
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.endpoints.account import (
    get_account_gateway_usage_summary,
    list_account_managed_agents,
)
from preloop.api.endpoints.cost import get_cost_summary
from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.db.session import get_db_session
from preloop.schemas.cost_analytics import CostAnalyticsSummaryResponse
from preloop.schemas.gateway_usage import (
    AccountGatewayUsageSummaryResponse,
    AccountManagedAgentListResponse,
)

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(__file__).resolve().parent / "results"
ALLOWED_DATABASE = "p2_368"
PRINCIPAL = "synthetic-agent-0"


def _engine():
    url = os.environ.get("DATABASE_URL", "")
    engine = create_engine(url)
    if engine.url.host not in ("127.0.0.1", "localhost") or (
        engine.url.database != ALLOWED_DATABASE
    ):
        raise SystemExit(
            f"Only the disposable localhost {ALLOWED_DATABASE} database is allowed"
        )
    return engine


def _git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def seed(engine) -> dict[str, Any]:
    """Create 200k rows over 400 days, 10 owners, 16 aliases, 400 sessions."""
    fixture_path = ARTIFACTS / "summary-fixture.json"
    if fixture_path.exists():
        return json.loads(fixture_path.read_text())
    now = datetime.now(UTC).replace(tzinfo=None)
    account_id = uuid4()
    users = [uuid4() for _ in range(10)]
    sessions = [uuid4() for _ in range(400)]
    with Session(engine) as db:
        db.execute(
            insert(models.Account),
            [
                {
                    "id": account_id,
                    "organization_name": "Synthetic performance",
                    "is_active": True,
                    "email_verified": False,
                    "is_superuser": False,
                }
            ],
        )
        db.execute(
            insert(models.User),
            [
                {
                    "id": user_id,
                    "account_id": account_id,
                    "username": f"synthetic-{i}",
                    "email": f"synthetic-{i}@example.com",
                    "is_active": True,
                    "email_verified": False,
                    "user_source": "local",
                }
                for i, user_id in enumerate(users)
            ],
        )
        db.execute(
            insert(models.RuntimeSession),
            [
                {
                    "id": session_id,
                    "account_id": account_id,
                    "session_source_type": "custom",
                    "session_source_id": f"synthetic-agent-{i % 10}:session-{i}",
                    "runtime_principal_id": f"synthetic-agent-{i % 10}",
                    "runtime_principal_type": "custom",
                    "started_at": now - timedelta(days=400),
                    "last_activity_at": now,
                }
                for i, session_id in enumerate(sessions)
            ],
        )
        db.execute(
            insert(models.ManagedAgent),
            [
                {
                    "id": uuid4(),
                    "account_id": account_id,
                    "owner_user_id": user_id,
                    "runtime_session_id": sessions[i],
                    "agent_kind": "custom",
                    "session_source_type": "custom",
                    "session_source_id": f"synthetic-agent-{i}",
                    "display_name": f"Synthetic agent {i}",
                    "lifecycle_updated_at": now,
                    "last_seen_at": now,
                }
                for i, user_id in enumerate(users)
            ],
        )
        for day in range(400):
            rows = [
                {
                    "id": uuid4(),
                    "account_id": account_id,
                    "user_id": users[j % 10],
                    "runtime_session_id": sessions[(day * 10 + j % 10) % 400],
                    "runtime_principal_id": f"synthetic-agent-{j % 10}",
                    "runtime_principal_type": "custom",
                    "timestamp": now - timedelta(days=day, seconds=j + 1),
                    "endpoint": "/v1/chat/completions",
                    "method": "POST",
                    "status_code": 200,
                    "duration": 0.1,
                    "action_type": "model_gateway",
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "total_tokens": 1100,
                    "estimated_cost": 0.01,
                    "model_alias": f"example-model-{j % 16}",
                    "provider_name": "openai",
                    "meta_data": {
                        "tools_meta": [
                            {"name": "example_tool", "schema_tokens_estimate": 10}
                        ]
                    },
                }
                for j in range(500)
            ]
            db.execute(insert(models.ApiUsage), rows)
            if day % 50 == 49:
                db.commit()
                print(f"seeded {(day + 1) * 500} rows", flush=True)
        db.commit()
        db.execute(text("ANALYZE"))
        db.commit()
    fixture = {
        "account_id": str(account_id),
        "user_id": str(users[0]),
        "principal_id": PRINCIPAL,
        "now": now.isoformat(),
        "rows": 200000,
        "days": 400,
        "users": 10,
        "model_aliases": 16,
        "sessions": 400,
        "agents": 10,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(fixture, indent=2) + "\n")
    return fixture


def drop_fixture(engine) -> None:
    """Delete the synthetic account and its usage rows.

    ``api_usage.account_id`` is ON DELETE SET NULL, so removing the account
    alone would leave the ledger rows behind. Delete usage first.
    """
    fixture_path = ARTIFACTS / "summary-fixture.json"
    if not fixture_path.exists():
        print("no fixture file; nothing to drop", flush=True)
        return
    fixture = json.loads(fixture_path.read_text())
    account_id = fixture["account_id"]
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM api_usage WHERE account_id = :id"),
            {"id": account_id},
        )
        conn.execute(
            text("DELETE FROM managed_agent WHERE account_id = :id"),
            {"id": account_id},
        )
        conn.execute(
            text("DELETE FROM runtime_session WHERE account_id = :id"),
            {"id": account_id},
        )
        conn.execute(
            text('DELETE FROM "user" WHERE account_id = :id'),
            {"id": account_id},
        )
        conn.execute(
            text("DELETE FROM account WHERE id = :id"),
            {"id": account_id},
        )
    print(f"dropped synthetic account {account_id}", flush=True)


def _expected(days: int, per_user: bool) -> dict[str, float]:
    requests = days * (50 if per_user else 500)
    return {
        "total_requests": requests,
        "total_tokens": requests * 1100,
        "estimated_cost": requests * 0.01,
    }


def _is_session_sql(statement: str) -> bool:
    folded = " ".join(statement.split()).lower()
    return (
        "group by" in folded and "runtime_session" in folded and "api_usage" in folded
    )


def _is_timeseries_sql(statement: str) -> bool:
    folded = " ".join(statement.split()).lower()
    return "date_trunc" in folded and "api_usage" in folded


def main() -> None:
    """Measure first request plus five repeat samples, then EXPLAIN hot queries."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), default="before")
    parser.add_argument(
        "--drop",
        action="store_true",
        help="Delete the synthetic account and exit",
    )
    args = parser.parse_args()
    engine = _engine()
    if args.drop:
        drop_fixture(engine)
        return

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    fixture = seed(engine)
    now = datetime.fromisoformat(fixture["now"]).replace(tzinfo=UTC)
    app = FastAPI()
    app.add_api_route(
        "/api/v1/account/gateway-usage/summary",
        get_account_gateway_usage_summary,
        response_model=AccountGatewayUsageSummaryResponse,
    )
    app.add_api_route(
        "/api/v1/cost/summary",
        get_cost_summary,
        response_model=CostAnalyticsSummaryResponse,
    )
    app.add_api_route(
        "/api/v1/agents",
        list_account_managed_agents,
        response_model=AccountManagedAgentListResponse,
    )

    def session_dependency() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    def account_dependency() -> models.Account:
        with Session(engine) as db:
            return crud_account.get(db, id=fixture["account_id"])

    def user_dependency() -> models.User:
        with Session(engine) as db:
            return crud_user.get(db, id=fixture["user_id"])

    app.dependency_overrides[get_db_session] = session_dependency
    app.dependency_overrides[get_account_for_user] = account_dependency
    app.dependency_overrides[get_current_active_user] = user_dependency
    statements: list[dict[str, Any]] = []

    def before_cursor(
        conn: Any,
        cursor: Any,
        statement: str,
        params: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        context.benchmark_started = perf_counter()

    def after_cursor(
        conn: Any,
        cursor: Any,
        statement: str,
        params: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        head = statement.lstrip().upper()
        if head.startswith("SELECT") or head.startswith("WITH"):
            statements.append(
                {
                    "statement": statement,
                    "params": params,
                    "ms": (perf_counter() - context.benchmark_started) * 1000,
                }
            )

    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "after_cursor_execute", after_cursor)
    results: dict[str, Any] = {
        "phase": args.phase,
        "revision": _git_revision(),
        "database": ALLOWED_DATABASE,
        "fixture": fixture,
        "method": (
            "In-process TestClient, real handlers/CRUD/PostgreSQL; "
            "fixture auth, no network transit"
        ),
        "samples": [],
    }
    explain_targets: list[dict[str, Any]] = []
    cases: list[tuple[int, str, str, bool]] = []
    for days, label in ((7, "7d"), (30, "30d"), (365, "1y")):
        for kind in ("summary_light", "summary_full", "cost_summary"):
            cases.append((days, label, kind, False))
        for kind in ("summary_light", "summary_full"):
            cases.append((days, label, kind, True))

    with TestClient(app) as client:
        for days, label, kind, per_user in cases:
            endpoint = (
                "/api/v1/cost/summary"
                if kind == "cost_summary"
                else "/api/v1/account/gateway-usage/summary"
            )
            params: dict[str, str] = {
                "start_date": (now - timedelta(days=days)).isoformat(),
                "end_date": now.isoformat(),
            }
            if kind != "cost_summary":
                params["include_breakdown"] = str(kind == "summary_full").lower()
            if per_user:
                params["runtime_principal_id"] = PRINCIPAL
            expected = _expected(days, per_user)
            times: list[float] = []
            counts: list[int] = []
            body: dict[str, Any] = {}
            for repeat in range(6):
                statements.clear()
                start = perf_counter()
                response = client.get(endpoint, params=params)
                times.append((perf_counter() - start) * 1000)
                assert response.status_code == 200, response.text
                body = response.json()
                assert body["total_requests"] == expected["total_requests"], body
                assert body["token_usage"]["total_tokens"] == expected["total_tokens"]
                assert abs(body["estimated_cost"] - expected["estimated_cost"]) < 0.001
                counts.append(len(statements))
                capture = days == 365 and repeat == 5 and kind == "summary_full"
                if capture:
                    for statement in statements:
                        if _is_session_sql(
                            statement["statement"]
                        ) or _is_timeseries_sql(statement["statement"]):
                            explain_targets.append(
                                {
                                    **statement,
                                    "case": ("per_user_" + kind if per_user else kind),
                                    "range": label,
                                }
                            )
            item = {
                "range": label,
                "case": kind,
                "per_user": per_user,
                "runtime_principal_id": PRINCIPAL if per_user else None,
                "first_ms": times[0],
                "warm_median_ms": statistics.median(times[1:]),
                "warm_min_ms": min(times[1:]),
                "warm_max_ms": max(times[1:]),
                "all_ms": times,
                "sql_counts": counts,
                "total_requests": body["total_requests"],
                "estimated_cost": body["estimated_cost"],
            }
            results["samples"].append(item)
            out = ARTIFACTS / f"summary-results-{args.phase}.json"
            out.write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(item), flush=True)

    event.remove(engine, "before_cursor_execute", before_cursor)
    event.remove(engine, "after_cursor_execute", after_cursor)
    plans = []
    with engine.connect() as conn:
        seen: set[tuple[str, str]] = set()
        for query in explain_targets:
            key = (query["case"], query["statement"][:180])
            if key in seen:
                continue
            seen.add(key)
            plan = conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query["statement"],
                query["params"],
            ).scalar()
            plans.append(
                {
                    "case": query["case"],
                    "range": query["range"],
                    "observed_ms": query["ms"],
                    "sql": query["statement"],
                    "plan": plan,
                }
            )
    (ARTIFACTS / f"summary-plans-{args.phase}.json").write_text(
        json.dumps(plans, indent=2, default=str) + "\n"
    )
    print(
        f"Completed {args.phase} benchmark: {len(results['samples'])} cases, "
        f"{len(plans)} plans",
        flush=True,
    )


if __name__ == "__main__":
    main()
