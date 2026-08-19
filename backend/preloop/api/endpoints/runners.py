"""Self-hosted runner registration, listing, and control-plane WebSocket."""

from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import schemas
from preloop.models.crud import (
    crud_api_key,
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
    crud_user,
)
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.flow_runner import FlowRunner
from preloop.models.models.user import User
from preloop.services.runner_service import hash_runner_token, mint_runner_token
from preloop.utils.permissions import require_permission

router = APIRouter()
logger = logging.getLogger(__name__)

# runner_id -> live websocket (this process only)
_live: Dict[str, WebSocket] = {}


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
    if body.runner_id:
        existing = crud_flow_runner.get(
            db, id=body.runner_id, account_id=str(current_user.account_id)
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Runner not found")
        token = mint_runner_token()
        existing.token_hash = hash_runner_token(token)
        if body.name:
            existing.name = body.name
        if body.hostname:
            existing.hostname = body.hostname
        if body.os:
            existing.os = body.os
        if body.arch:
            existing.arch = body.arch
        if body.labels:
            existing.labels = body.labels
        if body.instance_id:
            existing.instance_id = body.instance_id
        existing.status = "online"
        existing.last_heartbeat = datetime.now(timezone.utc)
        db.add(existing)
        db.commit()
        db.refresh(existing)
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
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc),
            "token_hash": hash_runner_token(token),
            "halt_requested": False,
        },
    )
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
    return schemas.RunnerFleetSummary(
        **crud_flow_runner.counts_for_account(db, account_id=current_user.account_id)
    )


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


def _authenticate_runner(db: Session, runner_id: UUID, token: str) -> FlowRunner:
    row = crud_flow_runner.get(db, id=runner_id)
    if not row or row.token_hash != hash_runner_token(token):
        raise HTTPException(status_code=401, detail="Invalid runner credentials")
    return row


def job_for_runner_replay(
    db: Session,
    *,
    pending_job: Dict[str, Any],
    mint_token: bool = False,
) -> Dict[str, Any]:
    """Copy a stored job; mint a token only for reconnect hello.

    Secrets are never persisted in ``pending_job``. Heartbeats also attach
    this copy so a missed hello can still see the job, but they must not
    mint: the CLI heartbeats every 15s while ``pending_job`` is set, and
    discards the payload once the lease is running.
    """
    job = dict(pending_job)
    if not mint_token:
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
    crud_flow_runner.touch_heartbeat(db, runner, status="online")
    hello: Dict[str, Any] = {"type": "hello", "runner_id": runner_key}
    db.refresh(runner)
    if runner.pending_job:
        hello["job"] = job_for_runner_replay(
            db, pending_job=runner.pending_job, mint_token=True
        )
    if runner.halt_requested:
        hello["halt"] = True
        if runner.current_execution_id:
            hello["halt_execution_id"] = str(runner.current_execution_id)
    await websocket.send_json(hello)

    try:
        while True:
            raw = await websocket.receive_json()
            msg_type = str(raw.get("type") or "")
            runner = crud_flow_runner.get(db, id=runner_id)
            if not runner:
                await websocket.send_json({"type": "error", "error": "gone"})
                break

            if msg_type == "heartbeat":
                status = "busy" if runner.current_execution_id else "online"
                crud_flow_runner.touch_heartbeat(db, runner, status=status)
                db.refresh(runner)
                reply: Dict[str, Any] = {"type": "ack"}
                if runner.pending_job:
                    reply["job"] = job_for_runner_replay(
                        db, pending_job=runner.pending_job, mint_token=False
                    )
                if runner.halt_requested:
                    reply["halt"] = True
                    if runner.current_execution_id:
                        reply["halt_execution_id"] = str(runner.current_execution_id)
                await websocket.send_json(reply)
                continue

            if msg_type == "status":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                if execution_id is None or execution_id != runner.current_execution_id:
                    await websocket.send_json({"type": "ack"})
                    continue
                status = str(raw.get("status") or "RUNNING").upper()
                runner.reported_status = status
                execution = crud_flow_execution.get(db, id=execution_id)
                if execution:
                    execution.status = status
                    db.add(execution)
                db.add(runner)
                db.commit()
                await websocket.send_json({"type": "ack"})
                continue

            if msg_type == "logs":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                lines = raw.get("lines") or []
                if (
                    execution_id is not None
                    and execution_id == runner.current_execution_id
                ):
                    for line in lines:
                        crud_flow_execution_log.append_log(
                            db,
                            str(execution_id),
                            {
                                "type": "agent_log_line",
                                "payload": {"line": str(line)},
                            },
                        )
                        await _publish_flow_update(
                            str(execution_id),
                            {
                                "execution_id": str(execution_id),
                                "type": "agent_log_line",
                                "payload": {"line": str(line)},
                            },
                        )
                await websocket.send_json({"type": "ack"})
                continue

            if msg_type == "unregister":
                runner.status = "offline"
                runner.pending_job = None
                runner.current_execution_id = None
                runner.halt_requested = False
                db.add(runner)
                db.commit()
                await websocket.send_json({"type": "ack"})
                break

            if msg_type == "complete":
                execution_id = _parse_runner_execution_id(raw.get("execution_id"))
                if execution_id is None or execution_id != runner.current_execution_id:
                    await websocket.send_json({"type": "ack"})
                    continue
                status = str(raw.get("status") or "SUCCEEDED").upper()
                runner.reported_status = status
                runner.pending_job = None
                runner.current_execution_id = None
                runner.halt_requested = False
                runner.status = "online"
                execution = crud_flow_execution.get(db, id=execution_id)
                if execution:
                    execution.status = status
                    execution.end_time = datetime.now(timezone.utc)
                    if raw.get("error"):
                        execution.error_message = str(raw["error"])
                    db.add(execution)
                    crud_api_key.deactivate_runtime_keys_for_flow_execution(
                        db,
                        account_id=runner.account_id,
                        execution_id=execution_id,
                        commit=False,
                    )
                db.add(runner)
                db.commit()
                await websocket.send_json({"type": "ack"})
                continue

            await websocket.send_json({"type": "error", "error": f"unknown {msg_type}"})
    except WebSocketDisconnect:
        logger.info("runner %s disconnected", runner_id)
    finally:
        _live.pop(runner_key, None)
        row = crud_flow_runner.get(db, id=runner_id)
        if row:
            row.status = "offline"
            db.add(row)
            db.commit()


async def push_job_to_runner(runner_id: UUID, job: Dict[str, Any]) -> bool:
    ws = _live.get(str(runner_id))
    if not ws:
        return False
    try:
        await ws.send_json({"type": "job", "job": job})
        return True
    except Exception:
        return False
