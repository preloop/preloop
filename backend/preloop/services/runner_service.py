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
from preloop.services.host_exec import (
    HOST_EXEC_AGENT_TYPE,
    host_exec_profile_name,
    runner_has_host_exec_profile,
)
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
HOSTED_RUNNER_NAME = "Preloop hosted"
PRIVATE_RUNNER_FALLBACK_NAME = "Private runner"

#: Every runtime reference that names a private runner starts with this.
RUNNER_REFERENCE_PREFIX = "runner:"

#: ``runner:queued:{pool}:{execution_id}`` has no runner yet.
QUEUED_RUNNER_REFERENCE_PREFIX = "runner:queued:"


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
    """True when the account has at least one private runner with a free slot.

    ``find_matching(..., online_only=True)`` includes runners that are full.
    Lease only claims a runner with a free slot, so a fully loaded fleet must
    not count as available capacity: otherwise a new unpinned flow queues for
    15 minutes and fails instead of using hosted compute.
    """
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
    return any(
        getattr(row, "status", None) in ("online", "busy")
        and int(getattr(row, "free_slots", 0)) > 0
        for row in (matches or [])
    )


def runner_id_from_session_reference(ref: Optional[str]) -> Optional[UUID]:
    """Parse ``runner:{runner_id}:{execution_id}`` assigned-lease references.

    Queued forms (``runner:queued:{pool}:{execution_id}``) have no runner yet
    and return None. Non-strings are ignored so mocked endpoint tests stay
    hosted rather than querying the database.
    """
    if not isinstance(ref, str) or not ref.startswith(RUNNER_REFERENCE_PREFIX):
        return None
    parts = ref.split(":")
    if len(parts) >= 3 and parts[1] != "queued":
        try:
            return UUID(parts[1])
        except ValueError:
            return None
    return None


def pool_from_session_reference(ref: Optional[str]) -> Optional[str]:
    """Pool string from ``runner:queued:{pool}:{execution_id}``."""
    if not isinstance(ref, str) or not ref.startswith(RUNNER_REFERENCE_PREFIX):
        return None
    parts = ref.split(":")
    if len(parts) >= 4 and parts[1] == "queued" and parts[2].strip():
        return parts[2].strip()
    return None


def is_runner_assigned_reference(ref: Optional[str]) -> bool:
    """True when a private runner already holds this execution.

    Assigned only: a queued reference is still waiting for a runner and
    holds hosted-side state (a monitor, a queue timeout), so it is not a
    private-runner assignment.
    """
    return runner_id_from_session_reference(ref) is not None


def runner_assigned_execution_clause(
    *,
    reference_column: Any,
    runner_id_column: Any,
) -> Any:
    """SQL form of :func:`is_runner_assigned_reference` for count queries.

    Kept next to the string parser so the reference shape is written down
    once. Two columns say the same thing from different ends: the runtime
    reference is set by the executor, ``flow_execution.runner_id`` by the
    lease, and a row with either is running on the account's own compute.

    Args:
        reference_column: ``FlowExecution.agent_session_reference`` column.
        runner_id_column: ``FlowExecution.runner_id`` column.

    Returns:
        A SQLAlchemy boolean clause, true for runner-assigned executions.
    """
    from sqlalchemy import and_, or_

    return or_(
        runner_id_column.isnot(None),
        and_(
            # An unset reference must compare FALSE rather than NULL, or the
            # negation of this clause would drop every hosted execution.
            reference_column.isnot(None),
            reference_column.like(f"{RUNNER_REFERENCE_PREFIX}%"),
            ~reference_column.like(f"{QUEUED_RUNNER_REFERENCE_PREFIX}%"),
        ),
    )


def derive_execution_runner(
    *,
    runner_id: Optional[UUID] = None,
    agent_session_reference: Optional[str] = None,
    runner_name: Optional[str] = None,
    pool: Optional[str] = None,
) -> Dict[str, Any]:
    """Where an execution ran, derived from the row rather than a new column.

    Private when ``runner_id`` is set or ``agent_session_reference`` uses a
    ``runner:...`` form (assigned or queued). A non-runner runtime reference
    identifies the built-in hosted executor. An execution
    without an assignment is unknown, including newly created retries.
    """
    resolved_id = runner_id or runner_id_from_session_reference(agent_session_reference)
    resolved_pool = pool or pool_from_session_reference(agent_session_reference)
    is_private = resolved_id is not None or (
        isinstance(agent_session_reference, str)
        and agent_session_reference.startswith("runner:")
    )
    if is_private:
        name = runner_name.strip() if isinstance(runner_name, str) else ""
        return {
            "kind": "private",
            "id": resolved_id,
            "name": name or PRIVATE_RUNNER_FALLBACK_NAME,
            "pool": resolved_pool,
        }
    if (
        not isinstance(agent_session_reference, str)
        or not agent_session_reference.strip()
    ):
        return {"kind": "unknown", "id": None, "name": "Not recorded", "pool": None}
    return {
        "kind": "hosted",
        "id": None,
        "name": HOSTED_RUNNER_NAME,
        "pool": None,
    }


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


_PERSISTED_SECRET_KEYS = ("account_api_token", "launch")


def persistable_job_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a lease payload without credentials that must not hit JSONB.

    The live WebSocket push still receives the original payload, including
    ``account_api_token``, for that lease only.
    """
    from preloop.config import settings

    stored = dict(payload)
    for key in _PERSISTED_SECRET_KEYS:
        stored.pop(key, None)
    if (
        settings.flow_artifact_direct_upload
        and stored.get("completion_protocol") != "host_exec"
    ):
        stored["evidence_direct_upload"] = True
    return stored


def unwrap_agent_config(config: Any) -> Any:
    """Strip the doubly wrapped ``{"agent_config": {...}}`` storage shape.

    Some stored flow configurations nest the configuration inside a single
    ``agent_config`` key. Every reader has to agree on one unwrapping: a
    reader that misses it sees no ``runner`` section, and a host-bound
    continuation then reads as free to run anywhere.

    Args:
        config: A stored or payload agent configuration, of any type.

    Returns:
        The inner configuration when the wrapper is present, else ``config``.
    """
    if (
        isinstance(config, dict)
        and set(config) == {"agent_config"}
        and isinstance(config["agent_config"], dict)
    ):
        return config["agent_config"]
    return config


def workspace_owner_runner_id(
    db: Session, *, payload: Dict[str, Any]
) -> Optional[UUID]:
    """The runner that holds the local workspace this job wants to resume.

    A private runner keeps a persisted workspace in its own configuration
    directory and never uploads it. Resuming that work on a second machine
    would start from a cold clone and quietly drop the unpushed commits, so
    the continuation is pinned to the host that owns the directory.

    ``None`` means "any matching runner will do": either this is not a
    resume, the flow does not persist its workspace, or the prior execution
    was never assigned to a private runner.

    Args:
        db: Database session.
        payload: The lease payload built for this execution.

    Returns:
        The owning runner id, or None when the job is not host bound.
    """
    from preloop.models.crud import crud_flow_execution

    resume_from = payload.get("resume_from")
    if not isinstance(resume_from, str) or not resume_from.strip():
        return None
    config = unwrap_agent_config(payload.get("agent_config"))
    runner_config = config.get("runner") if isinstance(config, dict) else None
    if not isinstance(runner_config, dict) or not _truthy(
        runner_config.get("persist_workspace")
    ):
        return None
    try:
        prior_id = UUID(resume_from.strip())
    except (ValueError, AttributeError, TypeError):
        return None
    prior = crud_flow_execution.get(db, id=prior_id)
    owner = getattr(prior, "runner_id", None) if prior is not None else None
    if owner is None and prior is not None:
        owner = runner_id_from_session_reference(
            getattr(prior, "agent_session_reference", None)
        )
    return owner


def _truthy(value: Any) -> bool:
    """Match the private runner CLI's reading of ``persist_workspace``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def runner_wait_notice(runner: Optional[FlowRunner], execution_id: Any) -> str:
    """Operator-visible reason a host-bound continuation is still queued."""
    name = getattr(runner, "name", None) or PRIVATE_RUNNER_FALLBACK_NAME
    return (
        f"Waiting for private runner {name}: it holds the local workspace for "
        f"execution {execution_id}. The workspace is never uploaded, so this "
        "continuation cannot move to another host."
    )


def runner_blocked_notice(runner: Optional[FlowRunner], execution_id: Any) -> str:
    """Operator action required once a host-bound continuation times out."""
    name = getattr(runner, "name", None) or PRIVATE_RUNNER_FALLBACK_NAME
    return (
        f"Private runner {name} did not come online within {DEFAULT_QUEUE_TIMEOUT}. "
        f"The recovery workspace for execution {execution_id} is still on that "
        "host, under the runner configuration directory. Bring the runner back "
        "and retry, or start a fresh conversation from the published branch. "
        "Preloop does not upload private workspaces or move them to another host."
    )


def _runner_may_accept(
    db: Session,
    *,
    account_id: UUID,
    runner: FlowRunner,
    pool: str,
    execution_id: UUID,
) -> bool:
    """Whether the account authorizer (hook H4) lets ``runner`` take the job.

    Asked with the ``runner:accept`` action before a slot is claimed, so a
    denied runner is skipped without a claim to roll back. True when no
    authorizer is registered.
    """
    from preloop.plugins.account_hooks import (
        ACTION_RUNNER_ACCEPT,
        AuthorizationContext,
        authorize,
        get_authorizer,
    )

    if get_authorizer() is None:
        return True
    ctx = AuthorizationContext(
        account_id=account_id,
        db=db,
        attributes={"pool": pool, "execution_id": str(execution_id)},
    )
    return authorize(ctx, ACTION_RUNNER_ACCEPT, runner).allowed


def lease_job(
    db: Session,
    *,
    account_id: UUID,
    pool: str,
    execution_id: UUID,
    payload: Dict[str, Any],
    required_runner_id: Optional[UUID] = None,
) -> Optional[FlowRunner]:
    """Assign a pending job to one matching online runner. None if queued.

    Candidates are re-fetched with ``SELECT ... FOR UPDATE SKIP LOCKED`` so
    two concurrent leases cannot hand out the same free slot. A runner may
    hold up to its capacity at once; ``find_matching`` already orders the
    emptiest machine first. Credentials are stripped from the stored payload;
    the caller still holds the original dict for the in-memory WebSocket push.

    ``required_runner_id`` pins the job to the host that holds its local
    recovery state. No other runner is considered, even an idle one in the
    same pool: the work would restart from a cold clone there.
    """
    from preloop.models.crud import crud_flow_execution

    # Keep the account lock through lease assignment. A stopped queued
    # execution cannot acquire a new runner after activation's snapshot.
    if not crud_flow_execution.admit_runtime_start(
        db, execution_id=execution_id, commit=False
    ):
        db.commit()
        return None
    matches = crud_flow_runner.find_matching(
        db, account_id=account_id, pool=pool, online_only=True
    )
    if required_runner_id is not None:
        matches = [row for row in matches if row.id == required_runner_id]
    available = [
        row
        for row in matches
        if row.status in ("online", "busy") and row.free_slots > 0
    ]
    required_profile = host_exec_profile_name(payload)
    stored = persistable_job_payload(payload)
    for candidate in available:
        if not _runner_may_accept(
            db,
            account_id=account_id,
            runner=candidate,
            pool=pool,
            execution_id=execution_id,
        ):
            continue
        if required_profile and not runner_has_host_exec_profile(
            candidate,
            required_profile,
            payload.get("model_identifier"),
            payload.get("agent_type") or HOST_EXEC_AGENT_TYPE,
        ):
            continue
        runner = crud_flow_runner.claim_free_slot(db, runner_id=candidate.id)
        if runner is None:
            continue
        if payload.get("_publication"):
            capability = runner.publication_capabilities or {}
            if (
                capability.get("version") != 1
                or capability.get("helper_ready") is not True
                or payload.get("agent_type")
                not in {"codex", "opencode", "pi", "deepseek"}
            ):
                db.rollback()
                continue
            crud_flow_runner.bind_publication_lease(
                db,
                runner_id=runner.id,
                execution_id=execution_id,
                account_id=account_id,
                nonce=payload["_publication"]["nonce"],
            )
        assignment = crud_flow_runner.create_assignment(
            db,
            runner_id=runner.id,
            execution_id=execution_id,
            pending_job=stored,
            commit=False,
        )
        assignment.reported_status = "PENDING"
        db.add(assignment)
        db.commit()
        db.refresh(runner)
        emit_runner_updated(runner, db)
        return runner
    db.commit()
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
    assignments = list(getattr(runner, "assignments", None) or [])
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
        "concurrency": int(getattr(runner, "concurrency", 0) or 0),
        "capacity": int(getattr(runner, "capacity", 0) or 0),
        "running_count": len(assignments),
        "running_execution_ids": [str(row.execution_id) for row in assignments],
        "registered_by_user_id": (
            str(runner.registered_by_user_id) if runner.registered_by_user_id else None
        ),
        "registered_by_email": registered_by_email,
        "capabilities": dict(getattr(runner, "capabilities", None) or {}),
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


def emit_runner_deleted(account_id: Any, runner_id: Any) -> None:
    """Tell console websockets subscribed to ``runners`` that a row is gone."""
    if not account_id or not runner_id:
        return
    emit_account_event(
        build_account_event(
            account_id=str(account_id),
            topic=ACCOUNT_TOPIC_RUNNERS,
            event_type="runner_deleted",
            runner_id=str(runner_id),
            payload={"id": str(runner_id)},
        )
    )
