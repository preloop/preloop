#!/usr/bin/env python
"""Seed a realistic flow-execution history and time the executions list.

Answers one question: how long do ``GET /api/v1/flows/executions`` and
``GET /api/v1/flows?stats_since=`` take for an account with a few thousand
executions, and which SQL statement is responsible.

The seeded shape (defaults) is a mid-sized Cloud account plus a noisy
neighbour, because a multi-tenant table is what the planner sees in
production:

- the measured account: 20 flows, 4,000 executions over 60 days, one flow
  holding about a tenth of them, a few dozen resume chains of depth 2 to 6;
- a neighbour account with 20,000 executions, so account filters matter,
  and 1,000,000 gateway usage rows from interactive sessions (no flow);
- trigger payloads of about 20 KB and execution logs of about 60 KB per run
  (TOASTed JSONB, as for real webhook payloads and agent transcripts);
- about 75 normalized log rows, 20 gateway usage rows and 10 MCP activity
  rows per execution.

Run against a disposable database that already has the schema
(``alembic upgrade head``)::

    PRELOOP_DISABLE_TELEMETRY=true \\
    DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/preloop \\
        python scripts/perf/benchmark_flow_executions.py --explain

Re-run with ``--account-id <id>`` (printed by the first run) to measure the
same rows after a code change. Nothing is deleted unless ``--cleanup``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any

os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from preloop.api.app import create_app  # noqa: E402
from preloop.api.auth import get_current_active_user  # noqa: E402
from preloop.models import models  # noqa: E402
from preloop.models.crud import (  # noqa: E402
    crud_account,
    crud_role,
    crud_user,
    crud_user_role,
)
from preloop.models.db.session import get_session_factory  # noqa: E402

PERF_ACCOUNT_NAME = "Perf Benchmark Account (flow executions list)"

# One statement per table, generated server side: a Python loop over a few
# hundred thousand rows would make seeding the slow part of the benchmark.
_SEED_SQL = """
WITH flows AS (
    INSERT INTO flow (id, account_id, name, prompt_template, agent_type,
                      agent_config, allowed_mcp_servers, allowed_mcp_tools,
                      is_preset, is_enabled)
    SELECT gen_random_uuid(), :account_id, 'Perf flow ' || g, 'Do the thing',
           'codex', '{}'::json, '[]'::json, '[]'::json, false, true
    FROM generate_series(1, :flows) g
    RETURNING id
)
SELECT count(*) FROM flows
"""

_EXEC_SQL = """
INSERT INTO flow_execution (id, flow_id, status, start_time, end_time,
                            created_at, updated_at, trigger_event_details,
                            execution_logs, mcp_usage_logs, tool_calls_count,
                            total_tokens, estimated_cost)
SELECT gen_random_uuid(),
       -- One flow in ten carries about a tenth of the history on its own.
       CASE WHEN g % 10 = 0 THEN f.ids[1]
            ELSE f.ids[1 + (g % array_length(f.ids, 1))] END,
       (ARRAY['SUCCEEDED','SUCCEEDED','SUCCEEDED','FAILED','STOPPED'])[1 + g % 5],
       now() - (g::float / :executions) * interval '1 day' * :days,
       now() - (g::float / :executions) * interval '1 day' * :days
           + interval '7 minutes',
       now() - (g::float / :executions) * interval '1 day' * :days,
       now() - (g::float / :executions) * interval '1 day' * :days,
       jsonb_build_object(
           '_subject', jsonb_build_object('text', 'PR #' || g, 'url',
                                          'https://example.com/pr/' || g),
           'payload', repeat(md5(g::text), :payload_kb * 1024 / 32)),
       jsonb_build_array(repeat(md5((g + 1)::text), :logs_kb * 1024 / 32)),
       (SELECT jsonb_agg(jsonb_build_object('tool', 'get_issue', 'n', i))
          FROM generate_series(1, 30) i),
       g % 40, 50000 + g, 0.25
FROM generate_series(1, :executions) g,
     (SELECT array_agg(id ORDER BY name) AS ids FROM flow
       WHERE account_id = :account_id) f
"""

# Resume chains: repairs point at a publishing execution through
# trigger_event_details._resume.resume_root, as the orchestrator writes them.
_CHAIN_SQL = """
WITH roots AS (
    SELECT fe.id, fe.flow_id, fe.start_time,
           row_number() OVER (ORDER BY fe.start_time DESC) AS n
    FROM flow_execution fe JOIN flow f ON f.id = fe.flow_id
    WHERE f.account_id = :account_id
    ORDER BY fe.start_time DESC
    LIMIT :chains
)
INSERT INTO flow_execution (id, flow_id, status, start_time, created_at,
                            updated_at, trigger_event_details, total_tokens,
                            estimated_cost)
SELECT gen_random_uuid(), r.flow_id, 'SUCCEEDED',
       r.start_time + d * interval '1 minute',
       r.start_time + d * interval '1 minute',
       r.start_time + d * interval '1 minute',
       jsonb_build_object('_resume', jsonb_build_object('resume_root', r.id::text),
                          'payload', repeat(md5(d::text), :payload_kb * 1024 / 32)),
       1000 * d, 0.01 * d
FROM roots r, generate_series(1, 1 + (r.n % 5)::int) d
"""

_LOG_SQL = """
INSERT INTO flow_execution_log (execution_id, log_type, message, timestamp)
SELECT fe.id,
       (ARRAY['tool_call','mcp_call','output','output','output'])[1 + i % 5],
       repeat('x', 200), fe.start_time
FROM flow_execution fe JOIN flow f ON f.id = fe.flow_id,
     generate_series(1, :logs_per_execution) i
WHERE f.account_id = :account_id
"""

_USAGE_SQL = """
INSERT INTO api_usage (id, account_id, flow_id, flow_execution_id, endpoint,
                       method, status_code, duration, action_type,
                       prompt_tokens, completion_tokens, total_tokens,
                       estimated_cost, model_alias, provider_name, timestamp)
SELECT gen_random_uuid(), :account_id, fe.flow_id, fe.id, '/v1/chat', 'POST',
       200, 1.0, 'model_gateway', 4000, 200, 4200, 0.01,
       (ARRAY['model-a','model-b','model-c'])[1 + i % 3], 'openai', fe.start_time
FROM flow_execution fe JOIN flow f ON f.id = fe.flow_id,
     generate_series(1, :usage_per_execution) i
WHERE f.account_id = :account_id
"""

_ACTIVITY_SQL = """
WITH sessions AS (
    INSERT INTO runtime_session (id, account_id, session_source_type,
                                 session_source_id, started_at)
    SELECT gen_random_uuid(), :account_id, 'flow_execution', fe.id::text,
           fe.start_time
    FROM flow_execution fe JOIN flow f ON f.id = fe.flow_id
    WHERE f.account_id = :account_id
    RETURNING id, session_source_id, started_at
)
INSERT INTO runtime_session_activity (id, account_id, runtime_session_id,
                                      flow_execution_id, activity_type, timestamp)
SELECT gen_random_uuid(), :account_id, s.id, s.session_source_id::uuid,
       'tool_call', s.started_at
FROM sessions s, generate_series(1, :activity_per_execution) i
"""


# Gateway traffic that has nothing to do with flows (interactive agent
# sessions), on the neighbour account. On a shared deployment this is most of
# ``api_usage``, and a query that scans the table pays for all of it.
_GATEWAY_NOISE_SQL = """
WITH s AS (
    INSERT INTO runtime_session (id, account_id, session_source_type,
                                 session_source_id, started_at)
    SELECT gen_random_uuid(), :account_id, 'claude_code', 'perf-ide-' || g, now()
    FROM generate_series(1, 200) g
    RETURNING id
), ids AS (SELECT array_agg(id) AS ids FROM s)
INSERT INTO api_usage (id, account_id, runtime_session_id, endpoint, method,
                       status_code, duration, action_type, prompt_tokens,
                       completion_tokens, total_tokens, estimated_cost,
                       model_alias, provider_name, meta_data, timestamp)
SELECT gen_random_uuid(), :account_id, ids.ids[1 + g % 200], '/v1/messages',
       'POST', 200, 1.0, 'model_gateway', 4000, 200, 4200, 0.01, 'model-a',
       'anthropic', jsonb_build_object('purpose', 'chat', 'client', 'ide'),
       now() - (g % 86400) * interval '1 minute'
FROM generate_series(1, :noise_rows) g, ids
"""


def _new_account(session, name: str) -> Any:
    account = crud_account.create(
        session, obj_in={"organization_name": name, "is_active": True}
    )
    user = crud_user.create(
        session,
        obj_in={
            "username": f"perf-user-{account.id}",
            "email": f"perf-{account.id}@example.com",
            "account_id": account.id,
            "hashed_password": "not-used",
            "is_active": True,
            "email_verified": True,
        },
    )
    owner_role = crud_role.get_by_name(session, name="owner")
    if owner_role:
        crud_user_role.create(
            session, obj_in={"user_id": user.id, "role_id": owner_role.id}
        )
    session.flush()
    return account.id


def _seed_account(session, account_id: Any, args, *, executions: int) -> None:
    params = {
        "account_id": account_id,
        "flows": args.flows,
        "executions": executions,
        "days": args.days,
        "payload_kb": args.payload_kb,
        "logs_kb": args.logs_kb,
        "chains": args.chains,
        "logs_per_execution": args.logs_per_execution,
        "usage_per_execution": args.usage_per_execution,
        "activity_per_execution": args.activity_per_execution,
    }
    for sql in (_SEED_SQL, _EXEC_SQL, _CHAIN_SQL, _LOG_SQL, _USAGE_SQL, _ACTIVITY_SQL):
        session.execute(text(sql), params)


def seed(session, args) -> Any:
    """Create the measured account and a noisy neighbour; return the former."""
    neighbour = _new_account(session, PERF_ACCOUNT_NAME + " (neighbour)")
    _seed_account(session, neighbour, args, executions=args.neighbour_executions)
    session.execute(
        text(_GATEWAY_NOISE_SQL),
        {"account_id": neighbour, "noise_rows": args.gateway_noise_rows},
    )
    account_id = _new_account(session, PERF_ACCOUNT_NAME)
    _seed_account(session, account_id, args, executions=args.executions)
    session.commit()
    for table in (
        "flow",
        "flow_execution",
        "flow_execution_log",
        "api_usage",
        "runtime_session",
        "runtime_session_activity",
    ):
        session.execute(text(f"ANALYZE {table}"))
    session.commit()
    return account_id


def _summarize(samples: list[float]) -> dict[str, Any]:
    warm = samples[1:] or samples
    return {
        "first_ms": round(samples[0], 1),
        "warm_median_ms": round(statistics.median(warm), 1),
        "warm_max_ms": round(max(warm), 1),
    }


class _StatementLog:
    """Collect every statement and its wall time for one request."""

    def __init__(self, engine):
        self.engine = engine
        self.rows: list[tuple[float, str, Any]] = []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._before)
        event.listen(self.engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._before)
        event.remove(self.engine, "after_cursor_execute", self._after)

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        conn.info["perf_started"] = time.perf_counter()

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        elapsed = (time.perf_counter() - conn.info.pop("perf_started")) * 1000.0
        self.rows.append((elapsed, statement, parameters))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account-id", help="Reuse a previously seeded account.")
    parser.add_argument("--flows", type=int, default=20)
    parser.add_argument("--executions", type=int, default=4000)
    parser.add_argument("--neighbour-executions", type=int, default=20000)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--chains", type=int, default=40)
    parser.add_argument("--payload-kb", type=int, default=20)
    parser.add_argument("--logs-kb", type=int, default=60)
    parser.add_argument("--logs-per-execution", type=int, default=75)
    parser.add_argument("--usage-per-execution", type=int, default=20)
    parser.add_argument("--activity-per-execution", type=int, default=10)
    parser.add_argument("--gateway-noise-rows", type=int, default=1_000_000)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print EXPLAIN (ANALYZE, BUFFERS) for the slowest statements.",
    )
    parser.add_argument("--output", help="Write the timings as JSON here.")
    args = parser.parse_args(argv)

    session = get_session_factory()()
    account_id = args.account_id or seed(session, args)
    owner_id = (
        session.query(models.User.id)
        .filter(models.User.account_id == account_id)
        .scalar()
    )
    flow_id = session.execute(
        text(
            "SELECT fe.flow_id FROM flow_execution fe JOIN flow f ON f.id = fe.flow_id "
            "WHERE f.account_id = :a GROUP BY fe.flow_id ORDER BY count(*) DESC LIMIT 1"
        ),
        {"a": account_id},
    ).scalar()

    app = create_app()
    app.dependency_overrides[get_current_active_user] = lambda: crud_user.get(
        session, id=owner_id
    )
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    cases = {
        "executions_limit_25": ("/api/v1/flows/executions", {"limit": 25}),
        "executions_limit_3": ("/api/v1/flows/executions", {"limit": 3}),
        "executions_flow_limit_3": (
            "/api/v1/flows/executions",
            {"limit": 3, "flow_id": str(flow_id)},
        ),
        "executions_running": (
            "/api/v1/flows/executions",
            {
                "limit": 10,
                "status": ["PENDING", "STARTING", "INITIALIZING", "RUNNING"],
            },
        ),
        "executions_limit_100": ("/api/v1/flows/executions", {"limit": 100}),
        "flows_stats_since": ("/api/v1/flows", {"stats_since": since}),
    }
    engine = session.get_bind()
    results: dict[str, Any] = {}
    slowest: list[tuple[float, str, Any]] = []
    with TestClient(app) as client:
        for name, (path, params) in cases.items():
            samples = []
            for _ in range(1 + args.iterations):
                with _StatementLog(engine) as log:
                    started = time.perf_counter()
                    response = client.get(path, params=params)
                    samples.append((time.perf_counter() - started) * 1000.0)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"{name}: {response.status_code} {response.text[:300]}"
                    )
            results[name] = {
                **_summarize(samples),
                "statements": len(log.rows),
                # Last (warm) request, slowest statement first: what to read.
                "statement_ms": [
                    [round(elapsed, 1), " ".join(statement.split())[:110]]
                    for elapsed, statement, _ in sorted(log.rows, key=lambda r: -r[0])
                ],
            }
            slowest.extend(log.rows)

    print(json.dumps({"account_id": str(account_id), "timings": results}, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(
                {"account_id": str(account_id), "timings": results}, handle, indent=2
            )

    if args.explain:
        seen: set[str] = set()
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            for elapsed, statement, parameters in sorted(slowest, key=lambda r: -r[0]):
                if statement in seen or not statement.lstrip().upper().startswith(
                    "SELECT"
                ):
                    continue
                seen.add(statement)
                if len(seen) > 4:
                    break
                cursor.execute("EXPLAIN (ANALYZE, BUFFERS) " + statement, parameters)
                plan = "\n".join(row[0] for row in cursor.fetchall())
                print(f"\n-- {elapsed:.1f} ms\n{statement}\n{plan}")
        finally:
            raw.close()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
