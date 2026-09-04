"""Lease and heartbeat helpers for self-hosted flow runners."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.crud.user import crud_user
from preloop.models.models.flow import Flow
from preloop.models.models.flow_runner import FlowRunner
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNNERS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)

RUNNER_OVERRIDE_KEY = "_runner"
DEFAULT_QUEUE_TIMEOUT = timedelta(minutes=15)
SERVER_RUNNER_POOL = "server"
AUTO_RUNNER_POOL = "auto"


def hash_runner_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_runner_token() -> str:
    return secrets.token_urlsafe(32)


def _explicit_pool(value: Any) -> Optional[str]:
    """Return a stripped pool string, or None when unset."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _is_server_pool(pool: Optional[str]) -> bool:
    return pool is not None and pool.lower() == SERVER_RUNNER_POOL


def _is_auto_pool(pool: Optional[str]) -> bool:
    return pool is not None and pool.lower() == AUTO_RUNNER_POOL


def _account_default_runner_pool(flow: Flow, db: Optional[Session]) -> Optional[str]:
    """Read account.default_runner_pool from the flow or the database."""
    account = getattr(flow, "account", None)
    if account is not None:
        value = getattr(account, "default_runner_pool", None)
        return value if isinstance(value, str) or value is None else None
    if db is None:
        return None
    account_id = getattr(flow, "account_id", None)
    if account_id is None:
        return None
    row = crud_account.get(db, id=account_id)
    if row is None:
        return None
    value = getattr(row, "default_runner_pool", None)
    return value if isinstance(value, str) or value is None else None


def _has_online_private_runner(db: Session, account_id: Any) -> bool:
    """True when the account has at least one online private runner."""
    if account_id is None:
        return False
    try:
        if not isinstance(account_id, UUID):
            UUID(str(account_id))
    except (TypeError, ValueError):
        return False
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=AUTO_RUNNER_POOL, online_only=True
    )
    return isinstance(matches, (list, tuple)) and len(matches) > 0


def resolve_runner_pool(
    flow: Flow,
    execution_context: Optional[Dict[str, Any]] = None,
    *,
    db: Optional[Session] = None,
) -> Optional[str]:
    """Resolve the runner pool for one execution.

    Precedence: trigger override > flow.runner_pool >
    account.default_runner_pool > any online private runner ("auto") >
    hosted executor (None). The literal ``server`` at any explicit level
    opts into the hosted executor.
    """
    details = (execution_context or {}).get("trigger_event_data") or {}
    override = details.get(RUNNER_OVERRIDE_KEY)
    payload = details.get("payload")
    if not override and isinstance(payload, dict):
        override = payload.get(RUNNER_OVERRIDE_KEY)
    chosen = _explicit_pool(override)
    if chosen is None:
        chosen = _explicit_pool(getattr(flow, "runner_pool", None))
    if chosen is None:
        chosen = _explicit_pool(_account_default_runner_pool(flow, db))
    if _is_server_pool(chosen):
        return None
    if chosen and not _is_auto_pool(chosen):
        return chosen
    account_id = getattr(flow, "account_id", None)
    if account_id is None:
        account_id = (execution_context or {}).get("account_id")
    if db is not None and _has_online_private_runner(db, account_id):
        return AUTO_RUNNER_POOL
    return None


_PERSISTED_SECRET_KEYS = ("account_api_token",)


def persistable_job_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a lease payload without credentials that must not hit JSONB.

    The live WebSocket push still receives the original payload, including
    ``account_api_token``, for that lease only.
    """
    stored = dict(payload)
    for key in _PERSISTED_SECRET_KEYS:
        stored.pop(key, None)
    return stored


def lease_job(
    db: Session,
    *,
    account_id: UUID,
    pool: str,
    execution_id: UUID,
    payload: Dict[str, Any],
) -> Optional[FlowRunner]:
    """Assign a pending job to one matching online runner. None if queued.

    Candidates are re-fetched with ``SELECT ... FOR UPDATE SKIP LOCKED`` so
    two concurrent leases cannot persist ``pending_job`` on the same row.
    Credentials are stripped from the stored payload; the caller still holds
    the original dict for the in-memory WebSocket push.
    """
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=pool, online_only=True
    )
    idle = [row for row in matches if row.status == "online" and not row.pending_job]
    stored = persistable_job_payload(payload)
    for candidate in idle:
        runner = crud_flow_runner.claim_idle(db, runner_id=candidate.id)
        if runner is None:
            continue
        runner.pending_job = stored
        runner.current_execution_id = execution_id
        runner.status = "busy"
        runner.halt_requested = False
        runner.reported_status = "PENDING"
        db.add(runner)
        db.commit()
        db.refresh(runner)
        emit_runner_updated(runner, db)
        return runner
    return None


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


def runner_console_payload(
    runner: FlowRunner, *, registered_by_email: Optional[str] = None
) -> Dict[str, Any]:
    """Fields the console runners table needs. Never includes the runner token."""
    heartbeat = runner.last_heartbeat
    execution_id = runner.current_execution_id
    return {
        "id": str(runner.id),
        "name": runner.name,
        "hostname": runner.hostname,
        "os": runner.os,
        "arch": runner.arch,
        "labels": list(runner.labels or []),
        "status": runner.status,
        "last_heartbeat": heartbeat.isoformat() if heartbeat is not None else None,
        "current_execution_id": str(execution_id) if execution_id else None,
        "registered_by_user_id": (
            str(runner.registered_by_user_id) if runner.registered_by_user_id else None
        ),
        "registered_by_email": registered_by_email,
    }


def emit_runner_updated(runner: FlowRunner, db: Optional[Session] = None) -> None:
    """Push one runner row to console websockets subscribed to ``runners``."""
    if not getattr(runner, "account_id", None):
        return
    email = None
    user_id = getattr(runner, "registered_by_user_id", None)
    if db is not None and user_id:
        user = crud_user.get(db, id=user_id)
        if user is not None:
            email = getattr(user, "email", None)
    emit_account_event(
        build_account_event(
            account_id=str(runner.account_id),
            topic=ACCOUNT_TOPIC_RUNNERS,
            event_type="runner_updated",
            runner_id=str(runner.id),
            payload=runner_console_payload(runner, registered_by_email=email),
        )
    )
