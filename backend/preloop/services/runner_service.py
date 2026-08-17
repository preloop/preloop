"""Lease and heartbeat helpers for self-hosted flow runners."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.models.flow import Flow
from preloop.models.models.flow_runner import FlowRunner

logger = logging.getLogger(__name__)

RUNNER_OVERRIDE_KEY = "_runner"
DEFAULT_QUEUE_TIMEOUT = timedelta(minutes=15)


def hash_runner_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_runner_token() -> str:
    return secrets.token_urlsafe(32)


def resolve_runner_pool(
    flow: Flow, execution_context: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Trigger override wins over the flow's default pool."""
    details = (execution_context or {}).get("trigger_event_data") or {}
    override = details.get(RUNNER_OVERRIDE_KEY)
    payload = details.get("payload")
    if not override and isinstance(payload, dict):
        override = payload.get(RUNNER_OVERRIDE_KEY)
    if isinstance(override, str) and override.strip():
        return override.strip()
    pool = getattr(flow, "runner_pool", None)
    if isinstance(pool, str) and pool.strip():
        return pool.strip()
    return None


def lease_job(
    db: Session,
    *,
    account_id: UUID,
    pool: str,
    execution_id: UUID,
    payload: Dict[str, Any],
) -> Optional[FlowRunner]:
    """Assign a pending job to one matching online runner. None if queued."""
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=pool, online_only=True
    )
    idle = [row for row in matches if row.status == "online" and not row.pending_job]
    if not idle:
        return None
    runner = idle[0]
    runner.pending_job = payload
    runner.current_execution_id = execution_id
    runner.status = "busy"
    runner.halt_requested = False
    runner.reported_status = "PENDING"
    db.add(runner)
    db.commit()
    db.refresh(runner)
    return runner


def mark_queued_or_fail(
    *,
    queued_since: datetime,
    timeout: timedelta = DEFAULT_QUEUE_TIMEOUT,
) -> str:
    """Return PENDING while waiting, FAILED after the offline timeout."""
    now = datetime.now(timezone.utc)
    if queued_since.tzinfo is None:
        queued_since = queued_since.replace(tzinfo=timezone.utc)
    if now - queued_since > timeout:
        return "FAILED"
    return "PENDING"


def is_online(runner: FlowRunner) -> bool:
    if not runner.last_heartbeat:
        return False
    hb = runner.last_heartbeat
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - hb <= ONLINE_HEARTBEAT_TTL
