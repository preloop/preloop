"""Measure activity pages on synthetic sessions in a local PostgreSQL database.

Run with this checkout on PYTHONPATH and DATABASE_URL pointing at a migrated local
database. The seed is rolled back, and no model provider is called.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session_activity


def measure() -> None:
    """Print page size and median query time; discard all seeded data."""
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This measurement requires a local database")
    with Session(create_engine(url)) as db:
        account = models.Account(organization_name="Synthetic session measurement")
        db.add(account)
        db.flush()
        at = datetime.now(UTC) - timedelta(days=1)
        session = models.RuntimeSession(
            account_id=account.id,
            session_source_type="codex_cli",
            session_source_id=str(uuid4()),
            started_at=at,
        )
        db.add(session)
        db.flush()
        rows = []
        for turn in range(300):
            for position in range(18):
                tool = position % 3 == 1
                kind = "tool_call" if tool else "transcript_message"
                meta: dict[str, Any] = {
                    "role": "user" if position == 0 else "assistant"
                }
                if position == 3:
                    kind = "model_gateway_call"
                    meta = {"request": "Example history. " * 4096, "total_tokens": 400}
                elif tool:
                    meta = {"arguments": {"path": "example.py"}, "result": "ok"}
                rows.append(
                    models.RuntimeSessionActivity(
                        account_id=account.id,
                        runtime_session_id=session.id,
                        activity_type=kind,
                        tool_name="read_file" if tool else None,
                        timestamp=at + timedelta(minutes=turn, seconds=position),
                        summary="Example captured text. " * 24,
                        metadata_=meta,
                    )
                )
        db.add_all(rows)
        db.flush()
        kwargs = {"account_id": str(account.id), "runtime_session_id": str(session.id)}
        print("Seed: 300 turns, 5400 rows, 1800 tool calls, 300 captured model calls")
        for limit in (0, 10, 20, 50):
            timings = []
            for _ in range(7):
                started = perf_counter()
                if limit:
                    page = crud_runtime_session_activity.list_timeline_turn_page(
                        db, **kwargs, limit=limit
                    )["rows"]
                else:
                    page = crud_runtime_session_activity.list_timeline_rows_for_runtime_session(
                        db, **kwargs, limit=100
                    )
                timings.append((perf_counter() - started) * 1000)
            payload = json.dumps([dict(row._mapping) for row in page], default=str)
            tools = sum(row.activity_type == "tool_call" for row in page)
            print(
                f"{'baseline' if not limit else str(limit) + ' turns'}: "
                f"{len(page)} rows, {tools} tools, {len(payload.encode()):,} bytes, "
                f"median {median(timings[1:]):.1f} ms (6 warm reads)"
            )
        db.rollback()


if __name__ == "__main__":
    measure()
