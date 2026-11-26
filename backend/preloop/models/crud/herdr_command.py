"""Exact runner binding and bounded terminal command queues."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL

ROUTE = "herdr_pane"


def route(record: models.AgentControlCommand) -> dict[str, Any]:
    """Read the server-created transport binding, never request metadata."""
    return (record.envelope or {}).get("payload", {}).get("herdr_target", {})


def eligible_target(
    db: Session,
    *,
    account_id: Any,
    session: models.RuntimeSession,
    agent: models.ManagedAgent,
) -> dict[str, str] | None:
    """Require an exact enrolled device/session identity and one online runner."""
    location = getattr(session, "terminal_location", None)
    if (
        not isinstance(location, dict)
        or location.get("multiplexer") != "herdr"
        or location.get("pane_closed_at")
        or location.get("state") not in {"idle", "done"}
    ):
        return None
    if (
        str(session.account_id) != str(account_id)
        or str(agent.account_id) != str(account_id)
        or agent.lifecycle_state != "active"
    ):
        return None
    kind = {"claude_code": "claude_code", "codex": "codex_cli"}.get(
        session.session_source_type
    )
    if kind is None or session.runtime_principal_type != agent.session_source_type:
        return None
    prefix = agent.session_source_id + ":"
    native = session.session_reference or (
        session.session_source_id[len(prefix) :]
        if session.session_source_id.startswith(prefix)
        else ""
    )
    if not native or session.runtime_principal_id not in {
        agent.session_source_id,
        prefix + native,
    }:
        return None
    if getattr(session, "turn_state", None) in {"working", "waiting_for_approval"}:
        return None
    if (
        db.query(models.ApprovalRequest.id)
        .filter(
            models.ApprovalRequest.account_id == account_id,
            models.ApprovalRequest.runtime_session_id == session.id,
            models.ApprovalRequest.status == "pending",
        )
        .first()
    ):
        return None
    candidate = (
        db.query(models.DiscoveredAgentCandidate.id)
        .filter(
            models.DiscoveredAgentCandidate.account_id == account_id,
            models.DiscoveredAgentCandidate.workstation_fingerprint
            == location.get("device_id"),
            models.DiscoveredAgentCandidate.managed_agent_id == agent.id,
        )
        .first()
    )
    if candidate is None:
        return None
    runners = (
        db.query(models.FlowRunner)
        .filter(
            models.FlowRunner.account_id == account_id,
            models.FlowRunner.workstation_fingerprint == location.get("device_id"),
            models.FlowRunner.last_heartbeat
            >= datetime.now(UTC) - ONLINE_HEARTBEAT_TTL,
            models.FlowRunner.status.in_(["online", "busy"]),
        )
        .all()
    )
    if len(runners) != 1:
        return None
    runner = runners[0]
    report = (runner.harness_inventory or {}).get("herdr") or {}
    if (
        report.get("protocol") != 22
        or report.get("status")
        or not report.get("observe_enabled")
        or not report.get("prompt_enabled")
    ):
        return None
    matches = [
        pane
        for pane in report.get("agents", [])
        if pane.get("harness_kind") == kind and pane.get("harness_session_id") == native
    ]
    if len(matches) != 1:
        return None
    pane = matches[0]
    if (
        pane.get("pane_id") != location.get("pane_id")
        or pane.get("runtime_session_id") != str(session.id)
        or not pane.get("managed")
        or pane.get("state") not in {"idle", "done"}
    ):
        return None
    return {
        "runner_id": str(runner.id),
        "device_id": runner.workstation_fingerprint,
        "runtime_session_id": str(session.id),
        "pane_id": pane["pane_id"],
        "harness_kind": kind,
        "harness_session_id": native,
    }


def outstanding(
    db: Session, *, runner: models.FlowRunner | None = None, expired: bool = False
) -> list[models.AgentControlCommand]:
    """Bound the authenticated runner queue or the global expiry sweep."""
    command = models.AgentControlCommand
    query = db.query(command).filter(
        command.kind == "command",
        command.status.in_(["pending", "delivered", "acked"]),
        command.envelope["payload"]["delivery_route"].astext == ROUTE,
        command.envelope["_result"].is_(None)
        | (command.envelope["_result"].astext.is_(None)),
    )
    if runner is not None:
        query = query.filter(
            command.account_id == runner.account_id,
            command.envelope["payload"]["herdr_target"]["runner_id"].astext
            == str(runner.id),
            command.envelope["payload"]["herdr_target"]["device_id"].astext
            == runner.workstation_fingerprint,
        )
    if expired:
        query = query.filter(command.expires_at <= datetime.now(UTC))
    return query.order_by(command.created_at, command.id).limit(100).all()


def bound_command(
    db: Session, *, runner: models.FlowRunner, command_id: str
) -> models.AgentControlCommand | None:
    """Lock a result's own account/runner-bound command; never trust claimed IDs."""
    command = models.AgentControlCommand
    return (
        db.query(command)
        .filter(
            command.account_id == runner.account_id,
            command.command_id == command_id,
            command.kind == "command",
            command.envelope["payload"]["delivery_route"].astext == ROUTE,
            command.envelope["payload"]["herdr_target"]["runner_id"].astext
            == str(runner.id),
            command.envelope["payload"]["herdr_target"]["device_id"].astext
            == runner.workstation_fingerprint,
        )
        .with_for_update()
        .first()
    )


def lock_command(
    db: Session, command: models.AgentControlCommand
) -> models.AgentControlCommand:
    """Refresh terminal state under a row lock before recording one outcome."""
    return (
        db.query(models.AgentControlCommand)
        .filter(
            models.AgentControlCommand.id == command.id,
            models.AgentControlCommand.account_id == command.account_id,
        )
        .with_for_update()
        .populate_existing()
        .one()
    )


def mark_expired(db: Session, command: models.AgentControlCommand) -> None:
    """Preserve the result's delivery uncertainty while marking expiry explicitly."""
    command.status = "expired"
    command.expired_at = datetime.now(UTC)
    db.add(command)
    db.flush()


def is_herdr_command(record: Any) -> bool:
    """Keep runner-bound commands out of plugin delivery and inbound handlers."""
    envelope = getattr(record, "envelope", None)
    return (
        isinstance(envelope, dict)
        and envelope.get("payload", {}).get("delivery_route") == ROUTE
    )


def commit(db: Session) -> None:
    """Commit the command, audit activity and delivery marker as one unit."""
    db.commit()
