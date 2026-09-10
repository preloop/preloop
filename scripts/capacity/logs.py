"""Optional inert execution fixtures and real NATS log-persistence accounting."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import closing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"


class LogFixture:
    """Seed through CRUD only; never enqueue or launch an agent execution."""

    def __init__(self, account_id: str, count: int) -> None:
        from loguru import logger

        logger.disable("preloop.models.db.session")
        from preloop.models import models, schemas
        from preloop.models.crud import crud_flow, crud_flow_execution
        from preloop.models.db.session import get_db_session

        database = urlparse(os.environ.get("DATABASE_URL", ""))
        if database.hostname not in {
            "postgres",
            "localhost",
            "127.0.0.1",
        } or not database.path.lstrip("/").startswith("capacity"):
            raise ValueError(
                "Log fixtures require a local disposable capacity database"
            )
        self.ids: list[UUID] = []
        self.cursors: dict[UUID, Any] = {}
        self.seen: dict[int, set[int]] = {}
        self.rows: dict[int, int] = {}
        with closing(get_db_session()) as sessions:
            db = next(sessions)
            flow: models.Flow = crud_flow.create(
                db,
                account_id=account_id,
                flow_in=schemas.FlowCreate(
                    name="Capacity inert log fixture",
                    prompt_template="Never execute this disabled fixture",
                    agent_type="custom",
                    agent_config={},
                    is_enabled=False,
                ),
            )
            for _ in range(count):
                execution = crud_flow_execution.create(
                    db,
                    schemas.FlowExecutionCreate(
                        flow_id=flow.id,
                        status="SUCCEEDED",
                        start_time=datetime.now(UTC),
                        end_time=datetime.now(UTC),
                    ),
                )
                self.ids.append(execution.id)
            db.commit()

    def reconcile(self, stage: int) -> dict[str, Any]:
        """Read at most one keyset page per execution per sample, without SQL."""
        from preloop.models.crud import crud_flow_execution_log
        from preloop.models.db.session import get_db_session

        self.seen = {stage: self.seen.get(stage, set())}
        self.rows = {stage: self.rows.get(stage, 0)}
        with closing(get_db_session()) as sessions:
            db = next(sessions)
            for execution_id in self.ids:
                rows = crud_flow_execution_log.get_agent_log_page(
                    db, execution_id, after=self.cursors.get(execution_id), limit=1000
                )
                for row in rows:
                    prefix, row_stage, sequence = (row.message or "").split(":")
                    if prefix != "capacity":
                        raise ValueError("Unexpected fixture log")
                    if int(row_stage) == stage:
                        self.seen[stage].add(int(sequence))
                        self.rows[stage] += 1
                if rows:
                    self.cursors[execution_id] = (rows[-1].timestamp, rows[-1].id)
        return {
            str(stage): {
                "unique_persisted": len(values),
                "rows": self.rows[stage],
                "duplicates": self.rows[stage] - len(values),
            }
            for stage, values in self.seen.items()
        }

    async def publish(
        self, account_id: str, stage: int, rate: float, seconds: float, maximum: int
    ) -> dict[str, Any]:
        """Offer a paced stream; never catch up with an unbounded burst."""
        import nats

        address = os.environ.get("NATS_URL", "nats://nats:4222")
        parsed = urlparse(address)
        if parsed.hostname not in {"nats", "localhost", "127.0.0.1"}:
            raise ValueError("Log publisher requires local NATS")
        client = await nats.connect(
            address, connect_timeout=5, max_reconnect_attempts=0
        )
        submitted = 0
        start = time.monotonic()
        try:
            while time.monotonic() - start < seconds and submitted < maximum:
                execution_id = str(self.ids[submitted % len(self.ids)])
                payload = {
                    "type": "agent_log_line",
                    "execution_id": execution_id,
                    "account_id": account_id,
                    "payload": {"line": f"capacity:{stage}:{submitted}"},
                }
                await client.publish(
                    "flow-updates." + execution_id, json.dumps(payload).encode()
                )
                submitted += 1
                if submitted % 100 == 0:
                    await client.flush(timeout=5)
                await asyncio.sleep(max(0, 1 / rate))
            await client.flush(timeout=5)
        finally:
            await client.close()
        elapsed = time.monotonic() - start
        return {
            "submitted": submitted,
            "elapsed_seconds": elapsed,
            "observed_logs_per_second": submitted / elapsed,
            "requested_logs_per_second": rate,
            "maximum_reached": submitted >= maximum,
        }
