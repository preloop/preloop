"""Shared hosted conversation lifecycle, independent of the client transport."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow_execution, flow_artifact
from preloop.models.crud import flow_conversation as crud
from preloop.schemas.flow_conversation import InteractiveSettings

BUSY_NOTICE = (
    "The agent is working. Wait for its answer before sending another message."
)
EXPIRED_NOTICE = "The checkpoint is missing or expired. Start a new conversation; this conversation cannot resume."


def interactive_settings(agent_type: str, config: Any) -> InteractiveSettings:
    """Validate the supported hosted transport without enabling old flows."""
    raw = (config or {}).get("interactive") or {}
    policy = InteractiveSettings.model_validate(raw)
    if policy.enabled:
        if (config or {}).get("opencode_cli_version", "1.18.29") != "1.18.29":
            raise ValueError(
                "Interactive conversations require the tested OpenCode version 1.18.29."
            )
        if agent_type != "opencode":
            raise ValueError(
                "Interactive conversations require hosted OpenCode. DeepSeek Harness, Pi and other harnesses are not supported."
            )
        if (config or {}).get("runner_id") or (config or {}).get(
            "execution_path"
        ) == "persistent":
            raise ValueError(
                "Interactive conversations require a hosted OpenCode container."
            )
        if (config or {}).get("workspace_snapshots") == "never":
            raise ValueError(
                "Interactive conversations require workspace checkpoints for resume."
            )
    return policy


def resume_checkpoint(db: Session, row: models.FlowConversation) -> dict[str, Any]:
    """Require both artifacts and an explicit native identity; never start cold."""
    from preloop.agents.cli_session import valid_session_id
    from preloop.services.flow_artifacts import artifact_reference

    prior = crud_flow_execution.get(db, id=row.execution_id, account_id=row.account_id)
    session = dict(prior.cli_session or {}) if prior else {}
    if (
        not prior
        or prior.flow_id != row.flow_id
        or not valid_session_id("opencode", session.get("session_id", ""))
    ):
        raise ValueError(EXPIRED_NOTICE)
    for kind in ("native_session", "workspace"):
        artifact = flow_artifact.latest(
            db,
            account_id=row.account_id,
            flow_id=row.flow_id,
            thread_id=str(row.id),
            execution_id=prior.id,
            kind=kind,
        )
        if (
            artifact is None
            or artifact.ciphertext is None
            or artifact.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        ):
            raise ValueError(EXPIRED_NOTICE)
        if kind == "native_session":
            reference = artifact_reference(artifact).model_dump(mode="json")
            if (session.get("artifact_reference") or {}).get(
                "artifact_id"
            ) != reference["artifact_id"]:
                raise ValueError(EXPIRED_NOTICE)
            session["artifact_reference"] = reference
    return session


def reconcile(db: Session, row: models.FlowConversation) -> None:
    """Recover visible terminal state even after a worker or browser disconnect."""
    if row.state == "failed" and row.stop_reason == "dispatch_failed":
        return
    if row.state in {"ended", "stopped"} or not row.execution_id:
        return
    execution = crud_flow_execution.get(
        db, id=row.execution_id, account_id=row.account_id
    )
    if not execution or execution.status not in {
        "SUCCEEDED",
        "FAILED",
        "STOPPED",
        "CANCELLED",
        "ABORTED",
    }:
        return
    if row.end_requested and row.stop_reason == "ended":
        crud.save(
            db,
            row,
            state="ended",
            idle_deadline=None,
            notice="You ended this conversation. Start a new conversation to continue.",
        )
    elif execution.status != "SUCCEEDED":
        if any(
            word in str(execution.error_message or "").lower()
            for word in ("resume", "checkpoint")
        ):
            crud.save(
                db,
                row,
                state="failed",
                idle_deadline=None,
                notice="The saved session could not be resumed. Start a new conversation; no fresh session was started.",
            )
            return
        crud.save(
            db,
            row,
            state="failed",
            idle_deadline=None,
            notice="The execution stopped or hit a limit. Start a new conversation.",
        )
    elif row.stop_reason == "limit":
        crud.save(db, row, state="failed", idle_deadline=None)
    elif row.end_requested:
        crud.save(
            db,
            row,
            state="ended",
            idle_deadline=None,
            notice="The turn limit was reached. Start a new conversation."
            if row.stop_reason == "turn_limit"
            else "You ended this conversation. Start a new conversation to continue.",
        )
    else:
        try:
            resume_checkpoint(db, row)
        except ValueError:
            crud.save(
                db, row, state="failed", idle_deadline=None, notice=EXPIRED_NOTICE
            )
        else:
            crud.save(
                db,
                row,
                state="stopped",
                idle_deadline=None,
                notice="The container stopped after the idle timeout. Send a message to resume this conversation.",
            )


def admission_notice(
    row: models.FlowConversation, now: datetime | None = None
) -> str | None:
    """Explain every refused input without silently queuing or losing a message."""
    now = now or datetime.now(UTC)
    if row.end_requested or row.state == "ended" or row.turns >= row.max_turns:
        return "This conversation has ended or reached its turn limit. Start a new conversation."
    if row.state == "failed":
        return row.notice
    if row.state in {"starting", "working"}:
        return BUSY_NOTICE
    if (
        row.state == "waiting"
        and row.idle_deadline
        and row.idle_deadline.replace(tzinfo=UTC) <= now
    ):
        return "The container is saving its checkpoint. Wait until it stops, then send your message to resume."
    return None


def budget_notice(
    db: Session, row: models.FlowConversation, flow: models.Flow
) -> str | None:
    """Refuse a new turn before spending beyond the conversation's remaining limits."""
    from preloop.services.flow_execution_limits import (
        evaluate_execution_limits,
        get_execution_usage,
        load_execution_limits,
    )

    if row.execution_id is None:
        return None
    execution = crud_flow_execution.get(
        db, id=row.execution_id, account_id=row.account_id
    )
    if execution is None:
        return "The previous execution is unavailable. Start a new conversation."
    violation = evaluate_execution_limits(
        load_execution_limits(db, execution), get_execution_usage(db, row.execution_id)
    )
    if violation:
        return "This conversation reached its token, request or spend limit. Start a new conversation after reviewing the flow limits."
    allowed = int(flow.timeout_seconds or settings.flow_execution_max_wait_seconds)
    if consumed_seconds(db, row.id) >= allowed:
        return "This conversation reached its wall-clock limit. Start a new conversation after reviewing the flow limits."
    return None


def runtime_environment(db: Session, context: dict[str, Any]) -> dict[str, str]:
    """Mint a narrow capability only for the controller-bound active execution."""
    conversation_id = (context.get("trigger_event_data") or {}).get("_conversation_id")
    if not conversation_id:
        return {}
    if context.get("model_gateway_enabled") is not True:
        raise ValueError(
            "Interactive conversations require the Preloop model gateway for metering and limits."
        )
    row = crud.get(db, UUID(conversation_id), UUID(str(context["account_id"])))
    if (
        row is None
        or str(row.execution_id) != str(context["execution_id"])
        or str(row.flow_id) != str(context["flow_id"])
    ):
        raise ValueError("Conversation runtime binding is no longer valid.")
    ttl = int(context.get("flow_timeout_seconds") or 86400) + 300
    token = jwt.encode(
        {
            "aud": "flow-conversation",
            "exp": datetime.now(UTC) + timedelta(seconds=ttl),
            "account_id": str(row.account_id),
            "conversation_id": str(row.id),
            "execution_id": str(row.execution_id),
        },
        settings.security.secret_key,
        algorithm="HS256",
    )
    # Child filtering prevents inheritance, but same-UID tools can still read
    # the container environment via /proc. See the interactive conversation guide.
    return {
        "PRELOOP_CONVERSATION_URL": settings.preloop_url.rstrip("/")
        + f"/api/v1/flow-conversations/{row.id}/runtime",
        "PRELOOP_CONVERSATION_TOKEN": token,
        "PRELOOP_CONVERSATION_TURN": str(row.turns),
    }


def bound_resume(db: Session, context: dict[str, Any]) -> dict[str, Any] | None:
    """Authorize checkpoint restoration from this conversation's previous turn."""
    details = context.get("trigger_event_data") or {}
    conversation_id = details.get("_conversation_id")
    if not conversation_id:
        return None
    row = crud.get(db, UUID(conversation_id), UUID(str(context["account_id"])))
    if row is None or str(row.execution_id) != str(context["execution_id"]):
        raise ValueError("Conversation execution binding mismatch.")
    resume = details.get("_resume") or {}
    if not resume:
        return None
    prior_id = UUID(resume["execution_id"])
    if prior_id not in crud.executions(db, row.id):
        raise ValueError("Conversation checkpoint does not belong to this thread.")
    # Validate the previous execution without changing the current binding.
    previous = models.FlowConversation(
        id=row.id, account_id=row.account_id, flow_id=row.flow_id, execution_id=prior_id
    )
    native = resume_checkpoint(db, previous)
    workspace = flow_artifact.latest(
        db,
        account_id=row.account_id,
        flow_id=row.flow_id,
        thread_id=str(row.id),
        execution_id=prior_id,
        kind="workspace",
    )
    if workspace is None:
        raise ValueError(EXPIRED_NOTICE)
    context["conversation_workspace_repositories"] = (
        workspace.manifest.get("metadata") or {}
    ).get("repositories", [])
    context["checkpoint_resume_authorized"] = True
    return native


def consumed_seconds(
    db: Session, conversation_id: UUID, current_execution_id: UUID | None = None
) -> int:
    """Charge startup, work and idle runtime across all prior containers."""
    import math

    total = 0.0
    for execution_id in crud.executions(db, conversation_id):
        if execution_id == current_execution_id:
            continue
        execution = crud_flow_execution.get(db, id=execution_id)
        if execution and execution.end_time and execution.start_time:
            total += max(
                0.0, (execution.end_time - execution.start_time).total_seconds()
            )
    return math.ceil(total)
