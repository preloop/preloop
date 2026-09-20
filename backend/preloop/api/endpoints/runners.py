"""Self-hosted runner registration, listing, and control-plane WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from string import ascii_letters, digits
from typing import Any, Dict, List, Mapping, Optional
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from preloop.agents.runner_launch import (
    prepare_runner_delivery,
)
from preloop.api.auth import get_current_active_user
from preloop.models import models, schemas
from preloop.models.crud import (
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
    crud_user,
)
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.models.db.session import release_transaction

from preloop.services.flow_pr_binding import record_runner_handoff_markers
from preloop.services.runner_service import (
    derive_execution_runner,
    emit_runner_updated,
    hash_runner_token,
    mint_runner_token,
)
from preloop.services.private_publication import (
    PrivatePublicationController,
    trusted_private_receipt,
)
from preloop.services.trusted_publisher import PublicationError
from preloop.services.host_exec import (
    apply_runner_completion_to_execution,
    finalize_runner_completion,
    normalize_host_exec_advertisements,
)
from preloop.cra.persist import (
    apply_cra_fail_closed_completion,
    apply_cra_persist_boundary,
    resolve_persist_authority,
)
from preloop.utils.permissions import require_permission

FlowRunner = models.FlowRunner
User = models.User

router = APIRouter()
logger = logging.getLogger(__name__)

# runner_id -> live websocket (this process only)
_live: Dict[str, WebSocket] = {}
RUNNER_LOG_BROADCAST_TIMEOUT = 1.0


def _valid_publication_helper_image(value: object) -> bool:
    """Validate a bounded, digest-pinned reference without regex backtracking."""
    if not isinstance(value, str) or len(value) > 1024:
        return False
    name, separator, digest = value.partition("@sha256:")
    alphanumeric = ascii_letters + digits
    return (
        bool(separator)
        and bool(name)
        and name[0] in alphanumeric
        and all(character in alphanumeric + "._:/-" for character in name)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _release_live_runner(runner_key: str, connection: WebSocket) -> bool:
    """Drop this socket from ``_live`` only if it is still the current one."""
    if _live.get(runner_key) is connection:
        _live.pop(runner_key, None)
        return True
    return False


def _to_response(
    row: FlowRunner, db: Optional[Session] = None
) -> schemas.RunnerResponse:
    data = schemas.RunnerResponse.model_validate(row)
    if db is not None and row.registered_by_user_id:
        user = crud_user.get(db, id=row.registered_by_user_id)
        if user is not None:
            data.registered_by_email = user.email
    return data


@router.post("/runners/register", response_model=schemas.RunnerRegisterResponse)
@require_permission("execute_flows")
def register_runner(
    body: schemas.RunnerRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Register a new runner or resume an existing one for this account."""
    advertised = body.host_exec_profiles or []
    capabilities = normalize_host_exec_advertisements(
        [profile.model_dump() for profile in advertised]
    )
    # A CI account registers a new ephemeral runner per job. Reap the ones
    # whose job died without unregistering before adding another.
    crud_flow_runner.sweep_stale_ephemeral(db, account_id=current_user.account_id)
    if body.runner_id:
        existing = crud_flow_runner.get(
            db, id=body.runner_id, account_id=str(current_user.account_id)
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Runner not found")
        token = mint_runner_token()
        updates = {
            "token_hash": hash_runner_token(token),
            "capabilities": capabilities,
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc),
            "ephemeral": body.ephemeral or existing.ephemeral,
        }
        for field in ("name", "hostname", "os", "arch", "labels", "instance_id"):
            if value := getattr(body, field):
                updates[field] = value
        existing = crud_flow_runner.update(db, db_obj=existing, obj_in=updates)
        # A process that omits concurrency is unreported, even if an earlier
        # process left a stale report on this row (a rollback to a
        # pre-multi-slot CLI reusing the same runner_id).
        existing = crud_flow_runner.set_reported_concurrency(
            db, runner=existing, reported=body.concurrency
        )
        emit_runner_updated(existing, db)
        return schemas.RunnerRegisterResponse(
            **_to_response(existing, db).model_dump(), token=token
        )

    token = mint_runner_token()
    name = (body.name or body.hostname or socket.gethostname() or "runner")[:200]
    row = crud_flow_runner.create(
        db,
        obj_in={
            "account_id": current_user.account_id,
            "registered_by_user_id": current_user.id,
            "instance_id": body.instance_id,
            "name": name,
            "hostname": body.hostname,
            "os": body.os,
            "arch": body.arch,
            "labels": body.labels or [],
            "ephemeral": body.ephemeral,
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc),
            "token_hash": hash_runner_token(token),
            "capabilities": capabilities,
        },
    )
    if body.concurrency is not None:
        row = crud_flow_runner.set_reported_concurrency(
            db, runner=row, reported=body.concurrency
        )
    emit_runner_updated(row, db)
    return schemas.RunnerRegisterResponse(
        **_to_response(row, db).model_dump(), token=token
    )


@router.get("/runners", response_model=List[schemas.RunnerResponse])
@require_permission("view_flows")
def list_runners(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    skip: int = 0,
    limit: int = 100,
):
    # The Runners page is where a phantom would be visible, so reap lapsed
    # one-shot rows on the way in rather than showing them offline forever.
    crud_flow_runner.sweep_stale_ephemeral(db, account_id=current_user.account_id)
    rows = crud_flow_runner.list_for_account(
        db, account_id=current_user.account_id, skip=skip, limit=limit
    )
    return [_to_response(row, db) for row in rows]


@router.get("/runners/fleet-summary", response_model=schemas.RunnerFleetSummary)
@require_permission("view_flows")
def runner_fleet_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    crud_flow_runner.sweep_stale_ephemeral(db, account_id=current_user.account_id)
    return schemas.RunnerFleetSummary(
        **crud_flow_runner.counts_for_account(db, account_id=current_user.account_id)
    )


@router.patch("/runners/{runner_id}/concurrency", response_model=schemas.RunnerResponse)
@require_permission("execute_flows")
def update_runner_concurrency(
    runner_id: UUID,
    body: schemas.RunnerConcurrencyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Set how many executions this runner may hold at once.

    The stored value is a ceiling. A runner process started with a lower
    ``--concurrency`` still lowers it while connected; raising it here does
    not make someone's laptop run more than that process agreed to.
    """
    row = crud_flow_runner.get(
        db, id=runner_id, account_id=str(current_user.account_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Runner not found")
    row = crud_flow_runner.set_concurrency(db, runner=row, concurrency=body.concurrency)
    emit_runner_updated(row, db)
    return _to_response(row, db)


@router.get("/runners/{runner_id}", response_model=schemas.RunnerResponse)
@require_permission("view_flows")
def get_runner(
    runner_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    row = crud_flow_runner.get(
        db, id=runner_id, account_id=str(current_user.account_id)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Runner not found")
    return _to_response(row, db)


async def _publish_flow_update(execution_id: str, payload: Dict[str, Any]) -> None:
    try:
        from preloop.sync.services.event_bus import get_nats_client

        nats_client = await get_nats_client()
        await nats_client.publish(
            f"flow-updates.{execution_id}", json.dumps(payload).encode("utf-8")
        )
    except Exception as exc:
        logger.debug("runner log NATS publish skipped: %s", exc)


async def persist_runner_logs(
    db: Session,
    execution_id: UUID,
    lines: list[str],
    batch_id: str | None,
) -> None:
    """Commit one bounded batch before acknowledgment; replay IDs are stable.

    Database work runs off the event loop, using this connection's session
    sequentially. Live broadcasts are best effort and share one short budget;
    clients can recover committed lines from the execution log API.
    """
    # Older runners send their bounded terminal log buffer in one frame.
    # Acknowledged peers use small batches; preserve legacy delivery without
    # permitting unbounded inserts or per-line commits.
    if batch_id is not None and not isinstance(batch_id, str):
        raise ValueError("Invalid runner log batch identity")
    limit = 128 if batch_id else 8192
    if (
        not isinstance(lines, list)
        or len(lines) > limit
        or any(
            not isinstance(line, str) or len(line.encode("utf-8")) > 66 * 1024
            for line in lines
        )
        or sum(len(line.encode("utf-8")) for line in lines) > 4 * 1024 * 1024
    ):
        raise ValueError("Invalid runner log batch")
    identity = UUID(batch_id) if batch_id else uuid4()
    entries = [
        (
            str(execution_id),
            {
                "_persistence_id": str(uuid5(execution_id, f"{identity}:{index}")),
                "type": "agent_log_line",
                "payload": {"line": line},
            },
        )
        for index, line in enumerate(lines)
    ]
    await run_in_threadpool(crud_flow_execution_log.append_logs, db, entries)

    async def broadcast() -> None:
        for line in lines:
            await _publish_flow_update(
                str(execution_id),
                {
                    "execution_id": str(execution_id),
                    "type": "agent_log_line",
                    "payload": {"line": line},
                },
            )

    try:
        await asyncio.wait_for(broadcast(), timeout=RUNNER_LOG_BROADCAST_TIMEOUT)
    except TimeoutError:
        logger.debug("runner live log broadcast timed out for %s", execution_id)


def _authenticate_runner(db: Session, runner_id: UUID, token: str) -> FlowRunner:
    row = crud_flow_runner.get(db, id=runner_id)
    if not row or row.token_hash != hash_runner_token(token):
        raise HTTPException(status_code=401, detail="Invalid runner credentials")
    return row


def runner_needs_lease_token(assignment: Any) -> bool:
    """True while a persisted lease has not yet started on the runner.

    Production is multi-replica: ``push_job_to_runner`` only hits the
    socket if this process holds ``_live``, so a runner with a free slot
    usually first sees a brand-new lease on the next 15s heartbeat. Mint a
    token for that unstarted lease (``reported_status`` is ``None`` or
    ``PENDING``). Skip once that job is mid-execution or terminal so
    heartbeats do not churn keys.

    Args:
        assignment: One runner assignment, or anything exposing
            ``reported_status``.

    Returns:
        True when the lease still needs a freshly minted token.
    """
    status = str(getattr(assignment, "reported_status", None) or "").strip().upper()
    return status in {"", "PENDING"}


def job_for_heartbeat_ack(
    db: Session,
    assignment: Any,
    *,
    publication_capabilities: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Job copy for a heartbeat ack; mints only while the lease is unstarted.

    Args:
        db: Database session.
        assignment: The runner assignment being replayed.
        publication_capabilities: The socket's current capability snapshot.

    Returns:
        A deliverable job copy, or None when nothing should be resent.
    """
    pending_job = getattr(assignment, "pending_job", None)
    if not pending_job:
        return None
    state = pending_job.get("_publication")
    if state and (
        state.get("phase") != "agent"
        or not runner_needs_lease_token(assignment)
        or (publication_capabilities or {}).get("helper_ready") is not True
    ):
        return None
    return job_for_runner_replay(
        db,
        pending_job=pending_job,
        mint_token=runner_needs_lease_token(assignment),
    )


def job_for_runner_replay(
    db: Session,
    *,
    pending_job: Dict[str, Any],
    mint_token: bool = False,
) -> Dict[str, Any]:
    """Copy a stored job; mint a token when the caller still needs one.

    Secrets are never persisted in ``pending_job``. Hello always mints so
    a reconnect can start the lease. Heartbeats mint only while the
    runner has not yet reported a running or terminal status; see
    ``job_for_heartbeat_ack``.
    """
    job = dict(pending_job)
    if (
        not mint_token
        or job.get("launch_version")
        or job.get("completion_protocol") == "host_exec"
    ):
        return job
    execution_id = _parse_runner_execution_id(job.get("execution_id"))
    if execution_id is None:
        return job
    execution = crud_flow_execution.get(db, id=execution_id)
    if execution is None or getattr(execution, "flow_id", None) is None:
        return job
    flow = crud_flow.get(db, id=execution.flow_id)
    if flow is None:
        return job
    from preloop.services.flow_runtime_token import create_flow_runtime_token

    token, _ = create_flow_runtime_token(db, flow=flow, execution_id=execution.id)
    if token:
        job["account_api_token"] = token
    return job


def _parse_runner_execution_id(value: Any) -> Optional[UUID]:
    """Parse an untrusted runner execution id without breaking the WS loop."""
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


@router.websocket("/runners/{runner_id}/ws")
async def runner_ws(
    websocket: WebSocket,
    runner_id: UUID,
    db: Session = Depends(get_db),
):
    """Durable control channel: heartbeat, lease, logs, complete, halt."""
    await websocket.accept()
    token = websocket.query_params.get("token") or websocket.headers.get(
        "x-runner-token", ""
    )
    try:
        runner = _authenticate_runner(db, runner_id, token)
    except HTTPException:
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=1008)
        return

    runner_key = str(runner.id)
    _live[runner_key] = websocket
    connection_id = secrets.token_hex(32)
    crud_flow_runner.set_publication_capabilities(
        db, runner_id=runner.id, capabilities={"connection_id": connection_id}
    )
    # One controller per job, not per socket: a runner may hold several
    # executions at once and each publication has its own nonce, writer
    # credential and expiry task.
    publications: Dict[str, PrivatePublicationController] = {}

    def publication_for(execution_id: Any) -> PrivatePublicationController:
        """The controller for one execution on this socket, created on demand."""
        key = str(execution_id)
        controller = publications.get(key)
        if controller is None:
            controller = PrivatePublicationController(
                db,
                runner_id=runner.id,
                account_id=runner.account_id,
                connection_id=connection_id,
            )
            publications[key] = controller
        return controller

    async def close_publication(execution_id: Any) -> None:
        """Close and forget one execution's controller."""
        controller = publications.pop(str(execution_id), None)
        if controller is not None:
            await controller.close()

    async def close_all_publications() -> None:
        """Close every controller this socket opened."""
        for key in list(publications):
            controller = publications.pop(key, None)
            if controller is not None:
                await controller.close()

    for assignment in list(runner.assignments or []):
        state = (assignment.pending_job or {}).get("_publication")
        if state:
            controller = publication_for(assignment.execution_id)
            controller.execution_id = assignment.execution_id
            controller.nonce = state["nonce"]
    crud_flow_runner.touch_heartbeat(db, runner, status="online")
    hello: Dict[str, Any] = {
        "type": "hello",
        "runner_id": runner_key,
        "log_acknowledgements": True,
        # Echoed so a one-shot CI runner can tell an old control plane (which
        # would keep its row offline forever) from one that deletes it.
        "ephemeral": bool(runner.ephemeral),
    }
    db.refresh(runner)
    hello["concurrency"] = runner.capacity
    emit_runner_updated(runner, db)
    # Replay every held job. ``job`` is the first one so a single-slot CLI
    # that predates concurrency still resumes; ``jobs`` carries them all.
    replays = []
    halts = []
    for assignment in list(runner.assignments or []):
        if assignment.halt_requested:
            halts.append(str(assignment.execution_id))
        job = assignment.pending_job
        if job and not job.get("_publication"):
            replays.append(
                await prepare_runner_delivery(
                    db, job_for_runner_replay(db, pending_job=job, mint_token=True)
                )
            )
    if replays:
        hello["job"] = replays[0]
        hello["jobs"] = replays
    if halts:
        hello["halt"] = True
        hello["halt_execution_id"] = halts[0]
        hello["halt_execution_ids"] = halts
    # Sending is a network wait too. A runner that is connected but not reading
    # (a closed laptop) applies backpressure here, so the reads that built this
    # payload must not still be holding their locks while we block on the peer.
    release_transaction(db)
    await websocket.send_json(hello)

    try:
        while True:
            # A heartbeat arrives every few seconds; between two of them this
            # handler must not be "idle in transaction". The reads that build
            # the previous reply (runner, assignments, executions) would
            # otherwise keep AccessShareLock on those tables for the whole
            # quiet gap, which is long enough to block an `ALTER TABLE` during
            # a rolling upgrade and, through it, every query behind it.
            release_transaction(db)
            raw = await websocket.receive_json()
            msg_type = str(raw.get("type") or "")
            runner = crud_flow_runner.get(db, id=runner_id)
            if not runner:
                await websocket.send_json({"type": "error", "error": "gone"})
                break

            if (runner.publication_capabilities or {}).get(
                "connection_id"
            ) != connection_id:
                await websocket.send_json(
                    {"type": "error", "error": "Runner connection was replaced"}
                )
                break
            if msg_type.startswith("publication_"):
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                controller = publication_for(execution_id)
                try:
                    reply = await controller.handle(raw)
                    await websocket.send_json(reply)
                except (PublicationError, ValueError):
                    await close_publication(execution_id)
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "Publication protocol rejected; recovery retained",
                        }
                    )
                continue

            if msg_type == "heartbeat":
                capability = raw.get("publication_capabilities")
                ready = (
                    isinstance(capability, dict)
                    and type(capability.get("version")) is int
                    and capability.get("version") == 1
                    and capability.get("helper_ready") is True
                    and _valid_publication_helper_image(capability.get("helper_image"))
                )
                updated_capability = crud_flow_runner.set_publication_capabilities(
                    db,
                    runner_id=runner.id,
                    expected_connection_id=connection_id,
                    capabilities={
                        "connection_id": connection_id,
                        **(
                            {
                                "version": 1,
                                "helper_ready": True,
                                "helper_image": capability["helper_image"],
                            }
                            if ready
                            else {}
                        ),
                    },
                )
                if not updated_capability:
                    break
                reported = raw.get("concurrency")
                declared = (
                    reported
                    if isinstance(reported, int) and not isinstance(reported, bool)
                    else None
                )
                crud_flow_runner.set_reported_concurrency(
                    db, runner=runner, reported=declared
                )
                if raw.get("ephemeral") is True and not runner.ephemeral:
                    crud_flow_runner.mark_ephemeral(db, runner_id=runner.id)
                    db.refresh(runner)
                for assignment in list(runner.assignments or []):
                    if assignment.halt_requested:
                        await close_publication(assignment.execution_id)
                if "host_exec_profiles" in raw:
                    runner.capabilities = normalize_host_exec_advertisements(raw)
                # Busy means no free slot, not "holds a job": a runner with
                # spare capacity must stay dispatchable while it works.
                crud_flow_runner.touch_heartbeat(
                    db, runner, status="busy" if runner.free_slots <= 0 else "online"
                )
                db.refresh(runner)
                reply: Dict[str, Any] = {"type": "ack", "concurrency": runner.capacity}
                heartbeat_jobs = []
                halts = []
                for assignment in list(runner.assignments or []):
                    if assignment.halt_requested:
                        halts.append(str(assignment.execution_id))
                    heartbeat_job = job_for_heartbeat_ack(
                        db,
                        assignment,
                        publication_capabilities=runner.publication_capabilities,
                    )
                    if heartbeat_job is None:
                        continue
                    heartbeat_jobs.append(
                        await prepare_runner_delivery(db, heartbeat_job)
                        if runner_needs_lease_token(assignment)
                        else heartbeat_job
                    )
                if heartbeat_jobs:
                    reply["job"] = heartbeat_jobs[0]
                    reply["jobs"] = heartbeat_jobs
                if halts:
                    reply["halt"] = True
                    reply["halt_execution_id"] = halts[0]
                    reply["halt_execution_ids"] = halts
                # The reply is fully built, and a stalled peer can park this
                # send for as long as the keepalive allows. Same reason as the
                # release before `receive_json`.
                release_transaction(db)
                await websocket.send_json(reply)
                continue

            if msg_type == "status":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                assignment = (
                    runner.assignment_for(execution_id)
                    if execution_id is not None
                    else None
                )
                if assignment is None:
                    await websocket.send_json({"type": "ack"})
                    continue
                status = str(raw.get("status") or "RUNNING").upper()
                if status not in {"PENDING", "STARTING", "RUNNING"}:
                    await websocket.send_json({"type": "ack"})
                    continue
                assignment.reported_status = status
                execution = crud_flow_execution.get(db, id=execution_id)
                if execution:
                    execution.status = status
                    db.add(execution)
                db.add(assignment)
                db.commit()
                await websocket.send_json({"type": "ack"})
                continue

            if msg_type == "logs":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                lines = raw.get("lines") or []
                assignment = (
                    runner.assignment_for(execution_id)
                    if execution_id is not None
                    else None
                )
                if assignment is not None:
                    try:
                        await persist_runner_logs(
                            db, execution_id, lines, raw.get("batch_id")
                        )
                    except (ValueError, TypeError):
                        await websocket.send_json(
                            {
                                "type": "error",
                                "error": "Invalid runner log batch",
                                "batch_id": raw.get("batch_id"),
                            }
                        )
                        continue
                    execution = crud_flow_execution.get(
                        db, id=execution_id, account_id=str(runner.account_id)
                    )
                    if execution is not None:
                        for line in lines:
                            record_runner_handoff_markers(
                                db,
                                execution,
                                line,
                                isolated_publication=bool(
                                    (assignment.pending_job or {}).get("_publication")
                                ),
                            )
                # Marker helpers may flush or commit. End even a read-only
                # lookup transaction before waiting on network input again.
                db.commit()
                await websocket.send_json(
                    {
                        "type": "logs_ack" if raw.get("batch_id") else "ack",
                        "execution_id": raw.get("execution_id"),
                        "batch_id": raw.get("batch_id"),
                    }
                )
                continue

            if msg_type == "unregister":
                await close_all_publications()
                if crud_flow_runner.set_publication_capabilities(
                    db,
                    runner_id=runner.id,
                    capabilities={},
                    expected_connection_id=connection_id,
                    offline=True,
                    clear_lease=True,
                ):
                    fresh_runner = crud_flow_runner.get_fresh(db, runner_id=runner_id)
                    if fresh_runner is not None:
                        emit_runner_updated(fresh_runner, db)
                        # A one-shot runner said goodbye on purpose: the
                        # process is gone, so this row goes with it instead of
                        # waiting out the heartbeat grace. Only this row: a
                        # sibling CI job in the same account may still be
                        # waiting idle for its own execution.
                        if fresh_runner.ephemeral:
                            crud_flow_runner.delete_ephemeral(
                                db, runner_id=fresh_runner.id
                            )
                await websocket.send_json({"type": "ack"})
                break

            if msg_type == "complete":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                assignment = (
                    runner.assignment_for(execution_id)
                    if execution_id is not None
                    else None
                )
                if assignment is None:
                    await websocket.send_json({"type": "ack"})
                    continue
                # A normalized failure is not runtime termination evidence.
                # Keep the lease and halt intent until the owner acknowledges
                # an actual terminal outcome.
                if str(raw.get("status") or "").upper() not in {
                    "SUCCEEDED",
                    "FAILED",
                    "STOPPED",
                }:
                    await websocket.send_json(
                        {"type": "error", "error": "Invalid runner completion status"}
                    )
                    continue
                # Snapshot the leased job before close/clear_lease commit so
                # evidence_direct_upload and isolated flags stay local.
                leased_job = (
                    deepcopy(assignment.pending_job)
                    if isinstance(assignment.pending_job, Mapping)
                    else None
                )
                status, completion_error, result = finalize_runner_completion(
                    raw, pending_job=leased_job
                )
                isolated = bool((leased_job or {}).get("_publication"))
                execution = crud_flow_execution.get(
                    db, id=execution_id, account_id=str(runner.account_id), refresh=True
                )
                prompt = None
                trigger_payload = None
                if execution is not None:
                    trigger_payload = execution.trigger_event_details
                    flow = crud_flow.get(
                        db, id=execution.flow_id, account_id=str(runner.account_id)
                    )
                    if flow is not None:
                        prompt = flow.prompt_template
                result_artifact = result if isinstance(result, dict) else None
                approvals, authority = resolve_persist_authority(
                    result_artifact, db, execution_id, prompt=prompt
                )
                decision = apply_cra_persist_boundary(
                    result_artifact,
                    prompt=prompt,
                    trigger_payload=trigger_payload,
                    platform_approvals=approvals,
                    authority=authority,
                    execution_runner=derive_execution_runner(
                        runner_id=runner.id,
                        agent_session_reference=getattr(
                            execution, "agent_session_reference", None
                        ),
                    ),
                )
                result = decision.artifact
                status, completion_error = apply_cra_fail_closed_completion(
                    status, completion_error, decision
                )
                if isolated:
                    if status == "SUCCEEDED":
                        try:
                            trusted_private_receipt(execution)
                        except (PublicationError, AttributeError):
                            status = "FAILED"
                            completion_error = (
                                "Private publication completion was not acknowledged"
                            )
                    await close_publication(execution_id)
                # The monitor may have timed out or cancelled this execution
                # before its owner finally reports exit. Confirm termination
                # and release the lease without replacing that terminal result.
                execution = crud_flow_execution.lock_for_runner_completion(
                    db, execution_id=execution_id, account_id=runner.account_id
                )
                if execution is None:
                    db.rollback()
                    await websocket.send_json(
                        {"type": "error", "error": "Runner execution no longer exists"}
                    )
                    break
                already_terminal = execution.status in {
                    "SUCCEEDED",
                    "FAILED",
                    "STOPPED",
                    "CANCELLED",
                    "TIMEOUT",
                    "TIMED_OUT",
                    "ABORTED",
                }
                if not crud_flow_runner.set_publication_capabilities(
                    db,
                    runner_id=runner.id,
                    capabilities=runner.publication_capabilities,
                    expected_connection_id=connection_id,
                    clear_lease=True,
                    execution_id=execution_id,
                    commit=False,
                ):
                    db.rollback()
                    break
                if already_terminal:
                    crud_flow_execution.confirm_stop(
                        db, execution_id=execution_id, commit=False
                    )
                else:
                    apply_runner_completion_to_execution(
                        db,
                        execution,
                        account_id=runner.account_id,
                        status=status,
                        error=completion_error,
                        result=result if isinstance(result, dict) else None,
                        message=raw,
                        pending_job=leased_job,
                    )
                crud_api_key.deactivate_runtime_keys_for_flow_execution(
                    db,
                    account_id=runner.account_id,
                    execution_id=execution_id,
                    commit=False,
                )
                db.commit()
                fresh_runner = crud_flow_runner.get_fresh(db, runner_id=runner_id)
                if fresh_runner is not None:
                    emit_runner_updated(fresh_runner, db)
                await websocket.send_json({"type": "ack"})
                continue

            await websocket.send_json({"type": "error", "error": f"unknown {msg_type}"})
    except WebSocketDisconnect:
        db.rollback()
        logger.info("runner %s disconnected", runner_id)
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            await close_all_publications()
        except PublicationError:
            logger.warning(
                "Private publication credential cleanup failed for runner %s", runner_id
            )
        finally:
            _release_live_runner(runner_key, websocket)
            if crud_flow_runner.set_publication_capabilities(
                db,
                runner_id=runner_id,
                capabilities={},
                expected_connection_id=connection_id,
                offline=True,
            ):
                row = crud_flow_runner.get_fresh(db, runner_id=runner_id)
                if row:
                    emit_runner_updated(row, db)


async def push_job_to_runner(runner_id: UUID, job: Dict[str, Any]) -> bool:
    ws = _live.get(str(runner_id))
    if not ws:
        return False
    try:
        await ws.send_json({"type": "job", "job": job})
        return True
    except Exception:
        return False
